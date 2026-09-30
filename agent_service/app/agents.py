"""Agent registry: which agents exist, where their instructions live, what they return.

Adding an agent = drop an instructions file in agents/ and add one AgentSpec below.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pydantic import BaseModel

from .schemas import AgentInfo, MilestoneRiskAnalystOutput, MilestoneSlideTitleOutput

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    name: str
    description: str
    instructions_file: str  # filename inside the agents directory
    output_kind: Literal["json", "text"]
    output_schema: type[BaseModel] | None = None  # required when output_kind == "json"
    max_tokens: int | None = None  # falls back to settings.default_max_tokens
    input_prefix: str = ""  # prepended to the serialized input, exactly as in the tested harness


AGENTS: list[AgentSpec] = [
    AgentSpec(
        name="milestone_risk_analyst",
        description=(
            "Cross-references task tracker, risk registrar and engineering notes against "
            "the milestone reference for ONE milestone cluster; returns structured JSON."
        ),
        instructions_file="milestone_risk_analyst.md",
        output_kind="json",
        output_schema=MilestoneRiskAnalystOutput,
        # The Analyst's JSON can run ~2,000 tokens; the 2048 default risked cutting it off
        # mid-object (which shows up as a JSONDecodeError). This is a cap, not a target.
        max_tokens=4096,
        # The validated harness (test_milestone_risk_analyst.py) sends this exact prefix.
        input_prefix="Source data:\n",
    ),
    AgentSpec(
        name="milestone_risk_executor",
        description=(
            "Formats the Analyst's JSON into 1-3 sentences of report prose, leading with "
            "the status field. Mechanical formatting; no new judgment."
        ),
        instructions_file="milestone_risk_executor.md",
        output_kind="text",
        # Seen in live n8n runs: 600 was too tight. The model can spend well over 600 tokens
        # before producing any visible text, which gets cut off at max_tokens and shows up as
        # an empty response (both retry attempts can hit this on the same input). This is a
        # cap, not a target: the answer itself is still 1-3 short sentences.
        max_tokens=2000,
    ),
    AgentSpec(
        name="milestone_slide_title",
        description=(
            "Compresses the Executor's already-approved paragraph into a short slide "
            "title and one-line status subtitle. Pure compression; no new judgment."
        ),
        instructions_file="milestone_slide_title.md",
        output_kind="json",
        output_schema=MilestoneSlideTitleOutput,
        max_tokens=600,
    ),
    # Planner, remaining Executors and Reviewer get added here once designed and tested.
]


# ---------------------------------------------------------------------------
# Instruction loading
# ---------------------------------------------------------------------------
def _docx_to_text(path: Path) -> str:
    """Flatten a .docx to text, keeping paragraphs and tables in document order."""
    doc = Document(str(path))
    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, doc).text
            if text.strip():  # matches the tested harness, which drops blank paragraphs
                lines.append(text)
        elif tag == "tbl":
            table = Table(child, doc)
            for row in table.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(lines).strip()


def load_instructions(path: Path) -> str:
    if not path.is_file():
        # python-docx raises its own PackageNotFoundError for a missing .docx; normalize.
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = _docx_to_text(path)
    elif suffix in {".md", ".txt"}:
        text = path.read_text(encoding="utf-8").strip()
    else:
        raise ValueError(f"Unsupported instructions format: {path.name}")
    if not text:
        raise ValueError(f"Instructions file is empty: {path.name}")
    return text


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
@dataclass
class LoadedAgent:
    spec: AgentSpec
    instructions: str | None
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.instructions is not None


class AgentRegistry:
    def __init__(self, agents_dir: Path, specs: list[AgentSpec] | None = None):
        self.agents_dir = agents_dir
        self.specs = specs if specs is not None else AGENTS
        self._agents: dict[str, LoadedAgent] = {}
        self.load()

    def load(self) -> None:
        """(Re)read every instructions file. Missing/bad files mark the agent unavailable
        instead of crashing the service."""
        loaded: dict[str, LoadedAgent] = {}
        for spec in self.specs:
            if spec.output_kind == "json" and spec.output_schema is None:
                raise ValueError(f"Agent {spec.name}: JSON agents need an output_schema")
            path = self.agents_dir / spec.instructions_file
            try:
                text = load_instructions(path)
                loaded[spec.name] = LoadedAgent(spec, text)
                log.info("Loaded agent %s from %s (%d chars)", spec.name, path.name, len(text))
            except FileNotFoundError:
                msg = f"Instructions file not found: {spec.instructions_file}"
                loaded[spec.name] = LoadedAgent(spec, None, msg)
                log.warning("Agent %s unavailable: %s", spec.name, msg)
            except Exception as exc:  # corrupt docx, empty file, bad encoding...
                msg = f"Could not load {spec.instructions_file}: {exc}"
                loaded[spec.name] = LoadedAgent(spec, None, msg)
                log.error("Agent %s unavailable: %s", spec.name, msg)
        self._agents = loaded

    def get(self, name: str) -> LoadedAgent | None:
        return self._agents.get(name)

    def info(self) -> list[AgentInfo]:
        return [
            AgentInfo(
                name=a.spec.name,
                description=a.spec.description,
                output_kind=a.spec.output_kind,
                instructions_file=a.spec.instructions_file,
                available=a.available,
                detail=a.error,
            )
            for a in self._agents.values()
        ]
