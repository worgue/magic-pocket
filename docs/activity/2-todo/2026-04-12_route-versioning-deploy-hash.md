# CloudFront route に deploy_hash バージョニングを追加

| 項目 | 値 |
|------|-----|
| ID | #8 |
| Priority | `medium` |
| Category | `feature` |
| Date | 2026-04-12 |
| Tags | `cloudfront`, `versioning`, `cache`, `deploy`

## 目的
- `is_versioned = true` (ManifestStaticFilesStorage) の代替として `versioning = "deploy_hash"` モードを追加する
- デプロイ時の git hash を URL prefix に付与し、CloudFront Function で strip → S3 転送することで、manifest 計算なし・高速デプロイ・動画対応のキャッシュバスティングを実現する
- フィードバック `20260412-signage-static-versioning-deploy-hash` への対応

## 設計概要

### versioning フィールド

Route の `is_versioned: bool` を `versioning: Literal["content_hash", "deploy_hash"] | None` に拡張。

| 値 | 意味 | STATIC_URL | CloudFront | Django Storage |
|---|---|---|---|---|
| `"content_hash"` | ファイル内容ハッシュ (現行 `is_versioned = true` 相当) | `/static/` | 長期キャッシュ (ResponseHeadersPolicy) | ManifestStaticFilesStorage |
| `"deploy_hash"` | git hash を URL prefix に付与 | `/static/{hash}/` | CF Function で hash prefix を strip + 長期キャッシュ | StaticFilesStorage |
| 省略 / None | バージョニングなし | `/static/` | デフォルトキャッシュ | StaticFilesStorage |

`is_versioned = true` は `versioning = "content_hash"` のエイリアスとして後方互換を維持。

### deploy_hash の動作フロー

1. pocket がデプロイ時に `git rev-parse --short HEAD` で hash を取得
2. Lambda 環境変数 `DEPLOY_HASH` に注入（ユーザーが明示設定済みならそちらを優先）
3. Django 側: `STATIC_URL = f"static/{DEPLOY_HASH}/"` で hash 付き URL を生成
4. collectstatic は StaticFilesStorage (manifest 計算なし、高速)
5. S3 アップロードは `/static/` (hash prefix なし)
6. CloudFront Function: `/static/{hash}/foo.js` → `/static/foo.js` に変換してオリジンに転送
7. CloudFront のキャッシュキーはフル URL (hash 込み) → デプロイごとにキャッシュ自然更新

## タスクリスト
- [ ] `pocket/settings.py`: Route に `versioning` フィールド追加、`is_versioned` を deprecation 扱い（pre-validator で `versioning = "content_hash"` に変換）
- [ ] `pocket/context.py`: RouteContext に `versioning` を反映、deploy_hash 用の computed fields
- [ ] `cloudfront.yaml`: deploy_hash route に CF Function (hash prefix strip) を生成するテンプレート追加
- [ ] `cloudformation.py` / deploy 時: git hash 取得 → `DEPLOY_HASH` 環境変数として awscontainer に注入
- [ ] `awscontainer.yaml`: `DEPLOY_HASH` 環境変数を Lambda に渡す
- [ ] `pocket/django/runtime.py` or docs: Django settings での `DEPLOY_HASH` → `STATIC_URL` の利用ガイド
- [ ] テスト: settings バリデーション、CF Function レンダリング、deploy_hash 注入のテスト
- [ ] `docs/guide/configuration.md`: `versioning` フィールドの説明、`deploy_hash` 方式のサンプルと動作解説
- [ ] ruff / pyright / pytest

## 次のステップ
- 設計の詳細詰め（CF Function のコード、DEPLOY_HASH 注入タイミング、is_versioned との併用制約）→ 実装着手

## 更新履歴
- 2026-04-12: 作成（フィードバック #20260412-signage-static-versioning-deploy-hash への対応として）
