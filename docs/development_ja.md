# Development

## Environment

Kanary の開発とテストは `uv` を前提とします。

```bash
uv sync
```

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

主な検査内容:

- 必須項目
  - `rule_id`
  - `source`
  - `severity`
  - `tags`
  - `evaluate()`
- `tags = []` warning
- source 参照の整合
- plugin ID 重複
- `StaleRule.timeout` の妥当性
- no matching output warning

## Reload

plugin directory は継続監視され、変更は自動検知されます。default の `--auto-reload off` では、反映は `kanaryctl reload ...` で明示的に行います。

```text
file change detected
  ↓
すべての plugin file を load して検査
  ↓
mark plugins dirty
  ↓
explicit reload or auto-reload
  ↓
置き換える plugin をすべて作る
  ↓
targeted replace
```

失敗したときに何が保たれるかは段階によります。

- load と検査: plugin file が load できない、または検査に通らない場合は何も適用せず、動いている plugin はすべてそのまま動きます（`reload_failed`。loader の error が付きます）。
- 適用: 何かを変える前に、置き換える plugin をすべて作ります。class や `__init__()` が失敗した plugin は適用せず、動いている instance がそのまま続き、`reload_error` が表示されます。他の plugin は適用します（`partial`）。新しい source や output の `init()` が失敗した場合は適用したうえで、起動時と同じくやり直し続けます。

## Repository Layout

```text
src/kanary/
demo/
examples/
dev/
tests/
docs/
```

- `src/kanary/`: engine 本体と組み込み helper
- `demo/`: 最小例
- `examples/`: getting started のコード、factory pattern、SQLite のデモ（いろいろな種類の plugin は template: `src/kanary/templates/`）
- `dev/`: 開発補助ツール
- `tests/`: unittest
- `docs/`: 利用者向け文書
