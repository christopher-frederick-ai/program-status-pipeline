"""Repeat-run harness for tuning the Analyst and Executor instructions.

The model's wording changes from run to run, so one good output proves little. This runs the same
milestone clusters N times, scores every run against a few behaviour checks, and shows how much the
outputs differ from run to run. Results are saved, so a prompt change can be compared with the run
before it.

    python scripts/repeat_runs.py --runs 5 --label baseline
    (edit an instruction file in agent_service/agents/)
    python scripts/repeat_runs.py --runs 5 --label resource-rule
    python scripts/repeat_runs.py --compare runs/<baseline>.json runs/<resource-rule>.json

    python scripts/repeat_runs.py --dry-run            # show the plan, call nothing
    python scripts/repeat_runs.py --report runs/<file>.json   # re-print (and re-score) a saved run set

Each run set is saved to runs/<timestamp>[-label].json with a matching .txt report. The JSON keeps
the raw outputs, so you can change the checks in evaluate() (or EXPECTED) later and --report re-scores
old runs without new API calls.

Needs `pip install openpyxl` (same as run_analysis.py) and a running service. Reads the shared
secret like smoke_test.py. Before running, it asks the service to re-read the instruction files,
so an edited prompt is always the one being tested.

The expectations in EXPECTED describe the sample data in sample_data/. If you change the data,
change them too.
"""

import argparse
import concurrent.futures as cf
import datetime as dt
import difflib
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import mean

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_analysis  # noqa: E402  (cluster building, UTF-8 console setup)
import smoke_test  # noqa: E402  (HTTP helper and secret loading)

AGENTS_DIR = PROJECT / "agent_service" / "agents"
RUNS_DIR = PROJECT / "runs"
ANALYST_PATH = "/agents/milestone_risk_analyst/run"
EXECUTOR_PATH = "/agents/milestone_risk_executor/run"

# What each milestone in sample_data/ should produce. `flag` is the expected
# milestone_status_flagged, `removal` the Closed risk ids to recommend, `mention` text that must
# appear in the Analyst's status/explanation (and in the Executor paragraph). `forbid_in_report`
# is text the Executor paragraph must not contain.
EXPECTED = [
    {"match": "Authority to Operate", "short": "ATO", "flag": "no", "removal": [], "mention": [],
     "forbid_in_report": ["formal risk"]},  # RISK-014 is logged, so the report must not say it is not
    {"match": "Critical Design Review", "short": "CDR", "flag": "yes", "removal": ["RISK-021"], "mention": []},
    {"match": "Customer Demo", "short": "Customer Demo", "flag": "no", "removal": [], "mention": ["R. Chen"]},
]

# Reassuring or alarming adjectives the Analyst prompt asks it to avoid.
BANNED_WORDS = ("comfortable", "healthy", "solid", "dire", "alarming", "reassuring")

# Text fields that get compared between runs.
FIELDS = {
    "status": lambda r: (r.get("analyst") or {}).get("status", ""),
    "explanation": lambda r: (r.get("analyst") or {}).get("explanation", ""),
    "executor": lambda r: r.get("executor_text", ""),
}


