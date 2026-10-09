# AWS 権限

`pocket deploy` の実行に必要な AWS IAM 権限の一覧です。

## コア権限（常に必要）

すべてのデプロイで必要となる権限です。

| サービス | 権限 | 用途 |
|----------|------|------|
| **CloudFormation** | `cloudformation:*` | インフラの作成・更新・削除 |
| **ECR** | `ecr:*` | コンテナイメージの管理・プッシュ |
| **Lambda** | `lambda:*` | 関数の作成・更新・実行。`pocket resource container reload-env` の side-channel env 更新 (`UpdateFunctionConfiguration`) も含む |
| **API Gateway V2** | `apigateway:*` | HTTP エンドポイントの管理 |
| **S3** | `s3:*` | ステートバケット・静的ファイル |
| **IAM** | `iam:CreateRole`, `iam:DeleteRole`, `iam:GetRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`, `iam:PassRole`, `iam:TagRole`, `iam:UntagRole`, `iam:ListRoleTags`, `iam:ListRolePolicies` | Lambda 実行ロールの管理（CFn が LambdaRole に Tag を付与するため Tag 系 Action も必要。`ListRolePolicies` は pocket が API で作るロール (CodeBuild / AWS Backup) の削除時の inline policy 列挙） |
| **CloudWatch Logs** | `logs:*` | ログの作成・参照 |
| **Secrets Manager** | `secretsmanager:*` | シークレットの生成・保存・取得 |
| **SSM Parameter Store** | `ssm:GetParameter`, `ssm:PutParameter`, `ssm:DeleteParameter`, `ssm:DeleteParameters`, `ssm:GetParametersByPath` | パラメータストア利用時（`DeleteParameter` は dsql endpoint の unpublish / `migrate-secret-paths` の単一パラメータ削除用） |
| **STS** | `sts:GetCallerIdentity` | アカウント ID の取得 |

!!! note "Secrets Manager と SSM"
    シークレットストアの設定（`secrets.store`）に応じて、Secrets Manager または SSM のいずれかが必要です。デフォルトは Secrets Manager です。

## オプション権限

`pocket.toml` の設定に応じて追加で必要となる権限です。

### CloudFront（`[cloudfront]` 使用時）

| 権限 | 用途 |
|------|------|
| `cloudfront:*` | ディストリビューション・Function・KVS リソースの管理 |
| `cloudfront-keyvaluestore:*` | SPA token gating（`require_token`）構成で deploy が KVS へ `token_secret` を書き込む（`DescribeKeyValueStore` / `PutKey`）。`cloudfront:*` とは別 service prefix のため別途必要 |
| `acm:RequestCertificate`, `acm:DescribeCertificate`, `acm:DeleteCertificate` | カスタムドメインの SSL 証明書 |
| `route53:ListHostedZones` | ドメインから hosted zone の自動検索（`hosted_zone_id_override` 未設定時） |
| `route53:ChangeResourceRecordSets`, `route53:GetChange` | DNS レコードの自動作成 |

### CloudFront WAF（`[cloudfront.<name>.waf]` 使用時）

| 権限 | 用途 |
|------|------|
| `wafv2:*` | us-east-1 の WebACL / IPSet の管理。`pocket deploy` で CFn 経由作成、`pocket waf ip ...` CLI で IPSet の中身を side-channel 更新 |

### VPC（`[container.main.vpc]` 使用時）

| 権限 | 用途 |
|------|------|
| `ec2:*`（VPC 関連） | VPC・サブネット・NAT Gateway・セキュリティグループの管理 |

### RDS（`[rds]` 使用時）

| 権限 | 用途 |
|------|------|
| `rds:*` | Aurora Serverless v2 クラスターの管理 |
| `ec2:*SecurityGroup*` | DB 用セキュリティグループの管理 |
| `ssm:GetParameter`, `ssm:PutParameter`, `ssm:DeleteParameter` | static master password の SSM パラメータ管理（`secrets.store` の設定とは独立に必要） |
| `secretsmanager:DescribeSecret`, `secretsmanager:RotateSecret` | managed secret のローテーション窓（`rotation_schedule`）の差分検出と適用（`secrets.store` の設定とは独立に必要） |

### EFS（`[container.main.vpc.efs]` 使用時）

| 権限 | 用途 |
|------|------|
| `elasticfilesystem:*` | ファイルシステム・マウントターゲット・アクセスポイントの管理 |

### SQS（ハンドラーに `sqs` 設定時）

| 権限 | 用途 |
|------|------|
| `sqs:*` | キューの作成・メッセージ操作 |

### SQS DLQ アラート（`sqs.dead_letter_alert` が enabled のハンドラーがある時）

