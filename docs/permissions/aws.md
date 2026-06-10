# AWS 権限

`pocket deploy` の実行に必要な AWS IAM 権限の一覧です。

## コア権限（常に必要）

すべてのデプロイで必要となる権限です。

| サービス | 権限 | 用途 |
|----------|------|------|
| **CloudFormation** | `cloudformation:*` | インフラの作成・更新・削除 |
| **ECR** | `ecr:*` | コンテナイメージの管理・プッシュ |
| **Lambda** | `lambda:*` | 関数の作成・更新・実行。`pocket resource awscontainer reload-env` の side-channel env 更新 (`UpdateFunctionConfiguration`) も含む |
| **API Gateway V2** | `apigateway:*` | HTTP エンドポイントの管理 |
| **S3** | `s3:*` | ステートバケット・静的ファイル |
| **IAM** | `iam:CreateRole`, `iam:DeleteRole`, `iam:GetRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`, `iam:PassRole`, `iam:TagRole`, `iam:UntagRole`, `iam:ListRoleTags`, `iam:ListRolePolicies` | Lambda 実行ロールの管理（CFn が LambdaRole に Tag を付与するため Tag 系 Action も必要。`ListRolePolicies` は CodeBuild ロール削除時の inline policy 列挙） |
| **CloudWatch Logs** | `logs:*` | ログの作成・参照 |
| **Secrets Manager** | `secretsmanager:*` | シークレットの生成・保存・取得 |
| **SSM Parameter Store** | `ssm:GetParameter`, `ssm:PutParameter`, `ssm:DeleteParameters`, `ssm:GetParametersByPath` | パラメータストア利用時 |
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

### VPC（`[awscontainer.vpc]` 使用時）

| 権限 | 用途 |
|------|------|
| `ec2:*`（VPC 関連） | VPC・サブネット・NAT Gateway・セキュリティグループの管理 |

### RDS（`[rds]` 使用時）

| 権限 | 用途 |
|------|------|
| `rds:*` | Aurora Serverless v2 クラスターの管理 |
| `ec2:*SecurityGroup*` | DB 用セキュリティグループの管理 |
| `ssm:GetParameter`, `ssm:PutParameter`, `ssm:DeleteParameter` | static master password の SSM パラメータ管理（`secrets.store` の設定とは独立に必要） |

### EFS（`[awscontainer.vpc.efs]` 使用時）

| 権限 | 用途 |
|------|------|
| `elasticfilesystem:*` | ファイルシステム・マウントターゲット・アクセスポイントの管理 |

### SQS（ハンドラーに `sqs` 設定時）

| 権限 | 用途 |
|------|------|
| `sqs:*` | キューの作成・メッセージ操作 |

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

### EventBridge Scheduler（`[scheduler]` 使用時）

| 権限 | 用途 |
|------|------|
| `scheduler:*` | CFn によるスケジュール（`AWS::Scheduler::Schedule`）の作成・更新・削除 |

### Resource Groups Tagging（外部 VPC 参照 = `awscontainer.vpc.manage = false` 時）

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
        "ses:*",
        "codebuild:*",
        "dsql:*",
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
#  "tag": [...]}

baseline = set(groups["core"]) | set(groups["cloudfront"])  # 例: 被覆対象の group を選ぶ
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

## Permissions Boundary

組織のセキュリティポリシーで IAM ロールに Permissions Boundary が必要な場合、`pocket.toml` で設定できます。

```toml
[awscontainer]
permissions_boundary = "arn:aws:iam::123456789012:policy/MyBoundaryPolicy"
```

この設定は以下のロールに適用されます：

- **Lambda 実行ロール** — CloudFormation で作成されるロールに `PermissionsBoundary` が設定されます
- **CodeBuild ロール** — `build.backend = "codebuild"` 使用時、ビルド用ロールにも同じ Boundary が適用されます

CodeBuild ロールのみ別の Boundary を指定したい場合は、環境変数 `CODEBUILD_PERMISSIONS_BOUNDARY` で上書きできます。