# --------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------
def sentences(text: str) -> list[str]:
    """Rough sentence split that does not break on initials such as "R. Chen"."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'])", (text or "").strip())
    out: list[str] = []
    for p in parts:
        if out and re.search(r"\b[A-Z]\.$", out[-1]):
            out[-1] += " " + p
        else:
            out.append(p)
    return [s for s in out if s.strip()]


def banned_found(text: str) -> list[str]:
    return sorted({m.lower() for m in re.findall(r"\b(" + "|".join(BANNED_WORDS) + r")\b", text or "", re.I)})


def risk_ids(items) -> set[str]:
    ids = set()
    for item in items or []:
        value = item.get("risk_id") if isinstance(item, dict) else item
        if value:
            m = re.search(r"RISK-\d+", str(value))
            ids.add(m.group(0) if m else str(value).strip())
    return ids


def expectation_for(name: str) -> dict | None:
    return next((e for e in EXPECTED if e["match"].lower() in name.lower()), None)


def short_name(name: str) -> str:
    e = expectation_for(name)
    return e["short"] if e else name[:24]


def evaluate(rec: dict, expect: dict | None, with_executor: bool) -> dict[str, bool]:
    """Every check for one run: name -> passed. A failed or missing run fails everything."""
    analyst = rec.get("analyst") if isinstance(rec.get("analyst"), dict) else None
    ok = analyst is not None
    analyst = analyst or {}
    status = str(analyst.get("status", ""))
    both = f"{status} {analyst.get('explanation', '')}"

    checks: dict[str, bool] = {
        "run completed": ok and "error" not in rec,
        "valid JSON on first attempt": ok and rec.get("attempts") == 1,
    }
    if expect:
        checks["flag matches expected"] = ok and str(analyst.get("milestone_status_flagged", "")).lower() == expect["flag"]
        checks["removal list matches expected"] = ok and risk_ids(analyst.get("risks_recommended_for_removal")) == set(expect["removal"])
    checks["status is 1-2 sentences"] = ok and 1 <= len(sentences(status)) <= 2
    checks["neutral wording (no banned adjectives)"] = ok and not banned_found(both)
    for phrase in (expect or {}).get("mention", []):
        checks[f"analyst mentions {phrase}"] = ok and phrase.lower() in both.lower()

    if with_executor:
        text = str(rec.get("executor_text") or "")
        has = bool(text)
        checks["executor: 1-3 sentences"] = has and 1 <= len(sentences(text)) <= 3
        checks["executor: no semicolons"] = has and ";" not in text
        for phrase in (expect or {}).get("mention", []):
            checks[f"executor mentions {phrase}"] = has and phrase.lower() in text.lower()
        for phrase in (expect or {}).get("forbid_in_report", []):
            checks[f"executor avoids '{phrase}'"] = has and phrase.lower() not in text.lower()
    return checks


# --------------------------------------------------------------------------------------
# Text comparison
# --------------------------------------------------------------------------------------
def similarity(a: str, b: str) -> float:
    """1.0 = identical wording. Word-level, so it reflects wording changes, not punctuation noise."""
    return difflib.SequenceMatcher(None, a.split(), b.split(), autojunk=False).ratio()


def pairwise(texts: list[str]) -> list[tuple[int, int, float]]:
    return [(i, j, similarity(texts[i], texts[j])) for i in range(len(texts)) for j in range(i + 1, len(texts))]


def consistency(texts: list[str]) -> float | None:
    pairs = pairwise(texts)
    return mean(p[2] for p in pairs) if pairs else None


def medoid(texts: list[str]) -> int:
    """Index of the most typical text: the one most similar to all the others."""
    if len(texts) < 3:
        return 0
    scores = [mean(similarity(t, o) for k, o in enumerate(texts) if k != i) for i, t in enumerate(texts)]
    return scores.index(max(scores))


def word_diff(a: str, b: str, keep: int = 7) -> str:
    """Inline word diff: [-removed-] {+added+}. Long unchanged stretches are shortened."""
    aw, bw = a.split(), b.split()
    out: list[str] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, aw, bw, autojunk=False).get_opcodes():
        if tag == "equal":
            run = aw[i1:i2]
            if len(run) > 2 * keep + 2:
                skipped = len(run) - 2 * keep
                run = run[:keep] + [f"... ({skipped} words unchanged) ..."] + run[-keep:]
            out.append(" ".join(run))
        else:
            if i2 > i1:
                out.append("[-" + " ".join(aw[i1:i2]) + "-]")
            if j2 > j1:
                out.append("{+" + " ".join(bw[j1:j2]) + "+}")
    return " ".join(out)


def texts_of(recs: list[dict], field: str) -> list[tuple[int, str]]:
    """(run number, text) for the runs that produced this field."""
    out = []
    for n, rec in enumerate(recs, 1):
        text = FIELDS[field](rec or {})
        if text:
            out.append((n, str(text)))
    return out


def indent(text: str, pad: str = "      ") -> str:
    return "\n".join(pad + line for line in text.splitlines())


# --------------------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------------------
def run_once(cluster: dict, post, with_executor: bool) -> dict:
    """One Analyst call (and optionally one Executor call). Never raises: failures become records."""
    name = cluster["milestone_reference"]["Milestone"]
    t0 = time.perf_counter()
    try:
        code, analyst = post(ANALYST_PATH, {"input": cluster})
        if code != 200:
            return {"error": f"analyst HTTP {code}: {analyst}"}
        rec = {
            "analyst": analyst["output"],
            "attempts": analyst.get("attempts"),
            "usage": analyst.get("usage"),
            "request_id": analyst.get("request_id"),
            "model": analyst.get("model"),
        }
        if with_executor:
            code, executor = post(EXECUTOR_PATH, {"input": {"milestone": name, **analyst["output"]}})
            if code == 200:
                rec["executor_text"] = executor["output"]
            else:
                rec["error"] = f"executor HTTP {code}: {executor}"
    except Exception as exc:  # noqa: BLE001  (connection refused, timeout, bad JSON, ...)
        return {"error": f"{type(exc).__name__}: {exc}"}
    rec["seconds"] = round(time.perf_counter() - t0, 1)
    return rec


def collect(clusters: list[dict], runs: int, post, with_executor: bool, workers: int, say) -> dict[str, list[dict]]:
    """All milestones x all runs, `workers` at a time. Results are kept in run order."""
    by_name = {c["milestone_reference"]["Milestone"]: c for c in clusters}
    results: dict[str, list[dict | None]] = {name: [None] * runs for name in by_name}
    jobs = [(name, i) for i in range(runs) for name in by_name]  # run-major: sets fill in evenly

    def work(job):
        name, i = job
        return job, run_once(by_name[name], post, with_executor)

    done = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in cf.as_completed([pool.submit(work, j) for j in jobs]):
            (name, i), rec = fut.result()
            results[name][i] = rec
            done += 1
            outcome = f"{rec['seconds']}s" if "seconds" in rec and "error" not in rec else "FAILED"
            say(f"{done}/{len(jobs)} {short_name(name)} run {i + 1}: {outcome}")
    return results  # type: ignore[return-value]


def prompt_hashes() -> dict[str, str]:
    """Short fingerprints of the instruction files, so a result set says which prompt made it."""
    out = {}
    for key, fname in (("analyst", "milestone_risk_analyst.md"), ("executor", "milestone_risk_executor.md")):
        p = AGENTS_DIR / fname
        out[key] = hashlib.sha256(p.read_bytes()).hexdigest()[:8] if p.is_file() else "unknown"
    return out


# --------------------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------------------
def tally(recs: list[dict], expect: dict | None, with_executor: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in recs:
        for name, passed in evaluate(rec or {}, expect, with_executor).items():
            counts[name] = counts.get(name, 0) + (1 if passed else 0)
    return counts


def label_of(meta: dict) -> str:
    return meta.get("label") or "(no label)"


def header_lines(meta: dict) -> list[str]:
    p = meta.get("prompts", {})
    return [
        f"Run set: {label_of(meta)}   created {meta.get('created', '?')}",
        f"Runs per milestone: {meta.get('runs', '?')}   as_of_date: {meta.get('as_of', '?')}   "
        f"executor: {'yes' if meta.get('with_executor') else 'no'}   model: {meta.get('model') or '?'}",
        f"Prompt fingerprints: analyst {p.get('analyst', '?')}, executor {p.get('executor', '?')}",
    ]


def milestone_section(name: str, recs: list[dict], with_executor: bool, all_diffs: bool, full_diff: bool) -> list[str]:
    expect = expectation_for(name)
    n = len(recs)
    failed = sum(1 for r in recs if not r or "error" in r)
    lines = [f"=== {name}   ({n} runs, {failed} failed) ==="]

    lines.append("Checks (runs passing)")
    for check, passed in tally(recs, expect, with_executor).items():
        flag = "" if passed == n else "   <-- not every run"
        lines.append(f"  {passed}/{n}  {check}{flag}")

    good = [r for r in recs if r and isinstance(r.get("analyst"), dict)]
    lines.append("Variation across runs")
    if good:
        flags = Counter(str(r["analyst"].get("milestone_status_flagged", "?")).lower() for r in good)
        conf = Counter(r["analyst"].get("confidence_score", "?") for r in good)
        removal = Counter(", ".join(sorted(risk_ids(r["analyst"].get("risks_recommended_for_removal")))) or "none" for r in good)
        fmt = lambda c: ", ".join(f"{k} x{v}" for k, v in sorted(c.items(), key=lambda kv: str(kv[0])))  # noqa: E731
        lines.append(f"  flag: {fmt(flags)}")
        lines.append(f"  confidence: {fmt(conf)}")
        lines.append(f"  risks recommended for removal: {fmt(removal)}")
        secs = [r["seconds"] for r in good if "seconds" in r]
        outs = [(r.get("usage") or {}).get("output_tokens") for r in good]
        outs = [o for o in outs if isinstance(o, int)]
        if secs:
            lines.append(f"  time per run: {mean(secs):.0f}s average   Analyst output tokens: "
                         f"{min(outs) if outs else '?'}-{max(outs) if outs else '?'}")

    scored = []
    for field in FIELDS:
        texts = texts_of(recs, field)
        c = consistency([t for _, t in texts])
        if c is not None:
            scored.append(f"{field} {c:.2f}")
    if scored:
        lines.append("  wording consistency (1.00 = identical every run): " + ", ".join(scored))

    diff_fields = list(FIELDS) if full_diff else ["status", "executor"]
    for field in diff_fields:
        texts = texts_of(recs, field)
        if len(texts) < 2:
            continue
        if all_diffs:
            base_no, base = texts[0]
            for no, text in texts[1:]:
                lines.append(f"  {field}: run {base_no} -> run {no}   (similarity {similarity(base, text):.2f})")
                lines.append(indent(word_diff(base, text)))
        else:
            i, j, s = min(pairwise([t for _, t in texts]), key=lambda p: p[2])
            if s < 1.0:
                (a_no, a), (b_no, b) = texts[i], texts[j]
                lines.append(f"  {field}: most different pair, run {a_no} -> run {b_no}   (similarity {s:.2f})")
                lines.append(indent(word_diff(a, b)))
            else:
                lines.append(f"  {field}: identical in every run")
    return lines


def build_report(data: dict, all_diffs: bool = False, full_diff: bool = False) -> str:
    meta = data["meta"]
    with_exec = bool(meta.get("with_executor"))
    lines = header_lines(meta) + [""]
    total = clean = 0
    for name, recs in data["milestones"].items():
        lines += milestone_section(name, recs, with_exec, all_diffs, full_diff) + [""]
        for check, passed in tally(recs, expectation_for(name), with_exec).items():
            total += 1
            clean += 1 if passed == len(recs) else 0
    lines.append(f"Summary: {clean} of {total} checks passed in every run.")
    lines.append("Diff key: [-text-] was in the earlier run and not the later, {+text+} is new in the later run.")
    return "\n".join(lines)


def build_compare(a: dict, b: dict) -> str:
    """Pass rates and typical wording, set A (before) against set B (after)."""
    am, bm = a["meta"], b["meta"]
    lines = [
        f"A (before): {label_of(am)}   prompts {am.get('prompts')}   {am.get('runs')} runs   as_of {am.get('as_of')}",
        f"B (after):  {label_of(bm)}   prompts {bm.get('prompts')}   {bm.get('runs')} runs   as_of {bm.get('as_of')}",
    ]
    if am.get("as_of") != bm.get("as_of"):
        lines.append("NOTE: the two sets used different as_of_date values, which can change 'overdue' judgments.")
    if am.get("prompts") == bm.get("prompts"):
        lines.append("NOTE: both sets used the same prompt fingerprints, so differences are run-to-run noise.")
    lines.append("")

    better = worse = 0
    for name in a["milestones"]:
        if name not in b["milestones"]:
            continue
        ra, rb = a["milestones"][name], b["milestones"][name]
        ea, eb = bool(am.get("with_executor")), bool(bm.get("with_executor"))
        expect = expectation_for(name)
        ta, tb = tally(ra, expect, ea), tally(rb, expect, eb)
        lines.append(f"=== {name} ===")
        lines.append(f"  {'check':<44} {'A':>6} {'B':>6}   change")
        for check in dict.fromkeys(list(ta) + list(tb)):
            cell_a = f"{ta[check]}/{len(ra)}" if check in ta else "-"
            cell_b = f"{tb[check]}/{len(rb)}" if check in tb else "-"
            if check in ta and check in tb:
                pa, pb = ta[check] / len(ra), tb[check] / len(rb)
                change = "" if abs(pa - pb) < 1e-9 else ("better" if pb > pa else "WORSE")
            else:
                change = "(only in one set)"
            better += change == "better"
            worse += change == "WORSE"
            lines.append(f"  {check:<44} {cell_a:>6} {cell_b:>6}   {change}")

        for field in ("status", "executor"):
            xa, xb = texts_of(ra, field), texts_of(rb, field)
            if not xa or not xb:
                continue
            ca, cb = consistency([t for _, t in xa]), consistency([t for _, t in xb])
            if ca is not None and cb is not None:
                lines.append(f"  {field} wording consistency: A {ca:.2f} -> B {cb:.2f}")
            ma, mb = xa[medoid([t for _, t in xa])][1], xb[medoid([t for _, t in xb])][1]
            lines.append(f"  most typical {field}, A -> B   (similarity {similarity(ma, mb):.2f})")
            lines.append(indent(word_diff(ma, mb)))
        lines.append("")
    lines.append(f"Overall: {better} check(s) better, {worse} worse.")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------
def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if "meta" not in data or "milestones" not in data:
        sys.exit(f"{path} is not a repeat_runs result file.")
    return data


def select_clusters(clusters: list[dict], wanted: list[str] | None) -> list[dict]:
    chosen = []
    for c in clusters:
        name = c["milestone_reference"]["Milestone"]
        if wanted:
            if not any(w.lower() in name.lower() for w in wanted):
                continue
        elif not (c["task_tracker"] or c["risk_register"]):
            continue  # nothing linked to this milestone, so nothing to analyze
        chosen.append(c)
    return chosen


def stamp(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=5, help="runs per milestone (default 5)")
    ap.add_argument("--label", default="", help="short name for this run set, e.g. baseline or resource-rule")
    ap.add_argument("--milestone", action="append", help="substring of a milestone name; repeatable")
    ap.add_argument("--analyst-only", action="store_true", help="skip the Executor calls")
    ap.add_argument("--workers", type=int, default=3, help="calls in flight at once (default 3)")
    ap.add_argument("--as-of", default=dt.date.today().isoformat(), help="report date (default: today)")
    ap.add_argument("--data-dir", type=Path, default=PROJECT / "sample_data")
    ap.add_argument("--out", type=Path, help="result file (default: runs/<timestamp>[-label].json)")
    ap.add_argument("--no-reload", action="store_true", help="do not ask the service to re-read the instruction files")
    ap.add_argument("--all-diffs", action="store_true", help="diff every run against run 1, not just the most different pair")
    ap.add_argument("--full-diff", action="store_true", help="also diff the long `explanation` field")
    ap.add_argument("--dry-run", action="store_true", help="show the plan; call nothing")
    ap.add_argument("--report", type=Path, metavar="FILE", help="re-print the report for a saved result file")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"), help="compare two saved result files")
    args = ap.parse_args()

    if args.compare:
        print(build_compare(load(args.compare[0]), load(args.compare[1])))
        return
    if args.report:
        print(build_report(load(args.report), args.all_diffs, args.full_diff))
        return
    if args.runs < 1 or args.workers < 1:
        sys.exit("--runs and --workers must be at least 1.")

    clusters = select_clusters(run_analysis.build_clusters(args.data_dir, args.as_of), args.milestone)
    if not clusters:
        sys.exit("No milestones selected.")
    with_executor = not args.analyst_only
    calls = len(clusters) * args.runs * (2 if with_executor else 1)
    names = ", ".join(short_name(c["milestone_reference"]["Milestone"]) for c in clusters)
    plan = (f"{len(clusters)} milestone(s) [{names}] x {args.runs} runs = {calls} API call(s) "
            f"({'Analyst + Executor' if with_executor else 'Analyst only'}), {args.workers} at a time, as_of {args.as_of}")
    if args.dry_run:
        print("Would run:", plan)
        print("Prompt fingerprints:", prompt_hashes())
        return

    secret = smoke_test.read_secret()
    post = lambda path, body: smoke_test.call("POST", path, secret, body)  # noqa: E731
    if not args.no_reload:
        code, _ = smoke_test.call("POST", "/admin/reload", secret)
        stamp(f"Instruction files re-read by the service (HTTP {code})")
        if code != 200:
            sys.exit("Could not reload the instruction files. Is the service running and the secret correct?")

    stamp(f"Running {plan}")
    started = time.perf_counter()
    results = collect(clusters, args.runs, post, with_executor, args.workers, stamp)
    stamp(f"All done in {time.perf_counter() - started:.0f}s")

    models = {r.get("model") for recs in results.values() for r in recs if r and r.get("model")}
    data = {
        "meta": {
            "label": args.label,
            "created": dt.datetime.now().isoformat(timespec="seconds"),
            "runs": args.runs,
            "as_of": args.as_of,
            "with_executor": with_executor,
            "model": ", ".join(sorted(models)),
            "service": smoke_test.BASE,
            "prompts": prompt_hashes(),
        },
        "milestones": results,
    }
    report = build_report(data, args.all_diffs, args.full_diff)

    out = args.out
    if out is None:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.label).strip("-")
        out = RUNS_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}{'-' + slug if slug else ''}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    out.with_suffix(".txt").write_text(report + "\n", encoding="utf-8")

    print()
    print(report)
    print(f"\nSaved {out} and {out.with_suffix('.txt').name}")


if __name__ == "__main__":
    main()
