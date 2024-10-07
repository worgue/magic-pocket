# コマンドライン

magic-pocketには、pocketで始まるCLIが用意されています。
全てのコマンドは、`--stage`オプションで対象となるデプロイ環境を指定できます。

以下のコード例は、dev環境を操作する場合です。

## pocket status
```bash
pocket status --stage=dev
```
環境の作成状況を確認します。

## pocket deploy
```bash
pocket deploy --stage=dev
```
デプロイします。具体的には、`pocket.toml`に記述がある場合、以下の作業を行います。記述がない場合、何もしません。

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
```bash
pocket django manage collectstatic --noinput --stage=dev
```
django management commandを実行

## pocket django storage upload `STORAGE`
```bash
pocket django storage upload static --stage=dev
```
ローカルのFileSystemStorageから対象ステージのS3Boto3Storageへデータをsync。STORAGEはsettings.pyのSTORAGESのキー名です。


## pocket resource awscontainer status
```bash
pocket resource awscontainer status --stage=dev
```
Lambdaの作成状況を確認

## pocket resource awscontainer secretsmanager list
```bash
pocket resource awscontainer secretsmanager list --stage=dev
```
SecretsManagerの値を確認

## pocket resource awscontainer yaml
```bash
pocket resource awscontainer yaml --stage=dev
```
CloudFormation用のyamlファイルを確認

## pocket resource awscontainer yaml-diff
```bash
pocket resource awscontainer yaml-diff --stage=dev
```
CloudFormation用のyamlファイルの差分を確認

## pocket resource neon status
```bash
pocket resource neon status --stage=dev
```
Neonの作成状況を確認

## pocket resource s3 status
```bash
pocket resource s3 status --stage=dev
```
S3バケットの作成状況を確認

## pocket resource spa status
```bash
pocket resource spa status --stage=dev
```
SPAアップロード先S3バケットの作成状況を確認
