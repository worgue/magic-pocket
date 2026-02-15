# 実行環境

実行環境内で、リソース情報を取得することが出来ます。
大きく分けて、以下の2つの方法があります。

- pocket.tomlから取得
- 環境変数へのデータ登録

## pocket.tomlから取得
一部の情報は、pocket.tomlから直接取得します。

### STORAGESとCACHES
この2つの設定は、`settings.py`に指定するのとは異なるmagic-pocket独自表記が必要です。(1)
{.annotate}

1. settings.pyの自由度が高いが、magic-pocketでは一部の機能しか使わないため。

```python
from pocket.django.utils import get_caches, get_storages

STORAGES = get_storages()
CACHES = get_caches()
```

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

### pocket.tomlへdjango settingsを直接設定
settings.pyから、直接tomlの設定を読み込ませることも可能です。
以下は、`DEFAULT_FROM_EMAIL`と`CORS_ALLOWED_ORIGINS`を設定する例です。

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

これを`settings.py`で、以下の様に読み込めます。
```python
from pocket.django.runtime import get_django_settings

vars().update(get_django_settings().items())
```

??? question "環境変数とenvファイルで良いのでは？"
    機密性の低い情報では、env.devやenv.prdなどのenvファイルをコミットして、devやprdのみ環境変数としても構いません。
    それでも、この機能を実装した理由は、以下2点です。

    1. 全ての環境差分を`pocket.toml`で管理することができる
    2. ネストした辞書形式のsettingsを読み込む際に便利


## 環境変数へのデータ登録
以下のコマンドはデプロイ情報を環境変数に設定します。

```python
from pocket.django.runtime import set_envs

set_envs()

# Read enviroment variables here
# SECRET_KEY = os.environ.get("SECRET_KEY")
# etc...
```

読み込む情報は以下の通りです。

<div class="annotate" markdown>
- :simple-awssecretsmanager: AWS SecretsManagerから情報を取得(1)
- CF: CloudFormationのoutputからdjangoのALLOWED_HOSTSを取得
</div>
1.  `pocket.toml`の`[awscontainer.secretsmanager.pocket_secrets]`で設定され、自動生成されたパスワードや、Neonから取得したDB接続情報などを、管理します。

    また、`[awscontainer.secretsmanager.secrets]`には、SecretsManagerのarnを利用して、任意の情報を取得することも可能です。

    いずれの場合も、パーミッションはクラウドフォーメーション経由で自動的に設定されます。

!!! warning "環境変数からsettings.pyに読み込むのを忘れないでください"
    どの様な方法でも構いませんが、`settings.py`の中で値を登録することを忘れないでください。

    また、環境変数には型がありません。
    DEBUG=os.environ.get("DEBUG")として、DEBUG=Falseを設定すると、デバッグモードになってしまいます。気をつけましょう。

    作者はdjagno-environを利用することをお勧めします。
