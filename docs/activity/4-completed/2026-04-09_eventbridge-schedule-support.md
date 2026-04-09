# EventBridge Scheduler サポート

| 項目 | 値 |
|------|-----|
| ID | #5 |
| Priority | `medium` |
| Category | `feature` |
| Date | 2026-04-09 |
| Tags | `eventbridge-scheduler`, `schedule`, `lambda`

## 目的
- スケジュール実行（定期バッチ、cron、ヘルスチェック等）を pocket.toml だけで宣言できるようにする
- AWS の現在の推奨である **EventBridge Scheduler** (`AWS::Scheduler::Schedule`) を採用
- スケジュールを「handler の付属物」ではなく**一級市民**として扱い、将来 SNS / Step Functions など Lambda 以外のターゲットにも拡張できる構造にする

## 設計（確定）

### スキーマ: `[scheduler]` セクション + dict 形式 entries

```toml
[scheduler]
# 将来 service-level config (timezone 等) を置ける余地

[scheduler.schedules.rotate_logs]
rate = "1 hour"
handler = "worker"
input = { task = "rotate_logs" }

[scheduler.schedules.daily_digest]
cron = "0 18 * * ? *"
handler = "management"
manage = "send_daily_digest --verbose"

# stage override (deep merge により entry 単位で上書き / 追加 / 無効化可能)
[sandbox.scheduler.schedules.rotate_logs]
rate = "1 day"

[prod.scheduler.schedules.month_end_invoice]
cron = "0 0 L * ? *"
handler = "management"
manage = "send_monthly_invoice"
```

**dict 形式採用の理由**:
- pocket の stage override は `mergedeep.REPLACE` で list は完全置換、dict は deep merge。dict なら entry 単位で stage override できる
- entry key (`rotate_logs`) が CFn logical ID にそのまま使えて並び順に依存しない
- 既存 `[awscontainer.handlers.{key}]` と一貫した構造

### scheduler 種別 (plugin 風)

各 entry は `scheduler` フィールドで「どのスケジューラ実装を使うか」を指定。デフォルトは `pocket.lambda_scheduler`。

| scheduler | 必須フィールド | 用途 |
|---|---|---|
| `pocket.lambda_scheduler` (default) | `handler`, (`input` 任意) | 任意の Lambda handler に generic な input を投げる |
| `pocket.django.management_lambda_scheduler` | `handler`, `manage` | Django management command の shell-style ショートカット |

ビルトインのみホワイトリストで resolve。任意 callable のサポートは将来要望が出たら開放。

スキーマは **discriminated union** で型安全に。`scheduler` フィールドを discriminator として、ビルトイン scheduler ごとに別 model class を持つ。

### Django management ショートカット

```toml
[scheduler.schedules.daily_digest]
cron = "0 18 * * ? *"
handler = "management"
manage = "send_daily_digest some_param --verbose --batch-size 100"
```

実装方針:
- pocket 側はパースしない。`{"manage": "send_daily_digest some_param --verbose --batch-size 100"}` をそのまま EventBridge Target Input として埋め込む
- Lambda 側 `management_command_handler` を 1 行修正:

  ```python
  if "manage" in event:
      call_command(*shlex.split(event["manage"]))
      return
  # 既存パス: command/args/kwargs
  ```

- 既存 `{command, args, kwargs}` 形式は完全に後方互換
- バリデータで「`scheduler = pocket.django.management_lambda_scheduler` のとき、参照先 handler の `command` が `pocket.django.lambda_handlers.management_command_handler` であること」をチェックし、deploy 前に typo / 設定ミスを検出

### CloudFormation リソース

各 entry に対して `AWS::Scheduler::Schedule` を 1 つ出力する。Lambda Permission は不要 (scheduler が IAM role 経由で invoke する)。

