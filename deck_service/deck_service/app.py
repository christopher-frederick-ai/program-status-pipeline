"""FastAPI service: POST /render_weekly_deck.

Same conventions as agent_service: shared-secret header, instruction files as editable
markdown, logs carry sizes and token counts (never content).
Run: uvicorn deck_service.main:app --host 0.0.0.0 --port 8010
"""
import hmac
import logging
import shutil
import time

from fastapi import Depends, FastAPI, Header, HTTPException

from .adapter import AdapterError, build_deck_input
from .builder import BuildError, Settings, build_deck
from .schemas import CheckResult, DeckRequest, DeckResponse

log = logging.getLogger("deck_service")


def create_app(client_factory=None, settings: Settings | None = None, renderer=None, imager=None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Deck service")
    state = {"client": None}

    def get_client():
        if state["client"] is None:
            if client_factory:
                state["client"] = client_factory()
            else:
                import anthropic
                state["client"] = anthropic.Anthropic(timeout=900, max_retries=2)
        return state["client"]

    def auth(x_service_token: str = Header(default="")):
        if not settings.service_token:
            raise HTTPException(503, "SERVICE_SHARED_SECRET is not configured")  # fail closed
        if not hmac.compare_digest(x_service_token, settings.service_token):
            raise HTTPException(401, "invalid service token")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "libreoffice": bool(shutil.which("soffice")),
                "pdftoppm": bool(shutil.which("pdftoppm")),
                "instructions_present": settings.instructions_path.exists(), "model": settings.model}

    @app.post("/render_weekly_deck", response_model=DeckResponse, dependencies=[Depends(auth)])
    def render_weekly_deck(req: DeckRequest):
        t0 = time.time()
        kwargs = {k: v for k, v in (("renderer", renderer), ("imager", imager)) if v}
        try:
            inp = build_deck_input(req)
        except AdapterError as e:
            raise HTTPException(422, str(e))
        log.info("deck request report_date=%s milestones=%d in_report=%d", inp["report_date"],
                 len(inp["milestones"]), sum(m["in_weekly_report"] for m in inp["milestones"]))
        try:
            r = build_deck(inp, get_client(), settings, **kwargs)
        except BuildError as e:
            log.error("deck build failed: %s", e)
            raise HTTPException(502, str(e))
        except Exception as e:  # model/API failures: report type only, never content
            log.error("deck build error: %s", type(e).__name__)
            raise HTTPException(502, f"model call failed: {type(e).__name__}")
        checks = [CheckResult(name=n, passed=ok, detail=d) for n, ok, d in r.results]
        return DeckResponse(request_id=r.request_id, status=r.status, report_date=inp["report_date"], files=r.files,
                            slide_count=r.slide_count, checks=checks,
                            failed_checks=[c.name for c in checks if not c.passed], repair_rounds=r.repairs,
                            model=settings.model, usage=r.usage, latency_ms=int((time.time() - t0) * 1000))

    return app

