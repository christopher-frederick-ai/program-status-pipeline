"""FastAPI service exposing the pipeline's agents to n8n.

n8n calls:  POST http://agent-service:8000/agents/<agent_name>/run
            header  X-Service-Token: <shared secret>
            body    {"input": "<text>" | {...json...}}
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .agents import AgentRegistry
from .auth import require_token
from .clusters import build_clusters
from .config import Settings, get_settings
from .llm import LLM, AnthropicLLM, LLMError
from .logging_config import configure_logging, request_id_var
from .runner import run_agent, serialize_input
from .schemas import AgentInfo, ClustersRequest, ClustersResponse, RunRequest, RunResponse

log = logging.getLogger("c2.service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("Starting agent service (model=%s)", settings.anthropic_model)

    if not settings.service_shared_secret:
        # Fail fast: an unauthenticated agent service must never come up by accident.
        raise RuntimeError("SERVICE_SHARED_SECRET is not set; refusing to start.")
    if not settings.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY is not set; agent runs will return 503.")

    app.state.registry = AgentRegistry(settings.agents_dir)
    app.state.llm = None  # created lazily / injectable in tests
    yield
    log.info("Agent service stopped")


app = FastAPI(title="C2 Program Status Agent Service", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Middleware: request id + access log
# ---------------------------------------------------------------------------
@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    token = request_id_var.set(rid)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse({"detail": "Internal server error."}, status_code=500)
    ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-ID"] = rid
    if request.url.path != "/health":  # keep docker healthchecks out of the log
        log.info("%s %s -> %d (%dms)", request.method, request.url.path, response.status_code, ms)
    request_id_var.reset(token)
    return response


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def get_registry(request: Request) -> AgentRegistry:
    return request.app.state.registry


def get_llm(request: Request, settings: Settings = Depends(get_settings)) -> LLM:
    if request.app.state.llm is None:
        if not settings.anthropic_api_key:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "LLM is not configured (ANTHROPIC_API_KEY missing).",
            )
        request.app.state.llm = AnthropicLLM(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            timeout=settings.anthropic_timeout_seconds,
        )
    return request.app.state.llm


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health(registry: AgentRegistry = Depends(get_registry), settings: Settings = Depends(get_settings)):
    """Unauthenticated liveness probe for Docker. Reveals no program data."""
    infos = registry.info()
    return {
        "status": "ok",
        "llm_configured": bool(settings.anthropic_api_key),
        "agents_available": sum(1 for a in infos if a.available),
        "agents_total": len(infos),
    }


@app.get("/agents", response_model=list[AgentInfo], dependencies=[Depends(require_token)])
async def list_agents(registry: AgentRegistry = Depends(get_registry)):
    return registry.info()


@app.post("/admin/reload", dependencies=[Depends(require_token)])
async def reload_agents(registry: AgentRegistry = Depends(get_registry)):
    """Re-read all instruction files without restarting the container."""
    registry.load()
    return {"agents": registry.info()}


@app.post("/clusters", response_model=ClustersResponse, dependencies=[Depends(require_token)])
async def clusters(body: ClustersRequest, settings: Settings = Depends(get_settings)):
    """Join tasks, risks and notes to each milestone. No LLM call; no program data is logged."""
    try:
        result = build_clusters(
            body.milestones,
            body.tasks,
            body.risks,
            body.engineering_notes,
            body.as_of_date.isoformat(),
            include_empty=body.include_empty,
            max_chars=settings.max_input_chars,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log.info(
        "Built %d cluster(s), skipped %d, %d warning(s)",
        len(result.clusters),
        len(result.skipped),
        len(result.warnings),
    )
    return ClustersResponse(
        request_id=request_id_var.get(),
        as_of_date=body.as_of_date.isoformat(),
        clusters=result.clusters,
        skipped=result.skipped,
        warnings=result.warnings,
    )


@app.post(
    "/agents/{agent_name}/run",
    response_model=RunResponse,
    dependencies=[Depends(require_token)],
)
async def run(
    agent_name: str,
    body: RunRequest,
    request: Request,
    registry: AgentRegistry = Depends(get_registry),
    settings: Settings = Depends(get_settings),
    llm: LLM = Depends(get_llm),
):
    agent = registry.get(agent_name)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent '{agent_name}'.")
    if not agent.available:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, agent.error or "Agent unavailable.")
    if body.input is None or body.input == "" or body.input == {} or body.input == []:
        raise HTTPException(422, "`input` must not be empty.")
    if len(serialize_input(body.input)) > settings.max_input_chars:
        raise HTTPException(
            413,
            f"`input` exceeds {settings.max_input_chars} characters.",
        )

    try:
        result = await run_agent(
            agent,
            body.input,
            llm,
            default_max_tokens=settings.default_max_tokens,
        )
    except LLMError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc

    return RunResponse(
        agent=agent_name,
        request_id=request_id_var.get(),
        output=result.output,
        model=result.model,
        usage=result.usage,
        attempts=result.attempts,
        latency_ms=result.latency_ms,
    )
