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
- owner 未設定 warning
- source 参照の整合
- plugin ID 重複
- `StaleRule.timeout` の妥当性

## Reload

rule directory は継続監視され、変更時に自動 reload されます。
