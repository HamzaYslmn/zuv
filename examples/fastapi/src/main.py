"""FastAPI example for zuv.

All paths are anchored to this file's location, so the app behaves the same
regardless of where it's launched from.
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(BASE_DIR / "example.env", override=False)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from starlette.types import Scope
from starlette.exceptions import HTTPException

from api import router as api_router

PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "127.0.0.1")

FRONTEND_DIR = next(
    (p for p in (PROJECT_ROOT / "frontend", BASE_DIR / "frontend") if p.is_dir()),
    None,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    print(f"Starting service on http://{HOST}:{PORT}")
    print(f"  project  = {PROJECT_ROOT}")
    print(f"  frontend = {FRONTEND_DIR or '<none>'}")
    yield


app = FastAPI(title="zuv fastapi example", version="1.0.0", lifespan=lifespan)
app.include_router(api_router, prefix="/api")


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException:
            return await super().get_response("index.html", scope)


if FRONTEND_DIR is not None:
    app.mount("/", SPAStaticFiles(directory=FRONTEND_DIR, html=True), name="spa")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
