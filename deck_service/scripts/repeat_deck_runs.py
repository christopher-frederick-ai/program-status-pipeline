#!/usr/bin/env python3
"""Run the deck builder N times on one fixture and compare the runs, the way repeat_runs.py does
for the Analyst. Needs ANTHROPIC_API_KEY and LibreOffice. Writes a JSON record to runs/.

  python scripts/repeat_deck_runs.py fixtures/week_2026-09-24.json -n 5 --label round3
"""
import argparse, json, sys, time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deck_service.adapter import build_deck_input
from deck_service.builder import BuildError, Settings, build_deck
from deck_service.schemas import DeckRequest


def run_many(inp, n, client, settings, **kw):
    runs = []
    for i in range(n):
        t0 = time.time()
        try:
            r = build_deck(inp, client, settings, **kw)
            man = json.loads(Path(r.files["manifest"]).read_text())
            runs.append({"run": i + 1, "status": r.status, "repairs": r.repairs, "slide_count": r.slide_count,
                         "kinds": [s["kind"] for s in man["slides"]],
                         "failed": [x[0] for x in r.results if not x[1]], "usage": r.usage,
                         "seconds": round(time.time() - t0), "files": r.files})
        except BuildError as e:
            runs.append({"run": i + 1, "status": "error", "error": str(e), "failed": ["no_deck"]})
    return runs


def summarize(runs):
    ok = [r for r in runs if r["status"] != "error"]
    fails = Counter(f for r in runs for f in r["failed"])
    return {
        "runs": len(runs),
        "all_checks_passed": sum(r["status"] == "ok" for r in runs),
        "needed_repair": sum(r.get("repairs", 0) > 0 for r in ok),
        "slide_counts": sorted(Counter(r["slide_count"] for r in ok).items()),
        "distinct_slide_sequences": len({tuple(r["kinds"]) for r in ok}),
        "checks_failing_in_final_decks": dict(fails),
        "tokens": {k: sum(r["usage"][k] for r in ok) for k in ("input_tokens", "output_tokens")},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture"); ap.add_argument("-n", type=int, default=5); ap.add_argument("--label", default="run")
    ap.add_argument("--no-repair", action="store_true", help="max_repairs=0: measure the instructions alone")
    a = ap.parse_args()
    import anthropic
    fx = json.loads(Path(a.fixture).read_text())
    fx.pop("_comment", None)
    s = Settings.from_env()
    if a.no_repair:
        s.max_repairs = 0
    runs = run_many(build_deck_input(DeckRequest(**fx)), a.n, anthropic.Anthropic(timeout=900), s)
    summ = summarize(runs)
    out = Path("runs") / f"{a.label}_{Path(a.fixture).stem}.json"
    out.parent.mkdir(exist_ok=True); out.write_text(json.dumps({"summary": summ, "runs": runs}, indent=2))
    print(json.dumps(summ, indent=2)); print("saved", out)


if __name__ == "__main__":
    main()
