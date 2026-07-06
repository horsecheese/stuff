"""routes/collections.py — Collections API"""
from fastapi import APIRouter, Request

from core import ComicDB

router = APIRouter(prefix="/api")


@router.get("/collections")
async def get_collections():
    return {"collections": ComicDB().get_collections()}


@router.post("/collections")
async def create_collection(request: Request):
    data = await request.json()
    db = ComicDB()
    db.upsert_collection(data["name"], data.get("description", ""), data.get("cover_id", ""))
    return {"ok": True}


@router.delete("/collections/{name}")
async def delete_collection(name: str):
    ComicDB().delete_collection(name)
    return {"ok": True}
