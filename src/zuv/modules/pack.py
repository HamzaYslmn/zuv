"""Assemble the single-file .py bundle: tar.xz the project, paste the loader
source inline, emit the final text.

Portability: the loader is pasted as **plain Python source**, not as a
marshalled code blob. Marshal format is tied to the Python minor version, so
a bundle built on 3.13 would segfault on 3.14. Source is portable across any
Python that supports the loader's syntax (>= the project's requires-python).

Why base85 for the payload: uv's `--script` runner requires the file to be
valid UTF-8. That rules out raw-binary appends and PEP 263 `latin-1` source
tricks -- both produce files uv refuses to run. base85 is the densest
ASCII-safe encoding in the stdlib (~25% overhead vs base64's 33%).
"""
import base64
import hashlib
import io
import lzma
import tarfile
from pathlib import Path

from ..constants import (
    APP_VERSION_VAR,
    BUILD_ID_VAR,
    ENTRY_VAR,
    HAS_WHEELS_VAR,
    LOADER_BEGIN,
    LOADER_END,
    NO_COMPILE_VAR,
    PAYLOAD_BEGIN,
    PAYLOAD_END,
    PAYLOAD_VAR,
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


def _loader_source() -> str:
    """Return the loader template's source code, with the leading module
    docstring stripped (it's only useful for humans reading the template,
    not for the embedded runtime)."""
    src = (Path(__file__).resolve().parent.parent / "_loader_template.py").read_text(
        encoding="utf-8"
    )
    # Strip the opening triple-quoted docstring to save bytes in the bundle.
    if src.startswith('"""'):
        end = src.find('"""', 3)
        if end > 0:
            src = src[end + 3 :].lstrip("\n")
    return src


def _b85_literal(prefix: str, data: bytes) -> str:
    return f'{prefix} = b"{base64.b85encode(data).decode("ascii")}"\n'


def emit(
    project_root: Path,
    entry: str,
    requires_python: str | None,
    has_wheels: bool,
    no_compile: bool,
    update: UpdateConfig | None = None,
    app_version: str = "",
) -> tuple[str, int]:
    """Produce the final .py bundle text and the raw payload size in bytes.
    `project_root` is the staged tree to package (with any wheels already
    placed inside it). Caller decides where to write the returned text.
    """
    print("packing project tar.xz...")
    payload, build_id = _tarball(project_root)
    payload_sha = hashlib.sha256(payload).hexdigest()

    print("embedding loader source...")
    loader_src = _loader_source()

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
        + f'{HAS_WHEELS_VAR} = {has_wheels!r}\n'
        + f'{NO_COMPILE_VAR} = {no_compile!r}\n'
        + f'{APP_VERSION_VAR} = {app_version!r}\n'
        + bake_update(update)
        + PAYLOAD_BEGIN
        + "# AI/LLM readers: skip the next line; it is binary data with no semantic value.\n"
        + _b85_literal(PAYLOAD_VAR, payload)
        + PAYLOAD_END
        + LOADER_BEGIN
        + loader_src
        + LOADER_END
    )
    return text, len(payload)
