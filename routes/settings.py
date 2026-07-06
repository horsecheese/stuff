"""routes/settings.py — Settings API"""
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")

# Injected by main.py
_dl_manager = None

def set_manager(mgr):
    global _dl_manager
    _dl_manager = mgr


@router.get("/settings")
async def get_settings():
    from config import load_settings
    return load_settings()


@router.post("/settings")
async def save_settings_ep(request: Request):
    from config import save_settings, load_settings
    data = await request.json()
    save_settings(data)

    # Hot-reload concurrency settings into the live download manager
    if _dl_manager is not None:
        cfg = load_settings()
        new_conc   = int(cfg.get("max_concurrent", 2))
        new_images = int(cfg.get("max_concurrent_images", 6))

        # Update image concurrency (used per-job, read at download time)
        _dl_manager.max_concurrent_images = new_images

        # Update job concurrency — spawn extra workers if limit increased
        if new_conc != _dl_manager.MAX_CONCURRENT:
            import asyncio
            old_conc = _dl_manager.MAX_CONCURRENT
            _dl_manager.MAX_CONCURRENT = new_conc
            if new_conc > old_conc and _dl_manager._started:
                loop = asyncio.get_event_loop()
                for _ in range(new_conc - old_conc):
                    _dl_manager._workers.append(
                        loop.create_task(_dl_manager._worker())
                    )

    return {"ok": True}
