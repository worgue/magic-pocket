# 依存関係の週次自動更新

| 項目 | 値 |
|------|-----|
| ID | #1 |
| Priority | `medium` |
| Category | `infra` |
| Date | 2026-04-04 |
| Periodic | `weekly` |
| Tags | `dependabot`, `github-actions`, `uv`, `cargo` |

## 目的
- `uv.lock` が古くなると GitHub のセキュリティアラートが大量に出るため、定期的に lockfile を更新したい
- Dependabot は `uv.lock` を直接サポートしていないため、GitHub Actions で `uv lock --upgrade` を実行する仕組みを別途構築する
- magic-pocket はライブラリとして広めの依存範囲を持つため、`pyproject.toml` の更新は必要最小限（`increase-if-necessary`）にする

## タスクリスト
- [ ] `dependabot.yml` を更新
  - `pip` と `cargo` の schedule を `weekly` / `day: friday` に変更
  - `pip` に `versioning-strategy: increase-if-necessary` を追加
- [ ] GitHub Actions ワークフローを作成（`.github/workflows/uv-lock-update.yml`）
  - cron で毎週金曜日に実行
  - ルート・`example-tidb/`・`example-neon/` の `uv.lock` を `uv lock --upgrade` で更新
  - 変更があれば PR を自動作成
- [ ] 動作確認（手動トリガーで試す）

## 設計メモ

### dependabot.yml の変更

```yaml
version: 2
updates:
  - package-ecosystem: "cargo"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "friday"
    open-pull-requests-limit: 10
    groups:
      aws-sdk:
        patterns:
          - "aws-*"

  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
      day: "friday"
    open-pull-requests-limit: 10
    versioning-strategy: increase-if-necessary
```

### GitHub Actions ワークフロー（uv-lock-update.yml）

- `schedule: cron: "0 9 * * 5"` (毎週金曜 9:00 UTC)
- `workflow_dispatch` で手動実行も可能に
- 手順:
  1. checkout
  2. uv をインストール
  3. 各ディレクトリで `uv lock --upgrade` を実行
  4. 変更があれば branch を作成し PR をオープン（`peter-evans/create-pull-request` action を利用）

## 次のステップ
- 方針の承認後、実装に着手

## 更新履歴
- 2026-04-04: 作成
