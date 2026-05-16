"""Build a runnable .py from a project (pyproject.toml + src/main.py).

The output is a PEP 723 script that uv executes. It does NOT bundle deps —
instead it embeds a list of dependencies; the runtime loader materializes a
`.venv` next to the script on first run and `uv pip install`s them there.

Build steps:
  1. Read deps from <project>/pyproject.toml.
  2. tar.gz the <project>/src/ tree.
  3. Emit <output> = shebang + latin-1 coding decl + PEP 723 header +
     ENV literal + raw bytes payload literal + loader source.
"""
import base64
import compileall
import hashlib
import io
import platform
import shutil
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path
from stat import S_IXGRP, S_IXOTH, S_IXUSR

from .__version__ import __version__
from .constants import PAYLOAD_VAR, ZUV_SHEBANG

PEP723_HEADER = """\
# /// script
# requires-python = ">={py}"
# dependencies = []
# ///
"""


def _build_tag() -> dict[str, str]:
    return {
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def _read_deps(pyproject: Path) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return list(data.get("project", {}).get("dependencies", []))


def _pre_compile(src_dir: Path) -> Path:
    """Copy src/ to a temp dir and pre-compile .pyc files into it."""
    tmp = Path(tempfile.mkdtemp(prefix="zuv-src-"))
    shutil.copytree(src_dir, tmp, dirs_exist_ok=True)
    compileall.compile_dir(tmp, quiet=1, workers=0)
    return tmp


def _make_tarball(root: Path) -> tuple[bytes, str]:
    buf = io.BytesIO()
    hasher = hashlib.sha256()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=9) as tf:
        for path in sorted(root.rglob("*"), key=str):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            hasher.update(rel.encode("utf-8"))
            hasher.update(path.read_bytes())
            tf.add(path, arcname=rel)
    return buf.getvalue(), hasher.hexdigest()


def _loader_source() -> str:
    return Path(__file__).with_name("_loader_template.py").read_text(encoding="utf-8")


def build_pyz(project_dir: Path, output: Path, entry: str | None) -> int:
    pyproject = project_dir / "pyproject.toml"
    src_dir = project_dir / "src"

    if not pyproject.exists():
        print(f"error: no pyproject.toml in {project_dir}", file=sys.stderr)
        return 2
    if not (src_dir / "main.py").exists():
        print(f"error: no src/main.py in {project_dir}", file=sys.stderr)
        return 2

    if output.parent.name == "dist" and output.parent.exists():
        print(f"cleaning {output.parent}...")
        shutil.rmtree(output.parent)
    output.parent.mkdir(parents=True, exist_ok=True)

    deps = _read_deps(pyproject)
    print(f"deps: {len(deps)} ({', '.join(deps) if deps else 'none'})")

    print("pre-compiling source...")
    staging = _pre_compile(src_dir)
    try:
        print("packing source tar.gz...")
        payload, build_id = _make_tarball(staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    env = {
        "zuv_version": __version__,
        "entry": entry or "main:main",
        "build_id": build_id,
        "build_tag": _build_tag(),
        "dependencies": deps,
    }
    py_req = f"{sys.version_info.major}.{sys.version_info.minor}"
    b85 = base64.b85encode(payload).decode("ascii")

    parts = [
        ZUV_SHEBANG,
        PEP723_HEADER.format(py=py_req),
        f"_ZUV_ENV = {env!r}\n",
        f"{PAYLOAD_VAR} = (\n",
    ]
    for i in range(0, len(b85), 80):
        parts.append(f'    b"{b85[i:i+80]}"\n')
    parts.append(")\n")
    parts.append(_loader_source())

    output.write_text("".join(parts), encoding="utf-8")
    output.chmod(output.stat().st_mode | S_IXUSR | S_IXGRP | S_IXOTH)

    print(f"built {output} ({output.stat().st_size / 1024:.1f} KB)")
    return 0
