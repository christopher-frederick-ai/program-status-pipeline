"""Automated checks on a generated deck.

Each check measures a property that should hold on EVERY run, so the same checks serve three jobs:
the service's repair loop, repeat_deck_runs.py (variance across runs), and a quick CLI look at any
saved deck:  python scripts/check_deck.py REQUEST.json OUTDIR
OUTDIR holds deck.pptx, deck.pdf, manifest.json.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

BANNED = ["disaster", "crisis", "failing", "failure", "unacceptable", "catastrophic",
          "alarming", "dire", "negligent", "incompetent", "blame"]
RATING_WORDS = ["Delayed", "At Risk", "On Track", "Complete", "In Progress", "Not Started"]
ISO_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
PROSE_DATE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:,\s*(20\d\d))?\b")
ALARM_IN_TEXT = re.compile(r"overdue|blocked|behind|delay|slip|no assignee|not on track|at risk", re.I)
ALL_CLEAR = re.compile(r"\bno (open )?(risks?|issues?)\b|\bnone (were )?(named|reported)\b", re.I)
FOOTER_BAND_IN = 6.95   # body text must end above this (footer lives below it)

DEFAULT_EXPECT = {"min_slides": 5, "max_slides": 10,
                  "required_kinds": ["cover", "exec_summary", "schedule", "accuracy", "risks"],
                  "optional_kinds": ["tasks", "decisions"]}


def dates_in(text: str, default_year: str) -> set:
    """ISO dates plus dates spelled out in prose ('September 12, 2026', 'September 24'), as ISO."""
    out = set(ISO_RE.findall(text))
    for m in PROSE_DATE.finditer(text):
        out.add(f"{m.group(3) or default_year}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}")
    return out


def normalize_input(inp: dict) -> dict:
    """Take the adapter's output and add what the checks need. Existing values are kept."""
    inp = json.loads(json.dumps(inp))
    year = inp["report_date"][:4]
    allowed, approved = {inp["report_date"]}, set()
    for m in inp["milestones"]:
        m.setdefault("match", [m["id"], m["name"].split(" (")[0]])
        allowed |= {m["planned"]} | ({m["forecast"]} if m["forecast"] != "TBD" else set())
        approved |= {m["planned"]} | ({m["forecast"]} if m["forecast"] != "TBD" else set())
        d = dates_in(m.get("approved_text", ""), year)
        allowed |= d
        approved |= d
    inp["allowed_dates"], inp["approved_dates"] = sorted(allowed), sorted(approved)
    opts = {k: v for k, v in inp.get("options", {}).items() if v is not None}
    inp["expect"] = {**DEFAULT_EXPECT, **opts}
    return inp


def slide_texts(prs):
    out = []
    for s in prs.slides:
        texts = []
        for sh in s.shapes:
            if sh.has_text_frame:
                texts.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    for c in row.cells:
                        texts.append(c.text_frame.text)
        out.append(texts)
    return out


