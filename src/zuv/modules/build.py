"""`zuv build` orchestrator. Reads the project's pyproject, optionally stages
a copy + downloads wheels (--deps), assembles the .py text via `pack.emit`,
and writes either a plain .py or a zip with launchers (--zip).
"""
import os
import shutil
import stat
import sys
import tomllib
from pathlib import Path

from ..constants import WHEELS_DIRNAME
from . import cache, pack, wheels, zip as zipmod
from .updater import UpdateConfig, describe as describe_update


def _resolve_entry(project_dir: Path, entry: str | None, zuv_cfg: dict) -> str | None:
    chosen = (
        entry
        or zuv_cfg.get("entry")
        or ("src/main.py" if (project_dir / "src" / "main.py").is_file() else "main.py")
    )
    return chosen if (project_dir / chosen).is_file() else None


def build_pyz(
    project_dir: Path,
    output: Path,
    entry: str | None,
    embed_deps: list[str] | None = None,
    no_compile: bool = False,
    make_zip: bool = False,
    update: UpdateConfig | None = None,
) -> int:
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        print(f"error: no pyproject.toml in {project_dir}", file=sys.stderr)
        return 2

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    zuv_cfg = data.get("tool", {}).get("zuv", {}) or {}
    requires_python = data.get("project", {}).get("requires-python")

    resolved_entry = _resolve_entry(project_dir, entry, zuv_cfg)
    if resolved_entry is None:
        print(f"error: entry script not found in {project_dir}", file=sys.stderr)
        return 2

    cache.clean_output_parent(output.parent)

    print(f"project: {project_dir}")
    print(f"entry:   {resolved_entry}")
    print(f"python:  {requires_python or '(unspecified)'}")
    if update is not None:
        print(f"updates: {describe_update(update)}")
    print("shipping .py sources; loader will compile to .pyc on first run")

    stage_root: Path | None = None
    has_wheels = False
    try:
        if embed_deps is not None:
            tar_root = cache.stage_copy(project_dir)
            stage_root = tar_root.parent
            print(f"embedding wheels for: {', '.join(embed_deps)}")
            count = wheels.download_wheels(
                project_dir, tar_root / WHEELS_DIRNAME, embed_deps
            )
            if count == 0:
                print("error: no wheels were downloaded; cannot build offline bundle",
                      file=sys.stderr)
                return 2
            has_wheels = True
            print(f"  staged {count} wheel files")
        else:
            tar_root = project_dir

        text, payload_size = pack.emit(
            tar_root, resolved_entry, requires_python,
            has_wheels, no_compile, update,
        )
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)

    if make_zip:
        py_name = f"{output.stem}.py"
        zipmod.write_zip(output, py_name, text)
        size = output.stat().st_size
        print(f"built {output} ({size / 1024:.1f} KB; payload {payload_size / 1024:.1f} KB raw)")
        print(f"  contains: {py_name}, run.bat, run.sh")
        print("  recipient: extract, then double-click run.bat (Windows) or `./run.sh` (Unix/macOS).")
        print("             Launchers auto-install uv if missing.")
        return 0

    output.write_text(text, encoding="utf-8")
    if os.name != "nt":
        output.chmod(output.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    size = output.stat().st_size
    print(f"built {output} ({size / 1024:.1f} KB; payload {payload_size / 1024:.1f} KB raw)")
    return 0
