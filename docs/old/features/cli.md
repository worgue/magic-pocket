# コマンドライン

magic-pocketには、pocketで始まるCLIが用意されています。
全てのコマンドは、`--stage`オプションで対象となるデプロイ環境を指定できます。

以下のコード例は、dev環境を操作する場合です。

## pocket status
環境の作成状況を確認します。

```bash
# dev環境の作成状況を確認
pocket status --stage=dev
```

## pocket deploy
デプロイします。

```bash
# dev環境のデプロイ
pocket deploy --stage=dev
```

具体的には、`pocket.toml`に記述がある場合、以下の作業を行います。記述がない場合、何もしません。

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

## pocket django manage `COMMAND` `ARGS`
任意のdjango management commandを実行します。

```bash
# collectstatic を実行
pocket django manage collectstatic --noinput --stage=dev
# migrateを実行
pocket django manage migrate --stage=dev
```

## pocket django storage upload `STORAGE`
ローカルからデプロイ先に、データをsyncします。`STORAGE`は`pocket.toml`の`storages`のキーです(1)。
ローカルは`filesystem`、デプロイ先は`s3`で定義されている必要があります。
{ .annotate }

1. `[general.django_fallback.django.storages]`がローカル用、`[awscontainer.django.storages]`がリモート用です。このSTORAGESのキーは、そのまま`settings.py`のSTORAGESのキー名にもなります。

??? question annotate "何のためのコマンド？"
    大きなファイルを利用する際の、アップロード先を管理するためのコマンドです。

    ラムダ環境でファイルを利用したい場合、djangoの`STORAGES`経由でS3を使うのが簡単です。
    その場合、ローカルでも`pathlib.Path`ではなく`STORAGES`を使えば分岐が不要になります。
    その状況で、ローカルの`STORAGES`とリモートの`STORAGES`を同期させるコマンドです。

    例えば、システムが正しく動くために、`load_books`というコマンドで、`books.csv`を読み込む必要があるとします。
    以下の様にすれば、アップロード先に悩む必要がありません。

    1. `pocket.toml`でローカルとデプロイ先の`STORAGES`を定義(1)
    2. ローカルで`data/management/books.csv`を作成
    3. `load_books`コマンドでは、`django.core.files.storage.storages['management'].open('books.csv', r)`でファイルを読み込み動作確認
    4. `pocket django storage upload management --stage=dev`でリモートにデータをupload
    5. `pocket django manage load_books`でリモートでも同じデータを読み込む

1. 少し下の「pocket.tomlのストレージ設定の例」を見てください


```bash
pocket django storage upload management --stage=dev
```

上記のコマンドでは、以下の`pocket.toml`が定義されていた場合に、`data/management`ディレクトリのデータをS3の`managemtnt`ディレクトリにアップロードします。どちらの環境でも、`storages['management']`でアクセスできます。

```toml
# pocket.tomlのストレージ設定の例
[general.django_fallback.storages]
management = { store = "filesystem", location = "data/management" }
[awscontainer.django.storages]
management = { store = "s3", location = "management" }
```



## pocket resource awscontainer status
Lambdaの作成状況を確認します。
```bash
# dev環境の例
pocket resource awscontainer status --stage=dev
```

## pocket resource awscontainer secretsmanager list
SecretsManagerの情報を確認します。
```bash
# dev環境で必要なsecretの作成状況を確認する例
pocket resource awscontainer secretsmanager list --stage=dev
# 値を表示する場合
pocket resource awscontainer secretsmanager list --stage=dev --show-values
```

## pocket resource awscontainer yaml
Lambdaを作るCloudFormationのyamlファイルを確認します。
```bash
# dev環境の例
pocket resource awscontainer yaml --stage=dev
```

## pocket resource awscontainer yaml-diff
Lambdaを作るCloudFormationのyamlファイルの差分を確認します。
すでにLambdaが動いていて、`pocket.toml`の設定変更を行った場合に利用します。
```bash
# dev環境の例
pocket resource awscontainer yaml-diff --stage=dev
```

## pocket resource neon status
Neonの作成状況を確認します。
```bash
# dev環境の例
pocket resource neon status --stage=dev
```

## pocket resource s3 status
S3バケットの作成状況を確認します。
```bash
# dev環境の例
pocket resource s3 status --stage=dev
```

## pocket resource spa status
SPAアップロード先S3バケットやCloudFrontの作成状況を確認します。
```bash
# dev環境の例
pocket resource spa status --stage=dev
```
