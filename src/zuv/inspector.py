"""`zuv inspect` - LLM-friendly summary of a built .py with the payload elided."""
import base64
import re
import sys
from pathlib import Path

from .constants import (
    APP_VERSION_VAR,
    BUILD_ID_VAR,
    ENTRY_VAR,
    LOADER_BEGIN,
    LOADER_END,
    PAYLOAD_BEGIN,
    PAYLOAD_END,
    PAYLOAD_VAR,
    SHA_VAR,
    UPDATE_FILE_VAR,
    UPDATE_PROVIDER_VAR,
    UPDATE_REPO_VAR,
    UPDATE_TAG_VAR,
)


def _find_str(text: str, var: str) -> str | None:
    m = re.search(rf'^{re.escape(var)}\s*=\s*[\'"]([^\'"]*)[\'"]', text, re.MULTILINE)
    return m.group(1) if m else None


def _slice_b85(text: str, begin: str, end: str, var: str) -> str | None:
    b = text.find(begin)
    e = text.find(end, b + len(begin)) if b >= 0 else -1
    if b < 0 or e < 0:
        return None
    block = text[b + len(begin):e]
    m = re.search(rf'{re.escape(var)}\s*=\s*b"([^"]*)"', block)
    return m.group(1) if m else None


def _slice_section(text: str, begin: str, end: str) -> str | None:
    b = text.find(begin)
    e = text.find(end, b + len(begin)) if b >= 0 else -1
    if b < 0 or e < 0:
        return None
    return text[b + len(begin):e]


def _pep723_block(text: str) -> str | None:
    m = re.search(r"^# /// script\n(.*?)^# ///$", text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else None


def inspect(path: Path) -> int:
    if not path.is_file():
        print(f"error: not a file: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")
    size_kb = path.stat().st_size / 1024

    entry = _find_str(text, ENTRY_VAR)
    build_id = _find_str(text, BUILD_ID_VAR)
    sha = _find_str(text, SHA_VAR)
    version = _find_str(text, APP_VERSION_VAR)
    payload_b85 = _slice_b85(text, PAYLOAD_BEGIN, PAYLOAD_END, PAYLOAD_VAR)
    loader_src = _slice_section(text, LOADER_BEGIN, LOADER_END)
    pep723 = _pep723_block(text)

    if entry is None or payload_b85 is None:
        print(
            f"error: not a zuv-built file (missing {ENTRY_VAR} or {PAYLOAD_VAR})",
            file=sys.stderr,
        )
        return 2

    payload_decoded = len(base64.b85decode(payload_b85))

    print(f"File:      {path}")
    print(f"File size: {size_kb:.1f} KB")
    print(f"Entry:     {entry}")
    print(f"Build ID:  {build_id or '?'}")
    print(f"SHA-256:   {sha or '?'}")
    print(f"Version:   {version or '(unspecified)'}")
    print(f"Payload:   {len(payload_b85)} chars base85 / {payload_decoded} bytes tar.xz (elided)")
    if loader_src is not None:
        lines = loader_src.count("\n")
        print(f"Loader:    {len(loader_src)} chars / {lines} lines of Python source (portable)")

    # Update config, if any
    repo = _find_str(text, UPDATE_REPO_VAR)
    if repo:
        provider = _find_str(text, UPDATE_PROVIDER_VAR) or "?"
        tag = _find_str(text, UPDATE_TAG_VAR) or "?"
        file = _find_str(text, UPDATE_FILE_VAR) or "?"
        print(f"Updates:   {provider}:{repo} (release {tag}, asset {file})")

    print()
    print("PEP 723 metadata:")
    if pep723:
        for line in pep723.splitlines():
            print(f"  {line}")
    else:
        print("  (none)")
    return 0
