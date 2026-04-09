# DeepDiff 依存の削除

| 項目 | 値 |
|------|-----|
| ID | #6 |
| Priority | `low` |
| Category | `tech-stack` |
| Date | 2026-04-09 |
| Tags | `cleanup`, `dependency`, `cloudformation`
| Depends | (要確認: `pocket migrate` の全スタックタグ付与完了が前提) |

## 目的
- `pocket_cli/resources/aws/cloudformation.py` の `yaml_synced` / `yaml_diff` で利用している `DeepDiff` 依存を削除する
- `pocket migrate` で全スタックにタグ付与が完了すれば、`yaml_synced` 判定は CloudFormation のスタックタグ照合だけで完結できるため `DeepDiff` が不要になる
- `yaml_diff` 自体は CLI の `yaml-diff` コマンドで引き続き必要だが、CLI 側のみの依存に切り出すか、軽量な diff 実装に置き換える

## タスクリスト
- [ ] `pocket migrate` で本番含む全スタックがタグ付与済みであることを確認
- [ ] `yaml_synced` の判定ロジックを CloudFormation スタックタグベースに切り替え（`DeepDiff` 呼び出しを削除）
- [ ] `yaml_diff`（CLI の `yaml-diff` コマンド用）を別関数に分離
  - 案A: CLI 専用に `pocket_cli` 内部で `DeepDiff` を使い続ける（pyproject の依存はそのまま）
  - 案B: `difflib` 等の標準ライブラリで diff 出力に置き換え、`DeepDiff` を完全削除
- [ ] `packages/magic-pocket-cli/pyproject.toml` から `deepdiff` を削除（案B 採用時）
- [ ] テストの回帰確認

## 設計上の論点
- 案A vs 案B の選択。`DeepDiff` は YAML 比較で見やすい diff を出してくれるが、CLI 表示用なら `difflib.unified_diff` でも実用上問題ないかも
- マイグレーション完了の判定方法（手動確認 / スクリプトで全スタック走査）

## 次のステップ
- `pocket migrate` の適用状況を確認後に着手

## 更新履歴
- 2026-04-09: 作成（MEMORY.md の TODO を Activity Doc 化）
