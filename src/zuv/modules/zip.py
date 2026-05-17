"""Wrap a built .py bundle in a .zip with run.bat + run.sh launchers that
auto-install uv on first run. Used by `zuv build --zip`.

The inner .py is byte-for-byte the same single-file bundle a non-zip build
would produce -- still runnable on its own with `uv run <file>.py`. The
launchers only exist for recipients who don't have uv yet.
"""
import zipfile
from pathlib import Path

_RUN_BAT = r"""@echo off
setlocal
set "DIR=%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo uv not found. Installing from https://astral.sh/uv ...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if errorlevel 1 (
        echo Failed to install uv. See https://astral.sh/uv
        pause
        exit /b 1
    )
    set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
uv run "%DIR%__APP__" %*
if errorlevel 1 pause
"""

_RUN_SH = """#!/usr/bin/env sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Installing from https://astral.sh/uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi
exec uv run "$DIR/__APP__" "$@"
"""


def write_zip(output: Path, py_name: str, py_text: str) -> None:
    """Write `py_text` (the bundle .py) plus tailored run.bat + run.sh
    launchers into a zip at `output`. run.sh gets 0755 perms via the zip
    external-attrs so `unzip` extracts it executable on Unix."""
    run_bat = _RUN_BAT.replace("__APP__", py_name)
    run_sh = _RUN_SH.replace("__APP__", py_name)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(py_name, py_text)
        zf.writestr("run.bat", run_bat)
        info = zipfile.ZipInfo("run.sh")
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (0o755 << 16) | 0x8000  # regular file, rwxr-xr-x
        zf.writestr(info, run_sh)