| 権限 | 用途 |
|------|------|
| `sns:*` | 通知 topic / email 購読の作成と、deploy 後の購読状態照会 |
| `cloudwatch:*` | DLQ 監視の CloudWatch アラーム作成 |

### SES（`[ses]` 使用時）

| 権限 | 用途 |
|------|------|
| `ses:SendEmail`, `ses:SendRawEmail` | メール送信（Lambda 実行ロールに付与） |

### CodeBuild（`build.backend = "codebuild"` 使用時）

| 権限 | 用途 |
|------|------|
| `codebuild:*` | ビルドプロジェクトの管理・実行 |

### DSQL（`[dsql]` 使用時）

| 権限 | 用途 |
|------|------|
| `dsql:*` | DSQL クラスターの作成・参照・削除 |

endpoint の publish（stored user secret 正準パスへの書き込み・削除）はコア権限の
Secrets Manager / SSM（`secrets.store` に応じた側）でカバーされます。

### AWS Backup（`[dsql]` 使用時、または `[backup.rds]` 宣言時）

dsql はオンデマンドバックアップ / restore CLI が `[backup]` 宣言の有無に関わらず使えるため常にこの群が必要で、rds は `[backup.rds]` を宣言した時のみ必要です（PITR は `rds:*` のクラスタ属性で AWS Backup を使いません）。

| 権限 | 用途 |
|------|------|
| `backup:*` | 定期バックアップ（`[backup.dsql]` / `[backup.rds]` の plan / selection の provision と destroy）、オンデマンドバックアップ（`pocket resource dsql backup` / `backup-status`）、復元（`pocket resource dsql restore` / `restore-status`）、バックアップデータの削除（`pocket backup cleanup`。`[backup]` の `deletable = true` 宣言 + 明示確認の経路のみ） |
| `backup-storage:MountCapsule` | [`CreateBackupVault` の必須付随権限](https://docs.aws.amazon.com/aws-backup/latest/devguide/create-a-vault.html)（Resource は `*` 固定）。無いと vault（pocket 管理の `{stage}-{project}-{namespace}-backup`）の初回作成が AccessDenied になります。サービスロールの ensure・受け渡しはコア権限の iam 系でカバー |
| `kms:CreateGrant`, `kms:Decrypt`, `kms:DescribeKey`, `kms:GenerateDataKey`, `kms:RetireGrant` | `CreateBackupVault` が既定の AWS managed key（`aws/backup`）を vault に紐付ける際に呼び出し元へ要求する KMS 権限。無いと `backup:*` + `backup-storage:MountCapsule` を満たしていても vault の初回作成が AccessDenied になります（AWS のエラーは "Creating a backup vault requires backup-storage and KMS permissions"） |

!!! note "バックアップデータの削除を権限で禁止したい場合"
    `backup:*` にはバックアップ**データ**の削除（`DeleteRecoveryPoint` / `DeleteBackupVault`）も含まれます。pocket 自体は `[backup]` の `deletable = true` 宣言と利用者の明示確認を経ない限りデータを削除しませんが、組織のポリシーとして権限レベルで禁止したい場合は、deploy role 側で明示 Deny を足してください。

    ```json
    {
      "Effect": "Deny",
      "Action": ["backup:DeleteRecoveryPoint", "backup:DeleteBackupVault"],
      "Resource": "*"
    }
    ```

    より強い保護（root 含め誰も消せない）が必要なら AWS Backup Vault Lock を検討してください。

### EventBridge Scheduler（`[scheduler]` 使用時）

| 権限 | 用途 |
|------|------|
| `scheduler:*` | CFn によるスケジュール（`AWS::Scheduler::Schedule`）の作成・更新・削除 |

### Resource Groups Tagging（外部 VPC 参照 = `container.<name>.vpc.manage = false` 時）

| 権限 | 用途 |
|------|------|
| `tag:TagResources`, `tag:UntagResources` | 共有 VPC スタックへの consumer タグの付け外し（deploy / destroy 時） |

## IAM ポリシー例

### 最小構成（Django + Neon）

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudformation:*",
        "ecr:*",
        "lambda:*",
        "apigateway:*",
        "s3:*",
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:ListRoleTags",
        "iam:ListRolePolicies",
        "logs:*",
        "secretsmanager:*",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

### フル構成

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "cloudformation:*",
        "ecr:*",
        "lambda:*",
        "apigateway:*",
        "s3:*",
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:AttachRolePolicy",
        "iam:DetachRolePolicy",
        "iam:PassRole",
        "iam:TagRole",
        "iam:UntagRole",
        "iam:ListRoleTags",
        "iam:ListRolePolicies",
        "logs:*",
        "secretsmanager:*",
        "ssm:*",
        "sts:GetCallerIdentity",
        "cloudfront:*",
        "cloudfront-keyvaluestore:*",
        "acm:*",
        "route53:*",
        "ec2:*",
        "rds:*",
        "elasticfilesystem:*",
        "sqs:*",
        "sns:*",
        "cloudwatch:*",
        "ses:*",
        "codebuild:*",
        "dsql:*",
        "backup:*",
        "backup-storage:MountCapsule",
        "kms:CreateGrant",
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:GenerateDataKey",
        "kms:RetireGrant",
        "scheduler:*",
        "tag:TagResources",
        "tag:UntagResources"
      ],
      "Resource": "*"
    }
  ]
}
```

## CLI: `pocket permissions list`

`pocket.toml` の構成から、上記の表に基づいて必要な AWS Action 一覧を出力する CLI を提供しています。
デプロイ用 IAM Role の inline policy を組み立てる際などに使用します。

```bash
# テキスト形式（1 行 1 Action）
pocket permissions list --stage=prod

