# fastapi-example

A minimal FastAPI app that ships as a single `.zuv` file - source, frontend,
and dependencies all bundled. On first run zuv extracts to a cache, builds
a venv, and starts the server. On every subsequent run it just starts.

The persistent `data/` folder survives version upgrades, so user-toggled
settings stay put when you ship a new build.

## Layout

```
fastapi/
  pyproject.toml         # project + [tool.zuv] config
  data/                  # persistent volume (kept across versions)
    settings.json
  src/
    main.py              # entry script (paths are anchored here)
    example.env
    api/__init__.py      # /api/health, /api/info, /api/settings (GET/PUT)
    frontend/index.html  # SPA, served at /
```

## Configuration (`pyproject.toml`)

```toml
[tool.zuv]
entry  = "src/main.py"   # the starter script
volume = "data"          # persistent folder (relative to project root)
```

## Build

Build straight from the upstream repo (no local checkout of zuv needed):

```powershell
uvx --from "git+https://github.com/HamzaYslmn/zuv#subdirectory=src" zuv build . `
    -o dist/fastapi.zuv.py `
    --update-repo HamzaYslmn/zuv
```

`--update-repo` bakes the GitHub release that the bundle will check for
self-updates on startup.

## Run

```powershell
uv run dist/fastapi.zuv.py
```

Then open <http://127.0.0.1:8000> - the page has a "Persistent switch"
card that calls `GET /api/settings` and `PUT /api/settings`.

## How persistence works

- `data/` inside the project is the seed shipped with the bundle.
- On first run, zuv copies it into `<sibling>/.zuv/data/` and mounts it back
  into the extract dir via a junction (Windows) or symlink (POSIX).
- On every later run (including after a version bump), the mount is re-attached
  to the same persistent folder. The seed never overwrites user state.

To prove it: toggle the switch on the page, rebuild with a bumped version,
re-run - the new app loads with your saved value.

### Volumes are per-bundle-directory

