#!/usr/bin/env python3
"""
benchmark.py - seeded-bug benchmark for testscope.

Run from the "Dev org" folder (the one that contains apex-recipes/, testscope.py/,
baseline_failures.txt):

    python benchmark.py --only 2,3                 # quick: bug 2 and bug 3, 1 run each
    python benchmark.py --only 1,2,3 --repeats 3   # final numbers: median of 3 runs

For every case it: seeds the bug -> deploys -> runs testscope (selected tests)
-> runs the full suite -> git restore -> re-deploys the original.
Everything is saved to results.csv and evidence/.
"""
import argparse, csv, glob, json, os, re, shutil, statistics, subprocess, sys, time

CASES = {
    1: ("TriggerHandler",         "swap",   "bug1"),   # central class
    2: ("MetadataTriggerService", "negate", "bug2"),   # condition inverted
    3: ("FormattingRecipes",      "return", "bug3"),   # return value changed
}


def sh(cmd, cwd=None):
    """Run a command (string => shell, list => no shell). Returns (rc, stdout+stderr)."""
    p = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def json_from(text):
    i = text.find("{")
    if i < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[i:])[0]
    except Exception:
        return None


# ---------------- mutations ----------------
def m_swap(text):
    old = "return TriggerHandler.bypassedHandlers.contains("
    if text.count(old) != 1:
        raise RuntimeError("isBypassed return line not found exactly once")
    return text.replace(old, "return !TriggerHandler.bypassedHandlers.contains("), "negated return in isBypassed()"


def m_negate(text):
    lines = text.splitlines(keepends=True)
    for i, l in enumerate(lines):
        if l.lstrip().startswith(("//", "*", "/*")):
            continue
        r = l.rstrip("\r\n")
        eol = l[len(r):]
        m = re.search(r"\bif\s*\((.*)\)\s*\{\s*$", r)
        if m and not r.lstrip().startswith("}"):
            lines[i] = r[:m.start()] + "if (!(" + m.group(1) + ")) {" + eol
            return "".join(lines), f"line {i+1}: if ({m.group(1)}) -> if (!(...))"
    raise RuntimeError("no single-line 'if (...) {' found; use manual edit")


def m_return(text):
    lines = text.splitlines(keepends=True)
    for i, l in enumerate(lines):
        if l.lstrip().startswith(("//", "*", "/*")):
            continue
        r = l.rstrip("\r\n")
        eol = l[len(r):]
        m = re.match(r"^(\s*)return\s+(.+?);\s*(//.*)?$", r)
        if not m:
            continue
        v = m.group(2).strip()
        if v == "null":
            continue
        if re.fullmatch(r"'[^'\\]*'", v):
            nv = v[:-1] + "_BUG'"
        elif v in ("true", "false"):
            nv = "false" if v == "true" else "true"
        elif re.fullmatch(r"-?\d+(\.\d+)?", v):
            nv = f"{v} + 1"
        else:
            nv = "null"
        lines[i] = f"{m.group(1)}return {nv};{eol}"
        return "".join(lines), f"line {i+1}: return {v}; -> return {nv};"
    raise RuntimeError("no suitable return statement found; use manual edit")


MUT = {"swap": m_swap, "negate": m_negate, "return": m_return}


# ---------------- sf helpers ----------------
def deploy(repo, rel, org, evid, tag):
    rc, out = sh(f'sf project deploy start --source-dir "{rel}" --target-org {org} --wait 15 --json', cwd=repo)
    open(os.path.join(evid, f"{tag}_deploy.txt"), "w", encoding="utf-8").write(out)
    data = json_from(out)
    if data and data.get("result", {}).get("status") == "Succeeded":
        return True
    # fallback: known display bug - check via report
    m = re.search(r"0Af\w{15}", out)
    if m:
        rc2, out2 = sh(f"sf project deploy report --job-id {m.group(0)} --target-org {org}", cwd=repo)
        open(os.path.join(evid, f"{tag}_deploy_report.txt"), "w", encoding="utf-8").write(out2)
        return "Succeeded" in out2
    return False


def full_suite(org, baseline, path):
    t = time.perf_counter()
    rc, out = sh(f"sf apex run test --test-level RunLocalTests --wait 60 --json --target-org {org}")
    secs = time.perf_counter() - t
    open(path, "w", encoding="utf-8").write(out)
    data = json_from(out)
    if not data or "result" not in data:
        return {"secs": secs, "error": "could not parse full-suite json"}
    res = data["result"]
    failed = sorted({t["ApexClass"]["Name"] for t in res.get("tests", []) if t.get("Outcome") == "Fail"})
    return {"secs": secs, "failing": res["summary"].get("failing"),
            "new": sorted(set(failed) - baseline)}