# JSON 形式（{"actions": [...]}）
pocket permissions list --stage=prod --format=json
```

出力に含まれる Action は、本ページ上部の「コア権限」「オプション権限」テーブルと同じ粒度
（`cloudformation:*` `s3:*` などのワイルドカード中心）で、`pocket.toml` 内の `[cloudfront]`
`[rds]` `[ses]` 等の有無や `secrets.store` / `build.backend` の値に応じて自動的に増減します。
Action レベルの細かい絞り込み (Least Privilege) は別途検討対象です。

## Python API

CLI と同じ計算ロジックを Python から直接呼び出せます。デプロイ用 IAM Role を
プロビジョニングする外部ツールが、inline policy の Action を組み立てる用途を想定した
public API です。

| 関数 | 役割 |
|------|------|
| `pocket.permissions.compute_actions(settings)` | `Settings` から必要 Action 一覧 (`list[str]`) を算出する。CLI `pocket permissions list` の実体。 |
| `pocket.permissions.action_groups()` | feature group ごとの Action を `settings` 非依存で名前付きで返す (`dict[str, list[str]]`)。 |

`action_groups()` は、外部ツール側で「自分が常時付与している baseline 権限が
magic-pocket の付与群を被覆できているか」を CI で guard したい場合などに使います。

```python
import pocket.permissions as perms

groups = perms.action_groups()
# {"core": [...], "ssm": [...], "secretsmanager": [...], "cloudfront": [...],
#  "waf": [...], "vpc": [...], "rds": [...], "efs": [...], "sqs": [...],
#  "ses": [...], "codebuild": [...], "dsql": [...], "scheduler": [...],
#  "tag": [...], "external_roles": []}

