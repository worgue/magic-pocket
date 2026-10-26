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
   - `packages/magic-pocket-cli/pyproject.toml` の `version` と依存
     `magic-pocket>=X.Y.Z,<X.(Y+1)` (**上限も同 minor に束縛する**。0.x では minor が
     breaking の単位のため、上限なしだと cli を version pin した消費者にも lib の
     破壊的変更がリリース当日に届いて deploy が壊れる)
2. **`uv.lock` を反映する** (version バンプで差分が出る。`uv lock` か `uv run` で更新)。
3. **CHANGELOG を確定する**: `[Unreleased]` を
   `## [X.Y.Z](https://github.com/worgue/magic-pocket/releases/tag/X.Y.Z) - YYYY-MM-DD`
   に書き換える。
4. **コミットする**: `:bookmark: X.Y.Z リリース (<要約>)`。
5. **dev を push する**: `git push origin dev` (pre-push hook で gitleaks / ruff /
   semgrep / pyright / pytest が走る。green でないと push されない)。
6. **tag を作成して push する**: `git tag -a X.Y.Z -m "..."` → `git push origin X.Y.Z`。
   これで `release.yml` が発火し PyPI publish される。
7. **publish を確認する**: `gh run watch <id>` で workflow 成功を確認し、
   `curl -s https://pypi.org/pypi/magic-pocket/json` 等で latest が `X.Y.Z` になることを確認。

## リリース後に必ず実行する (毎回)

8. **example の magic-pocket バージョンを更新する** (`example-neon` / `example-tidb` は
   PyPI 公開版 `magic-pocket[django]==X.Y.Z` を pin 参照している):
   1. 各 `example-*/pyproject.toml` の `magic-pocket[django]==X.Y.Z` を新バージョンへ更新
   2. 各 example で `uv lock`。リリース直後は uv の index cache に新版がまだ無く、
      「requirements are unsatisfiable」で失敗することがある。その場合は
      `uv lock --refresh-package magic-pocket` で通る
   3. `example-dsql/Cargo.toml` の `magic-pocket-rs` の `tag = "X.Y.Z"` を新バージョンへ更新し、
      `cargo update -p magic-pocket-rs` で `Cargo.lock` を反映する (tag push の後に行う)
   4. 6 ファイル (pyproject.toml + uv.lock × 2 example、example-dsql の Cargo.toml + Cargo.lock) を
      コミット: `:arrow_up: example の magic-pocket を X.Y.Z に更新`

   !!! note "example は Dependabot の対象外 (手動で毎回)"
       `.github/dependabot.yml` は repo root (`directory: /`) だけを見ているため、
       `example-*/` の pin は Dependabot では上がらない。リリースごとにこの手順で
       手動更新する。sandbox にデプロイ済みの Lambda への反映は、次回その example を
       再 deploy したとき。

   !!! note "vendor wheel は廃止済み"
       以前は git 管理外の vendor wheel を参照していたが、gitignore + 非決定的ビルドで
       wheel を作り直すと `uv.lock` のハッシュが不一致になり `uv sync --frozen` が落ちる
       footgun があったため PyPI 版に切替えた。未リリース変更を example で検証したい
       ときだけ、一時的に `[tool.uv.sources]` でローカル wheel を override する。

9. **GitHub Release オブジェクトを作成する** (毎回。CHANGELOG の当該節を本文にする。
   本文は引数渡しではなくファイルで渡すとバッククォート等の展開事故がない):

   ```bash
   awk '/^## \[X.Y.Z\]/{f=1;next} /^## \[/{f=0} f' CHANGELOG.md > /tmp/notes.md
   gh release create X.Y.Z --title "X.Y.Z" --notes-file /tmp/notes.md
   ```

   agent 環境では「公開面の作成」として権限ゲートに止まることがある。その場合は
   作成を人間に依頼する (作り忘れが続くと Release 一覧と tag がずれる。抜けた版は
   同じコマンドで後から作ればよい)。

10. **main を fast-forward する**: `git push origin dev:main`。

!!! note "dev / main の運用について"
    magic-pocket は **dev ベース運用**。日常の作業・リリースコミット・tag はすべて
    dev 上で行い、tag も dev のコミットに打つ (`release.yml` は tag push で発火する
    ため branch は問わない)。main はリリース等の区切りで dev を fast-forward で
    反映する (手順 10)。

!!! note "手順の正本はこのページ"
    `AGENTS_COMMON.md` には手順を書かず、このページを参照する。手順を変えるときは
    ここだけを直す (要約を別に持つと、要約に無い手順 = example-dsql の tag 更新や
    GitHub Release 作成が抜ける。0.40.0〜0.46.0 で実際に抜けた)。
