# 概要

基本的な機能は以下の通りです。

1. Django実行環境を設定ファイル`pocket.toml`で定義します。
2. CLIコマンドで、実行環境の作成や確認、デプロイを行います。
3. デプロイした実行環境では、`pocket.django.runtime`モジュールを用いて、`pocket.toml`やSecretsManagerから情報を取得します。
