import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from app.gateway.config import get_gateway_config
from app.gateway.routers import (
    agents,
    artifacts,
    channels,
    mcp,
    memory,
    models,
    skills,
    suggestions,
    threads,
    uploads,
)
from deerflow.config.app_config import get_app_config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _DropHealthAccessFilter(logging.Filter):
    """Drop uvicorn access lines for health-check polling.

    The supervisor log is the canonical log; with monitors hitting
    /livez and /readyz every few seconds it would otherwise be ~95%
    health-check spam.
    """

    _PATHS = ("/livez", "/readyz", "/health", "/metrics")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(p in msg for p in self._PATHS)


logging.getLogger("uvicorn.access").addFilter(_DropHealthAccessFilter())

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""

    # Load config and check necessary environment variables at startup
    try:
        get_app_config()
        logger.info("Configuration loaded successfully")
    except Exception as e:
        error_msg = f"Failed to load configuration during gateway startup: {e}"
        logger.exception(error_msg)
        raise RuntimeError(error_msg) from e
    config = get_gateway_config()
    logger.info(f"Starting API Gateway on {config.host}:{config.port}")

    # NOTE: MCP tools initialization is NOT done here because:
    # 1. Gateway doesn't use MCP tools - they are used by Agents in the LangGraph Server
    # 2. Gateway and LangGraph Server are separate processes with independent caches
    # MCP tools are lazily initialized in LangGraph Server when first needed

    # Start IM channel service if any channels are configured
    try:
        from app.channels.service import start_channel_service

        channel_service = await start_channel_service()
        logger.info("Channel service started: %s", channel_service.get_status())
    except Exception:
        logger.exception("No IM channels configured or channel service failed to start")

    # Start supervised cron jobs (dossier briefings, etc.)
    try:
        from app.gateway.cron_supervisor import start_crons

        start_crons()
    except Exception:
        logger.exception("Failed to start cron jobs")

    yield

    # Stop cron jobs
    try:
        from app.gateway.cron_supervisor import stop_crons

        stop_crons()
    except Exception:
        logger.exception("Failed to stop cron jobs")

    # Stop channel service on shutdown
    try:
        from app.channels.service import stop_channel_service

        await stop_channel_service()
    except Exception:
        logger.exception("Failed to stop channel service")
    logger.info("Shutting down API Gateway")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """

    app = FastAPI(
        title="DeerFlow API Gateway",
        description="""
## DeerFlow API Gateway

API Gateway for DeerFlow - A LangGraph-based AI agent backend with sandbox execution capabilities.

### Features

- **Models Management**: Query and retrieve available AI models
- **MCP Configuration**: Manage Model Context Protocol (MCP) server configurations
- **Memory Management**: Access and manage global memory data for personalized conversations
- **Skills Management**: Query and manage skills and their enabled status
- **Artifacts**: Access thread artifacts and generated files
- **Health Monitoring**: System health check endpoints

### Architecture