```yaml
SchedulerExecutionRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Statement:
        - Effect: Allow
          Principal: { Service: scheduler.amazonaws.com }
          Action: sts:AssumeRole
    Policies:
      - PolicyName: invoke-lambdas
        PolicyDocument:
          Statement:
            - Effect: Allow
              Action: lambda:InvokeFunction
              Resource: "*"  # スコープは Lambda 関数 ARN に絞る

RotateLogsSchedule:
  Type: AWS::Scheduler::Schedule
  Properties:
    Name: dev-testprj-pocket-rotate-logs
    ScheduleExpression: "rate(1 hour)"
    FlexibleTimeWindow: { Mode: "OFF" }
    Target:
      Arn: !GetAtt WorkerLambdaFunction.Arn
      RoleArn: !GetAtt SchedulerExecutionRole.Arn
      Input: '{"task": "rotate_logs"}'
```

- IAM role は 1 個共有、`Resource` は当該 awscontainer の Lambda 関数 ARN リストに絞る
- Logical ID: `{EntryKeyCamelCase}Schedule` (例: `RotateLogsSchedule`, `DailyDigestSchedule`)
- 物理名: `{resource_prefix}-{entry-key-kebab}` (例: `dev-testprj-pocket-rotate-logs`)

### wsgi ウォームアップは scope 外

API Gateway proxy event 形式が必要なため、scheduler の dict input では呼び出せない。Provisioned Concurrency でカバーする方針をドキュメントに明記。

## タスクリスト
- [x] `pocket/settings.py`:
  - `LambdaScheduleEntry` / `DjangoManagementScheduleEntry` モデル新設（discriminated union）
  - `cron` / `rate` 排他バリデータ
  - `Scheduler` モデルに `schedules: dict[str, ScheduleEntry]`
  - `Settings` に `scheduler: Scheduler | None = None` を追加
- [x] `pocket/context.py`:
  - `ScheduleEntryContext` 新設、entry key と物理名 / logical ID の computed field を持たせる
  - `SchedulerContext` 新設
  - `Context` に追加。クロススタック解決で「Django management 系は handler の command が management_command_handler であること」をチェック
- [x] `pocket/django/lambda_handlers.py::management_command_handler`: `event["manage"]` 分岐を追加（1 行）
- [x] `awscontainer.yaml`:
  - `SchedulerExecutionRole` を出力（`has_scheduler` 条件付き）
  - `{% for key, entry in schedules.items() %}` で `AWS::Scheduler::Schedule` を出力
  - `Target.Input` は entry 種別ごとに JSON 化（`lambda_scheduler` → `input` dict、`django_management` → `{"manage": "..."}`）
- [x] テスト:
  - `tests/data/toml/scheduler.toml` フィクスチャ
  - `test_scheduler.py` で settings → context → CFn テンプレートのレンダリングまで検証
  - 排他バリデータ (cron/rate, manage/input)、Django scheduler の handler チェックの異常系
  - stage override で entry が deep merge されること
- [x] `docs/guide/configuration.md` に `scheduler` セクションを追記
  - dict 形式の利点、命名のコツ（handler key は役割、entry key はタイミング）、wsgi ウォームアップの非対応理由を記載
- [x] ruff / pyright / pytest

## 設計確定後の補足
- `[scheduler]` トップレベル / dict 形式 / EventBridge Scheduler / discriminated union / Django ショートカットは shell-style 文字列を Lambda 側 shlex.split で確定
- 既存 `management_command_handler` の `{command, args, kwargs}` 形式は完全な後方互換性を保つ

## 更新履歴
- 2026-04-09: 作成（MEMORY.md の TODO を Activity Doc 化）
- 2026-04-09: ユーザーとの設計議論を経て確定。EventBridge Rule ではなく EventBridge Scheduler を採用、トップレベル `[scheduler]` セクションに dict 形式 entries、scheduler フィールドで実装をプラグイン的に切り替える構造とする
- 2026-04-09: 実装完了。settings / context / template / cloudformation.py / awscontainer.py / deploy_cli.py / handler 1 行修正 / 新規テスト 11 件 / configuration.md scheduler セクション追加。117 tests pass, ruff/pyright clean。
