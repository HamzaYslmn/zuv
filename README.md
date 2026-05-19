![zuv banner](./docs/zuv_banner.png)

# zuv

**Pack your uv project into one `.py` file.** Hand it over, drop it on a server, email it - `uv run app.zuv.py` and it works.

## Why

- **One file, no setup.** No zip + README + `requirements.txt` + venv dance. Recipient runs one command.
- **Cross-platform, cross-Python.** Same file works on Windows / Linux / macOS and any Python minor version (bytecode is built on the target).
- **Tiny.** ~10 KB even for a FastAPI app - deps install at first run, they're not embedded (unless you ask with `--deps`).
- **Just uv.** No bespoke runtime, no PyInstaller-style freezing. The bundle is a PEP 723 script; `uv run` is the entrypoint.

## Install

```sh
uv tool install zuv
```

## Quick start

```sh
zuv build                       # -> ./dist/<name>.zuv.py
uv run dist/<name>.zuv.py       # run it
```

`dist/` is wiped on every build. Bundles ship `.py` sources (no bytecode), so the same file works on any OS and any Python minor version. First run extracts into `dist/.zuv/<name>_<hash>/`, installs deps into a local `.venv`, runs your entry. Later runs skip extraction.

## Commands

### `zuv build [project] [flags]`

| arg / flag       | what it does |
|------------------|--------------|
| `project`        | Path to the uv project (containing `pyproject.toml`). Default: current directory. |
| `-o, --output`   | Output file path. Default: `./dist/<project-name>.zuv.py` (or `.zuv.zip` with `--zip`). |
| `-e, --entry`    | Entry script relative to project root. Default: `[tool.zuv].entry`, then `src/main.py`, then `main.py`. |
| `--zip`          | Wrap the `.zuv.py` in a `.zuv.zip` with `run.bat` (Windows) + `run.sh` (Unix/macOS) launchers. The launchers install `uv` from `https://astral.sh/uv` if missing, then run the bundle. For recipients with neither uv nor Python. |
| `--deps [LIST]`  | Embed wheels for the locked deps so the bundle runs offline. Bare = current OS only. `all` = every supported platform. Comma list = pick from `windows`, `linux`, `linux-arm`, `macos`, `macos-arm`. Wheels are tied to the Python minor you build with. *Note:* wheels can't carry OS libs (`libGL`, `libpq`, …); on bare Linux, `apt install` what `ImportError` names. Prefer `-headless` variants. |
| `--no-compile`   | Tell the loader to skip the first-run `.py` → `.pyc` compile. Cache stays as plain `.py`; per-import startup is slower. |
| `--update-repo REPO`     | Make the bundle self-update from a GitHub or GitLab repo's **Releases**. Accepts a URL (`https://github.com/user/repo`, `https://gitlab.com/user/repo`) or shorthand (`user/repo` → GitHub, `gitlab:user/repo` → GitLab). On every startup it asks the Releases API for the named asset and the release's `tag_name`. If the bundled `[project] version` from pyproject and the remote tag both parse as dotted-int versions, the loader skips when local ≥ remote (no prompt for older or equal). Otherwise (e.g. rolling `latest` tag) it falls back to comparing the asset's id; prompts `install latest version? [Y/n]` when something changed. Declined versions are remembered. Non-TTY runs skip silently. Private repos: set `$GH_TOKEN` (GitHub) or `$GITLAB_TOKEN` (GitLab). `ZUV_NO_UPDATE=1` disables. |
| `--update-tag TAG`       | Release tag to fetch the asset from. Default: `latest` (special value: hits the provider's `/releases/latest` endpoint). Use a fixed tag to pin recipients to one release. |
| `--update-file FILENAME` | Asset filename inside the release. Default: `<output-stem>.zuv.py`. Lets one release host several bundles (e.g. `fastapi.zuv.py`, `dashboard.zuv.py`). |

### `zuv run <file> [-- script-args...]`

Thin wrapper around `uv run <file>`. Identical to running the bundle directly with uv.

### `zuv inspect <file>`

Print the entry, build hash, sha256, PEP 723 metadata, and a summary of the embedded loader bytecode. Payload is elided.

### `zuv clean [target]`

Remove every `.zuv/` extraction cache under `target` (default: cwd). `target` can be a directory or a built `.zuv.py` (its parent is used). Persistent volumes (see below) are **kept** by default; pass `--data` to also wipe them.

### `zuv volume <locate|wipe|backup> <file>`

Developer helpers for a bundle's persistent volume (see [Persistent storage](#persistent-storage-volumes)).

- `locate <file>` - print the on-disk host path of the volume.
- `wipe <file> [-y]` - delete the volume (prompts unless `-y`).
- `backup <file> [-o OUT]` - write a `tar.gz` of the volume contents.

## Runtime env vars

| var                       | default       | purpose |
|---------------------------|---------------|---------|
| `ZUV_CACHE_DIR`           | next to script | Override where the bundle extracts. |
| `ZUV_MAX_EXTRACT_BYTES`   | 2 GiB         | Decompression-bomb cap. |
| `ZUV_NO_UPDATE`           | (unset)       | If set, disables the `--update-repo` self-update check at startup. |
| `GH_TOKEN` / `GITHUB_TOKEN` | (unset)     | Sent as `Authorization: Bearer …` on GitHub update checks. Required for private GitHub repos. |
| `GITLAB_TOKEN`            | (unset)       | Sent as `PRIVATE-TOKEN: …` on GitLab update checks. Required for private GitLab repos. |

If the script's directory isn't writable, the loader falls back to `$XDG_CACHE_HOME/zuv` / `%LOCALAPPDATA%\zuv` / `~/.cache/zuv`. Persistent volumes follow the same root.

## How it works (1 paragraph)

The output is a PEP 723 script: metadata, a base85-encoded `tar.xz` of your project (`_ZUV_PAYLOAD`), and a tiny loader (`_ZUV_LOADER`) that verifies the sha256, extracts into `.zuv/<stem>_<hash>/`, and runs `uv run --project <extracted> <entry>`. Deps install at first run, so the bundle stays small.

## Persistent storage (volumes)

### Standard project layout

The recommended layout for a `zuv`-friendly project keeps source, persistent
data, and project metadata in clearly separated places:

```
myproject/
  pyproject.toml      # project metadata + [tool.zuv]
  .python-version
  src/                # all source code
    main.py
    api/
    frontend/
  data/               # persistent storage (declared as the volume)
```

With `[tool.zuv] volume = "data"`, the `data/` directory is mounted as a
persistent host folder at runtime, so anything the app writes there survives
rebuilds and version upgrades. Everything else under the project (sources,
lockfile, `.venv`) is treated as ephemeral and may be re-extracted between
versions.

Without a volume, every new build of a bundle extracts into a fresh `.zuv/<stem>_<build_id>/` directory and the previous cache is garbage-collected on first run, so anything the app wrote inside its project tree is lost on upgrade.

Declare a volume in your project's `pyproject.toml`:

```toml
[tool.zuv]
volume = "data"
```

- `volume` is a relative path inside the project (no `..`, no absolute paths). One per project.
- At runtime the loader mounts `<script_dir>/.zuv/<volume_path>/` at `<extracted>/<volume>` via a symlink (POSIX) or junction (Windows, no admin / no Dev Mode required).
- The host folder is **persistent**: it is not part of any `<stem>_<build_id>/` cache, so `zuv clean` and version upgrades leave it untouched.
- App code keeps using the same relative path (e.g. `Path("data/app.db")`); mounting is transparent.

**Seeding (Docker-style):** if your project ships content under `<volume>/` (e.g. a default SQLite file), the **first** extraction promotes that content into the persistent volume. Subsequent versions never re-seed, so user data is preserved.

**Cleanup:**
- `zuv clean` keeps volumes.
- `zuv clean --data` also wipes volume directories.
- `zuv volume wipe app.zuv.py` wipes one bundle's volume.

**Filesystem support:** mounting needs a filesystem that allows symlinks (POSIX) or junctions (NTFS on Windows). It will fail on FAT32 / exFAT USB sticks, some SMB / network shares, and some VM shared folders. If the bundle lives on such a volume, set `ZUV_CACHE_DIR` to a path on a normal local disk (e.g. `%LOCALAPPDATA%\zuv` on Windows or `~/.cache/zuv` on Linux/macOS); the volume will then mount under that path while the bundle itself stays on the original drive. If both fail, the loader prints a warning and the app runs without persistence (data stays inside the per-build extract dir and is lost on upgrade).

## Caveat

Don't name your project the same as one of its dependencies (`fastapi` depending on `fastapi` confuses uv). Use `fastapi-example` or similar.

## Examples

```sh
zuv build examples/bigtest && uv run dist/bigtest.zuv.py
zuv build examples/fastapi && uv run dist/fastapi.zuv.py
```