LangGraph requests are handled by nginx reverse proxy.
This gateway provides custom endpoints for models, MCP configuration, skills, and artifacts.
        """,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        openapi_tags=[
            {
                "name": "models",
                "description": "Operations for querying available AI models and their configurations",
            },
            {
                "name": "mcp",
                "description": "Manage Model Context Protocol (MCP) server configurations",
            },
            {
                "name": "memory",
                "description": "Access and manage global memory data for personalized conversations",
            },
            {
                "name": "skills",
                "description": "Manage skills and their configurations",
            },
            {
                "name": "artifacts",
                "description": "Access and download thread artifacts and generated files",
            },
            {
                "name": "uploads",
                "description": "Upload and manage user files for threads",
            },
            {
                "name": "threads",
                "description": "Manage DeerFlow thread-local filesystem data",
            },
            {
                "name": "agents",
                "description": "Create and manage custom agents with per-agent config and prompts",
            },
            {
                "name": "suggestions",
                "description": "Generate follow-up question suggestions for conversations",
            },
            {
                "name": "channels",
                "description": "Manage IM channel integrations (Feishu, Slack, Telegram)",
            },
            {
                "name": "health",
                "description": "Health check and system status endpoints",
            },
        ],
    )

    # CORS is handled by nginx - no need for FastAPI middleware

    # Include routers
    # Models API is mounted at /api/models
    app.include_router(models.router)

    # MCP API is mounted at /api/mcp
    app.include_router(mcp.router)

    # Memory API is mounted at /api/memory
    app.include_router(memory.router)

    # Skills API is mounted at /api/skills
    app.include_router(skills.router)

    # Artifacts API is mounted at /api/threads/{thread_id}/artifacts
    app.include_router(artifacts.router)

    # Uploads API is mounted at /api/threads/{thread_id}/uploads
    app.include_router(uploads.router)

    # Thread cleanup API is mounted at /api/threads/{thread_id}
    app.include_router(threads.router)

    # Agents API is mounted at /api/agents
    app.include_router(agents.router)

    # Suggestions API is mounted at /api/threads/{thread_id}/suggestions
    app.include_router(suggestions.router)

    # Channels API is mounted at /api/channels
    app.include_router(channels.router)

    async def _collect_health() -> tuple[str, dict[str, str]]:
        checks: dict[str, str] = {}
        overall = "healthy"

        try:
            get_app_config()
            checks["config"] = "ok"
        except Exception as e:
            checks["config"] = f"error: {e}"
            overall = "unhealthy"

        try:
            from app.channels.service import get_channel_service
            svc = get_channel_service()
            checks["channels"] = svc.get_status() if svc is not None else "not configured"
        except Exception:
            checks["channels"] = "not configured"

        try:
            import httpx
            cfg = get_app_config()
            channels_cfg = getattr(cfg, "channels", None) or {}
            langgraph_url = channels_cfg.get("langgraph_url", "http://localhost:2024") if isinstance(channels_cfg, dict) else "http://localhost:2024"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{langgraph_url}/ok")
                checks["langgraph"] = "ok" if resp.status_code == 200 else f"status {resp.status_code}"
                if resp.status_code != 200 and overall == "healthy":
                    overall = "degraded"
        except Exception:
            checks["langgraph"] = "unreachable"
            if overall == "healthy":
                overall = "degraded"

        return overall, checks

    @app.get("/livez", tags=["health"])
    async def livez() -> dict:
        # Liveness: process is up enough to answer. Always 200 unless the
        # event loop itself is wedged (in which case this won't return).
        return {"status": "alive"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> Response:
        # Readiness: returns 503 if any required dependency is broken so
        # an LB or monitor can route around us.
        overall, checks = await _collect_health()
        body = {"status": overall, "checks": checks}
        status_code = 200 if overall == "healthy" else 503
        return Response(content=json.dumps(body), media_type="application/json", status_code=status_code)

    @app.get("/health", tags=["health"])
    async def health_check() -> dict:
        # Rich JSON for humans/dashboards. Always 200 — use /readyz for LBs.
        overall, checks = await _collect_health()
        return {"status": overall, "service": "deer-flow-gateway", "checks": checks}

    @app.get("/api/admin/active-runs", tags=["health"])
    def active_runs() -> dict:
        # Plain `def` (not async) on purpose: the body does sync blocking I/O
        # (httpx to LangGraph + a thread sweep). FastAPI runs sync handlers in a
        # threadpool, so this can't starve the async event loop / health probes
        # (the 2026-06-16 kill-loop cause). Do NOT make this async.
        # Drilldown for the Grafana dashboard's "busy thread age" panel.
        from app.gateway.metrics import _collect_thread_status

        cfg = get_app_config()
        channels_cfg = getattr(cfg, "channels", None) or {}
        langgraph_url = (
            channels_cfg.get("langgraph_url", "http://localhost:2024")
            if isinstance(channels_cfg, dict)
            else "http://localhost:2024"
        )
        return _collect_thread_status(langgraph_url)

    @app.get("/metrics", tags=["health"])
    def metrics() -> Response:
        # Plain `def` (not async) on purpose: refresh_all() does sync blocking
        # I/O (httpx to LangGraph + parsing dispatch_audit.jsonl + JSON reads).
        # FastAPI threadpools sync handlers, keeping the event loop free for
        # /livez//readyz. Do NOT make this async (2026-06-16 kill-loop cause).
        # Prometheus scrape endpoint pulled by the local Grafana stack.
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        from app.gateway.metrics import refresh_all, registry

        cfg = get_app_config()
        channels_cfg = getattr(cfg, "channels", None) or {}
        langgraph_url = (
            channels_cfg.get("langgraph_url", "http://localhost:2024")
            if isinstance(channels_cfg, dict)
            else "http://localhost:2024"
        )
        refresh_all(langgraph_url=langgraph_url)
        return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return app


# Create app instance for uvicorn
app = create_app()
