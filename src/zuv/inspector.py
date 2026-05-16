"""`zuv inspect` - LLM-friendly summary of a built .py with the payload elided."""
import base64
import re
import sys
from pathlib import Path

from .constants import BUILD_ID_VAR, ENTRY_VAR, PAYLOAD_VAR


def _find_str(text: str, var: str) -> str | None:
    m = re.search(rf'^{re.escape(var)}\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None


def _find_payload(text: str) -> str | None:
    m = re.search(rf'^{re.escape(PAYLOAD_VAR)}\s*=\s*b"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None


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
    payload = _find_payload(text)
    pep723 = _pep723_block(text)

    if entry is None or payload is None:
        print(
            f"error: not a zuv-built file (missing {ENTRY_VAR} or {PAYLOAD_VAR})",
            file=sys.stderr,
        )
        return 2

    decoded = len(base64.b85decode(payload))

    print(f"File:      {path}")
    print(f"File size: {size_kb:.1f} KB")
    print(f"Entry:     {entry}")
    print(f"Build ID:  {build_id or '?'}")
    print(f"Payload:   {len(payload)} chars base85 / {decoded} bytes tar.xz (elided)")
    print()
    print("PEP 723 metadata:")
    if pep723:
        for line in pep723.splitlines():
            print(f"  {line}")
    else:
        print("  (none)")
    print()

    end_marker = "# === END ZUV_PAYLOAD ===\n"
    end_idx = text.find(end_marker)
    if end_idx >= 0:
        loader = text[end_idx + len(end_marker):]
        print("# === Loader source (safe for LLMs) ===")
        print(loader.rstrip())
    return 0
