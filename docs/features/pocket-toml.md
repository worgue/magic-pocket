# pocket.toml

デプロイに関する設定は、`pocket.toml`に記述します。

!!! info "今後利用する名称について"
    各デプロイ環境をステージと呼びます。これらは、`pocket.toml`を通じて、明示的な事前設定が必要です。

??? warning "別形式のデータ利用やソースを編集する場合"
    pocket.tomlの内容は、pocket.settings.Settingsとpocket.general_settings.GeneralSettingsに読み込まれます。
    これらの項目は自由に編集でき、これらを用意できれば、他の機能から見ても問題ありません。

    システムは、これらのSettingsを元にContextを作成します。
    こちらは、システムが使うために存在するデータでなので、これらのデータを直接編集してしまうと、正常に動作しなくなる可能性が高いです。

    このcontextは、contextlibやpydanticのcontextとは異なります。
    ややこしいので、名称を変えるかもしれません。
    このcontext自体を消すこともありえます。


## general settings
デプロイ環境に依存しない設定を記述します。

`region`: str
:   **(required)** デプロイ環境のリージョンを指定します。

`stages`: list[str]
:   **(required)** ステージのリストを指定します。

`object_prefix`: str = "pocket-"
:   作成されるリソースのprefixとして、幅広く利用されます。

