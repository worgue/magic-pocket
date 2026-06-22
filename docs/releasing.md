# リリース手順

`magic-pocket` (runtime lib) と `magic-pocket-cli` (deploy CLI) を PyPI へ
リリースするときの手順。**tag `X.Y.Z` の push で GitHub Actions
(`.github/workflows/release.yml`、PyPI Trusted Publishing / OIDC) が両パッケージを
build & publish** する。

## バージョンの決め方 (SemVer)

- **バグ修正のみ** → patch (例: `0.2.1` → `0.2.2`)
- **後方互換な新機能** → minor (例: `0.3.0` → `0.4.0`)。0.x でも feature は minor
- **破壊的変更** → 0.x ではコミット内容次第で minor に含める (1.0 以降は major)

`### Features` を含むリリースは原則 minor バンプ。

## 手順

1. **version をバンプする** (tag と一致が必須。`release.yml` の "Check tag matches
   package versions" が両 pyproject の version == tag を検証する):
   - `pyproject.toml` の `version`
   - `packages/magic-pocket-cli/pyproject.toml` の `version` と依存 `magic-pocket>=X.Y.Z`
2. **`uv.lock` を反映する** (version バンプで差分が出る。`uv lock` か `uv run` で更新)。
3. **CHANGELOG を確定する**: `[Unreleased]` を
   `## [X.Y.Z](https://github.com/worgue/magic-pocket/releases/tag/X.Y.Z) - YYYY-MM-DD`
   に書き換える。
4. **コミットする**: `:bookmark: X.Y.Z リリース (<要約>)`。
5. **main を push する**: `git push origin main` (pre-push hook で gitleaks / ruff /
   semgrep / pyright / pytest が走る。green でないと push されない)。
6. **tag を作成して push する**: `git tag -a X.Y.Z -m "..."` → `git push origin X.Y.Z`。
   これで `release.yml` が発火し PyPI publish される。
7. **publish を確認する**: `gh run watch <id>` で workflow 成功を確認し、
   `curl -s https://pypi.org/pypi/magic-pocket/json` 等で latest が `X.Y.Z` になることを確認。

## リリース後に必ず実行する (毎回)

8. **example の vendor wheel を更新する** (`example-neon` / `example-tidb` は
   magic-pocket を **git 管理外の vendor wheel** で参照しているため、Dependabot では
   更新できない。手動対応が必要):
   1. repo root で `uv build` → `dist/magic_pocket-X.Y.Z-py3-none-any.whl` を生成
   2. 各 `example-*/vendor/` に新 wheel をコピーし、**旧バージョンの wheel は削除**
   3. 各 `example-*/pyproject.toml` の `[tool.uv.sources]` の wheel filename を `X.Y.Z` に更新
   4. 各 example で `uv lock --upgrade-package magic-pocket`
   5. 4 ファイル (pyproject.toml + uv.lock × 2 example) をコミット:
      `:arrow_up: example の magic-pocket vendor wheel を X.Y.Z に更新`
      (`.whl` 本体は git 管理外なのでコミットされない)

9. **GitHub Release オブジェクトを作成する** (毎回。CHANGELOG の当該節を本文にする):

   ```bash
   gh release create X.Y.Z --title "X.Y.Z" --notes "<CHANGELOG の該当節>"
   ```

!!! note "main / tag の push について"
    magic-pocket は main ベース運用なので、main / tag はそのまま
    `git push origin main` / `git push origin X.Y.Z` でよい。
