# Tutorial - Simple Django Project
ここでは、Lambda + Neon + S3でDjangoプロジェクトを動かします。
Admin画面を確認することが目標です。

## 事前準備
rye, AWS, Neon

??? rye
    magic-pocketは、PyPIからインストール可能です。
    ここでは、[rye](https://rye.astral.sh/){:target="_blank"}を利用します。
    他のツールを使う場合、コマンドを適宜調整してください。
    ryeを使ったことがない場合、uvの方が良いかもしれません。
    ryeからuvへの移行方法が分かり次第、ドキュメントはuvに変更される予定です。

??? AWS
    [AWSアカウント](https://aws.amazon.com/){:target="_blank"}が必要です。

    credentialsは、`~/.aws/credentials`への設定を想定しています。

    boto3からAWSを操作しますが、credentialsを直接読み込むことはしないので、[boto3の設定](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html#shared-credentials-file){:target="_blank"}を参考に設定してください。

!!! Neon
    [Neonアカウント](https://neon.tech/){:target="_blank"}が必要です。

    APIキーは、`NEON_API_KEY`という環境変数で設定してください。

    チュートリアルに沿えば、`.env`で設定できます。

## Djangoプロジェクトの作成
!!! warning annotate "プロジェクト名"
    自分の名前などを入れて、他のmagic-pocketプロジェクトと、コンフリクトしないプロジェクト名にしてください。
    プロジェクト名から自動生成させるS3バケット名(1)がコンフリクトしてエラーになります。
    `pocket`などの名前は動かないかもしれません(2)。

1. `pocket`という文字列や環境名がプレフィックスとなるので、通常はコンフリクトしません。`pocket.toml`内で直接指定することも可能ですが、stageごとに異なる値にしたり、`stage`を変数としたり、チュートリアルの範囲を超えるため、プロジェクト名で対応してください。
2. magic-pocketをまだ誰も使っていなくて、動く可能性もあります。

```bash
# global django (1)
rye install django
django-admin startproject `your-project-name`
cd `your-project-name`
rye pin 3.12
rye init --virtual
```

1. django-adminコマンドを利用するため、グローバルにdjangoをインストールします。

## Djangoのインストールとrunserver
```bash
# project django(1)
rye add django
python manage.py migrate
# runserver(2)
python manage.py runserver
```

1. プロジェクトにdjangoを追加します。
2. localhost:8000でdjangoが動いていることを確認してください。

## django-environとpsycopgのインストール
```bash
rye add django-environ psycopg
```

!!! warning "Lambda環境はデフォルトでlinux/amd64です"
    macで開発している場合、`rye add "psycopg[binary]"`とする必要があるかもしれません。


## maginc-pocketのインストール
```bash
rye add magic-pocket
```

## pocket.toml, Dockerfile, .envの作成とsettings.pyの修正
```bash
rye run pocket django init
```

## deploy
!!! info annotate "NEON_API_KEY"
    `NEON_API_KEY`という環境変数を設定する必要があります。

    $ NEON_API_KEY=`key` rye run pocket deploy --stage=dev

    という形でCLI実行時に指定も可能ですが、magic-pocketはデフォルトで`.env`を見に行きます。
    ここまでで、.envが出来ているはずなので、.envに設定するのが楽だと思います。

    ??? warning "djangoとmagic-pocketの`.env`読み込み機能は別実装です"
        `.env`があるので追加すれば良いのですが、このチュートリアルのdjangoが`settings.py`で`.env`を指定して読みこむことと、magic-pocketの`NEON_API_KEY`読み込みは関係ありません。
        magic-pocketは、内部のコードで`NEON_API_KEY`設定のみ、`.env`を参照します。

```bash
rye run pocket deploy --stage=dev
```
