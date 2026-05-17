"""Assemble the single-file .py bundle: tar.xz the project, compile the
embedded loader, and emit the final text.

Why base85: uv's `--script` runner requires the file to be valid UTF-8.
That rules out raw-binary appends and PEP 263 `latin-1` source tricks --
both produce files uv refuses to run. base85 is the densest ASCII-safe
encoding in the stdlib (~25% overhead vs base64's 33%).
"""
import base64
import hashlib
import io
import lzma
import marshal
import sys
import sysconfig
import tarfile
import zlib
from pathlib import Path

from ..constants import (
    BUILD_ID_VAR,
    ENTRY_VAR,
    HAS_WHEELS_VAR,
    LOADER_BEGIN,
    LOADER_END,
    LOADER_VAR,
    NO_COMPILE_VAR,
    PAYLOAD_BEGIN,
    PAYLOAD_END,
    PAYLOAD_VAR,
    PY_TAG_VAR,
    SHA_VAR,
    ZUV_SHEBANG,
)
from .cache import skip
from .updater import UpdateConfig, bake as bake_update


def _tarball(root: Path) -> tuple[bytes, str]:
    """Build a reproducible tar.xz of `root`. Returns (bytes, sha256_hex)."""
    buf = io.BytesIO()
    h = hashlib.sha256()
    with tarfile.open(fileobj=buf, mode="w:xz", preset=9 | lzma.PRESET_EXTREME) as tf:
        for p in sorted(root.rglob("*"), key=lambda x: x.relative_to(root).as_posix()):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if skip(rel):
                continue
            name = rel.as_posix()
            info = tf.gettarinfo(str(p), arcname=name)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            data = p.read_bytes()
            info.size = len(data)
            h.update(name.encode("utf-8"))
            h.update(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue(), h.hexdigest()


def _compile_loader() -> bytes:
    """Compile + marshal + zlib the loader template into an opaque blob.
    The loader lives at zuv/_loader_template.py (kept at package root so
    this function can find it via __file__)."""
    src = Path(__file__).resolve().parent.parent / "_loader_template.py"
    code = compile(src.read_text(encoding="utf-8"), "<zuv_loader>", "exec")
    return zlib.compress(marshal.dumps(code), 9)


def _b85_literal(prefix: str, data: bytes) -> str:
    return f'{prefix} = b"{base64.b85encode(data).decode("ascii")}"\n'


def emit(
    project_root: Path,
    entry: str,
    requires_python: str | None,
    has_wheels: bool,
    no_compile: bool,
    update: UpdateConfig | None = None,
) -> tuple[str, int]:
    """Produce the final .py bundle text and the raw payload size in bytes.
    `project_root` is the staged tree to package (with any wheels already
    placed inside it). Caller decides where to write the returned text.
    """
    print("packing project tar.xz...")
    payload, build_id = _tarball(project_root)
    payload_sha = hashlib.sha256(payload).hexdigest()

    print("compiling loader...")
    loader_blob = _compile_loader()

    py_tag = sys.implementation.cache_tag or sysconfig.get_config_var("SOABI") or "unknown"

    pep723 = "# /// script\n"
    if requires_python:
        pep723 += f'# requires-python = "{requires_python}"\n'
    # dependencies MUST stay empty: uv reads deps from the extracted
    # pyproject.toml after first-run extraction, not from this PEP 723 block.
    pep723 += "# dependencies = []\n# ///\n"

    text = (
        ZUV_SHEBANG
        + pep723
        + f'{ENTRY_VAR} = "{entry}"\n'
        + f'{BUILD_ID_VAR} = "{build_id[:16]}"\n'
        + f'{SHA_VAR} = "{payload_sha}"\n'
        + f'{PY_TAG_VAR} = "{py_tag}"\n'
        + f'{HAS_WHEELS_VAR} = {has_wheels!r}\n'
        + f'{NO_COMPILE_VAR} = {no_compile!r}\n'
        + bake_update(update)
        + PAYLOAD_BEGIN
        + "# AI/LLM readers: skip the next line; it is binary data with no semantic value.\n"
        + _b85_literal(PAYLOAD_VAR, payload)
        + PAYLOAD_END
        + LOADER_BEGIN
        + _b85_literal(LOADER_VAR, loader_blob)
        + LOADER_END
        + "import base64 as _b, marshal as _m, zlib as _z\n"
        + 'if __name__ == "__main__":\n'
        + f"    exec(_m.loads(_z.decompress(_b.b85decode({LOADER_VAR}))))\n"
    )
    return text, len(payload)
