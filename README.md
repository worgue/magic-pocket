# Magic Pocket - Django Serverless Deployment

ドキュメントはこちら
https://worgue.github.io/magic-pocket/


magic-pocket の目標は、サーバレスDjangoです。Djangoを以下の環境にデプロイします。

- AWS Lambda
- Neon Postgres
- S3 storages

## Motivation

小規模な個人プロジェクトを、1人で複数同時に運用するため開発されたライブラリです。
気軽に作り、飽きたら放っておく、というスタイルで運用するため、以下の要件が最終目標になっています。

- サーバーの保守が不要
- 使わない間のコストが不要
- 環境を作る時にやる気を出す必要なし
