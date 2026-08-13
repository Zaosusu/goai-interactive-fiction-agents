from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import asdict
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.routes import router as api_router
from app.agents.visual_asset_generation.model_manager import ensure_rembg_models_async, rembg_model_status

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
OUTPUT_DIR = BASE_DIR / "output"


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Model downloads run in detached helper processes. The HTTP API becomes
    # available immediately and Pipeline keeps its deterministic chroma-key
    # fallback until the verified ONNX files are ready.
    if not os.getenv("PYTEST_CURRENT_TEST") and os.getenv("REMBG_AUTO_DOWNLOAD", "1").lower() not in {"0", "false", "no"}:
        application.state.rembg_model_jobs = ensure_rembg_models_async()
    yield


app = FastAPI(title="NPC Agent Demo", default_response_class=JSONResponse, lifespan=lifespan)
cors_origins = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOW_ORIGINS",
        "http://127.0.0.1:3000,http://localhost:3000,http://127.0.0.1:5173,http://localhost:5173",
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=OUTPUT_DIR), name="output")


@app.get("/api/models/rembg")
async def rembg_models() -> dict:
    configured = os.getenv("REMBG_AUTO_MODELS", "u2netp")
    models = tuple(dict.fromkeys(item.strip() for item in configured.split(",") if item.strip()))
    return {"models": [asdict(rembg_model_status(model)) for model in models]}


@app.get("/")
async def index() -> Response:
    return Response(status_code=204)


@app.get("/creator")
async def creator_workbench() -> FileResponse:
    return FileResponse(STATIC_DIR / "creator.html")


@app.get("/play")
async def player_experience() -> FileResponse:
    return FileResponse(STATIC_DIR / "play.html")


@app.get("/pipeline")
async def pipeline_workbench() -> FileResponse:
    return FileResponse(STATIC_DIR / "pipeline.html")
