#!/usr/bin/env python3
"""TestScope: pick the Apex tests a change can affect, with a safe full-suite fallback.
 
Run from the folder that contains the apex-recipes repo, for example:
  python testscope.py --changed DMLRecipes
  python testscope.py --from-git --base-ref main
  python testscope.py --changed DMLRecipes --baseline-failures baseline_failures.txt --run --target-org devorg2
"""
import argparse
import json
import re
import shutil
import subprocess
import time
from collections import deque
from pathlib import Path
 
DEFAULT_ROOT = "apex-recipes/force-app"
FILE_PATTERN = re.compile(r"(?:^|/)(classes|triggers)/([^/]+?)\.(cls|trigger)(?:-meta\.xml)?$")
IGNORED_FILE = re.compile(r"\.(md|txt|csv|json)$", re.I)
COMMENTS_AND_STRINGS = re.compile(r"/\*.*?\*/|//[^\n]*|'(?:\\.|[^'\\\n])*'", re.S)
 
 
def read_text(path):
    """Read a text file; Windows PowerShell 'Out-File' may have written UTF-16."""
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig", errors="ignore")
 
 
def load_components(root):
    """Find every Apex class and trigger under root, in any package folder."""
    comps = {}
    for ext in (".cls", ".trigger"):
        for path in sorted(root.rglob("*" + ext)):
            comps[path.stem.lower()] = path
    return comps
 
 
def build_graph(comps):
    """dependents[X] = components that mention X, so a change in X can affect them."""
    is_test = set()
    refs = {}
    for key, path in comps.items():
        raw = read_text(path)
        if re.search(r"@isTest", raw, re.I):
            is_test.add(key)
        words = {w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", COMMENTS_AND_STRINGS.sub(" ", raw))}
        refs[key] = {w for w in words if w in comps and w != key}
    dependents = {key: set() for key in comps}
    for key, targets in refs.items():
        for target in targets:
            dependents[target].add(key)
    return dependents, is_test
 
 
def affected_components(changed, dependents):
    seen = set()
    queue = deque(changed)
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(dependents.get(current, set()) - seen)
    return seen
 
 
def git_changed_files(root, base_ref):
    result = subprocess.run(["git", "-C", str(root), "diff", "--name-only", base_ref],
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("git diff failed: " + result.stderr.strip())
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]
 
 
def read_baseline(path):
    if not path:
        return set()
    return {line.strip().lower() for line in read_text(path).splitlines() if line.strip()}
 
 
def parse_run(stdout):
    """Return (set of failing test classes, summary dict) from `sf apex run test --json`."""
    try:
        result = json.loads(stdout).get("result", {})
    except ValueError:
        return set(), {}
    failing = {t.get("ApexClass", {}).get("Name", "?") for t in result.get("tests", [])
               if t.get("Outcome") == "Fail"}
    return failing, result.get("summary", {})
 
 
def run_tests(classes, org, wait, full_suite):
    sf = shutil.which("sf")
    if not sf:
        raise SystemExit("Salesforce CLI (sf) not found on PATH")
    cmd = [sf, "apex", "run", "test", "--wait", str(wait), "--json", "--target-org", org]
    if full_suite:
        cmd += ["--test-level", "RunLocalTests"]
    else:
        for name in classes:
            cmd += ["--class-names", name]
    start = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return time.perf_counter() - start, proc.stdout
 
 
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=DEFAULT_ROOT, help="folder searched recursively for .cls and .trigger files")
    ap.add_argument("--changed", nargs="*", default=[], help="changed class/trigger names")
    ap.add_argument("--from-git", action="store_true", help="find changed files with git diff")
    ap.add_argument("--base-ref", default="HEAD", help="git ref to compare against")
    ap.add_argument("--baseline-failures", help="file with test classes that already fail before any change")
    ap.add_argument("--run", action="store_true", help="run the selected tests (or the full suite on fallback)")
    ap.add_argument("--target-org", help="required with --run; use a sandbox or developer org alias")
    ap.add_argument("--wait", type=int, default=60)
    ap.add_argument("--report", default="testscope_report.json")
    args = ap.parse_args()
 
    if args.run and not args.target_org:
        raise SystemExit("--run needs --target-org so tests never run on an unintended default org")
 
    root = Path(args.root)
    started = time.perf_counter()
    comps = load_components(root)
    dependents, is_test = build_graph(comps)
 
    changed_names = list(args.changed)
    unmapped = []
    if args.from_git:
        for path in git_changed_files(root, args.base_ref):
            match = FILE_PATTERN.search(path)
            if match:
                changed_names.append(match.group(2))
            elif not IGNORED_FILE.search(path):
                unmapped.append(path)
 
    changed_keys, unknown = [], []
    for name in changed_names:
        (changed_keys if name.lower() in comps else unknown).append(name.lower())
    changed_keys = sorted(set(changed_keys))
 
    baseline = read_baseline(args.baseline_failures)
    affected = affected_components(changed_keys, dependents)
    tests = sorted(comps[k].stem for k in affected if k in is_test)
    known_failing = [t for t in tests if t.lower() in baseline]
    selected = [t for t in tests if t.lower() not in baseline]
 
    reasons = []
    if not changed_names and not unmapped:
        reasons.append("no change detected")
    if unknown:
        reasons.append("changed names not found in the Apex graph: " + ", ".join(unknown))
    if unmapped:
        reasons.append("changed files outside Apex classes/triggers (not analysed): " + ", ".join(unmapped))
    if not selected:
        reasons.append("no clean test class reaches the change")
    fallback = bool(reasons)
    analysis_seconds = time.perf_counter() - started
 
    report = {
        "components_scanned": len(comps),
        "test_classes_found": len(is_test),
        "changed": [comps[k].stem for k in changed_keys],
        "affected_components": sorted(comps[k].stem for k in affected),
        "selected_tests": selected,
        "skipped_already_failing": known_failing,
        "full_suite_fallback": fallback,
        "fallback_reasons": reasons,
        "analysis_seconds": round(analysis_seconds, 3),
    }
 
    if args.run:
        seconds, stdout = run_tests(selected, args.target_org, args.wait, fallback)
        failing, summary = parse_run(stdout)
        report["run"] = {
            "mode": "full_suite" if fallback else "selected",
            "test_seconds": round(seconds, 1),
            "total_seconds": round(seconds + analysis_seconds, 1),
            "tests_ran": summary.get("testsRan"),
            "failing_classes": sorted(failing),
            "new_failing_classes": sorted(f for f in failing if f.lower() not in baseline),
        }
        Path("last_run_output.json").write_text(stdout, encoding="utf-8")
 
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
 
    print(f"Scanned {len(comps)} components, {len(is_test)} test classes")
    print("Changed:", ", ".join(report["changed"]) or "-")
    print(f"Affected components: {len(affected)}")
    print("Selected tests:", ", ".join(selected) or "-")
    if known_failing:
        print("Skipped (already failing in baseline):", ", ".join(known_failing))
    if fallback:
        print("GATE: run the FULL suite. Reasons:")
        for reason in reasons:
            print("  -", reason)
    else:
        print("GATE: selected tests are enough for the analysed dependencies")
        print("Env var:  $env:AGENTIA_APEX_TEST_CLASSES=\"" + ",".join(selected) + "\"")
    print(f"Analysis time: {analysis_seconds:.2f}s   Report: {args.report}")
    if args.run:
        print("Run:", json.dumps(report["run"], indent=2))
 
 
if __name__ == "__main__":
    main()