# Changelog
全ての重要な変更はこのファイルに記録されます。

書き方は [Keep a Changelog](http://keepachangelog.com/en/1.0.0/) に基づきます。<br>
バージョンは [Semantic Versioning](http://semver.org/spec/v2.0.0.html) に従います。

## [?.?.?](https://github.com/worgue/magic-pocket/releases/tag/0.1.0) - unreleased
### Features
- :material-console: pocket django deploy でデプロイ + マイグレーションなどの管理コマンド実行。実行内容が決まらないためUnreleases。
- neon接続時のIP制限
- RDSの利用
- 環境ごとに異なるファイルを返すurlsの作成（robots.txtとfavicon.ico用）
- :material-console: pocket deploy でデプロイ（以下全てpocket.toml設定がある場合のみ）
    - EFS の作成
    - Lambdaに関わるCloudFormation作成
        - SecurityGroupIngress(LambdaからEFSへのアクセス権限)の作成
    - SQSを利用したdjango managementコマンドのバッチ処理
        - Queue, Dead letter queue, EventSourceMappingの作成

## [0.1.0](https://github.com/worgue/magic-pocket/releases/tag/0.1.0) - unreleased

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
- :material-console: pocket status で環境の作成状況を確認
- :material-console: pocket deploy でデプロイ
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
- :material-language-python: settings.pyでの情報取得
    - :simple-awssecretsmanager: AWS SecretsManagerから情報を取得(1)
    - :simple-toml: pocket.tomlからdjangoのSTORAGES, CACHESを取得
    - CF: CloudFormationのoutputからdjangoのALLOWED_HOSTSを取得
- :simple-toml: デプロイ環境ごとのdjango settings登録
- :material-console: pocket django manage `COMMAND` `ARGS` で管理コマンドを実行
- :material-console: pocket django storage upload `STORAGE` でローカルのFileSystemStorageから対象ステージのS3Boto3Storageへデータをsync
- :material-console: pocket resource awscontainer status で Lambda の作成状況を確認
- :material-console: pocket resource awscontainer secretsmanager list で SecretsManager の値を確認
- :material-console: pocket resource awscontainer yaml で CloudFormation用のyaml ファイルを確認
- :material-console: pocket resource awscontainer yaml-diff で CloudFormation用のyaml ファイルの差分を確認
- :material-console: pocket resource neon status で neon の作成状況を確認
- :material-console: pocket resource s3 status で S3バケットの作成状況を確認
- :material-console: pocket resource spa status で spaアップロード先S3バケットの作成状況を確認