baseline = set(groups["core"]) | set(
    groups["cloudfront"]
)  # 例: 被覆対象の group を選ぶ
```

- キー名は **安定 (rename しない) ことを public API として保証** しています。
  `_`-prefixed の内部定数を直接 import せず、本関数を使ってください。
- `"core"` のみ常時付与群で、残りは `pocket.toml` の設定に応じて付与される
  feature-gated 群です（`compute_actions` はこの dict を単一のソースとして条件連結します）。
- 返り値は都度コピーなので、呼び出し側が変更しても本体に影響しません。

## deploy code と permissions.py の同期方針

本ページのテーブル（= `pocket.permissions` の action group）は、deploy コードが
実際に必要とする AWS Action を手書きでコード化した「真実の源」です。deploy コードが
新しい AWS API や CFn リソース型を触り始めたときにここが更新されないと、権限を
絞ったデプロイ用ロールが本番で `AccessDenied` になります。

この「二層ずれ」を防ぐため、`tests/test_permissions_sync.py` が以下を CI で検証します:

1. **boto3 静的解析** — `pocket/` と `pocket_cli/` の `boto3.client("X")` と
   そのメソッド呼び出しを AST 抽出し、対応する IAM Action が action group の
   いずれかに宣言されていることを確認（新しい service prefix の取りこぼしも検知）。
2. **CFn テンプレート解析** — CloudFormation テンプレートの全 `Type: AWS::*` を
   抽出し、リソース型ごとに必要な deploy Action が宣言されていることを確認。
   **未知のリソース型が現れるとテストが fail** し、権限の検討を強制します。

deploy コードに新しい boto3 呼び出し / CFn リソース型を追加してこのテストが
fail した場合は、次の順で対応してください:

1. その操作にデプロイ用ロールの権限が必要か判断する
   （runtime = Lambda 実行ロール側のみで使う呼び出しは、テスト内の
   `_EXCLUDED_CALLS` に理由コメント付きで除外できます）
2. 必要なら `pocket/permissions.py` の該当 action group（無ければ新グループ）に
   Action を追加する
3. 本ページの対応するテーブルにも同じ Action を追記する

## IAM role を事前に作る（`[iam] external_roles`） {#external-roles}

既定では、pocket は deploy 中に IAM role を作ります（Lambda 実行 role と scheduler role は
CloudFormation、CodeBuild と AWS Backup の role は API）。そのため deploy 権限に
`iam:CreateRole` / `iam:PutRolePolicy` などの IAM の書き込みが要ります。

組織のルールで deploy 権限（CI や開発者のキー）に IAM の書き込みを持たせられない場合は、
role を別の経路（IAM の管理者、Terraform、社内のプロビジョニング処理など）で事前に作り、
pocket にはそれを参照させます。

```toml
[iam]
external_roles = true
```

### 流れ

1. `pocket permissions roles --stage <stage>` で、その stage に必要な role の定義を出力する
2. 出力どおりに role を作る（または更新する）
3. `pocket deploy` する。deploy は何かを変更する前に、role が存在し pocket の service を信頼しているかを検査する

出力は「pocket が必要とする権限の仕様」です。role の policy をどう組むか（出力をそのまま使う、
組織の標準セットに含める、など）は role の持ち主が決め、必要な権限が足りていることも持ち主が
保証します。pocket は policy の中身を検査しません。`pocket.toml` を変えて必要な権限が増えたとき
（`[container.*.iam]` の変更、`[rds]` / `[dsql]` / `[scheduler]` の追加など）は、1 の出力を見て
role を更新してから deploy してください。更新せずに deploy すると、deploy は通りますが Lambda などが
実行時に `AccessDenied` になります。

### `pocket permissions roles` の出力

```bash
pocket permissions roles --stage=prod
pocket permissions roles --stage=prod --account-id=123456789012  # sts を呼ばない
```

```json
{
  "stage": "prod",
  "region": "ap-northeast-1",
  "account_id": "123456789012",
  "external_roles": true,
  "roles": [
    {
      "name": "prod-myprj-pocket-main-lambda-role",
      "arn": "arn:aws:iam::123456789012:role/prod-myprj-pocket-main-lambda-role",
      "assume_role_policy": {"Version": "2012-10-17", "Statement": [...]},
      "managed_policy_arns": ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"],
      "inline_policies": {"prod-myprj-pocket-access-cloudformation": {...}}
    }
  ]
}
```

- `assume_role_policy` は `CreateRole` の `AssumeRolePolicyDocument`、`inline_policies` の各項目は
  `PutRolePolicy` の `PolicyName` / `PolicyDocument` にそのまま渡せます（CloudFormation の変数は展開済み）。
  出力どおりに作るのが最も簡単ですが、同じ権限を含む別の policy で組んでも構いません
- 出力は `pocket.toml` と account / region だけで決まり、AWS 上のリソースの状態には依存しません。
  DB（RDS / DSQL）をまだ作っていない stage でも、最終形の role を先に作れます。DB の権限は、
  作成時に AWS が乱数で決める ARN ではなく、タグ（RDS の secret に付く
  `aws:rds:primaryDBClusterArn`、DSQL cluster の `Name`）で対象を絞っています

role は stage の構成に応じて次のものが出ます。

| role | 名前 | 出る条件 |
|---|---|---|
| Lambda 実行 | `{prefix}{container}-lambda-role` | container ごと |
| scheduler | `{prefix}{container}-scheduler-role` | その container に schedule があるとき |
| CodeBuild | `{prefix}codebuild-role` | `build.backend = "codebuild"`（既定）の container があるとき |
| AWS Backup | `{prefix}backup-role` | `[dsql]` があるとき、または `[backup.rds]` を宣言したとき |

`{prefix}` は `{stage}-{project}-{namespace}-`（例: `prod-myprj-pocket-`）です。

### deploy の検査

deploy の最初に、各 role について次を確認します。1 つでも欠けていれば、何も変更せずに止まります。

- role が存在する
- 信頼ポリシーが出力の service（`lambda.amazonaws.com` 等）に `sts:AssumeRole` を許可している

policy の中身（managed / inline）は検査しません。pocket の版更新で policy の文面が変わるたびに
role の更新を求めると、IAM を別に管理する組織では deploy が止まり続けるためです。権限の充足は
role の持ち主の責任で、足りなければ実行時の `AccessDenied` として現れます。

### deploy 権限の違い

`pocket permissions list` は、`external_roles = true` の stage では IAM の書き込み
（`iam:CreateRole` / `DeleteRole` / `PutRolePolicy` / `DeleteRolePolicy` / `AttachRolePolicy` /
`DetachRolePolicy` / `TagRole` / `UntagRole` / `ListRoleTags` / `ListRolePolicies`）を出しません。
`iam:GetRole` と `iam:PassRole` は残ります（`PassRole` は上の role に絞って構いません）。
検査は role の存在と信頼ポリシーだけなので、policy を読む権限は要りません。

`pocket destroy` は external の role を削除しません（role の持ち主は利用者です）。

### 既存の stage を切り替える

Lambda 実行 role と scheduler role は、pocket が作ったもの（CloudFormation の管理下）とは
**別の名前**になります。切り替えの deploy で、CloudFormation が参照先を新しい role に替えてから
古い role を削除するため、途中で Lambda が止まることはありません。CodeBuild と AWS Backup の
role は名前が同じなので、pocket が作った role をそのまま引き継げます。

1. `pocket.toml` に `[iam] external_roles = true` を書く
2. `pocket permissions roles` の出力を参考に role を作る
3. `pocket deploy` する。**この deploy は IAM の書き込み権限が残った状態で実行してください**。
   CloudFormation が古い role を削除する際に `iam:DeleteRole` などを使います
4. deploy 権限を `pocket permissions list` の新しい出力に絞る

## Permissions Boundary

組織のセキュリティポリシーで IAM ロールに Permissions Boundary が必要な場合、`pocket.toml` で設定できます。

```toml
[container.main]
permissions_boundary = "arn:aws:iam::123456789012:policy/MyBoundaryPolicy"
```

この設定は以下のロールに適用されます：

- **Lambda 実行ロール** — CloudFormation で作成されるロールに `PermissionsBoundary` が設定されます
- **CodeBuild ロール** — `build.backend = "codebuild"` 使用時、ビルド用ロールにも同じ Boundary が適用されます
- **AWS Backup / DSQL のサービスロール** — `[backup.*]` などで pocket が API から直接作るロールにも同じ Boundary が適用されます

`pocket.toml` に書く代わりに、環境変数 `POCKET_PERMISSIONS_BOUNDARY_ARN` でも指定できます（環境変数が優先されます）。
CodeBuild ロールのみ別の Boundary を指定したい場合は、環境変数 `CODEBUILD_PERMISSIONS_BOUNDARY` で上書きできます。

!!! warning "deploy を実行するすべての環境に Boundary を渡してください"
    deploy 権限が「Boundary 付きのロール作成だけ」を許している account では、Boundary の指定が無い deploy は
    `iam:CreateRole` の `AccessDenied` で止まります。既存ロールの更新だけの deploy は通るため、pocket の更新で
    新しいロールが増えたとき（例: 0.37.0 の stage 単位の backup サービスロール）に初めて表に出ます。
    環境変数で渡している場合は、手元だけでなく CI の deploy job にも同じ環境変数を設定してください。


## メール受信の追加権限

`[inbound.*]` は `inbound` グループと、`sqs_alert` グループ（SNS・CloudWatch）を要求します。
S3/SQS/CloudFormationは既存のcore/sqsグループを利用します。送信用 `ses` グループとは独立です。
`inbound` グループの内容は次のとおりです。

| Action | 用途 |
|---|---|
| `ses:DescribeActiveReceiptRuleSet` / `ses:DescribeReceiptRuleSet` / `ses:DescribeReceiptRule` | active setと既存ルールの照会 |
| `ses:CreateReceiptRule` / `ses:UpdateReceiptRule` / `ses:DeleteReceiptRule` / `ses:SetReceiptRulePosition` | 自分の受信ルールの作成・更新・削除 |
| `ses:GetEmailIdentity` | ドメイン検証の状態確認、既存identityの検知 |
| `ses:CreateEmailIdentity` / `ses:DeleteEmailIdentity` / `ses:PutEmailIdentity*Attributes` / `ses:TagResource` / `ses:UntagResource` | 受信ドメインのstack（`AWS::SES::EmailIdentity`）の作成・更新・削除 |
| `route53:ListHostedZones` / `route53:ChangeResourceRecordSets` / `route53:GetChange` | DKIM CNAMEとMXの作成（`manage_dns = false` では使いません） |

共有のreceipt rule setの作成・有効化
（`ses:CreateReceiptRuleSet` / `ses:SetActiveReceiptRuleSet`）はpocketでは行わないため、
pocketの実行主体には不要です。account/regionの管理者が別途実施します。
詳しくは[メール受信](../inbound.md)を参照してください。
