"""Request and response models for the deck service.

The request is what Workflow 3 already has: the saved sidecar (files/report_<date>.json) and the
rows of milestones.xlsx. The service joins them (see adapter.py), so n8n needs no Code node.
"""
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ISO_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$")


def norm_date(v: Any, allow_tbd: bool = False) -> str:
    """'2026-04-15', '2026-04-15T00:00:00.000Z' -> '2026-04-15'. 'TBD' only where allowed."""
    s = str(v).strip() if v is not None else ""
    if allow_tbd and s.upper() in ("TBD", ""):
        return "TBD"
    m = ISO_PREFIX.match(s)
    if not m:
        raise ValueError(f"must be a YYYY-MM-DD date{' or TBD' if allow_tbd else ''}, got {v!r}")
    return m.group(1)


class SidecarMilestone(BaseModel):
    """One entry of report_<date>.json -> milestones. Older sidecars lack the slide_* fields."""
    model_config = ConfigDict(extra="ignore")
    name: str
    executor_text: str = Field(min_length=1)
    milestone_status_flagged: Literal["yes", "no"]
    risks_recommended_for_removal: list[Any] = Field(default_factory=list)
    slide_title: str = ""
    slide_subtitle: str = ""
    # status / explanation are the Analyst's working notes. They are accepted but deliberately
    # never forwarded to the deck builder: only what the reviewer approved reaches the slides.
    status: str = ""
    explanation: str = ""

    @field_validator("milestone_status_flagged", mode="before")
    @classmethod
    def _lower(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class Sidecar(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = ""
    date: str
    milestones: dict[str, SidecarMilestone] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, v):
        # n8n's Extract From File (JSON) may put the document under "data"
        if isinstance(v, dict) and "milestones" not in v and isinstance(v.get("data"), dict):
            return v["data"]
        return v

    @field_validator("date")
    @classmethod
    def _d(cls, v): return norm_date(v)


class MilestoneRow(BaseModel):
    """A row of milestones.xlsx as n8n's Extract From XLSX returns it."""
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    milestone: str = Field(alias="Milestone")
    planned: str = Field(alias="Planned Date")
    forecast: str = Field(alias="Forecast/Actual Date")
    status: str = Field(alias="Status")

    @field_validator("planned", mode="before")
    @classmethod
    def _p(cls, v): return norm_date(v)

    @field_validator("forecast", mode="before")
    @classmethod
    def _f(cls, v): return norm_date(v, allow_tbd=True)


class DeckOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_slides: Optional[int] = Field(default=None, ge=1, le=40)
    max_slides: Optional[int] = Field(default=None, ge=1, le=40)


class DeckRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sidecar: Sidecar
    milestone_rows: list[MilestoneRow] = Field(min_length=1)
    program: Optional[str] = None      # default: derived from sidecar.title
    options: DeckOptions = Field(default_factory=DeckOptions)


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class DeckResponse(BaseModel):
    request_id: str
    # "ok": every automated check passed. "needs_review": a deck was produced but some checks
    # still fail after the repair rounds. It is still returned: a person reviews every deck.
    status: Literal["ok", "needs_review"]
    report_date: str
    files: dict[str, str]            # pptx / pdf / manifest -> path on the shared /files mount
    slide_count: int
    checks: list[CheckResult]
    failed_checks: list[str]
    repair_rounds: int
    model: str
    usage: dict[str, int]
    latency_ms: int
