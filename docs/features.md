# Features

設定ファイル`pocket.toml`を読み込み、デプロイ先のリソースを管理します。
主な機能は2つで、コマンドラインでの環境作成と、Django側での環境情報の読み込みです。

## コマンドライン

### pocket status

環境の作成状況を確認します。

### pocket deploy

デプロイします。具体的には以下の作業を行います。

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

### pocket django

djangoに関連する複数のコマンドが用意されています。

`pocket django manage COMMAND ARGS`
:    django management commandを実行

`pocket django storage upload STORAGE`
:    ローカルのFileSystemStorageから対象ステージのS3Boto3Storageへデータをsync

### pocket resource

デプロイされるリソースを個別に確認するコマンドも用意されています。

`pocket resource awscontainer status`
:    Lambdaの作成状況を確認

`pocket resource awscontainer secretsmanager list`
:    SecretsManagerの値を確認

`pocket resource awscontainer yaml`
:    CloudFormation用のyamlファイルを確認

`pocket resource awscontainer yaml-diff`
:    CloudFormation用のyamlファイルの差分を確認

`pocket resource neon status`
:    Neonの作成状況を確認

`pocket resource s3 status`
:    S3バケットの作成状況を確認

`pocket resource spa status`
:    SPAアップロード先S3バケットの作成状況を確認

## Djangoからの環境情報取得
作成したリソース情報をpythonから取得することが出来ます。
また、pocket.tomlに環境ごとに異なるdjango settingsを、直接指定することも可能です。
以下をsettings.pyの最初に追加してください。

```python
from pocket.django.runtime import init

vars().update(init())
```

上記のコマンドは以下の情報を読み込みsettingsに設定します。

<div class="annotate" markdown>
- :simple-awssecretsmanager: AWS SecretsManagerから情報を取得(1)
- :simple-toml: pocket.tomlからdjangoのSTORAGES, CACHESを取得
- CF: CloudFormationのoutputからdjangoのALLOWED_HOSTSを取得
</div>
1. Neon DB情報や自動生成されたパスワード

!!! warning annotate "init関数"
    `init`はいくつかの関数を呼んでいますが、これらの挙動は定まっていません。

    `set_user_secrets_from_secretsmanager`
    :    SecretsManagerからデータを取得して環境変数に登録します。

    `set_env_from_resources`
    :    magic-pocketが作成したリソース情報を環境変数に登録します。

    `set_django_env`
    :    django用に環境変数のaliasを設定します。

    `get_django_settings`
    :    pocket.tomlに直接設定された(1)、環境ごとのdjango settingsを取得します。

    `get_storages`
    :    pocket.tomlに設定された(2)、ストレージ情報を取得します。

    `get_caches`
    :    pocket.tomlに設定された(3)、キャッシュ情報を取得します。

1. [stage.]awscontainer.django.settings.SOME_SETTINGSの形でtomlに設定されているもの。
2. [stage.]awscontainer.django.storagesとして設定可能です。
3. [stage.]awscontainer.django.cachesとして設定可能です。
