# Development

## Environment

Kanary development and tests assume `uv`.

```bash
uv sync
```

This creates `.venv` on the first run.

## Tests

```bash
uv run python -m unittest discover -s tests
```

## Lint

```bash
uv run python -m kanary lint ./plugins
uv run python -m kanary lint ./plugins ./local-plugins
uv run python -m kanary lint ./examples --exclude console
```

Typical checks:

- required rule fields
  - `rule_id`
  - `source`
  - `severity`
  - `tags`
  - `evaluate()`
- warning for `tags = []`
- source reference validation
- duplicate plugin IDs
- `StaleRule.timeout` validation
- warning for rules with no matching output

## Reload

Plugin directories are watched continuously and changes are detected automatically. With the default `--auto-reload off`, they are applied explicitly with `kanaryctl reload ...` (`--auto-reload dirty` or `all` applies them on detection).

```text
file change detected
  ↓
load and validate every plugin file
  ↓
mark plugins dirty
  ↓
explicit reload or auto-reload
  ↓
create every replacement
  ↓
targeted replace
```

What a failure keeps depends on the stage:

- Loading and validation: if a plugin file does not load or validate, nothing is applied and every running plugin goes on (`reload_failed`, with the loader error).
- Applying: every replacement is created before anything changes. A plugin whose class or `__init__()` fails is not applied, and its running instance goes on with its `reload_error` shown; the others are applied (`partial`). A new source or output whose `init()` fails is applied and keeps trying, as at startup.

## Repository Layout

```text
src/kanary/
demo/
examples/
dev/
tests/
docs/
```

- `src/kanary/`: engine and built-in helpers
- `demo/`: smallest examples
- `examples/`: the getting-started code, factory patterns, and the SQLite demo (plugins of every kind are templates: `src/kanary/templates/`)
- `dev/`: development utilities
- `tests/`: unittest suite
- `docs/`: user-facing documentation
