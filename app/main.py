"""海岛民宿能耗托管系统 —— FastAPI 入口。"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from .config import SIMULATOR_AUTOSTART, SIMULATOR_INTERVAL_S
from .database import init_db
from .routers import properties, telemetry, bookings, alerts, billing, reports


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if SIMULATOR_AUTOSTART:
        from .simulator import start_background
        start_background(SIMULATOR_INTERVAL_S)
    yield


app = FastAPI(
    title="海岛民宿能耗托管系统",
    description="空调/热水器/光伏/蓄水池接入 · 预冷看板 · 异常告警 · 能耗账单 · 月度对比",
    version="1.0.0",
    lifespan=lifespan,
)

for r in (properties, telemetry, bookings, alerts, billing, reports):
    app.include_router(r.router)


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok", "simulator_autostart": SIMULATOR_AUTOSTART}


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
