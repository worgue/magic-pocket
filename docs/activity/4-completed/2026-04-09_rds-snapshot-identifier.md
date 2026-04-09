# RDS の snapshot からの復元サポート

| 項目 | 値 |
|------|-----|
| ID | #7 |
| Priority | `medium` |
| Category | `feature` |
| Date | 2026-04-09 |
| Tags | `rds`, `migration`, `snapshot`

## 目的
- `[rds]` セクションに `snapshot_identifier` (および必要なら `snapshot_arn`) を追加し、pocket が RDS Aurora クラスタを作成する際に既存 snapshot から復元できるようにする
- awsde 等の既存ツールから magic-pocket への移行で、本番 DB データを持ち込めるようにする
- signage からのフィードバック (`20260409-app-rds-snapshot-identifier-support`) への対応

## 設計
- **初回作成時のみ有効**: `SnapshotIdentifier` は CloudFormation の `AWS::RDS::DBCluster` に渡す
- **空作成と共存**: `snapshot_identifier` 未指定なら従来通り空のクラスタを作成
- **マスターパスワードの整合**: Aurora を snapshot から復元するとパスワードは snapshot 内のものが使われる。pocket は `ManageMasterUserPassword: true` を使って AWS 管理シークレットに切り替えているので、AWS 側で自動ローテーションが走り整合する想定 (要検証)
- **置換回避**: snapshot_identifier を後から削除しても CFn がクラスタを置換しないよう、README で「一度復元したら pocket.toml から外してよい、ただし CFn 的には値が残っていても影響なし」という扱いを明示

## タスクリスト
- [x] `pocket/settings.py`: `Rds` モデルに `snapshot_identifier: str | None = None` を追加
- [x] `pocket/context.py`: `RdsContext` に `snapshot_identifier` を追加
- [x] `pocket_cli/resources/rds.py`: create_cluster 時に `SnapshotIdentifier` を渡す (boto3 直接管理なので CFn テンプレート修正ではなく create_db_cluster 呼び出しの引数)
- [x] `pocket_cli/resources/rds.py`: 既存クラスタ存在時 (変更) は snapshot_identifier を無視する (drift 防止)
- [x] マスターパスワード整合の挙動を確認 (`ManageMasterUserPassword` で自動ローテーションが走るか)
- [x] テスト追加 (`tests/test_rds.py` or `tests/data/toml/rds_snapshot.toml`)
- [x] `docs/guide/configuration.md` の `## rds` に snapshot_identifier の記載を追加
- [x] ruff / pyright / pytest

## 次のステップ
- 実装着手

## 更新履歴
- 2026-04-09: 作成（フィードバック #20260409-app-rds-snapshot-identifier-support への対応として切り出し、#4 とは別タスクとして扱う）
- 2026-04-09: 実装完了。Rds settings + context に snapshot_identifier を追加、rds.py の create で restore_db_cluster_from_snapshot へ分岐、復元後に ModifyDBCluster で ManageMasterUserPassword=True に切替。docs 追記、テスト 2 件追加。123 tests pass, ruff/pyright clean。
