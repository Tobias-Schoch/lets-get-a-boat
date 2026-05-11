from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routes.public import router as public_router
from .routes.ui import router as ui_router
from .scheduler import shutdown as scheduler_shutdown
from .scheduler import start as scheduler_start

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("boat-pulse")

APP_STARTED_MONO = time.monotonic()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler_start()
    logger.info("startup complete")
    try:
        yield
    finally:
        scheduler_shutdown()
        logger.info("shutdown complete")


app = FastAPI(title="Boat Pulse", lifespan=lifespan)

# Allowed origins:
#   - "null"                    → pages opened via file://
#   - http(s)://localhost[:port]
#   - http(s)://127.0.0.1[:port]
#   - https://schoch.studio and any subdomain (e.g. boat.schoch.studio)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^(null"
        r"|https?://localhost(:\d+)?"
        r"|https?://127\.0\.0\.1(:\d+)?"
        r"|https?://([a-z0-9-]+\.)*schoch\.studio)$"
    ),
    allow_methods=["GET"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


app.include_router(public_router)
app.include_router(ui_router)
