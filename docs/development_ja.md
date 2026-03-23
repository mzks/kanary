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
uv run python -m kanary lint ./rules
uv run python -m kanary lint ./rules ./local-rules
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
- owner 未設定 warning
- source 参照の整合
- plugin ID 重複
- `StaleRule.timeout` の妥当性
- no matching output warning

## Reload

rule directory は継続監視され、変更時に自動 reload されます。

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

reload 失敗時は旧 registry を維持します。

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
- `examples/`: 実運用寄りの例
- `dev/`: 開発補助ツール
- `tests/`: unittest
- `docs/`: 利用者向け文書
