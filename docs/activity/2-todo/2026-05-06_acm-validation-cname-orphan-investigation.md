# ACM validation CNAME orphan のテンプレート対応検討

| 項目 | 値 |
|------|-----|
| ID | #10 |
| Priority | `low` |
| Category | `bug` |
| Date | 2026-05-06 |
| Tags | `cfn`, `acm`, `route53`, `worgue-integration`

## 目的

worgue から報告された ACM 証明書 + Route53 validation CNAME の orphan 問題
(`feedbacks/active/magic-pocket/20260505-worgue-acm-validation-cname-orphan/`) のうち、
**現状のテンプレで未対応の edge case** を解消する。

調査の結果、worgue の主張する「`DomainValidationOptions.HostedZoneId` を指定していない」
は CloudFront ACM (us-east-1) では既に修正済み (テンプレ初出 2026-03-11 から指定あり)。
ただし、以下 2 点は未対応:

1. **awscontainer.yaml の ApiGateway 用 cert** で `apigateway.create_records=False`
   のとき `DomainValidationOptions` 全体がスキップされる
2. **cloudfront_acm.yaml の redirect_from 用 cert** が親の `hosted_zone_id` を参照しており、
   `rf.domain` が親と別ゾーンの場合に検証失敗する可能性

(redirect_from の hosted_zone_id 修正は別タスク #11 として切り出し済み。
このタスクでは 1 のみを対象にする。)

## 背景: worgue の orphan の発生経路

steeldoor sandbox stage 移行で `sandbox.steeldoor.worgue.jp` の ACM 証明書 + 検証 CNAME が
orphan として残った件:

- region: us-east-1 → CloudFront 用
- 現状の `cloudfront_acm.yaml` は `HostedZoneId` を指定済みなので、新規スタックでは
  orphan は発生しない
- steeldoor のケースは AcmStack 分離 (2026-03-11) より前の旧テンプレで作成された
  スタック由来と推定される。**既存 orphan は手動削除が必要** (magic-pocket 側で
  自動回収する手段はない)

## 検討事項: create_records=False のときの扱い

`apigateway.create_records=False` は「pocket に DNS A レコードを触らせない」という
明示的なオプトアウト。現実装では DNS A レコードに加えて検証 CNAME も pocket 管理外
となり、stack delete 時の orphan を許容している。

選択肢:

### Option A: 検証 CNAME だけは常に pocket 管理にする
- `DomainValidationOptions.HostedZoneId` を `create_records` に関係なく常に出力
- メリット: 検証 CNAME の orphan が消える
- デメリット: hosted_zone_id の解決が必須になる
  (現状は `create_records=False` のとき hosted_zone_id 解決をスキップしている — `pocket/context.py:40`)
- ユーザー側の必須要件: pocket に該当 hosted zone への書き込み権限が必要

### Option B: ドキュメントで注意喚起のみ
- テンプレは触らず、`docs/...` で `create_records=False` 利用時は orphan が発生
  し得る旨を明記
- メリット: 後方互換性が完全に保たれる
- デメリット: 根本解決にならない

### Option C: 新オプション `manage_validation_records` を追加
- `create_records` とは独立に「検証 CNAME だけは pocket に管理させる」オプション
- メリット: 既存挙動を保ちつつ、明示的な opt-in で orphan 回避できる
- デメリット: オプション増加で混乱を招く可能性

## タスクリスト

- [ ] Option A/B/C のどれを採用するかユーザーと合意
- [ ] (Option A or C の場合) `awscontainer.yaml` テンプレ修正
- [ ] (Option A or C の場合) `pocket/context.py` の `hosted_zone_id` 解決ロジック調整
- [ ] (Option B の場合) `docs/permissions/aws.md` または別 docs に注記
- [ ] テスト追加 / 既存テストの調整
- [ ] response.md (`feedbacks/active/magic-pocket/20260505-worgue-acm-validation-cname-orphan/`) を `done` に更新

## 関連

- フィードバック: `feedbacks/active/magic-pocket/20260505-worgue-acm-validation-cname-orphan/`
- 関連バグ修正: #11 (`cloudfront_acm.yaml` redirect_from の hosted_zone_id)
- 当該テンプレ: `packages/magic-pocket-cli/pocket_cli/templates/cloudformation/awscontainer.yaml` (line 287-300)

## 更新履歴
- 2026-05-06: 作成 (フィードバック `20260505-worgue-acm-validation-cname-orphan` の調査として)
