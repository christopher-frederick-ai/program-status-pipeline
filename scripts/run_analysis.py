"""Build per-milestone clusters from the four source files and run them through the
agent service: Analyst -> Executor.

    python scripts/run_analysis.py --dry-run                # just show the clusters (no API spend)
    python scripts/run_analysis.py --milestone CDR          # one milestone, live
    python scripts/run_analysis.py --out results.json       # all milestones with linked rows

Needs `pip install openpyxl` and a running service (see README). Reads the shared secret
from SERVICE_SHARED_SECRET or ../.env, like smoke_test.py.

A "cluster" = the milestone's reference row + the task and risk rows tagged to it via
"Related Milestone" + the engineering notes. This mirrors test_milestone_risk_analyst.py,
plus an `as_of_date` field (see --no-as-of): without it the model has to guess "today"
when judging whether a task is overdue.
"""

import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Callable

import openpyxl

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_test  # noqa: E402  (shares the HTTP helper and secret loading)

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles don't always default to UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def load_milestones(xlsx: Path) -> list[dict]:
    ws = openpyxl.load_workbook(xlsx, data_only=True)["Milestones"]
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if r and r[0]]


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if any((v or "").strip() for v in r.values())]


def build_clusters(data_dir: Path, as_of: str | None) -> list[dict]:
    milestones = load_milestones(data_dir / "milestones.xlsx")
    tasks = load_csv(data_dir / "asana_task_tracker_import.csv")
    risks = load_csv(data_dir / "asana_risk_register_import.csv")
    notes = (data_dir / "program_notes.txt").read_text(encoding="utf-8")

    known = {m["Milestone"] for m in milestones}
    for row in tasks + risks:  # catch typos in "Related Milestone" before they silently drop rows
        if row.get("Related Milestone") not in known:
            print(f"WARNING: row {row.get('Task Name')!r} tagged to unknown milestone "
                  f"{row.get('Related Milestone')!r}", file=sys.stderr)

    clusters = []
    for m in milestones:
        name = m["Milestone"]
        cluster = {}
        if as_of:
            cluster["as_of_date"] = as_of
        cluster["milestone_reference"] = m
        cluster["task_tracker"] = [r for r in tasks if r.get("Related Milestone") == name]
        cluster["risk_register"] = [r for r in risks if r.get("Related Milestone") == name]
        cluster["engineering_notes"] = notes
        clusters.append(cluster)
    return clusters


def _stamp(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def run_cluster(
    cluster: dict,
    post: Callable[[str, dict], tuple[int, dict]],
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Analyst, then Executor. `post(path, body) -> (status, json)` is injected for testing.
    `progress(msg)` is called as each step starts and ends (silent when None)."""
    say = progress or (lambda _msg: None)
    name = cluster["milestone_reference"]["Milestone"]

    say(f"{name}: Analyst running...")
    t0 = time.perf_counter()
    code, analyst = post("/agents/milestone_risk_analyst/run", {"input": cluster})
    say(f"{name}: Analyst finished (HTTP {code}, {time.perf_counter() - t0:.0f}s)")
    if code != 200:
        return {"milestone": name, "error": f"analyst HTTP {code}: {analyst}"}

    say(f"{name}: Executor running...")
    t0 = time.perf_counter()
    code, executor = post(
        "/agents/milestone_risk_executor/run",
        {"input": {"milestone": name, **analyst["output"]}},
    )
    say(f"{name}: Executor finished (HTTP {code}, {time.perf_counter() - t0:.0f}s)")
    if code != 200:
        return {"milestone": name, "analyst": analyst, "error": f"executor HTTP {code}: {executor}"}
    return {"milestone": name, "analyst": analyst, "executor": executor}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=PROJECT / "sample_data")
    ap.add_argument("--milestone", action="append", help="substring of milestone name; repeatable")
    ap.add_argument("--include-empty", action="store_true", help="also run milestones with no linked tasks/risks")
    ap.add_argument("--as-of", default=dt.date.today().isoformat(), help="report date (default: today)")
    ap.add_argument("--no-as-of", action="store_true", help="omit as_of_date, exactly matching the original harness input")
    ap.add_argument("--dry-run", action="store_true", help="print clusters; call nothing")
    ap.add_argument("--show-notes", action="store_true", help="with --dry-run, also print the full engineering notes")
    ap.add_argument("--out", type=Path, help="write full results to this JSON file")
    args = ap.parse_args()

    clusters = build_clusters(args.data_dir, None if args.no_as_of else args.as_of)
    selected = []
    for c in clusters:
        name = c["milestone_reference"]["Milestone"]
        if args.milestone and not any(s.lower() in name.lower() for s in args.milestone):
            continue
        if not args.milestone and not args.include_empty and not (c["task_tracker"] or c["risk_register"]):
            print(f"skipping {name!r}: no linked tasks or risks (use --include-empty to run it)")
            continue
        selected.append(c)

    if args.dry_run:
        for c in selected:
            shown = c if args.show_notes else {k: v for k, v in c.items() if k != "engineering_notes"}
            print(json.dumps(shown, indent=2, default=str, ensure_ascii=False))
            if not args.show_notes:
                print(f"(+ engineering_notes, {len(c['engineering_notes'])} chars; add --show-notes to print them)")
            print()
        return

    secret = smoke_test.read_secret()
    post = lambda path, body: smoke_test.call("POST", path, secret, body)  # noqa: E731
    results = []
    started = time.perf_counter()
    _stamp(f"Running {len(selected)} milestone(s) against {smoke_test.BASE} (each milestone's results print when it finishes)")
    for c in selected:
        res = run_cluster(c, post, progress=_stamp)
        results.append(res)
        print("=" * 70)
        print(res["milestone"])
        if "analyst" in res:
            print(json.dumps(res["analyst"]["output"], indent=2, ensure_ascii=False))
            print(f"[{res['analyst']['model']}, attempts={res['analyst']['attempts']}, "
                  f"tokens in/out={res['analyst']['usage']['input_tokens']}/{res['analyst']['usage']['output_tokens']}]")
        if "executor" in res:
            print("\n" + res["executor"]["output"])
        if "error" in res:
            print("ERROR:", res["error"])
    _stamp(f"All done in {time.perf_counter() - started:.0f}s")
    if args.out:
        args.out.write_text(json.dumps(results, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
        print(f"\nSaved {args.out}")


if __name__ == "__main__":
    main()
