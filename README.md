![zuv banner](./docs/zuv_banner.png)

# zuv

**Pack your uv project into one `.py` file.** Hand it over, drop it on a server, email it — `uv run app.zuv.py` and it works.

## Why

- **One file, no setup.** No zip + README + `requirements.txt` + venv dance. Recipient runs one command.
- **Cross-platform, cross-Python.** Same file works on Windows / Linux / macOS and any Python minor version (bytecode is built on the target).
- **Tiny.** ~10 KB even for a FastAPI app — deps install at first run, they're not embedded (unless you ask with `--deps`).
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
| `--update-repo REPO` | Make the bundle self-update from a GitHub or GitLab repo. Accepts a URL (`https://github.com/user/repo`, `https://gitlab.com/user/repo`) or shorthand (`user/repo` → GitHub, `gitlab:user/repo` → GitLab). On every startup it reads the target file's blob sha via the provider's API; if it changed since the last known sha, prompts `install latest version? [Y/n]`. On Y, downloads the new file, atomically replaces this script, and re-execs. Declined updates are remembered until a newer sha appears. Non-TTY runs (CI, pipes) skip silently. Private repos: set `$GH_TOKEN` (GitHub) or `$GITLAB_TOKEN` (GitLab). `ZUV_NO_UPDATE=1` disables. |
| `--update-branch BRANCH`  | Branch to fetch from. Default: `latest`. Use a rolling branch like `latest` (or `main`) that's force-pushed when a new build is ready — see `.github/workflows/release-bundle.yml` for a template. |
| `--update-file PATH`      | Path of the `.zuv.py` inside the repo. Default: `<output-stem>.zuv.py`. Lets one repo host several bundles (e.g. `fastapi.zuv.py`, `dashboard.zuv.py`). |

### `zuv run <file> [-- script-args...]`

Thin wrapper around `uv run <file>`. Identical to running the bundle directly with uv.

### `zuv inspect <file>`

Print the entry, build hash, sha256, PEP 723 metadata, and a summary of the embedded loader bytecode. Payload is elided.

### `zuv clean [target]`

Remove every `.zuv/` extraction cache under `target` (default: cwd). `target` can be a directory or a built `.zuv.py` (its parent is used).

## Runtime env vars

| var                       | default       | purpose |
|---------------------------|---------------|---------|
| `ZUV_CACHE_DIR`           | next to script | Override where the bundle extracts. |
| `ZUV_MAX_EXTRACT_BYTES`   | 2 GiB         | Decompression-bomb cap. |
| `ZUV_NO_UPDATE`           | (unset)       | If set, disables the `--update-repo` self-update check at startup. |
| `GH_TOKEN` / `GITHUB_TOKEN` | (unset)     | Sent as `Authorization: Bearer …` on GitHub update checks. Required for private GitHub repos. |
| `GITLAB_TOKEN`            | (unset)       | Sent as `PRIVATE-TOKEN: …` on GitLab update checks. Required for private GitLab repos. |

If the script's directory isn't writable, the loader falls back to `$XDG_CACHE_HOME/zuv` / `%LOCALAPPDATA%\zuv` / `~/.cache/zuv`.

## How it works (1 paragraph)

The output is a PEP 723 script: shebang, metadata, a base85-encoded `tar.xz` of your project (`_ZUV_PAYLOAD`), and a tiny loader (`_ZUV_LOADER`) that verifies the sha256, extracts into `.zuv/<stem>_<hash>/`, and runs `uv run --project <extracted> <entry>`. Deps install at first run, so the bundle stays small.

## Caveat

Don't name your project the same as one of its dependencies (`fastapi` depending on `fastapi` confuses uv). Use `fastapi-example` or similar.

## Examples

```sh
zuv build examples/bigtest && uv run dist/bigtest.zuv.py
zuv build examples/fastapi && uv run dist/fastapi.zuv.py
```
