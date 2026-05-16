"""FastAPI example for zuv.

zuv runs this with cwd = the folder containing the .zuv file, so relative
paths "just work":
  - `.env`        → optional config next to the .zuv
  - `frontend/`   → optional UI folder next to the .zuv; falls back to bundled
"""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv

# cwd is the .zuv directory (set by the zuv loader); falls back to "." otherwise.
load_dotenv(".env")                          # sibling config (if present)
load_dotenv(Path(__file__).parent / "example.env", override=False)  # bundled default

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
from starlette.types import Scope
from starlette.exceptions import HTTPException

from api import router as api_router

PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "127.0.0.1")


def _frontend_dir() -> Path | None:
    sibling = Path("frontend")                     # next to the .zuv (cwd)
    bundled = Path(__file__).parent / "frontend"   # inside the bundle
    if sibling.is_dir():
        return sibling.resolve()
    if bundled.is_dir():
        return bundled
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    print(f"Starting service on http://{HOST}:{PORT}")
    print(f"  cwd      = {Path.cwd()}")
    print(f"  frontend = {_frontend_dir() or '<none>'}")
    yield


app = FastAPI(title="zuv fastapi example", version="1.0.0", lifespan=lifespan)
app.include_router(api_router, prefix="/api")


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException:
            return await super().get_response("index.html", scope)


fdir = _frontend_dir()
if fdir is not None:
    app.mount("/", SPAStaticFiles(directory=fdir, html=True), name="spa")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
