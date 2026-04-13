# RDS 既存インスタンスへの接続サポート

| 項目 | 値 |
|------|-----|
| ID | #4 |
| Priority | `medium` |
| Category | `feature` |
| Date | 2026-04-09 |
| Tags | `rds`, `database`, `aws`

## 目的
- 現在の `[rds]` セクションは Aurora PostgreSQL Serverless v2 の **新規作成**のみサポート
- 既存の RDS インスタンス (手動作成 / 別スタック管理 / 共有クラスター等) に Lambda から接続したいケースに対応する
- 接続情報 (host, port, secret ARN 等) を `pocket.toml` で指定するだけで `DATABASE_URL` が組み立てられるようにする

## タスクリスト
- [x] `pocket.toml` の `[rds]` に「既存接続モード」のスキーマを追加
  - `cluster_identifier` 等の指定で参照、もしくは `secret_arn` 直接指定の 2 パターンを検討
  - 既存 SG の取得・Lambda SG への ingress 追加方針を決定
- [x] `pocket/settings.py` の `Rds` モデルに mode 分岐を追加（新規作成 vs 既存参照）
- [x] `pocket/context.py` の `RdsContext` に既存接続パラメータを追加
- [x] `pocket_cli/resources/rds.py`: 既存モード時はリソースを作成せず参照情報だけを伝搬
- [x] `awscontainer.yaml`: SG ingress / IAM (secretsmanager:GetSecretValue) を既存 ARN 向けに発行
- [x] `pocket/runtime.py::_set_rds_database_url`: 既存モードでも secret から URL を組めるよう調整
- [x] テスト追加（`tests/data/toml/rds.toml` の隣に `rds_existing.toml` 等）
- [x] `docs/guide/configuration.md` / `database.md` 等に既存接続モードの記載追加

## 設計上の論点
- VPC は手動指定 (`vpc_ref`) か自動検出か
- secret 形式 (AWS 管理 vs ユーザー管理) のサポート範囲
- 既存クラスターの DB エンジン (PostgreSQL/MySQL) を context に伝える方法

## 次のステップ
- 既存ユーザーから明確な要望が出たタイミング、もしくは新規プロジェクトの DB 共有ニーズが出てきたら着手

## 更新履歴
- 2026-04-09: 作成（MEMORY.md の TODO を Activity Doc 化）
- 2026-04-13: 実装完了。`managed = false` + `secret_arn` + `security_group_id` で既存 RDS を参照するモードを追加。設計論点は secret_arn 直接指定 + SG ID 手動指定 + 明示的 `managed` フラグに確定。テスト 4 件追加、docs にセクション追記。135 tests pass。
