"""Filesystem hygiene: stage a project to a tempdir, wipe output dirs,
remove .zuv/ runtime caches.
"""
import shutil
import sys
import tempfile
from pathlib import Path

from ..constants import SKIP_NAMES, VOLUME_MARKER


def skip(rel: Path) -> bool:
    """True if any component of `rel` is in SKIP_NAMES."""
    return any(part in SKIP_NAMES for part in rel.parts)


def stage_copy(project_dir: Path) -> Path:
    """Copy the project to a tempdir, skipping SKIP_NAMES dirs. Returns the
    new project root. Caller is responsible for `shutil.rmtree`ing the parent.
    """
    stage_root = Path(tempfile.mkdtemp(prefix="zuv-stage-"))
    proj = stage_root / "p"

    def _ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in SKIP_NAMES]

    shutil.copytree(project_dir, proj, ignore=_ignore)
    return proj


def clean_output_parent(parent: Path) -> None:
    """Delete `parent` entirely and recreate it empty, so every build starts
    from a clean slate (called by `zuv build` before writing the output)."""
    if parent.exists():
        print(f"cleaning {parent}...")
        try:
            shutil.rmtree(parent)
        except OSError as e:
            print(f"  warn: could not remove {parent}: {e}", file=sys.stderr)
    parent.mkdir(parents=True, exist_ok=True)


def clean_caches(target: Path, include_data: bool = False) -> int:
    """Remove .zuv/ runtime caches under `target` (a directory or a built .py).
    Public entrypoint for `zuv clean`. Persistent volume dirs (marked with
    `.zuv-volume`) are preserved unless `include_data=True`."""
    if target.is_file():
        target = target.parent
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 2
    removed = 0
    kept = 0
    for cache in target.rglob(".zuv"):
        if not cache.is_dir():
            continue
        if include_data:
            try:
                shutil.rmtree(cache)
                print(f"removed {cache}")
                removed += 1
            except OSError as e:
                print(f"  skip {cache}: {e}", file=sys.stderr)
            continue
        # Default: preserve any child that is (or contains) a volume.
        for child in cache.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            if (child / VOLUME_MARKER).exists():
                kept += 1
                continue
            try:
                shutil.rmtree(child)
                print(f"removed {child}")
                removed += 1
            except OSError as e:
                print(f"  skip {child}: {e}", file=sys.stderr)
        try:
            if not any(cache.iterdir()):
                cache.rmdir()
        except OSError:
            pass
    if removed == 0 and kept == 0:
        print(f"no .zuv/ caches found under {target}")
    elif kept:
        print(f"kept {kept} persistent volume{'s' if kept != 1 else ''} (use `zuv clean --data` to wipe)")
    return 0
