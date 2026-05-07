# cloudfront_acm.yaml の redirect_from cert で rf.hosted_zone_id を使う

| 項目 | 値 |
|------|-----|
| ID | #11 |
| Priority | `low` |
| Category | `bug` |
| Date | 2026-05-06 |
| Tags | `cfn`, `acm`, `cloudfront`, `redirect-from`

## 目的

`packages/magic-pocket-cli/pocket_cli/templates/cloudformation/cloudfront_acm.yaml`
で redirect_from の cert に対する `DomainValidationOptions.HostedZoneId` が
**親 (CloudFront) の `hosted_zone_id` を参照している**バグを修正する。
`rf.domain` が親 cert の domain と別の Hosted Zone に属する場合、検証 CNAME が
誤った Hosted Zone に作成され検証失敗する可能性がある。

## 現状

`cloudfront_acm.yaml` line 19-31:

```yaml
# {% for rf in redirect_from %}
"Certificate{{ rf.yaml_key }}":
  Type: AWS::CertificateManager::Certificate
  Properties:
    DomainName: "{{ rf.domain }}"
    DomainValidationOptions:
      - DomainName: "{{ rf.domain }}"
        HostedZoneId: "{{ hosted_zone_id }}"   # ← 親の hosted_zone_id (バグ)
    ...
```

`RedirectFromContext` には独自の `hosted_zone_id` (computed_field) が用意されており、
`rf.hosted_zone_id` を参照すべき (`pocket/context.py:643`)。

## 修正方針

```yaml
HostedZoneId: "{{ rf.hosted_zone_id }}"
```

に変更。`RedirectFromContext.hosted_zone_id` は `hosted_zone_id_override` か
`get_hosted_zone_id_from_domain(rf.domain)` から解決される。

## 影響範囲

- redirect_from で親 CloudFront と別ゾーンのドメインを指定しているプロジェクト
  (現時点で該当プロジェクトがあるかは未確認)
- 既存スタックでは template_hash 変更により次回 deploy 時に CFn 更新が走る。
  cert の `DomainValidationOptions` 変更は cert 自体の置き換え (replace) を伴う
  可能性があるため、影響を慎重に評価する必要あり

## タスクリスト

- [ ] `cloudfront_acm.yaml` line 26 の `hosted_zone_id` を `rf.hosted_zone_id` に変更
- [ ] CFn 更新時に cert が replace されるかを AWS docs で確認
  (replace される場合は migration 注意書きが必要)
- [ ] テスト: redirect_from で異なるゾーンのドメインを指定したケース
- [ ] ruff / pyright / pytest

## 関連

- フィードバック (関連発見): `feedbacks/active/magic-pocket/20260505-worgue-acm-validation-cname-orphan/`
- 関連タスク: #10 (orphan 全般)

## 更新履歴
- 2026-05-06: 作成
