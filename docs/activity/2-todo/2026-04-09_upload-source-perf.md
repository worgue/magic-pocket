# `_upload_source` のパフォーマンス・メモリ改善

| 項目 | 値 |
|------|-----|
| ID | #3 |
| Priority | `low` |
| Category | `feature` |
| Date | 2026-04-09 |
| Tags | `codebuild`, `performance`, `memory`

## 目的
- `packages/magic-pocket-cli/pocket_cli/resources/aws/builders/codebuild.py::_upload_source` に巨大プロジェクトで顕在化する 2 つの問題があり、改善する
- dockerignore 修正と同じフィードバックの関連項目として指摘された

### 問題1: `rglob("*")` がディレクトリ枝刈りしない

`for path in sorted(context_dir.rglob("*"))` が全ファイルを walk しきってから filter するため、`.git` / `node_modules` / 巨大な data ディレクトリのような除外対象ディレクトリも全部 stat される。dockerignore バグが直っても walk 時間は改善されない。

### 問題2: `BytesIO` で zip 全展開 → OOM

zip ソース全体をメモリに乗せて `s3.put_object(Body=buf.read())` するため、巨大ソース (例: 19 GB 規模) だと Python プロセスが OOM Kill される (exit 137)。

## タスクリスト
- [ ] `os.walk` ベースに書き換え、`dirnames[:] = [...]` で除外ディレクトリを in-place に枝刈り
  - PathSpec での判定が「ディレクトリ単位」でも動くか確認（pathspec の `match_file` は末尾スラッシュの扱いに注意）
- [ ] zip バッファを `tempfile.SpooledTemporaryFile(max_size=...)` に変更し、メモリ閾値を超えたらディスクへ自動フォールバック
- [ ] `s3.upload_fileobj` でストリーミングアップロードに切り替え（`put_object(Body=read())` をやめる）
- [ ] テスト追加: 大量ファイルを含む context での walk 時間 / メモリ使用量の回帰防止（簡易的に「除外ディレクトリは stat されない」ことを spy で検証）
- [ ] ruff / pyright / pytest を通す

## 次のステップ
- 利用者から実害が出ていないので優先度 low。手が空いたとき、もしくは他に大規模プロジェクト由来の事故が出たら着手

## 更新履歴
- 2026-04-09: 作成（dockerignore フィードバックの関連項目として切り出し）
