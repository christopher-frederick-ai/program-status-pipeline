"""Join the saved sidecar with the milestone sheet into the deck builder's input.

Design rule carried over from Workflow 2's build_slide_bullets.js: a deck says only what the
reviewer approved. So the model sees each milestone's `executor_text` (the approved paragraph),
the approved slide title/subtitle, the Analyst's yes/no flag as a plain boolean, and the sheet's
own dates and status. The Analyst's `status` and `explanation` notes are NOT forwarded.
"""
import re

from .schemas import DeckRequest


class AdapterError(ValueError):
    """The sidecar and the milestone sheet do not line up."""


def _abbrev(name: str) -> str | None:
    m = re.search(r"\(([A-Za-z0-9/ \-]{2,12})\)\s*$", name)
    return re.sub(r"[^A-Za-z0-9]+", "", m.group(1)).upper() if m else None


def derive_program(title: str) -> str:
    base = title.split(":")[0].strip() if title else ""
    return re.sub(r"\s+Program$", "", base) or "Program"


def build_deck_input(req: DeckRequest) -> dict:
    rows = [r for r in req.milestone_rows if r.milestone.strip()]
    by_name = {r.milestone.strip(): r for r in rows}
    if len(by_name) != len(rows):
        raise AdapterError("milestone_rows contains the same milestone name more than once")

    unknown = [m.name for m in req.sidecar.milestones.values() if m.name.strip() not in by_name]
    if unknown:  # a renamed milestone would otherwise silently lose its status and dates
        raise AdapterError(f"sidecar milestones not found in milestone_rows by exact name: {unknown}")

    key_for_name = {m.name.strip(): k for k, m in req.sidecar.milestones.items()}
    milestones = []
    for r in rows:
        name = r.milestone.strip()
        sc = req.sidecar.milestones.get(key_for_name.get(name, ""))
        item = {
            "id": key_for_name.get(name) or _abbrev(name) or re.sub(r"[^A-Za-z0-9]+", "_", name).upper()[:12],
            "name": name,
            "planned": r.planned,
            "forecast": r.forecast,
            "reported_status": r.status.strip(),
            "in_weekly_report": sc is not None,
        }
        if sc is not None:
            item.update({
                # flag "yes" means the evidence does NOT support the reported status
                "reported_status_supported": sc.milestone_status_flagged == "no",
                "approved_text": sc.executor_text.strip(),
                "slide_title": sc.slide_title.strip(),
                "slide_subtitle": sc.slide_subtitle.strip(),
                "risks_recommended_for_removal": [
                    {"risk_id": x.get("risk_id", ""), "title": x.get("title", "")} if isinstance(x, dict)
                    else {"risk_id": str(x), "title": ""} for x in sc.risks_recommended_for_removal],
            })
        milestones.append(item)

    return {
        "program": (req.program or derive_program(req.sidecar.title)).strip(),
        "report_date": req.sidecar.date,
        "milestones": milestones,
        "options": req.options.model_dump(),
    }
