"""
FastAPI application entrypoint.

Wires routers, CORS, static frontend, DB init on startup, and health check.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import database
from routers import (
    auth_router,
    session_router,
    upload_router,
    chat_router,
    extraction_router,
    sections_router,
    prompts_router,
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    await database.init_db()
    yield


app = FastAPI(title="Patent Specification Drafting Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # same-origin frontend; permissive for API clients
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers
app.include_router(auth_router.router)
app.include_router(session_router.router)
app.include_router(upload_router.router)
app.include_router(chat_router.router)
app.include_router(extraction_router.router)
app.include_router(sections_router.router)
app.include_router(prompts_router.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "model": config.ANTHROPIC_MODEL, "api_key_set": bool(config.ANTHROPIC_API_KEY)}


# --- Frontend ---------------------------------------------------------------
# Serve the SPA at "/" and static assets under /static.
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse({"detail": "Frontend not found"}, status_code=404)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=config.PORT, reload=False)
