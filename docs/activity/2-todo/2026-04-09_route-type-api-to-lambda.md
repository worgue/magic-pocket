# Route の `type = "api"` を `"lambda"` にリネーム

| 項目 | 値 |
|------|-----|
| ID | #2 |
| Priority | `medium` |
| Category | `feature` |
| Date | 2026-04-09 |
| Tags | `breaking-change`, `cloudfront`, `naming`

## 目的
- `type = "api"` は実体としては「Lambda handler を API Gateway 経由で公開し CloudFront のオリジンに使う」だけで、Django 単体構成 (HTML を返す) にも使える。"api" という名前は誤解を招く
- 次のメジャーで破壊的変更を準備中であり、利用者も限られているため、エイリアスを残さず一気にリネームする
- ユーザーが古い `type = "api"` を書いていた場合、起動時に「`type = "api"` は廃止されました。`type = "lambda"` を使ってください（旧 api の制約は解除され、`is_default = true` も含めてすべての route で利用可能になりました）」と分かりやすいエラーを出す

## タスクリスト
- [ ] `pocket/settings.py`: `Route.type` の Literal を `"s3" | "lambda"` に変更
- [ ] `Route` に「`api` 文字列を検出したら明示エラー」を出す pre-validator を追加
- [ ] `pocket/context.py`: `RouteContext.type` も同様に変更、`is_api` → `is_lambda` にリネーム
- [ ] `CloudFrontContext` の関連プロパティを命名整理（"extra = default 以外" の規則を一貫させる）
  - `extra_routes` → `extra_s3_routes`（S3 専用と明示）
  - `api_routes`（default 除外） → `extra_lambda_routes`
  - `has_any_api_route` → `has_lambda_route`
- [ ] CloudFront テンプレート (`cloudfront.yaml`) 内の `is_api` 参照を更新
- [ ] 既存テスト・テストフィクスチャ (`tests/data/toml/cloudfront_api_route.toml` など) の `type = "api"` を全置換
- [ ] エラーメッセージのテストを追加（`type = "api"` を含む toml を読むと適切なエラーになる）
- [ ] `docs/guide/configuration.md` を更新（type の説明、サンプル、Django 単体構成セクション）
- [ ] ruff / pyright / pytest が全て通ることを確認

## 次のステップ
- 即着手して問題なし

## 更新履歴
- 2026-04-09: 作成
