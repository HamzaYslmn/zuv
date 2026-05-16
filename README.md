![zuv banner](./docs/zuv_banner.png)

# zuv

**zuv packs your entire uv project into a single `.py` file** so you can hand it to a friend, drop it on a server, or attach it to an email and it just runs. The recipient only needs [uv](https://astral.sh/uv) installed — no Python install, no `pip install -r`, no virtualenv setup, no folder structure to preserve.

```sh
uv run my-app.py
```

One file. One command. Same behaviour on any machine.

## Why

Shipping a Python project usually means a zip, a README about setting up a venv, a `requirements.txt`, and a prayer that the recipient has the right Python version. zuv collapses all of that into one `.py` file that:

- Carries the **whole project** inside it (your `src/`, `pyproject.toml`, `uv.lock`, configs, assets — everything you don't `.gitignore`).
- On first run, extracts itself into `.zuv/<name>_<hash>/` next to the script, lets uv build a `.venv` inside that folder, then runs your entry point.
- On every later run, skips the extraction and just executes.
- Stays tiny (under ~10 KB even for a FastAPI app) because dependencies are installed at runtime by uv from PyPI, not embedded.

## Install

```sh
uv tool install zuv
```

## Project layout

Any standard uv project works. The most common shape:

```
my-project/
  pyproject.toml          # [project] dependencies = [...]
  src/
    main.py               # an executable script (with if __name__ == "__main__")
```

`main.py` at the project root also works.

## Build

From inside the project:

```sh
zuv build
# -> ./dist/<project-name>.py
```

Or point at a project explicitly:

```sh
zuv build ./my-project -o ./dist/my-app.py -e src/main.py
```

`zuv build` wipes `./dist/` first and writes a fresh single-file script. The entry point is resolved in this order:

1. `--entry`/`-e` flag
2. `[tool.zuv].entry` in `pyproject.toml`
3. `src/main.py` if it exists, otherwise `main.py`

## Run

```sh
uv run dist/my-app.py
```

First run: uv extracts the bundle, creates `dist/.zuv/<name>_<hash>/.venv`, installs deps, runs your entry. Subsequent runs go straight to executing.

## Try the included examples

```sh
zuv build examples/bigtest -o dist/bigtest.py
uv run dist/bigtest.py

zuv build examples/fastapi -o dist/fastapi.py
uv run dist/fastapi.py
```

## Sibling overrides

The bundle's entry script runs with the extracted project folder as its CWD, so anything you would normally find next to your code (config files, frontend bundles, .env files) still works the same way.

## Inspect a built file

```sh
zuv inspect dist/my-app.py
```

Prints the entry, build hash, PEP 723 metadata, and the embedded loader. The base85 payload itself is elided so the output stays useful for LLMs and code review.

## A small caveat

Don't name your project the same as one of its dependencies. For example, a project named `fastapi` that depends on `fastapi` will confuse uv during install. Rename it to `fastapi-example` (or similar) and you're fine.

## How it works

The output `.py` has four parts:

1. A `#!/usr/bin/env -S uv run --script` shebang and a PEP 723 metadata block declaring `requires-python` (no deps — uv reads those from the embedded `pyproject.toml` after extraction).
2. `_ZUV_ENTRY` and `_ZUV_BUILD_ID` module globals.
3. `_ZUV_PAYLOAD` — base85 of a `tar.gz` of your project tree.
4. A ~30-line loader that decodes the payload, extracts it into `.zuv/<stem>_<hash>/`, and execs `uv run --project <extracted> <entry>`.

Dependencies aren't bundled inside the `.py`. uv installs them into the extracted project's local `.venv` on first run, so binary wheels work natively and the bundle stays small.

## Layout

```
src/
  pyproject.toml
  zuv/
    cli.py                 # zuv CLI (build, inspect)
    builder.py             # tarball + base85 + emit .py
    inspector.py           # zuv inspect
    _loader_template.py    # runtime loader embedded in every output
    constants.py
examples/
  bigtest/                 # rich + pydantic smoke test
  fastapi/                 # FastAPI + uvicorn web app
```
