# Getting Started

この文書では、Kanary を使って小さな監視を 1 つ作り、`Source -> Rule -> Output` の流れを手を動かしながら理解します。

ここで使うコードは [examples/getting_started.py](../examples/getting_started.py) にまとまっています。

## 1. Install と起動

```bash
git clone <your-kanary-repo-url>
cd kanary
uv sync
uv run python -m kanary ./examples
```

別マシンから見たい場合:

```bash
uv run python -m kanary ./examples --api-host 0.0.0.0 --api-port 8000
```

## 2. Viewer

```text
http://127.0.0.1:8000/viewer
```

## 3. Source / Rule / Output

この文書で使う source, rule, output は [examples/getting_started.py](../examples/getting_started.py) に入っています。

- `LocalLoadSource`
- `LocalLoadBusy`
- `LocalLoadBusyThreshold`
- `FileOutput`
- `MailAlert`

## 4. helper class

helper class を使うときは通常 `evaluate()` を書かず、class 変数を設定します。

- `StaleRule`: `measurement`, `timeout`
- `RangeRule`: `measurement`, `low`, `high`, `hysteresis`
- `RateRule`: `measurement`, `per_seconds`, `high`, `low`
- `ThresholdRule`: `measurement`, `direction`, `thresholds`, `hysteresis`

## 5. ACK / Silence / Lint

```bash
kanaryctl --base-url http://127.0.0.1:8000 ack local_load.busy --operator operator_name --reason "investigating"
kanaryctl --base-url http://127.0.0.1:8000 silence-for --operator operator_name --minutes 10 --rule 'local_load.*'
uv run python -m kanary lint ./examples
```
