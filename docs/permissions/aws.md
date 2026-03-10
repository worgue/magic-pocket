# AWS 権限

`pocket deploy` の実行に必要な AWS IAM 権限の一覧です。

## コア権限（常に必要）

すべてのデプロイで必要となる権限です。

| サービス | 権限 | 用途 |
|----------|------|------|
| **CloudFormation** | `cloudformation:*` | インフラの作成・更新・削除 |
| **ECR** | `ecr:*` | コンテナイメージの管理・プッシュ |
| **Lambda** | `lambda:*` | 関数の作成・更新・実行 |
| **API Gateway V2** | `apigateway:*` | HTTP エンドポイントの管理 |
| **S3** | `s3:*` | ステートバケット・静的ファイル |
| **IAM** | `iam:CreateRole`, `iam:DeleteRole`, `iam:GetRole`, `iam:PutRolePolicy`, `iam:DeleteRolePolicy`, `iam:AttachRolePolicy`, `iam:DetachRolePolicy`, `iam:PassRole` | Lambda 実行ロールの管理 |
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
| `cloudfront:*` | ディストリビューション・Function・KVS の管理 |
| `acm:RequestCertificate`, `acm:DescribeCertificate`, `acm:DeleteCertificate` | カスタムドメインの SSL 証明書 |
| `route53:ChangeResourceRecordSets`, `route53:GetChange` | DNS レコードの自動作成 |

### VPC（`[awscontainer.vpc]` 使用時）

| 権限 | 用途 |
|------|------|
| `ec2:*`（VPC 関連） | VPC・サブネット・NAT Gateway・セキュリティグループの管理 |

### RDS（`[rds]` 使用時）

| 権限 | 用途 |
|------|------|
| `rds:*` | Aurora Serverless v2 クラスターの管理 |
| `ec2:*SecurityGroup*` | DB 用セキュリティグループの管理 |

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

### CodeBuild（`build.type = "codebuild"` 使用時）

| 権限 | 用途 |
|------|------|
| `codebuild:*` | ビルドプロジェクトの管理・実行 |

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
        "logs:*",
        "secretsmanager:*",
        "sts:GetCallerIdentity",
        "cloudfront:*",
        "acm:*",
        "route53:*",
        "ec2:*",
        "rds:*",
        "elasticfilesystem:*",
        "sqs:*",
        "ses:*",
        "codebuild:*"
      ],
      "Resource": "*"
    }
  ]
}
```

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
