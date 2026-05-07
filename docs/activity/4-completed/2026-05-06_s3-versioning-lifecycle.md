# [s3] に versioning と lifecycle_rules の宣言サポートを追加

| 項目 | 値 |
|------|-----|
| ID | #12 |
| Priority | `high` |
| Category | `feature` |
| Date | 2026-05-06 |
| Tags | `s3`, `versioning`, `lifecycle`, `worgue-integration`

## 目的

- `[s3]` セクションに `versioning: bool` と `[[s3.lifecycle_rules]]` を追加し、bucket 設定を pocket.toml で宣言的に管理できるようにする
- `pocket resource s3 create` を冪等な reconcile コマンドとして再定義 (既存 bucket にも config を適用できるようにする)
- フィードバック `20260506-worgue-s3-versioning-lifecycle` への対応 (worgue 側 adoc-32 が blocked → 解除)

## 実装内容

### Settings (`pocket/settings.py`)

```python
class S3LifecycleRule(BaseModel):
    id: str
    prefix: str
    noncurrent_version_expiration_days: int = Field(ge=1)


class S3(BaseSettings):
    bucket_name_format: FormatStr = "{stage}-{project}-{namespace}"
    cors: S3Cors | None = None
    versioning: bool = False
    lifecycle_rules: list[S3LifecycleRule] = []
```

### Context (`pocket/context.py`)

`S3Context` に `versioning` / `lifecycle_rules` を追加。`from_settings` で
S3LifecycleRule を S3LifecycleRuleContext に変換して渡す。

### Resource (`packages/magic-pocket-cli/pocket_cli/resources/s3.py`)

すべての設定を **fully declarative** な reconcile として実装:

- `_ensure_versioning()`:
  - `True` & 現状 `!= Enabled` → `PutBucketVersioning(Enabled)`
  - `False` & 現状 `Enabled` → `PutBucketVersioning(Suspended)`
  - その他 (`False` & 未設定 / `Suspended`) → no-op
- `_ensure_lifecycle()`:
  - 宣言あり → `PutBucketLifecycleConfiguration` で置き換え
  - 宣言なし & 現状あり → `DeleteBucketLifecycle`
  - 宣言なし & 現状なし → no-op
- `_ensure_cors()` (既存挙動も declarative に揃える):
  - 宣言あり → `PutBucketCors` で置き換え
  - 宣言なし & 現状あり → `DeleteBucketCors`
  - 宣言なし & 現状なし → no-op
- `versioning_require_update` / `lifecycle_require_update` / `cors_require_update`
  を `status` に組み込み、drift で `REQUIRE_UPDATE` を返す。
- `create()` / `update()` / `ensure_exists()` から各 `_ensure_*` を呼び出し。
- `public_access_block` getter を `NoSuchPublicAccessBlockConfiguration` 耐性に
  変更 (新規バケットや手動作成バケットに対して safe に reconcile できる)。

### CLI (`packages/magic-pocket-cli/pocket_cli/cli/s3_cli.py`)

`pocket resource s3 create` を冪等化。バケット既存時は早期 return せず
`ensure_exists()` を実行 (PAB / CORS / versioning / lifecycle を reconcile)。
worgue 側の運用 (toml 編集 → 再実行で差分反映) に合わせる。

### Docs (`docs/guide/configuration.md`)

`## s3` セクションに `versioning` と `lifecycle_rules` のフィールドと挙動を追記。
特に「versioning=False で suspend しない」「lifecycle_rules 空で既存に干渉しない」
の安全側の挙動を `!!! note` で明示。

### Tests (`tests/test_s3.py`)

新規テストファイル (17 件):

- Settings / Context のパースと相互変換
- `noncurrent_version_expiration_days >= 1` のバリデーション
- `versioning=True` で Enabled、`versioning=False` で既存 Enabled を Suspended に
  reconcile (declarative)
- `versioning=False` & 現状未設定 / Suspended のときは no-op
- `lifecycle_rules=[...]` で置き換え、`lifecycle_rules=[]` で既存を削除
  (`DeleteBucketLifecycle`)
- `cors` 未宣言で reconcile したとき、既存 CORS 設定が削除される
  (`DeleteBucketCors`)
- `status` が versioning / lifecycle / cors の drift を検出する

## 設計上の判断

### Fully declarative (no tristate)

初版では `versioning=False` / `lifecycle_rules=[]` を「何もしない」として既存
設定を保護する案で実装したが、レビューで「不可解 / 失敗しやすい挙動」と指摘を
受けて方針変更。最終案は **fully declarative** (toml の宣言が真実、pocket が
完全所有):

| 設定 | 動作 |
|------|------|
| `versioning = true` | `Enabled` に揃える |
| `versioning = false` (default) | 現状 `Enabled` のとき `Suspended` に揃える。それ以外 no-op |
| `lifecycle_rules = [...]` | 置き換え |
| `lifecycle_rules = []` (default) | 既存があれば `DeleteBucketLifecycle` |
| `cors = {...}` | 置き換え |
| `cors` 未宣言 (default) | 既存があれば `DeleteBucketCors` |

`bool | None` のような tristate も検討したが、実用ユースケースが想像しづらい
ため不採用。必要になれば後方互換を保ちつつ拡張可能。

なお S3 仕様上、一度 `Enabled` にした versioning は「未設定」状態には戻れず、
`Suspended` 止まり。docs の `!!! warning` に明記。

### CORS の挙動も fully declarative に揃えた

旧 `_ensure_cors()` は「`cors` 未宣言なら何もしない」だったが、versioning /
lifecycle と同じ性質の問題を抱えていたため一緒に直した。pocket 管理外で手動設定
した CORS ルールは reconcile で消えるため、必要なルールは toml に取り込むこと
を docs に明記。

### スコープ外 (将来検討)

worgue feedback 末尾に挙がっていた以下は今回スコープ外:

- `current_version_expiration_days` (現バージョンの自動削除)
- `transitions` (Glacier 等への遷移)
- `tag_filter` (タグ単位 lifecycle)
- 複数 bucket 対応 (`[s3.<name>]` 構造変更)

要望が出たら追加で対応する。

## 検証

- `uv run ruff check pocket/settings.py pocket/context.py
  packages/magic-pocket-cli/pocket_cli/resources/s3.py
  packages/magic-pocket-cli/pocket_cli/cli/s3_cli.py tests/test_s3.py` — passed
- `uv run pyright` (同上スコープ) — 0 errors
- `uv run pytest` — 155 passed, 1 skipped

## 関連

- フィードバック: `feedbacks/active/magic-pocket/20260506-worgue-s3-versioning-lifecycle/`
- worgue 側 blocked タスク (今回で解除): `worgue/docs/activity/1-doing/2026-05-05_projects-s3-persistence.md`

## 更新履歴
- 2026-05-06: 作成 + 実装完了
