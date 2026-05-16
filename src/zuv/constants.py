ZUV_SHEBANG = "#!/usr/bin/env -S uv run --script\n"

PAYLOAD_VAR = "_ZUV_PAYLOAD"
ENTRY_VAR = "_ZUV_ENTRY"
BUILD_ID_VAR = "_ZUV_BUILD_ID"
LOADER_VAR = "_ZUV_LOADER"
PY_TAG_VAR = "_ZUV_PY_TAG"
SHA_VAR = "_ZUV_SHA"

PAYLOAD_BEGIN = "# === BEGIN ZUV_PAYLOAD (opaque base85 tar.xz, machine-generated) ===\n"
PAYLOAD_END = "# === END ZUV_PAYLOAD ===\n"
LOADER_BEGIN = "# === BEGIN ZUV_LOADER (opaque base85 marshal+zlib bytecode) ===\n"
LOADER_END = "# === END ZUV_LOADER ===\n"

READY_SENTINEL = ".zuv-ready"
DEFAULT_MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
