# pocket permissions list --stage=<stage> CLI 追加

| 項目 | 値 |
|------|-----|
| ID | #9 |
| Priority | `low` |
| Category | `feature` |
| Date | 2026-04-28 |
| Tags | `cli`, `iam`, `permissions`, `worgue-integration`

## 目的

- worgue 側の `worgue project aws github-deploy provision` が GitHub Actions デプロイ用 IAM Role を作る際、現状は `*:*` 相当を一律付与している。
- pocket.toml の構成 (CloudFront / RDS / VPC / EFS / SQS / SES など) から必要な AWS 権限を算出するロジックは magic-pocket の責務として自然なので、CLI / Python API として提供する。
- フィードバック `feedbacks/active/magic-pocket/20260424-worgue-pocket-permissions-list/` への対応。

## 方針

**docs/permissions/aws.md のテーブルをそのままコード化する** 方針で進める。

- 粒度: 現 docs と同じ `cloudformation:*` `s3:*` などのワイルドカード中心
- 入力: `pocket.toml` + `--stage=<stage>`
- 出力: その stage に必要な AWS Action リスト
- 出力形式: text (デフォルト) / `--format json`

Action レベルの絞り込み (Least Privilege) は将来別タスク。実際にデプロイを回して
CloudTrail で叩かれた API を集計する try-and-error が必要なため重く、まずは
worgue 側の用途 (`--with-persistent-resources` を不要にする) を満たす最小実装に
絞る。

## 設計概要

### マッピング

`docs/permissions/aws.md` の表を以下のように構造化:

| pocket.toml 条件 | 追加権限 |
|------------------|----------|
| 常時 (コア) | `cloudformation:*`, `ecr:*`, `lambda:*`, `apigateway:*`, `s3:*`, `iam:CreateRole/...`, `logs:*`, `sts:GetCallerIdentity` |
| `secrets.store == "secretsmanager"` (デフォルト) | `secretsmanager:*` |
| `secrets.store == "ssm"` | `ssm:GetParameter/PutParameter/...` |
| `[cloudfront]` あり | `cloudfront:*`, `acm:*`, `route53:ChangeResourceRecordSets/GetChange` |
| `[awscontainer.vpc]` あり | `ec2:*` (VPC 関連) |
| `[rds]` あり | `rds:*`, `ec2:*SecurityGroup*` |
| `[awscontainer.vpc.efs]` あり | `elasticfilesystem:*` |
| ハンドラーに `sqs` あり | `sqs:*` |
| `[ses]` あり | `ses:SendEmail`, `ses:SendRawEmail` |
| `build.type == "codebuild"` | `codebuild:*` |

### CLI 仕様

```bash
pocket permissions list --stage=prod
pocket permissions list --stage=prod --format json
```

text 形式は 1 行 1 Action、json 形式は `{"actions": [...]}` 程度で worgue 側が
inline policy に組み込みやすい形を想定。

## タスクリスト
- [ ] pocket.toml の各セクションから条件を判定する関数を実装 (既存の Settings モデルを活用)
- [ ] `docs/permissions/aws.md` のテーブルを Python の dict / 関数として表現
- [ ] `pocket permissions list` サブコマンドを追加 (`--stage`, `--format`)
- [ ] テスト: 最小構成 / フル構成 / 各オプション単独で期待リストになるか
- [ ] `docs/permissions/aws.md` に CLI の使い方を追記 (worgue 連携の文脈も記載)
- [ ] ruff / pyright / pytest

## 次のステップ
- worgue 側 `feedback: 20260424-worgue-pocket-permissions-list` への response.md 作成 (accepted)
- 着手タイミングを別途調整 (low priority)

## 更新履歴
- 2026-04-28: 作成 (フィードバック `20260424-worgue-pocket-permissions-list` への対応として)
