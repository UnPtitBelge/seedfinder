"""FastAPI entrypoint — router wiring only. Run: uv run uvicorn trainscanner.main:app --reload"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from .config import WEB_DIST, settings
from .models import PublicConfig
from .routes import account, checkout, journeys, network, stations, stays, stripe_webhook

app = FastAPI(title="Seedfinder API", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"]
)


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "offline_demo": settings.offline_demo,
        "stripe_configured": bool(settings.stripe_secret_key),
    }


@app.get("/api/config", tags=["meta"])
def public_config() -> PublicConfig:
    """Browser-safe settings, so one Docker image works whatever keys the host injects."""
    return PublicConfig(stripe_publishable_key=settings.vite_stripe_publishable_key or None)


for r in (account, stations, journeys, stays, network, checkout, stripe_webhook):
    app.include_router(r.router)


# Production: the same process serves the built frontend (single origin, no CORS, one service to deploy).
class _SPAFiles(StaticFiles):
    """Starlette StaticFiles (the framework blocks path traversal) with an index.html fallback, so client-side
    routes like /map load the app. Unknown /api/* paths stay 404."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code != 404 or path.startswith("api"):
                raise
            return await super().get_response("index.html", scope)


if WEB_DIST.is_dir():
    app.mount("/", _SPAFiles(directory=WEB_DIST, html=True), name="spa")
