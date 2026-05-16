# zuv

Bundle any `uv` project into a single runnable `.py` file. End users only need `uv` installed.

```sh
uv run app.py
```

That's it. The bundled script is a [PEP 723](https://peps.python.org/pep-0723/) self-contained script. On first run it creates a `.venv` next to itself, installs the project's dependencies into it via `uv pip install`, extracts the bundled source, and runs the entry point. Subsequent runs reuse the cache.

## Install

```sh
uv tool install zuv
```

## Project layout

Your project needs a `pyproject.toml` and a `src/main.py` with a `main()` function:

```
my-project/
  pyproject.toml         # [project] dependencies = [...]
  src/
    main.py              # def main(): ...
```

## Build

```sh
zuv build ./my-project
# -> ./dist/my-project.py
```

`zuv build` wipes `./dist/` first, then writes a fresh single-file script. Override the path with `-o` and the entry point with `-e module:function` (default `main:main`).

### Build the included examples

```sh
zuv build examples/bigtest -o dist/bigtest.py
zuv build examples/fastapi -o dist/fastapi.py
```

Then:

```sh
uv run dist/bigtest.py
uv run dist/fastapi.py
```

## Sibling overrides

The script `chdir`s to its own folder before invoking your entry point, so any file you drop next to the `.py` is visible to user code as a cwd-relative path:

| File next to the bundle | Effect |
| --- | --- |
| `.env` | `load_dotenv(".env")` picks it up |
| `frontend/` | `Path("frontend")` resolves to the override |
| `.venv/` | shared dep cache (auto-created on first run) |
| `.zuv/` | extracted source cache (per build hash) |

This lets you ship a single `.py` and let users tweak config or assets next to it without rebuilding.

## How it works

The output `.py` is a normal Python script with three parts:

1. A `#!/usr/bin/env -S uv run --script` shebang plus a PEP 723 metadata block declaring the Python version.
2. An `_ZUV_ENV` dict with the entry point and the project's dependency list, and a `_ZUV_PAYLOAD` base85-encoded `tar.gz` of `src/`.
3. A small loader that bootstraps the venv, extracts the source, sets `ZUV_DIR` / `ZUV_CWD` / `ZUV_CACHE`, and imports the entry callable.

Deps are NOT bundled inside the `.py`. uv handles them at runtime, so the bundle stays tiny (under 15 KB even for a FastAPI app) and binary wheels work natively without extraction tricks.

## Layout

```
src/
  pyproject.toml
  zuv/
    cli.py                 # zuv build CLI
    builder.py             # tar.gz + base85 + emit .py
    _loader_template.py    # runtime loader embedded in every output
    constants.py
examples/
  bigtest/                 # rich + pydantic smoke test
  fastapi/                 # FastAPI + uvicorn web app
```
