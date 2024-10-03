# Features

設定ファイル`pocket.toml`を読み込み、デプロイ先のリソースを管理します。
主な機能は2つで、CLIコマンドでの環境作成と、Django側での環境情報の読み込みです。

## CLIコマンド

以下全てのコマンドは、`--stage`オプションで操作ターゲットを指定できます。

以下のコード例は、dev環境を操作する場合です。

### pocket status
```bash
pocket status --stage=dev
```
環境の作成状況を確認します。

### pocket deploy
```bash
pocket deploy --stage=dev
```
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

### pocket django manage `COMMAND` `ARGS`
```bash
pocket django manage collectstatic --noinput --stage=dev
```
django management commandを実行

### pocket django storage upload `STORAGE`
```bash
pocket django storage upload static --stage=dev
```
ローカルのFileSystemStorageから対象ステージのS3Boto3Storageへデータをsync。STORAGEはsettings.pyのSTORAGESのキー名です。


### pocket resource awscontainer status
```bash
pocket resource awscontainer status --stage=dev
```
Lambdaの作成状況を確認

### pocket resource awscontainer secretsmanager list
```bash
pocket resource awscontainer secretsmanager list --stage=dev
```
SecretsManagerの値を確認

### pocket resource awscontainer yaml
```bash
pocket resource awscontainer yaml --stage=dev
```
CloudFormation用のyamlファイルを確認

### pocket resource awscontainer yaml-diff
```bash
pocket resource awscontainer yaml-diff --stage=dev
```
CloudFormation用のyamlファイルの差分を確認

### pocket resource neon status
```bash
pocket resource neon status --stage=dev
```
Neonの作成状況を確認

### pocket resource s3 status
```bash
pocket resource s3 status --stage=dev
```
S3バケットの作成状況を確認

### pocket resource spa status
```bash
pocket resource spa status --stage=dev
```
SPAアップロード先S3バケットの作成状況を確認

## Djangoからの環境情報取得
作成したリソース情報をpythonから取得することが出来ます。
また、pocket.tomlに環境ごとに異なるdjango settingsを、直接指定することも可能です。

### STORAGESとCACHES
```python
from pocket.django.utils import get_caches, get_storages

STORAGES = get_storages()
CACHES = get_caches()
```

:simple-toml: pocket.tomlからデータを取得して返します。

??? info "値が設定されていない場合はdjangoのデフォルト値が返されます"
    **STORAGES**

    ```json
    {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}
    }
    ```

    **CACHES**

    ```json
    {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
    ```

!!! warning "ローカルなど、magic-pocketデプロイ環境以外の挙動"
    ローカルなどで、これらの関数が呼ばれた場合、`pocket.toml`の`[general.django_fallback.storages]`という設定が使われます。
    何も書かない場合の設定は、以下と同じです。

    ```toml
    [general.django_fallback.storages]
    default = { store = "filesystem" }
    staticfiles = { store = "filesystem", static = true }
    [general.django_fallback.caches]
    default = { store = 'locmem' }
    ```

    デプロイされた環境は、`POCKET_STAGE`という環境変数を見ることで判定します。

    `POCKET_STAGE`を変えてしまうと影響が大きいため、ローカルでS3ストレージなどを使う場合は、`pocket.toml`の`general.django_fallback`で対応するのが良いでしょう。

### 環境変数へのデータ登録
```python
from pocket.django.runtime import set_envs

set_envs()
```

上記のコマンドは以下の情報を読み込みsettingsに設定します。

<div class="annotate" markdown>
- :simple-awssecretsmanager: AWS SecretsManagerから情報を取得(1)
- CF: CloudFormationのoutputからdjangoのALLOWED_HOSTSを取得
</div>
1.  自動生成されたパスワードや、Neonから取得したDB接続情報などを、SecretsManagerに登録します。
    また、SecretsManagerのarnを利用して、環境変数に登録することも可能です。

### pocket.tomlへdjango settingsを直接設定
settings.pyから、直接tomlの設定を読み込ませることも可能です。
試しに、`DEFAULT_FROM_EMAIL`と`CORS_ALLOWED_ORIGINS`を設定してみます。

```toml
# local
[general.django_fallback.settings]
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:4173",
]

# dev
[dev.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Magic Pocket Test Version" <magic-pocket-test@example.com>'
CORS_ALLOWED_ORIGINS = ["https://dev.example.com"]

# prd
[prd.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Magic Pocket" <noreply@example.com>'
CORS_ALLOWED_ORIGINS = ["https://www.example.com"]
```

`settings.py`から読み込む方法は、以下の通りです。
```python
from pocket.django.runtime import get_django_settings

vars().update(get_django_settings().items())
```

この方法は特に、ネストした辞書形式のsettingsを読み込む際に便利です。
