"""Assemble per-milestone clusters from rows that are already in memory.

Behaviour mirrors build_clusters() in scripts/run_analysis.py, the reference that produced the
validated Analyst runs. tests/test_clusters.py checks the two give identical clusters on the
sample data. The differences are deliberate and small:

- rows arrive as JSON from n8n instead of being read from files,
- "Related Milestone" tags are matched ignoring stray leading/trailing whitespace,
- problems come back as data (`warnings`, `skipped`) instead of being printed.

No LLM is involved. A cluster is the milestone's reference row, the task and risk rows tagged to
it, and the full engineering notes (the Analyst is told to use only what applies to its milestone).
"""

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClusterResult:
    clusters: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _blank(row: dict) -> bool:
    return not any(_text(v) for v in row.values())


def build_clusters(
    milestones: list[dict],
    tasks: list[dict],
    risks: list[dict],
    notes: str,
    as_of: str | None,
    *,
    include_empty: bool = False,
    max_chars: int | None = None,
) -> ClusterResult:
    """One cluster per milestone, in the order the milestones were given.

    Key order inside a cluster matches the tested harness (as_of_date first, when given), because
    the Analyst sees the cluster as indented JSON.
    """
    milestones = [m for m in milestones if _text(m.get("Milestone"))]
    if not milestones:
        raise ValueError("No milestone rows with a 'Milestone' value were provided.")
    tasks = [r for r in tasks if not _blank(r)]  # trailing empty spreadsheet/CSV rows
    risks = [r for r in risks if not _blank(r)]

    result = ClusterResult()

    seen: set[str] = set()
    for m in milestones:
        name = _text(m["Milestone"])
        if name in seen:
            result.warnings.append(f"Milestone {name!r} appears more than once; rows will repeat.")
        seen.add(name)

    # A typo in "Related Milestone" would otherwise drop the row silently.
    for kind, rows in (("Task", tasks), ("Risk", risks)):
        for row in rows:
            if _text(row.get("Related Milestone")) not in seen:
                result.warnings.append(
                    f"{kind} {row.get('Task Name')!r} is tagged to unknown milestone "
                    f"{row.get('Related Milestone')!r} and is not in any cluster."
                )

    for m in milestones:
        name = _text(m["Milestone"])
        linked_tasks = [r for r in tasks if _text(r.get("Related Milestone")) == name]
        linked_risks = [r for r in risks if _text(r.get("Related Milestone")) == name]

        if not include_empty and not (linked_tasks or linked_risks):
            result.skipped.append({"milestone": name, "reason": "no linked tasks or risks"})
            continue

        cluster: dict[str, Any] = {}
        if as_of:
            cluster["as_of_date"] = as_of
        cluster["milestone_reference"] = m
        cluster["task_tracker"] = linked_tasks
        cluster["risk_register"] = linked_risks
        cluster["engineering_notes"] = notes

        if max_chars is not None:
            size = len(json.dumps(cluster, indent=2, ensure_ascii=False, default=str))
            if size > max_chars:
                result.warnings.append(
                    f"Cluster for {name!r} is {size} characters, over the {max_chars} limit the "
                    "agents accept. Shorten the engineering notes."
                )
        result.clusters.append(cluster)

    return result
