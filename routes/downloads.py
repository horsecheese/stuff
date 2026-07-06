"""routes/downloads.py — Downloads API + WebSocket"""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core import DownloadManager

router = APIRouter(prefix="/api")
_dl_manager: DownloadManager = None  # injected by main.py


def set_manager(mgr: DownloadManager):
    global _dl_manager
    _dl_manager = mgr


@router.get("/downloads")
async def get_downloads():
    return _dl_manager.get_status()


@router.post("/downloads/{job_id}/cancel")
async def cancel_download(job_id: str):
    _dl_manager.cancel(job_id)
    return {"ok": True}


@router.post("/downloads/{job_id}/retry")
async def retry_download(job_id: str):
    await _dl_manager.retry(job_id)
    return {"ok": True}


# WebSocket live feed — note: no /api prefix for WS
ws_router = APIRouter()


@ws_router.websocket("/ws/downloads")
async def ws_downloads(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_json(_dl_manager.get_status())
            await asyncio.sleep(0.6)
    except WebSocketDisconnect:
        pass
