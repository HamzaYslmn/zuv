"""Filesystem hygiene: stage a project to a tempdir, wipe output dirs,
remove .zuv/ runtime caches.
"""
import shutil
import sys
import tempfile
from pathlib import Path

from ..constants import SKIP_NAMES


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
    """Empty `parent` of its children (used by `zuv build --clean`)."""
    if not parent.exists():
        return
    print(f"cleaning {parent}...")
    for child in parent.iterdir():
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError as e:
            print(f"  skip {child.name}: {e}", file=sys.stderr)


def clean_caches(target: Path) -> int:
    """Remove .zuv/ runtime caches under `target` (a directory or a built .py).
    Public entrypoint for `zuv clean`."""
    if target.is_file():
        target = target.parent
    if not target.is_dir():
        print(f"error: not a directory: {target}", file=sys.stderr)
        return 2
    removed = 0
    for cache in target.rglob(".zuv"):
        if not cache.is_dir():
            continue
        try:
            shutil.rmtree(cache)
            print(f"removed {cache}")
            removed += 1
        except OSError as e:
            print(f"  skip {cache}: {e}", file=sys.stderr)
    if removed == 0:
        print(f"no .zuv/ caches found under {target}")
    return 0
