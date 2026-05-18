"""API routes for the zuv fastapi example."""
import json
import os
import platform
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"

router = APIRouter()

SETTINGS_FILE = DATA_DIR / "settings.json"


class Settings(BaseModel):
    switch: bool


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/info")
def info() -> dict:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "project_root": str(PROJECT_ROOT),
        "base_dir": str(BASE_DIR),
        "data_dir": str(DATA_DIR),
        "zuv_dir": os.environ.get("ZUV_DIR"),
        "zuv_cache": os.environ.get("ZUV_CACHE"),
    }


@router.get("/settings")
def get_settings() -> Settings:
    if not SETTINGS_FILE.exists():
        raise HTTPException(404, f"{SETTINGS_FILE} not found")
    return Settings.model_validate_json(SETTINGS_FILE.read_text("utf-8"))


@router.put("/settings")
def put_settings(payload: Settings) -> Settings:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(payload.model_dump(), indent=2), "utf-8")
    return payload
