ZUV_SHEBANG = "#!/usr/bin/env -S uv run --script\n"

PAYLOAD_VAR = "_ZUV_PAYLOAD"
ENTRY_VAR = "_ZUV_ENTRY"
BUILD_ID_VAR = "_ZUV_BUILD_ID"
LOADER_VAR = "_ZUV_LOADER"
PY_TAG_VAR = "_ZUV_PY_TAG"
SHA_VAR = "_ZUV_SHA"
HAS_WHEELS_VAR = "_ZUV_HAS_WHEELS"
NO_COMPILE_VAR = "_ZUV_NO_COMPILE"
UPDATE_PROVIDER_VAR = "_ZUV_UPDATE_PROVIDER"
UPDATE_REPO_VAR = "_ZUV_UPDATE_REPO"
UPDATE_TAG_VAR = "_ZUV_UPDATE_TAG"
UPDATE_FILE_VAR = "_ZUV_UPDATE_FILE"
APP_VERSION_VAR = "_ZUV_APP_VERSION"
WHEELS_DIRNAME = "_zuv_wheels"
# User-facing label -> list of pip --platform tags. Multiple tags per target
# let pip match newer manylinux/macos variants too.
WHEEL_PLATFORMS: dict[str, list[str]] = {
    "windows":   ["win_amd64"],
    "linux":     ["manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux1_x86_64"],
    "linux-arm": ["manylinux2014_aarch64", "manylinux_2_17_aarch64"],
    "macos":     ["macosx_10_12_x86_64", "macosx_11_0_x86_64"],
    "macos-arm": ["macosx_11_0_arm64", "macosx_12_0_arm64"],
}

PAYLOAD_BEGIN = "# === BEGIN ZUV_PAYLOAD (opaque base85 tar.xz, machine-generated) ===\n"
PAYLOAD_END = "# === END ZUV_PAYLOAD ===\n"
LOADER_BEGIN = "# === BEGIN ZUV_LOADER (opaque base85 marshal+zlib bytecode) ===\n"
LOADER_END = "# === END ZUV_LOADER ===\n"

READY_SENTINEL = ".zuv-ready"
DEFAULT_MAX_EXTRACT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# Directories/files never included in a bundle's tarball payload.
SKIP_NAMES = frozenset({
    ".venv", ".zuv", "dist", "build", "__pycache__",
    "node_modules", ".git", ".idea", ".vscode",
    ".mypy_cache", ".ruff_cache", ".pytest_cache",
    ".tox", ".nox", "htmlcov", ".DS_Store", "Thumbs.db",
})