def run_checks(fx, outdir):
    """Return a list of (name, passed, detail). fx must come from normalize_input."""
    out = Path(outdir)
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))

    files_ok = all((out / f).exists() for f in ("deck.pptx", "deck.pdf", "manifest.json"))
    check("outputs_exist", files_ok, "deck.pptx, deck.pdf, manifest.json")
    if not files_ok:
        return results

    man = json.loads((out / "manifest.json").read_text())
    prs = Presentation(out / "deck.pptx")
    per_slide = slide_texts(prs)
    alltext = "\n".join("\n".join(t) for t in per_slide)
    low = alltext.lower()
    n = len(prs.slides)
    ex = fx["expect"]
    ms = fx["milestones"]
    in_report = [m for m in ms if m.get("in_weekly_report")]

    # ---- structure
    check("slide_count_in_band", ex["min_slides"] <= n <= ex["max_slides"],
          f"{n} slides, allowed {ex['min_slides']}-{ex['max_slides']}")
    check("manifest_matches_deck", man.get("slide_count") == n == len(man.get("slides", [])),
          f"manifest {man.get('slide_count')}, deck {n}")
    kinds = [s["kind"] for s in man.get("slides", [])]
    missing = [k for k in ex["required_kinds"] if k not in kinds]
    check("required_slides_present", not missing, f"missing {missing}" if missing else "")
    canon = ex["required_kinds"] + ex["optional_kinds"]
    firsts = [k for i, k in enumerate(kinds) if k in canon and k not in kinds[:i]]
    expected = sorted(firsts, key=canon.index)
    check("slide_order", firsts == expected, f"got {firsts}")

    # ---- statuses given, never re-rated
    want = {m["id"]: ("yes" if m["reported_status_supported"] else "no") for m in in_report}
    got = {a["milestone_id"]: str(a.get("evidence_supports", "")).lower() for a in man.get("accuracy", [])}
    check("accuracy_flags_match_input", got == want,
          f"differences: { {k: (got.get(k), want[k]) for k in want if got.get(k) != want[k]} }; extra: {sorted(set(got) - set(want))}")
    miss_rep = [m["id"] for m in ms if m["reported_status"].lower() not in low]
    check("reported_status_shown", not miss_rep, str(miss_rep))
    allowed_words = " ".join([m["reported_status"] for m in ms] + [m.get("approved_text", "") + " " + m.get("slide_title", "") + " " + m.get("slide_subtitle", "") for m in ms]).lower()
    invented = [w for w in RATING_WORDS if re.search(rf"\b{re.escape(w)}\b", alltext, re.I)
                and not re.search(rf"\b{re.escape(w)}\b", allowed_words, re.I)]
    check("no_invented_ratings", not invented, f"rating words in deck but not in input: {invented}")

    # ---- coverage
    miss_m = [m["id"] for m in ms if not any(s.lower() in low for s in m["match"])]
    check("all_milestones_covered", not miss_m, str(miss_m))
    rem_ids = [r["risk_id"] for m in in_report for r in m.get("risks_recommended_for_removal", []) if r["risk_id"]]
    miss_rem = [r for r in rem_ids if r.lower() not in low]
    check("removal_risk_ids_shown", not miss_rem, str(miss_rem))

    # ---- dates: every approved date present, none invented
    deck_dates = set(ISO_RE.findall(alltext))
    miss_dates = sorted(set(fx["approved_dates"]) - deck_dates)
    check("approved_dates_present", not miss_dates, f"dates in the input but not in the deck: {miss_dates}")
    stray = sorted(deck_dates - set(fx["allowed_dates"]))
    check("no_unknown_dates", not stray, f"dates not in input: {stray}")

    # ---- hygiene
    leftovers = [p for p in ("{{", "}}", "lorem", "placeholder", "TODO", "undefined", "null", "None") if p.lower() in low]
    check("no_placeholder_leftovers", not leftovers, str(leftovers))
    bare = [i + 1 for i, ts in enumerate(per_slide) for t in ts if t.strip() and re.fullmatch(r"[\s•\-●▪]+", t)]
    check("no_bare_bullets", not bare, str(bare))
    hits = [w for w in BANNED if re.search(rf"\b{w}\b", alltext, re.I)]
    check("no_loaded_language", not hits, str(hits))

    # ---- the risks slide must not claim an all-clear the approved text contradicts
    risk_text = "\n".join("\n".join(per_slide[i]) for i, k in enumerate(kinds) if k == "risks" and i < len(per_slide))
    alarms = any(ALARM_IN_TEXT.search(m.get("approved_text", "")) for m in in_report)
    check("no_false_all_clear", not (ALL_CLEAR.search(risk_text) and alarms),
          "risks slide says nothing was named, but approved texts mention overdue/blocked/behind/delay items")

    # ---- layout
    W, H = prs.slide_width, prs.slide_height
    outside = []
    for i, s in enumerate(prs.slides, 1):
        for sh in s.shapes:
            if sh.left is None:
                continue
            if sh.left < -Emu(9525) or sh.top < -Emu(9525) or sh.left + sh.width > W + 9525 or sh.top + sh.height > H + 9525:
                outside.append(i)
    check("shapes_within_slide", not outside, f"slides {sorted(set(outside))}")
    pages = pdf_pages(out / "deck.pdf")
    check("pdf_page_count_matches", pages == n, f"pdf {pages}, deck {n}")
    check("no_body_text_in_footer_band", not (spill := pdf_body_spill(out / "deck.pdf", fx)), str(spill[:6]))
    check("no_overlapping_text", not (ov := pdf_overlaps(out / "deck.pdf")), str(ov[:4]))
    check("qa_issues_reported_empty", not man.get("qa", {}).get("issues_remaining"),
          str(man.get("qa", {}).get("issues_remaining")))
    return results


def pdf_pages(pdf):
    o = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    m = re.search(r"Pages:\s+(\d+)", o)
    return int(m.group(1)) if m else -1


def _bbox_pages(pdf):
    o = subprocess.run(["pdftotext", "-bbox-layout", str(pdf), "-"], capture_output=True, text=True).stdout
    for pm in re.finditer(r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', o, re.S):
        lines = []
        for lm in re.finditer(r'<line xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">(.*?)</line>', pm.group(3), re.S):
            x0, y0, x1, y1 = map(float, lm.groups()[:4])
            lines.append((x0, y0, x1, y1, " ".join(re.findall(r">([^<]+)</word>", lm.group(5)))))
        yield float(pm.group(1)), float(pm.group(2)), lines


def pdf_body_spill(pdf, fx):
    """Text lines reaching into the footer band that are not the footer or a slide number."""
    spill = []
    for _, ph, lines in _bbox_pages(pdf):
        for x0, y0, x1, y1, words in lines:
            is_footer = "Weekly status as of" in words or fx["program"] in words or re.fullmatch(r"\d+", words.strip() or "x")
            if y1 / ph * 7.5 > FOOTER_BAND_IN and not is_footer:
                spill.append(words[:50])
    return spill


def pdf_overlaps(pdf):
    """Pairs of text lines on one page whose boxes intersect noticeably."""
    found = []
    for pi, (_, _, lines) in enumerate(_bbox_pages(pdf), 1):
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, b = lines[i], lines[j]
                iw = min(a[2], b[2]) - max(a[0], b[0]); ih = min(a[3], b[3]) - max(a[1], b[1])
                if iw > 0 and ih > 0:
                    small = min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])) or 1
                    if iw * ih / small > 0.15:
                        found.append(f"p{pi}: '{a[4][:30]}' x '{b[4][:30]}'")
    return found


def report(results):
    width = max(len(r[0]) for r in results)
    fails = 0
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {'' if ok else detail}")
        fails += (not ok)
    print(f"\n{len(results) - fails}/{len(results)} checks passed")
    return 1 if fails else 0


def main(request_path, outdir):
    from .adapter import build_deck_input
    from .schemas import DeckRequest
    body = json.loads(Path(request_path).read_text())
    body.pop("_comment", None)
    fx = normalize_input(build_deck_input(DeckRequest(**body)))
    return report(run_checks(fx, outdir))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