### general.django_fallback
ローカル環境で利用する、Djangoのsettings.pyに設定する内容を記述します。
設定内容は、`awscontainer.django`と同じです。
!!! note ""
    詳しくは[Django設定](#django)を参照してください。


## stage settings
デプロ環境毎の設定を記述します。

!!! info "ステージ毎の設定"
    `pocket.toml`で設定された設定は、基本的に全てのステージに適用されます。
    そのため、`[neon]`という記述をすると、全てのステージで`neon`が作られます。

    ステージ毎に別の設定を行いたい場合は、ステージ名を最初に記述し、その下に設定を記述します。
    例えば、`[dev.neon]`という記述をすると、`dev`ステージのみ`neon`が作られます。

### s3
S3バケットの設定を記述します。

```toml
# 最小設定
[s3]
```

`public_dirs`: list[str] = []
:   ここで指定したディレクトリは、公開されます。

`bucket_name_format`: str = "{prefix}{stage}-{project}"
:   バケット名のフォーマットを指定します。
    デプロイ環境では、この名称のS3を利用しようとします。

    `{prefix}`は、`general_settings`の`object_prefix`で指定した文字列に置き換えられます。
    `{stage}`は、ステージ名に、`{project}`はプロジェクト名に置き換えられます。

    大量にステージを作成する場合、`{prefix}-{project}`などと`{stage}`を削除する事で、バケットが増えすぎるのを防ぐことができます。
    以下は、`prd`ステージのみバケットを分ける例です。

    ```toml
    [s3]
    bucket_name_format = "{prefix}-{project}"
    [prd.s3]
    bucket_name_format = "{prefix}{stage}-{project}"
    ```

### neon
Neonの設定を記述します。

```toml
# 最小設定
[neon]
```

オプションはありません。

### awscontainer
AWS Lambdaの設定を記述します。

```toml
# 最小設定
[awscontainer]
dockerfile_path = "pocket.Dockerfile"
```

`dockerfile_path`: str
:   **(required)** Lambdaで利用するDockerfileのパスを指定します。

#### awscontainer.handlers
Lambdaで利用するハンドラーの設定を記述します。
基本的には、以下の設定で、wsgiとmanagementコマンドを有効にする想定です。

```toml
# wsgiとmanagementのhandlersを作成
[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
[awscontainer.handlers.management]
command = "pocket.django.lambda_handlers.management_command_handler"
timeout = 600
```

#### awscotainer.handlers.`handler_name`.apigateway
API Gatewayの設定を記述します。domain設定をする場合、環境毎の切り分けが必要になります。
```toml
# devではAPI Gatewayデフォルトのurl、prdではexample.comを利用します。
[dev.awscontainer.handlers.wsgi]
apigateway = {}
[prd.awscontainer.handlers.wsgi]
apigateway = { domain = "example.com" }
```


#### awscontainer.secretsmanager
Lambdaで利用するAWS Secrets Managerの設定を記述します。

`pocket_secrets`: dict[str, PocketSecretSpec]
:   magic-pocketのSecrets自動生成機能を利用します。
    `key`に設定したい環境変数名を、`value`には、`type`とtype毎のオプションからなる生成ルールを記述します。
    デプロイ先で`pocket.django.runtime.set_envs()`を呼ぶことで、環境変数に登録します。

    **type = "password"**

    パスワードを生成します。利用可能なオプションは以下の通りです。

    `length`: int = 16
    :   パスワードの長さを指定します。

    ```toml
    # 50文字のパスワードを生成
    [awscontainer.secretsmanager.pocket_secrets]
    SECRET_KEY = { type = "password", options = { length = 50 } }
    ```

    **type = "neon_database_url"**

    Neonのデータベース接続用URLをNoneのAPI経由で取得してSecretsManagerに保存します。オプションはありません。

    ```toml
    # Neonのデータベース接続用URLを取得
    [awscontainer.secretsmanager.pocket_secrets]
    DATABASE_URL = { type = "neon_database_url" }
    ```

    **type = "rsa_pem_base64"**

    RSAの秘密鍵と公開鍵を生成して、base64形式で保存します。
    この値は、2つの環境変数に分けて登録され、そのためのsuffix指定は必須オプションです。
    必須のオプションは以下の通りです。

    `pem_base64_environ_suffix`: str
    :   **(required)** PEMキーのbase64形式を保存する環境変数名のsuffixを指定します。

    `pub_base64_environ_suffix`: str
    :   **(required)** 公開鍵のbase64形式を保存する環境変数名のsuffixを指定します。

    ```toml
    # JWT用のRSAキーを生成し、PEMキーを環境変数`JWT_RSA_PEM_BASE64`、公開鍵を環境変数`JWT_RSA_PUB_BASE64`に保存
    [awscontainer.secretsmanager.pocket_secrets.JWT_RSA]
    type = "rsa_pem_base64"
    options = { pem_base64_environ_suffix = "_PEM_BASE64", pub_base64_environ_suffix = "_PUB_BASE64" }
    ```

#### awscontainer.django
Lambda環境で利用する、Djangoのsettings.pyに設定する内容を記述します。
設定内容は、`general.django_fallback`と同じです。
!!! note ""
    詳しくは[Django設定](#django)を参照してください。

### spa
SPAのためのクラウドフロントとコンテンツアップロード先のバケットを作成します。

!!! tips "どこまでやるのか？"
    この機能は、SPA配信の外側を作ります。
    証明書、DNS設定、クラウドフロント設定、リダイレクト設定、コンテンツ保存用のバケット作成、などです。

    SPAのビルド、バケットへのアップロード、キャッシュの設定は別途必要です。
    javascript側では、[s3-spa-upload](https://www.npmjs.com/package/s3-spa-upload){:target="_blank"}を利用する想定です。

??? question "Djangoとは関係ないのでは？"
    直接は関係ありません。
    切り分けとしては、javascript側に有る方が自然です。

    ただ、javascriptのデプロイとなると、javascript側にもサーバーが必要になるケースとの混乱が発生します。
    magic-pocketのユーザーは、javascriptのコンピューティング環境は持ちたくないケースが多いと思います。
    その場合、この機能を使えば、SPA配信用のS3バケットを作るだけという安心があります。

```toml
# devとprdがある場合の最小設定
[dev.spa]
domain = "dev.example.com"
[prd.spa]
domain = "www.example.com"
```

```toml
# prd以外は同じバケットを利用する設定
[spa]
bucket_name_format = "{prefix}{project}-spa"
[prd.spa]
bucket_name_format = "{prefix}{stage}-{project}-spa"
```

```toml
# prdデプロイ時に、example.comからwww.example.comのリダイレクトを追加する設定
[prd.spa]
redirect_from = [{ domain = "example.com" }]
```

`domain`: str
:   **(required)** SPAのドメインを指定します。

`bucket_name_format`: str = "{prefix}{stage}-{project}-spa"
:   バケット名のフォーマットを指定します。
    デプロイ環境では、この名称のS3を利用しようとします。

    `{prefix}`は、`general_settings`の`object_prefix`で指定した文字列に置き換えられます。
    `{stage}`は、ステージ名に、`{project}`はプロジェクト名に置き換えられます。

`origin_path_format`: str = "/{stage}"
!!! note "Changed in 0.2.0"
    デフォルト値が変更されました。[Changelog](../changelog.md)を参照してください。
:    オリジンパスのフォーマットを指定します。外部から見える値ではありません。値を設定する場合は、"/"から始まる必要があります。

`redirect_from`: list[str]
:   リダイレクトするドメインをリスト形式で指定します。

!!! warning "フォーマットに関する制限"
    `bucket_name_format`か`origin_path_format`のどちらかに、`{stage}`と`{project}`が入っている必要があります。
    これは、意図せずアップロード先が重複するのを防ぐためです。

## django設定
general.django_fallbackとawscontainer.djangoで設定可能な項目を記述します。
以下の例では、Lambda側で利用する設定を記述しています。

### awscontainer.django.storages: dict[str, DjangoStorage]
Djangoの`STORAGES`に設定する内容を記述します。
keyは、`STORAGES`のkeyを指定し、valueには辞書形式データ（[`DjangoStorage`](#djangostorage)）を指定します。

```toml
# 一般的な最小設定
# [s3]設定で作ったバケットの`media`と`static`のディレクトリを利用
[awscontainer.django.storages]
default = { store = "s3", location = "media" }
staticfiles = { store = "s3", location = "static", static = true, manifest = true }
```

#### DjangoStorage
`store`: Literal["s3", "filesystem"]
:   **(required)** ストレージの種類を指定します。

`location`: str
:   **(required for s3)** ファイルの保存先を指定します。

`static`: bool = False
:   StaticFileのストレージを利用します。

`manifest`: bool = False
:   **(only for static=True)** マニフェストファイルを利用します。

`options`: dict[str, Any] = {}
:   その他のオプションを指定します。このオプションは全て、Djangoの`storages[key]["OPTIONS"]`にそのまま渡されます。

`store`, `static`, `manifest`は、djangoのStorageクラスを選択するための設定です。
以下6種のストレージから適切なものが選択されます。

- storages.backends.s3boto3.S3ManifestStaticStorage
- storages.backends.s3boto3.S3StaticStorage
- storages.backends.s3boto3.S3Boto3Storage
- django.contrib.staticfiles.storage.ManifestStaticFilesStorage
- django.contrib.staticfiles.storage.StaticFilesStorage
- django.core.files.storage.FileSystemStorage

### awscontainer.django.caches: dict[str, DjangoCache]
Djangoの`CACHES`に設定する内容を記述します。
keyは、`CACHES`のkeyを指定し、valueには辞書形式データ（[`DjangoCache`](#djangocache)）を指定します。

```toml
# 最小設定
[awscontainer.django.caches]
default = { store = "locmem" }
```

`store`: Literal["locmem"]
:   **(required)** キャッシュの種類を指定します。

??? question "他のキャッシュバックエンドは？"
    まだ正式なサポートはありません。
    今後、VPCとEFSをサポート予定です。

### awscontainer.django.settings
環境毎にtoml形式でsettingsを指定します。
この値は、`pocket.django.runtime.get_django_settings`を利用して取得したうえで、settigsの変数に登録する必要があります。

```toml
# prdとdevのLambda環境で別のDEFAULT_FROM_EMAILを設定
[dev.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Dev Version" <test@example.com>'
[prd.awscontainer.django.settings]
DEFAULT_FROM_EMAIL = '"Magic Pocket" <test@example.com>'
```

```python
# 上記のDEFAULT_FROM_EMAILをsettings.pyに登録する例
from pocket.django.runtime import get_django_settings
vars().update(get_django_settings())
```
