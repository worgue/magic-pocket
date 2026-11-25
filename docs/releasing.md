# リリース手順

`magic-pocket` (runtime lib) と `magic-pocket-cli` (deploy CLI) を PyPI へ
リリースするときの手順。**tag `X.Y.Z` の push で GitHub Actions
(`.github/workflows/release.yml`、PyPI Trusted Publishing / OIDC) が両パッケージを
build & publish** し、あわせて **Rust 版 CLI `pocket` の 4 target のバイナリをビルドして
GitHub Release (本文は CHANGELOG の当該節) に添付**する。

役割は 2 つに分かれる:

- **リリースの準備 (LLM が dev で行う)**: version のバンプ・CHANGELOG の確定・リリースコミット・
  dev への push と CI の確認 (手順 1〜5)
- **昇格とリリース (op が host で行う)**: `just ship-prod` (手順 6)。origin/dev 先端の CI を
  検証し、origin/main を ff して、`pyproject.toml` の version の tag がまだ無ければ tag を打って
  push する。この repo は deploy 先を持たないので、prod 昇格の最後が deploy ではなくリリースになる。
  PyPI への publish は取り消せないため、tag を打つのは確認を挟める host の操作に寄せている

`just ship-prod` は版を決めない。version の tag が既にあれば main の ff だけを行い
(未リリースの変更を main に届ける)、無ければその版をリリースする。**LLM は main と tag を push しない**。

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
   - `crates/pocket-cli/Cargo.toml` の `version` (Rust 版 CLI `pocket`。`--version` と
     Release の asset の版がこれになる。`release.yml` の build-cli も tag との一致を検証する)
2. **`uv.lock` と `Cargo.lock` を反映する** (version バンプで差分が出る。`uv lock` か
   `uv run` と、`cargo check -p magic-pocket-cli-rs` で更新)。
3. **CHANGELOG を確定する**: `[Unreleased]` を
   `## [X.Y.Z](https://github.com/worgue/magic-pocket/releases/tag/X.Y.Z) - YYYY-MM-DD`
   に書き換える。本文は GitHub Release にそのまま転記されるので、kadai の課題番号
   (`KN1234`) など内部トラッカーの ID は書かない (`grep -c 'KN[0-9]' CHANGELOG.md` が 0)。
4. **コミットする**: `:bookmark: X.Y.Z リリース (<要約>)`。
5. **dev を push し、CI の test が green になるのを待つ**: `git push origin dev`
   (pre-push hook は gitleaks だけ)。重い検査 (semgrep / clippy / pytest / cargo test) は
   GitHub Actions の `test` workflow が回すので、`gh run list --workflow test.yml --branch dev`
   で release commit の run を見つけ、`gh run watch <id> --exit-status` で success を確かめる。
   green を確かめたら、リリースコミットの sha を添えて op に手順 6 を依頼する。
6. **op が `just ship-prod` を実行する** (host の main worktree)。リリースするときだけ
   確認 (y/N) を挟み、次を順に行う:
   - version の一致 (`pyproject.toml` / `packages/magic-pocket-cli/pyproject.toml` /
     `crates/pocket-cli/Cargo.toml`)、CHANGELOG の `## [X.Y.Z]` の節があること・
     `[Unreleased]` に中身が残っていないこと・課題番号が無いことを確かめる
   - origin/dev 先端の CI (`test.yml`) が success であることを確かめる (実行中なら待つ。
     run が無ければ止まる)
   - origin/main を ff し、main worktree の checkout を追随させる
   - tag `X.Y.Z` を打って push する (`release.yml` が発火し PyPI へ publish される)
   - release の run の完走 (`publish` / `build-cli` ×4 / `github-release` の全 job) と、
     Release の asset が 8 個あることを確かめる

   先に内容だけ見るなら `DRY_RUN=1 just ship-prod` (push を表示だけにする)。
   リリースコミットの後ろに変更を積んだ場合は、`[Unreleased]` に未確定の変更が残るので
   ship-prod が止まる。リリースコミットの sha を渡す (`just ship-prod <sha>`) と、そこまでを
   昇格してリリースする (その commit が push の先端だったときの CI の run が要るので、
   リリースコミットを push して CI が回ってから次の変更を push する)。
7. **publish を確認する**: `curl -s https://pypi.org/pypi/magic-pocket/json` 等で latest が
   `X.Y.Z` になることを確認する。ship-prod が途中で止まったら、表示された原因を直して
   `just ship-prod` を再実行する (済んだ段階 = main の ff・手元の tag は飛ばして続きから進む)。
   release の一部の job だけ失敗したら `gh run rerun <id> --failed` で失敗分を再実行し
   (`github-release` は既存の Release があれば本文を更新して asset を `--clobber` で
   差し替えるので、再実行に耐える)、完走後に `just ship-prod` を再実行すると Release を
   確かめ直す。ship.sh を更新した直後の初回は「この ship.sh は origin/dev の版と違います」で
   止まるので、案内どおり main worktree で `git merge --ff-only origin/dev` してから実行し直す。

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

9. **GitHub Release を確認する** (作成は `release.yml` の `github-release` job が行う。
   本文は CHANGELOG の `## [X.Y.Z]` 節、asset は `pocket` の 4 target × (tar.gz + sha256) =
   8 個):

   ```bash
   gh release view X.Y.Z --json assets,body --jq '{assets: [.assets[].name], body: .body}'
   ```

   asset が 8 個無い・本文が空のときは手順 7 の `gh run rerun --failed` で作り直す。
   workflow を経ずに手で作った Release が先にあっても、job は本文を更新して asset を
   差し替える (作り直しに耐える)。

10. **main を example の pin 更新まで進める (op が host で行う)**: 手順 8 の commit を push して
    CI の `test` が success になったら、op に `just ship-prod` をもう一度依頼する。version の
    tag は既にあるので、main の ff だけが行われる。main の ruleset は更新を org admin に限り、
    required check `test` の success も要求するので、LLM (git-ops App) からの push は
    `GH013` で拒否される。ff できない (main が分岐している) ときは ship-prod が止まるので、
    force push せず報告する。

!!! note "dev / main の運用について"
    magic-pocket は **dev ベース運用**。日常の作業とリリースコミットは dev 上で行う。
    main は dev を fast-forward で追う昇格ポインタで、op の `just ship-prod` だけが進める
    (tag も ship-prod が main の先端と同じ commit に打つ)。

!!! note "手順の正本はこのページ"
    `AGENTS_COMMON.md` には手順を書かず、このページを参照する。手順を変えるときは
    ここだけを直す (要約を別に持つと、要約に無い手順 = example-dsql の tag 更新や
    GitHub Release の確認が抜ける。0.40.0〜0.46.0 で実際に Release 作成が抜けた。
    0.47.0 からは workflow が作る)。
