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
- warning for missing `owner`
- source reference validation
- duplicate plugin IDs
- `StaleRule.timeout` validation
- warning for rules with no matching output

## Reload

Plugin directories are watched continuously and reloaded automatically on change.

```text
file change
  ↓
load rules
  ↓
validation
  ↓
build registry
  ↓
atomic swap
```

If reload fails, the previous registry stays active.

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
- `examples/`: more realistic examples
- `dev/`: development utilities
- `tests/`: unittest suite
- `docs/`: user-facing documentation
