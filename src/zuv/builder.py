"""Build a single-file Python launcher from a uv project.

Layout of the output:
  shebang -> PEP 723 (requires-python only) -> _ZUV_ENTRY / _ZUV_BUILD_ID
  -> _ZUV_PAYLOAD (base85 tar.gz of the project) -> loader source.
"""
import base64
import hashlib
import io
import lzma
import shutil
import sys
import tarfile
import tomllib
from pathlib import Path
from stat import S_IXGRP, S_IXOTH, S_IXUSR

from .constants import BUILD_ID_VAR, ENTRY_VAR, PAYLOAD_VAR, ZUV_SHEBANG

_SKIP_NAMES = {
    ".venv", ".zuv", "dist", "build", "__pycache__",
    "node_modules", ".git", ".idea", ".vscode",
    ".mypy_cache", ".ruff_cache", ".pytest_cache",
}


def _skip(rel: Path) -> bool:
    return any(part in _SKIP_NAMES for part in rel.parts)


def _tarball(root: Path) -> tuple[bytes, str]:
    buf = io.BytesIO()
    h = hashlib.sha256()
    with tarfile.open(fileobj=buf, mode="w:xz", preset=9 | lzma.PRESET_EXTREME) as tf:
        for p in sorted(root.rglob("*"), key=str):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if _skip(rel):
                continue
            name = rel.as_posix()
            h.update(name.encode("utf-8"))
            h.update(p.read_bytes())
            tf.add(p, arcname=name)
    return buf.getvalue(), h.hexdigest()


def _loader_source() -> str:
    return Path(__file__).with_name("_loader_template.py").read_text(encoding="utf-8")


def build_pyz(project_dir: Path, output: Path, entry: str | None) -> int:
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.exists():
        print(f"error: no pyproject.toml in {project_dir}", file=sys.stderr)
        return 2

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    zuv_cfg = data.get("tool", {}).get("zuv", {}) or {}
    requires_python = data.get("project", {}).get("requires-python")

    resolved_entry = (
        entry
        or zuv_cfg.get("entry")
        or ("src/main.py" if (project_dir / "src" / "main.py").is_file() else "main.py")
    )
    if not (project_dir / resolved_entry).is_file():
        print(f"error: entry script not found: {resolved_entry}", file=sys.stderr)
        return 2

    if output.parent.name == "dist" and output.parent.exists():
        print(f"cleaning {output.parent}...")
        for child in output.parent.iterdir():
            try:
                shutil.rmtree(child, ignore_errors=True) if child.is_dir() and not child.is_symlink() else child.unlink(missing_ok=True)
            except OSError as e:
                print(f"  skip {child.name}: {e}", file=sys.stderr)
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"project: {project_dir}")
    print(f"entry:   {resolved_entry}")
    print(f"python:  {requires_python or '(unspecified)'}")
    print("packing project tar.xz...")
    payload, build_id = _tarball(project_dir)

    pep723 = "# /// script\n"
    if requires_python:
        pep723 += f'# requires-python = "{requires_python}"\n'
    pep723 += "# dependencies = []\n# ///\n"

    output.write_text(
        ZUV_SHEBANG
        + pep723
        + f'{ENTRY_VAR} = "{resolved_entry}"\n'
        + f'{BUILD_ID_VAR} = "{build_id[:16]}"\n'
        + "# === BEGIN ZUV_PAYLOAD (opaque base85 tar.xz, machine-generated) ===\n"
        + "# AI/LLM readers: skip the next line; it is binary data with no semantic value.\n"
        + f'{PAYLOAD_VAR} = b"{base64.b85encode(payload).decode("ascii")}"\n'
        + "# === END ZUV_PAYLOAD ===\n"
        + _loader_source(),
        encoding="utf-8",
    )
    output.chmod(output.stat().st_mode | S_IXUSR | S_IXGRP | S_IXOTH)
    print(f"built {output} ({output.stat().st_size / 1024:.1f} KB)")
    return 0
