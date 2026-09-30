"""Smoke-test the running agent service. Standard library only; works from PowerShell.

    python scripts/smoke_test.py            # health + agent list + Executor run
    python scripts/smoke_test.py --no-llm   # skip the Executor call (no API spend)

Reads SERVICE_SHARED_SECRET from the environment, or from ../.env next to this project.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = os.environ.get("AGENT_SERVICE_URL", "http://127.0.0.1:8000")


def read_secret() -> str:
    if os.environ.get("SERVICE_SHARED_SECRET"):
        return os.environ["SERVICE_SHARED_SECRET"]
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("SERVICE_SHARED_SECRET="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("SERVICE_SHARED_SECRET not found (set the env var or fill in .env).")


def call(method: str, path: str, secret: str | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Service-Token"] = secret
    data = json.dumps(body, default=str).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


SAMPLE_ANALYST_OUTPUT = {
    "milestone": "Critical Design Review (CDR)",
    "confidence_score": 4,
    "milestone_status_flagged": "yes",
    "status": "CDR is at risk: the radar adapter task is overdue and its risk record is stale.",
    "explanation": "The reference says On Track, but the linked task is overdue.",
    "risks_recommended_for_removal": ["RISK-021"],
}


def main() -> None:
    secret = read_secret()
    print("GET /health ->", *call("GET", "/health"))
    code, agents = call("GET", "/agents", secret)
    print("GET /agents ->", code)
    for a in agents if isinstance(agents, list) else []:
        print(f"   {a['name']}: available={a['available']}", a.get("detail") or "")
    code, _ = call("GET", "/agents")
    print("GET /agents without token ->", code, "(expected 401)")

    if "--no-llm" in sys.argv:
        return
    code, res = call("POST", "/agents/milestone_risk_executor/run", secret, {"input": SAMPLE_ANALYST_OUTPUT})
    print("POST executor/run ->", code)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
