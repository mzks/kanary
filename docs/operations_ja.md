# Operations

## 起動方法

```bash
uv sync
uv run python -m kanary ./rules
uv run python -m kanary ./rules --api-host 0.0.0.0 --api-port 18000
```

主な引数:

- `rule_directories...`
- `--api-port`
- `--api-host`
- `--log-level`
- `--state-db`
- `--node-id`
- `--exclude`

## Web viewer と CLI

Web viewer:

```text
http://<host>:8000/viewer
```

`kanaryctl` は API の thin client です。

```bash
kanaryctl alerts
kanaryctl ack sqlite.value1.stale --operator operator_name --reason "investigating"
kanaryctl silence-for --operator operator_name --minutes 10 --rule 'sqlite.*'
```
