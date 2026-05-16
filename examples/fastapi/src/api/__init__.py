"""API routes for the zuv fastapi example."""
import os
import platform
import sys
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/info")
def info() -> dict:
    cwd = Path.cwd()
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cwd": str(cwd),
        "zuv_dir": os.environ.get("ZUV_DIR"),
        "zuv_cache": os.environ.get("ZUV_CACHE"),
        "frontend_override": (cwd / "frontend").is_dir(),
        "env_override": (cwd / ".env").is_file(),
    }