The persistent volume lives next to the bundle file, at
`<dir-of-bundle>/.zuv/<volume>/`. Move the bundle to a different folder
(or download it via self-update into a folder that didn't have one yet) and
you get a **fresh volume seeded from the new bundle's `data/`** - not the
state from the old location.

To check or migrate state:

```powershell
# Where does this bundle's volume live?
zuv volume locate path\to\app.zuv.py

# Save a snapshot before risky upgrades.
zuv volume backup path\to\app.zuv.py -o snapshot.tar.gz

# Wipe a volume (re-seeds from the bundle on next run).
zuv volume wipe path\to\app.zuv.py
```

To carry state to a different directory, copy the volume contents manually:

```powershell
robocopy <old-dir>\.zuv\data <new-dir>\.zuv\data /E
```

## Paths convention

Everything is anchored to `main.py`, not to the current working directory:

```python
BASE_DIR     = Path(__file__).resolve().parent   # src/
PROJECT_ROOT = BASE_DIR.parent                   # project root
DATA_DIR     = PROJECT_ROOT / "data"             # the volume
```

The same trio is recomputed in `api/__init__.py` from its own `__file__`.
Run from any working directory - the app finds its files.

## zuv CLI reference

All commands shown below assume zuv is invoked via `uvx`:

```powershell
uvx --from "git+https://github.com/HamzaYslmn/zuv#subdirectory=src" zuv <command> ...
```

(Or install once with `uv tool install ...` and drop the `uvx --from` prefix.)

### `zuv build [PROJECT]`

Build a single-file `.py` bundle from a uv project.

| Flag                  | Default                          | What it does                                                                                                                              |
| --------------------- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `PROJECT`             | `.`                              | Path to the project (containing `pyproject.toml`).                                                                                        |
| `-o, --output PATH`   | `./dist/<project>.zuv.py`        | Output file path. `.zuv.py` / `.zuv.zip` suffix is added automatically.                                                                   |
| `-e, --entry PATH`    | `src/main.py` or `[tool.zuv].entry` | Entry script, relative to project root.                                                                                                |
| `--no-compile`        | off                              | Skip the first-run `.py` -> `.pyc` compile step. Slower per-import startup but no `__pycache__/` left behind.                             |
| `--zip`               | off                              | Wrap the bundle in a `.zip` with `run.bat` + `run.sh` launchers (auto-install `uv` if missing). Recipient extracts and double-clicks.     |
| `--update-repo REPO`  | none                             | GitHub or GitLab repo whose Releases page hosts the rolling build. Accepts `user/repo`, `gitlab:user/repo`, or a full URL. Enables self-update. |
| `--update-tag TAG`    | `latest`                         | Release tag to fetch. `latest` hits `/releases/latest`; anything else pins to that tag.                                                   |
| `--update-file NAME`  | `<output-stem>.zuv.py`           | Asset filename inside the release.                                                                                                        |
| `--deps [PLATFORMS]`  | unset (online deps)              | Embed wheels so the bundle runs offline. No value = current OS. `all` = every platform. Or a comma-list: `windows,linux,linux-arm,macos,macos-arm`. |

Self-update auth (for private repos): set `$GH_TOKEN` (GitHub) or
`$GITLAB_TOKEN` (GitLab). Set `ZUV_NO_UPDATE=1` to disable update checks at
runtime.

### `zuv run FILE [-- SCRIPT_ARGS]`

Thin wrapper over `uv run FILE`. Arguments after the bundle are forwarded.

### `zuv inspect FILE`

Print an LLM-friendly summary of a built `.py` (project, entry, version,
volume, update target). Payload is elided.

### `zuv clean [TARGET]`

Remove `.zuv/` cache directories under `TARGET` (default: cwd). Volume
directories (those containing `.zuv-volume`) are kept by default.

| Flag      | What it does                                                                       |
| --------- | ---------------------------------------------------------------------------------- |
| `--data`  | Also wipe persistent volumes. DESTRUCTIVE: user state in `data/` is permanently lost. |

### `zuv volume <sub> FILE`

Manage the persistent volume of a built bundle.

| Sub-command          | What it does                                            |
| -------------------- | ------------------------------------------------------- |
| `locate FILE`        | Print the on-disk path of the volume (exit 1 if absent). |
| `wipe FILE [-y]`     | Delete the volume. Prompts unless `-y/--yes` is given.  |
| `backup FILE [-o OUT]` | Write a `tar.gz` of the volume. Default name: `<file>.volume.tar.gz`. |

### `[tool.zuv]` in `pyproject.toml`

Project-level zuv settings live under `[tool.zuv]`. They make `zuv build`
work with zero flags - run `zuv build .` and the right entry + volume are
picked up from the project.

```toml
[project]
name = "example"
version = "0.1.1"
requires-python = ">=3.14"
dependencies = ["fastapi", "python-dotenv", "uvicorn", "zuv>=0.0.1"]

[tool.zuv]
entry  = "src/main.py"   # the starter script
volume = "data"          # persistent folder (relative to project root)
```

| Key      | Type   | Effect                                                                                            |
| -------- | ------ | ------------------------------------------------------------------------------------------------- |
| `entry`  | string | Entry script relative to project root. Overrides auto-detect (`src/main.py` -> `main.py`).        |
| `volume` | string | Persistent folder relative to project root. Mounted into the extract dir on every run; preserved across version upgrades. Must be a relative path, no `..`, must not be a file. Omit or leave empty for stateless bundles. |

Notes:

- **CLI flags override `[tool.zuv]`.** `zuv build . --entry src/other.py` uses
  `src/other.py` even if `[tool.zuv].entry` is set.
- **No `update` keys yet.** Self-update settings (`--update-repo`,
  `--update-tag`, `--update-file`) are CLI-only for now. Pyproject-declared
  update target is on the roadmap.
- **`requires-python` is honored.** The loader uses uv to provision a venv
  matching this constraint on first run.
- **Standard project layout** (what zuv expects when auto-detecting):

  ```
  project/
    pyproject.toml      # project + [tool.zuv]
    .python-version     # optional, used by uv
    data/               # persistent (declared as the volume)
    src/                # source code; main.py auto-detected as entry
      main.py
      ...
  ```


