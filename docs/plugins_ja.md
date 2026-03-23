# Plugin Model

この文書では、まずユーザーが満たすべき最小 interface を説明し、その後に組み込み helper class を説明します。

## 1. Source

必須:

- `source_id`
- `poll(ctx) -> kanary.SourceResult`

任意:

- `interval`
- `init(ctx)`
- `terminate(ctx)`

## 2. Rule

必須:

- `rule_id`
- `source`
- `severity`
- `tags`
- `evaluate(payload, ctx) -> kanary.Evaluation`

任意 metadata:

- `owner`
- `description`
- `runbook`

## 3. Output

必須:

- `output_id`
- `emit(event, ctx)`

## 4. 組み込み helper class

- `BufferedSource`
- `RangeRule`
- `StaleRule`
- `RateRule`
- `ThresholdRule`
- `RemoteKanarySource`
- `RemoteAlarm`
- `import_remote_alarms`
- `MailOutput`
