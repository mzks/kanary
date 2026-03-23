# Kanary

Kanary は、アラーム・通知・信頼性監視のための Python ベースの実行環境です。  
監視対象から値を読む `Source`、その値を評価する `Rule`、状態変化を外部へ送る `Output` を Python で定義します。

## まず何をするか

最初は `demo/` の最小例を動かしてください。

```bash
uv sync
uv run python -m kanary ./demo
```

その後は次の順がおすすめです。

1. [demo/basic_monitoring.py](demo/basic_monitoring.py) で最小の `Source`, `Rule`, `Output` を確認する
2. [docs/getting_started_ja.md](docs/getting_started_ja.md) と [examples/getting_started.py](examples/getting_started.py) を読む
3. `examples/` で PostgreSQL、Discord、peer monitoring などの例を確認する
4. 自分の `rules/` directory を作って 1 個ずつ定義を増やす

## 実行例

```bash
uv run python -m kanary ./demo
uv run python -m kanary ./demo --api-port 18000
uv run python -m kanary ./demo --api-host 0.0.0.0 --api-port 18000
uv run python -m kanary ./demo --state-db ./var/kanary.db
```

Web viewer:

```text
http://<host>:8000/viewer
```

## 環境変数

本体に必須の環境変数はありません。必要に応じて次を使えます。

- `KANARY_SQLITE_PATH`
- `KANARY_API_URL`
- `KANARY_API_HOST`
- `KANARY_NODE_ID`

## 文書

- [docs/getting_started_ja.md](docs/getting_started_ja.md)
- [docs/plugins_ja.md](docs/plugins_ja.md)
- [docs/operations_ja.md](docs/operations_ja.md)
- [docs/api_ja.md](docs/api_ja.md)
- [docs/development_ja.md](docs/development_ja.md)
- [docs/deployment_ja.md](docs/deployment_ja.md)
