# Changelog
全ての重要な変更はこのファイルに記録されます。

書き方は[Keep a Changelog](http://keepachangelog.com/en/1.0.0/)に基づきます。<br>
バージョンは[Semantic Versioning](http://semver.org/spec/v2.0.0.html)に従います。

## [?.?.?](https://github.com/worgue/magic-pocket/releases/tag/0.1.0) - unreleased
### Breaking Changes
- **AWS リソース系コマンドを `resource` group 配下へ再配置しました。** 旧トップレベル
  コマンド `pocket awscontainer` / `neon` / `tidb` / `dsql` / `rds` / `s3` / `vpc` /
  `cloudfront` 等は廃止され、`pocket resource awscontainer ...` のように `resource` を
  挟む新 path になりました（旧 path には alias を残していないため `No such command`
  で失敗します）。CLI を呼び出すスクリプト・上位ツールは新 path への追従が必要です。
  例: `pocket awscontainer reload-env` → `pocket resource awscontainer reload-env`。
- **`pocket.django.lambda_handlers.shell_handler` を `dangerous_shell_handler` に
  リネームしました。** 任意文字列を `shell=True` で実行する危険な handler である
  ことを名前で明示する目的です（capability 自体は維持）。`pocket.toml` の handler
  に旧名を指定している場合は新名への追従が必要です。SQS 駆動でコマンドを安全に
  完走させる用途には新設の `BaseCommandHandler` を利用してください。

### Bug Fixes
- `pocket permissions list` / `compute_actions()` に deploy が実際に必要とする
  Action の宣言漏れが 5 件あったのを修正。権限を絞ったデプロイ用ロールで
  該当構成を deploy すると `AccessDenied` になっていた:
  `dsql:*`（`[dsql]` 構成の cluster 操作）/ `scheduler:*`（`[scheduler]` 構成の
  CFn `AWS::Scheduler::Schedule` 作成）/ `tag:TagResources`・`tag:UntagResources`
  （外部 VPC 参照時の consumer タグ付け外し）/ `iam:ListRolePolicies`
  （CodeBuild ロール削除時の inline policy 列挙）/ `ssm:GetParameter`・
  `ssm:PutParameter`・`ssm:DeleteParameter`（`[rds]` の static master password
  管理。`secrets.store` とは独立に必要）。`action_groups()` に `dsql` /
  `scheduler` / `tag` グループを追加（キー追加のみの非破壊変更）。
- `POCKET_HOSTS` 環境変数が複数ホストをセパレータなしで連結していたのを
  カンマ区切りに修正（Python / Rust 両ランタイム）。apigateway 付き handler を
  2 つ以上定義すると、Django の `ALLOWED_HOSTS` に壊れたホスト名が入り
  2 つ目以降のホストが `DisallowedHost` になっていました（消費側の
  `add_or_append_env` は元々カンマ結合を前提としており、handler 1 つの構成では
  影響ありません）。
- `pocket resource awscontainer reload-env` / `status-env` が Lambda 関数名から
  namespace（既定 `pocket`）を取りこぼし、default namespace のデプロイで常に
  「Lambda function が見つかりません」で失敗していたのを修正（deploy 側と同じ
  正準 `function_name` を参照）。あわせて `status-env` の drift 警告が案内する
  コマンドが旧 path のままだったのを新 path に修正。

### Features
- :material-console: build once + commit hash 昇格をサポート。`pocket django build` で
  作業ツリーを一度ビルドして git commit hash（full）タグで ECR へ push し、
  `pocket promote` / `pocket django promote --commit-hash <sha>` で同一イメージを
  再ビルドなしで各ステージへ昇格できます（`:<stage>` タグの付け替え + Lambda 更新）。
  `[awscontainer].ecr_name` で ECR リポジトリ名を上書きでき、同一アカウント内の
  ステージ間でリポジトリを共有可能（明示指定したリポジトリは `pocket destroy` で
  削除されません）。通常の `pocket django deploy` の挙動は不変です。
- SQS 駆動の安全な command worker 基盤 `pocket.command_handler.BaseCommandHandler`
  を追加。SQS イベントを別 Lambda invocation の本体として受け、`build_argv` で固定
  した実行ファイルを `shell=False` の list argv で完走させ、出力 / ステータスを sink
  hook（`on_start` / `on_output` / `on_finish` / `on_crash`）に委譲します。long-running
  job を wsgi tier から worker tier に逃がす定石を共通化し、Lambda の freeze による
  「ステータスが running 固着」を構造的に防ぎます。crash 時は `try/finally` で
  `on_crash` を呼んでから例外を re-raise（握りつぶさない）。`dangerous_shell_handler`
  の安全な後継です。
- :material-console: `pocket django deploy`でデプロイ + マイグレーションなどの管理コマンド実行。実行内容が決まらないためUnreleases。
- :material-console: `pocket django resetdb`でデータベースの public スキーマをリセット（`DROP SCHEMA public CASCADE`）
- Neon接続時のIP制限
- RDSの利用

### Improvements
- deploy コードと `compute_actions()` の同期検証テストを追加
  (`tests/test_permissions_sync.py`)。boto3 呼び出しの AST 静的解析と
  CloudFormation テンプレートのリソース型解析の 2 系統で、deploy が必要とする
  Action の宣言漏れを CI で検知する（過去に 3 回再発した「権限を絞った deploy
  ロールが本番で AccessDenied」の構造的な再発防止。未知の CFn リソース型の
  追加時はテストが fail し権限の検討を強制する）。同期方針は
  `docs/permissions/aws.md` に記載。
- S3バケットのCORS設定を`pocket.toml`で宣言可能に（CloudFrontドメイン自動解決）
- `pocket destroy`がデフォルトでシークレットも削除するように変更（`--without-secrets`で残す）
- `pocket destroy`でCloudFrontスタック削除の完了を待機するように修正
- `pocket deploy`時にSSM/SMの不要なシークレットを自動クリーンアップ
- 環境ごとに異なるファイルを返すurlsの作成（`robots.txt`と`favicon.ico`用）
- :material-console: `pocket deploy`でデプロイ（以下全て`pocket.toml`に設定がある場合のみ）
    - EFSの作成
    - Lambdaに関わるCloudFormation作成
        - SecurityGroupIngress(LambdaからEFSへのアクセス権限)の作成
    - SQSを利用したdjango managementコマンドのバッチ処理
        - Queue, Dead letter queue, EventSourceMappingの作成
- :material-console: `pocket remove`でデプロイした環境を出来る限り削除し、削除できないものは表示

## [0.2.0](https://github.com/worgue/magic-pocket/releases/tag/0.2.0) - Unreleased
### Breaking Changes
- pocket.tomlの`[spa.origin_path_format]`のデフォルト値が変更されました。空文字から`/{stage}`に変更されました。

### Documentation
- Features/pocket.tomlを追加
- Features/コマンドライン、Tutorial/Simple Projectの説明を改善

## [0.1.1](https://github.com/worgue/magic-pocket/releases/tag/0.1.1) - 2024-10-16

**Full Changelog**: https://github.com/worgue/magic-pocket/compare/0.1.0...0.1.1

### Bug Fixes
- spa用のリソース作成時にリダイレクトするためのリソースが作られないバグを修正

## [0.1.0](https://github.com/worgue/magic-pocket/releases/tag/0.1.0) - 2024-10-11

### Dependencies
- click>=8.1.7
- tomli>=1.1.0 ; python_version < '3.11'
- mergedeep>=1.3.4
- pydantic>=2.5.3
- pydantic-settings>=2.1.0
- boto3>=1.34.28
- rich>=13.7.0
- deepdiff>=6.7.1
- pyyaml>=6.0.1
- python-on-whales>=0.68.0
- jinja2>=3.1.3
- awslambdaric>=2.0.10
- apig_wsgi>=2.18.0
- django-storages>=1.14.2,!=1.14.3

### Features
- :material-console: `pocket status`で環境の作成状況を確認
- :material-console: `pocket deploy`でデプロイ
    - :material-database: NeonへのDB作成
    - :simple-awssecretsmanager: SecretsManagerへのNeon DBの接続情報登録
    - :simple-amazons3: ストレージ用にS3を作成し権限を設定
    - :simple-docker: コンテナイメージを作成しECRへアップロード
    - :material-language-javascript: フロントエンドSPAのビルドデータをアップロードするS3を作成
    - CF: Lambdaに関わるCloudFormationを登録・更新
        - LambdaのIAM Role, SecurityGroup, Function
        - API Gateway の LogGroup, Api, ApiGatewayManagedOverrides, Route, Integration, lambda Permission, Certificate, DomainName, RecordSet, ApiMapping
        - API Gatewayのhost名のoutput
    - CF: SPAに関わるCloudFormationを登録・更新
        - CloudFrontのOriginAccessControl, Certificate, CloudFrontFunction, Distribution, RecordSet
- :material-language-python: `settings.py`での情報取得
    - :simple-awssecretsmanager: AWS SecretsManagerから情報を取得(1)
    - :simple-toml: `pocket.toml`からdjangoの`STORAGES`, `CACHES`を取得
    - CF: CloudFormationのoutputからdjangoの`ALLOWED_HOSTS`を取得
- :simple-toml: デプロイ環境ごとのdjango settings登録
- :material-console: `pocket django manage COMMAND ARGS` で管理コマンドを実行
- :material-console: `pocket django storage upload STORAGE` でローカルのFileSystemStorageから対象ステージのS3Boto3Storageへデータをsync
- :material-console: `pocket resource awscontainer status`でLambdaの作成状況を確認
- :material-console: `pocket resource awscontainer secretsmanager list`でSecretsManagerの値を確認
- :material-console: `pocket resource awscontainer yaml`でCloudFormation用のyaml ファイルを確認
- :material-console: `pocket resource awscontainer yaml-diff`でCloudFormation用のyamlファイルの差分を確認
- :material-console: `pocket resource neon status`でNeonの作成状況を確認
- :material-console: `pocket resource s3 status`でS3バケットの作成状況を確認
- :material-console: `pocket resource spa status`でspaアップロード先S3バケットの作成状況を確認