def testscope(cls, org):
    t = os.path.join("testscope.py", "testscope.py")
    rc, out = sh([sys.executable, t, "--changed", cls, "--baseline-failures", "baseline_failures.txt",
                  "--run", "--target-org", org])
    run = None
    k = out.find("Run:")
    if k >= 0:
        run = json_from(out[k:])
    sel = re.search(r"Selected tests:\s*(.*)", out)
    skp = re.search(r"Skipped.*?:\s*(.*)", out)
    return {"out": out, "run": run,
            "selected": [s.strip() for s in sel.group(1).split(",")] if sel else [],
            "skipped": skp.group(1) if skp else ""}


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="apex-recipes")
    ap.add_argument("--org", default="devorg2")
    ap.add_argument("--only", default="2,3")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--evidence", default="evidence")
    a = ap.parse_args()

    os.makedirs(a.evidence, exist_ok=True)
    baseline = {l.strip() for l in open("baseline_failures.txt", encoding="utf-16") if l.strip()}
    rows = []

    for n in [int(x) for x in a.only.split(",")]:
        cls, kind, tag = CASES[n]
        print(f"\n===== CASE {n}: {cls} ({kind}) =====")
        hits = glob.glob(os.path.join(a.repo, "force-app", "**", "classes", "**", cls + ".cls"), recursive=True)
        if not hits:
            print("  class file not found, skipping"); continue
        path = hits[0]
        rel = os.path.relpath(path, a.repo)
        rc, st = sh(f'git status --porcelain -- "{rel}"', cwd=a.repo)
        if st.strip():
            print(f"  {rel} has uncommitted changes. git restore it first. Skipping."); continue

        row = {"case": n, "class": cls, "kind": kind}
        try:
            text = open(path, encoding="utf-8", newline="").read()
            new, desc = MUT[kind](text)
            open(path, "w", encoding="utf-8", newline="").write(new)
            row["mutation"] = desc
            print("  mutation:", desc)
            rc, diff = sh(f'git diff -- "{rel}"', cwd=a.repo)
            open(os.path.join(a.evidence, f"{tag}.diff"), "w", encoding="utf-8").write(diff)

            if not deploy(a.repo, rel, a.org, a.evidence, f"{tag}_bug"):
                row["error"] = "bug deploy failed (maybe compile error)"; print("  ", row["error"]); continue

            fulls, sels = [], []
            for r in range(1, a.repeats + 1):
                print(f"  run {r}/{a.repeats}: testscope ...")
                ts = testscope(cls, a.org)
                open(os.path.join(a.evidence, f"{tag}_testscope_r{r}.txt"), "w", encoding="utf-8").write(ts["out"])
                if os.path.exists("testscope_report.json"):
                    shutil.copy("testscope_report.json", os.path.join(a.evidence, f"{tag}_testscope_report_r{r}.json"))
                sels.append(ts)
                print(f"  run {r}/{a.repeats}: full suite (2-5 min) ...")
                fj = os.path.join(a.evidence, f"full_{tag}_r{r}.json")
                fulls.append(full_suite(a.org, baseline, fj))
                shutil.copy(fj, f"full_{tag}.json")

            full_new = set(fulls[-1].get("new", []))
            sel_new = set((sels[-1]["run"] or {}).get("new_failing_classes", []))
            missed = sorted(full_new - sel_new)
            row.update({
                "full_failing": fulls[-1].get("failing", ""),
                "full_new_failing": ";".join(sorted(full_new)),
                "selected_classes": len(sels[-1]["selected"]),
                "selected_tests_ran": (sels[-1]["run"] or {}).get("tests_ran", ""),
                "selected_new_failing": ";".join(sorted(sel_new)),
                "caught": ";".join(sorted(full_new & sel_new)),
                "missed": ";".join(missed),
                "recall": (f"{len(full_new & sel_new)}/{len(full_new)}" if full_new else "n/a (no test detects this bug)"),
                "full_secs_median": med([f["secs"] for f in fulls]),
                "selected_secs_median": med([(s["run"] or {}).get("total_seconds") for s in sels]),
                "full_secs_all": ";".join(str(round(f["secs"], 1)) for f in fulls),
                "selected_secs_all": ";".join(str((s["run"] or {}).get("total_seconds")) for s in sels),
                "runs": a.repeats,
            })
            f, s = row["full_secs_median"], row["selected_secs_median"]
            row["time_saved_pct"] = round(100 * (f - s) / f, 1) if f and s != "" else ""
            print(f"  recall={row['recall']}  full={f}s  selected={s}s  saved={row['time_saved_pct']}%  missed={missed or '-'}")
        except Exception as e:
            row["error"] = str(e); print("  ERROR:", e)
        finally:
            sh(f'git restore "{rel}"', cwd=a.repo)
            ok = deploy(a.repo, rel, a.org, a.evidence, f"{tag}_restore")
            row["restored_and_redeployed"] = ok
            print("  restored + redeployed original:", "OK" if ok else "FAILED - fix manually!")
            rows.append(row)

    cols = ["case", "class", "kind", "mutation", "full_failing", "full_new_failing", "selected_classes",
            "selected_tests_ran", "selected_new_failing", "caught", "missed", "recall", "full_secs_median",
            "selected_secs_median", "time_saved_pct", "full_secs_all", "selected_secs_all", "runs",
            "restored_and_redeployed", "error"]
    with open(a.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    print(f"\nDone. {a.out} + {a.evidence}/ written.")


if __name__ == "__main__":
    main()

