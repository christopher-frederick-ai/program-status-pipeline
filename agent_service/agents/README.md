# Agent instruction files

Each agent registered in `app/agents.py` reads its system prompt from a file in this
folder. Supported formats: `.docx`, `.md`, `.txt`.

## Files expected right now

| Agent | File | Status |
|---|---|---|
| `milestone_risk_analyst` | `milestone_risk_analyst.md` | Cleaned up after the first live run. Review it. |
| `milestone_risk_executor` | `milestone_risk_executor.md` | Drafted from the design summary. Review it. |

If an instruction file is missing, the service still starts. That agent is listed as
`available: false` by `GET /agents`, and calling it returns HTTP 503.

To measure the effect of an edit over repeated runs, use `scripts/repeat_runs.py` (see the main README).

Instruction files are read at service startup. After editing one, restart the service
(`docker compose restart agent-service`) or call `POST /admin/reload` (requires the token).

## Adding an agent

1. Drop the instructions file here.
2. Add one `AgentSpec(...)` entry to `AGENTS` in `app/agents.py`.
3. If the agent returns JSON, define a Pydantic model for its output in `app/schemas.py`
   and reference it in the spec.
