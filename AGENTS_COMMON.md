# AGENTS_COMMON.md

このリポジトリで作業する coding agent (Claude Code / Codex 等) 共通の指示書です。
agent 非依存の指示はすべてここに書きます (Claude / Codex 固有の記述のみ CLAUDE.md / CODEX.md)。

## 言語設定
**このプロジェクトは日本語が基準です。エージェントとのやり取りは日本語で行ってください。**
コメント、ドキュメント、コミットメッセージなど、すべて日本語で記述してください。

## ファイルフォーマット
- 文字エンコーディング: UTF-8
- すべてのファイルは改行（LF）で終わる
- 行末の空白は削除する

## 公開リポジトリに書いてよい語彙

このリポジトリは公開されている。tracked ファイル (docs / コード / テスト / example の
`pocket.toml` / workflow のコメントを含む) に、**法人名・個人名・社内ツール名・内部 URL /
ARN・実ドメイン・メールアドレス**を書かないこと。pre-push の gitleaks (scan-leaks) が
denylist で検出して push を止める (全履歴スキャンなので、過去 commit に混ぜた場合は
commit の書き直しが要る)。

- docs / tests の例に組織名や namespace が要るときは `platform` / `myorg` のような中立語を使う
- example の `pocket.toml` で実値が要る項目 (domain / email / `[iam] namespace` 等) は
  placeholder で commit し、deploy 前に書き戻す (書き戻しは commit しない)
- `git add` はパスを明示する (`-A` で書き戻し済みの実値を巻き込んだ事故あり)
- GitHub の organization 名を含む URL (`github.com/<org>/magic-pocket` 等) は許容されている
- kadai の課題番号 (`KN1234`) は内部トラッカーの ID なので、CHANGELOG / docs / GitHub Release など
  公開物には書かない (読者に意味が通らない)。コミットメッセージへの記載は可

## Python

### 開発環境の Python

配布パッケージ (`magic-pocket` / `magic-pocket-cli`) の `requires-python` は `>=3.10` ですが、
**開発環境 (`dev` group) は Python 3.11 以上**です (`pyproject.toml` の
`[tool.uv.dependency-groups] dev = { requires-python = ">=3.11" }`)。3.10 で
`uv sync --all-groups` すると dev group の一部が入らず、pre-commit / pytest が動きません。

理由: dev group 専用の依存 (pre-commit → virtualenv → filelock) で、脆弱性の修正版が
Python 3.11 以上専用のものがあり、3.10 を含めたままだと lock に脆弱な版が残って
Dependabot alert が消えないため。配布側の対応 Python は変えずに dev group だけ上げています。
同種の alert (dev 専用依存で修正版の Python 要件が配布側より新しい) が出たときも同じ方法で対処します。

### コード品質

**重要**: uvによるパッケージ管理は以下の方法を厳守してください。

- `uv add <package>`: パッケージを追加
- `uv remove <package>`: パッケージを削除
- `uv sync --all-groups`: 依存関係を同期

**重要**: Pythonファイル（.py）を編集・作成した後は、必ず以下のチェックを実行してください。

1. **フォーマット**: `uv run ruff format` でコードを整形
2. **リントチェック**: `uv run ruff check` でエラーを検出
3. **typeチェック**: `uv run pyright` で型エラーを検出
4. **エラー修正**: エラーが検出された場合は、エラー内容を報告し、修正すること

**重要**:

- Ruffエラーを無視する設定（`# noqa`, `# ruff: noqa`, `pyproject.toml`でのignore設定など）は、勝手に追加してはいけない
- エラーを無視する必要がある場合は、必ずユーザーに報告して判断を委ねること
- エラーを残したままタスクを完了しないこと

```bash
# 1. フォーマット実行（Pythonファイル編集後は必須）
uv run ruff format .

# 2. リントチェック（エラー検出）
uv run ruff check .

# 特定のファイルのみチェック
uv run ruff check <file_path>

# 3. typeチェック（型エラー検出）
uv run pyright
```

### 型チェックのガイドライン

- **型エラーの解決方法**: `typing.cast()` は使用せず、ユーザーに確認したうえで、型アノテーション + `# type: ignore` で対処してください。
- **理由**:
  - `cast()` よりもシンプルで読みやすい
  - 実行時のオーバーヘッドがない（インポート不要）
  - 意図が明確（型チェッカーの判断が厳しすぎる場合の無視）
- **例**:
  ```python
  # 良い例
  result: tuple[int] = cursor.fetchone()  # type: ignore
  count = result[0]

  # 避ける例
  result = cast(tuple[int], cursor.fetchone())
  count = result[0]
  ```

- **boto3 client を引数で受け取るとき**: `mypy-boto3-*` の client 型を注釈する (`def ensure_role(iam_client: IAMClient, ...)`、import は `if TYPE_CHECKING:` 内)。deploy 権限の同期テスト (`tests/test_permissions_sync.py`) が型注釈で呼び出しを追跡しており、注釈の無い client 引数はテストで落ちる。新しい service は `pyproject.toml` の `boto3-stubs-lite` の extras に足す
- **Rust 版 CLI から AWS の API を呼ぶとき**: 同じテストが `crates/pocket-cli/src` の `.send()` /
  `.into_paginator()` を走査し、呼ぶ API が `pocket/permissions.py` に無いと落ちる。
  `client.operation()...send()` の形で書く (builder を変数に入れるなら同じ関数の `let`)。
  AWS でない `.send()` (reqwest) と、署名した HTTP で呼ぶ API は、テストの表
  (`_RUST_NON_AWS_SENDS` / `_RUST_SIGNED_HTTP`) に足す

### Pythonの制約事項

- except Exception は絶対に使用してはいけません。他にも曖昧な例外キャッチは避け、特定の例外をキャッチしてください。出来ない場合、raiseでプログラムが止まって構いません。
- 例外は重要な情報を含むので、無理にexceptしないでください。以下の様な、単にエラーメッセージを表示してsys.exit(1)するだけの例外処理は作らず自然に失敗させて問題ありません。
  ```python
  except mysql.connector.Error as e:
      print(f"MySQLエラー: {e}", file=sys.stderr)
      sys.exit(1)
  ```
- **sys.pathへの動的追加禁止**: `sys.path.append()`や`sys.path.insert()`でパスを動的に追加してはならない。パッケージ構造とインストールで解決すること。
- **インポートはファイル先頭で行う**: すべてのimport文はファイルの先頭に配置する。関数内での遅延インポートが必要な場合は、事前に確認すること。

### Pythonの推奨事項

- 引数を取るPythonスクリプトには、Clickライブラリを使用してください。argparseは使用しないでください。
- 続けて5行以上の情報をprintする場合、print()を何度も呼ばず、print_xxx()関数を作成してまとめてください。4行以下なら分ける必要はありません。print_xxx()関数はファイルの最後にまとめて配置してください。xxxには、表示内容が分かる名前を入れてください。長くても構いません。

### パッケージ構成と依存方針

このリポジトリは Python 2 パッケージ + Rust CLI の構成です。依存を追加・移設する際は、どれに属する変更かを必ず意識してください。

- **`magic-pocket` (runtime, リポジトリルート)**: Lambda ランタイムに同梱される。全ユーザーの Lambda にバンドルされるため、**依存は最小限に保つ**こと。新規の実行時依存を安易に追加しない。
- **`magic-pocket-cli` (`packages/magic-pocket-cli/`)**: deploy / CLI 専用。`requests` 等の重い依存はこちらに置く。
- **`magic-pocket-cli-rs` (`crates/pocket-cli/`、bin `pocket`)**: CLI の Rust 版 (段階移行中。
  現状は `static` 系と `settings`)。**Rust に移したコマンドの新機能は Rust 版だけに実装する**
  (静的系は Rust 版に一本化済みで、Python 版 `pocket static` は Rust 版の導入への
  案内だけを出す stub)。
  未移植のコマンドは Python 版だけに実装する。
  どのコマンドがどちらかは `docs/guide/cli.md` の「コマンドごとの実装の所在」の表が正で、
  移植したら表を更新する。
  Rust lib (`crates/pocket-rs`) には CLI 用の依存を足さない (runtime の依存を増やさない)。
- **CloudFormation template (`packages/magic-pocket-cli/pocket_cli/templates/cloudformation/`)**:
  Python 版と Rust 版 (`crates/pocket-cli/src/stack/`) が同じファイルを描画する。stack の
  `pocket:template_hash` タグは描画後のテキストの hash なので、**コメント 1 文字の変更でも
  全利用者の stack が REQUIRE_UPDATE になる**。template や template に渡す変数を変えたら
  `just vectors` で `tests/data/template_vectors` を作り直し、Rust 側を直して
  `tests/test_template_vectors.py` と `cargo test -p magic-pocket-cli-rs` を両方 green にする。
  template に渡す変数は値だけにする (メソッドを持つオブジェクトを渡さない。生成器が検査する)
- **`magic-pocket-settings` (`crates/pocket-settings/`)**: pocket.toml の設定モデルの Rust 版
  (runtime lib と CLI の両方が使う)。Python の `pocket.settings` との一致は共有 test vector で固定している。
  **Python の設定モデル (`pocket/settings.py` 等) を変えたら `just vectors` で
  `tests/data/settings_vectors` / `tests/data/toml_errors/*.expected.json` を作り直し、Rust 側を直して
  `tests/test_settings_vectors.py` と `cargo test -p magic-pocket-settings` を両方 green にする**。
  新しい field には既定でない値を与える fixture (`tests/data/toml_vectors/`) を、新しい検証には
  1 ファイル 1 欠陥の fixture (`tests/data/toml_errors/`) を足す (カバレッジ検査が field の不足を落とす)。
  example の pocket.toml に実値を書き戻したまま `just vectors` を回すと vector に実値が入るので、
  書き戻しを戻してから回す

CLI 専用だったロジックを runtime 側へ移設して公開 API 化する場合（例: 外部 provisioner から import 共有させる）、その依存を runtime に持ち込まないこと。**HTTP なら `requests` ではなく stdlib `urllib` で書き換える**（`pocket.provisioning.neon` はこの方針で実装済み）。やむを得ず runtime に依存を足す判断が要る場合は、**ユーザーに確認**してください。

なお CLI が runtime の新モジュールを import（re-export 含む）するようになったら、`packages/magic-pocket-cli/pyproject.toml` の `magic-pocket>=X.Y.Z` pin を、そのモジュールが載る版へ**必ず引き上げる**こと（古い runtime との組み合わせで import 落ちを防ぐ）。

### 設定 (pocket.toml) を追加するときの方針

**ステージ名に意味を持たせないこと。** 「`prod` のときだけデフォルトを安全側にする」といった分岐を実装しないでください。ステージ名は `general.stages` に書かれた任意の文字列で、プロジェクトによって `prod` / `production` / `live` と揺れます。特定の名前を特別扱いすると、命名が違うだけで安全側のデフォルトが効かなくなり、**効いていないことに気づけない**ぶん無設定より危険になりえます。現状このリポジトリにステージ名依存の分岐は 1 つもありません（`__none__` センチネル判定や data のキー参照は該当しません）。

判断は次の 2 択に落とします。

- **全ステージで安全側にすべき** → ステージで分けず、デフォルト自体を変える（例: `[rds.backup]` の `retention_days` は AWS 既定の 1 日ではなく 7 日）
- **ステージによって変えたい** → デフォルトは共通のまま。利用者がステージ上書き（`[prod.dsql]` 等）で明示する。docs にその書き方を示す

「prod だけ特別扱い」という中間案は採りません。

## Rust のビルド (開発用 VM のメモリ上限)

forge の VM (コンテナ) はメモリの上限が約 4GB で、超えても OOM kill されず、reclaim と swap で
**エラーを出さないまま進まなくなる** (VM ごと固まり、session が切れる)。`free` は空きを大きく
見せるので当てにならない。上限は `/sys/fs/cgroup/memory.high`、到達の回数は
`/sys/fs/cgroup/memory.events` の `high` で見る。

- **VM では `just test` / `just lint-rust` を使う**。CLI (`magic-pocket-cli-rs`) を cargo で直接
  ビルドするときは `--no-default-features` を付ける (`cargo test -p magic-pocket-cli-rs --no-default-features`)。
  付けないと aws-sdk-ec2 (rustc 1 個で約 3GB) がコンパイルされて固まる。素の `cargo build` /
  `cargo test` (workspace 全体) も同じ
- aws-sdk-ec2 は CLI の feature `ec2` (既定で有効) の後ろにある。**EC2 を呼ぶコードは
  `crates/pocket-cli/src/ec2.rs` に集め、feature あり・なしの両方に同じ関数を置く**。
  feature ありの側は VM ではコンパイルされないので、触ったら push して CI (ec2 込みの
  clippy / test) で確かめる。VM でビルドした `pocket` は EC2 を呼ぶ箇所でエラーになる
- **root の `Cargo.toml` の `[profile.dev]` と `.cargo/config.toml` の `jobs = 2` は外さない**
  (依存の debuginfo を切ってリンクのメモリと `target/` を減らし、重い crate の同時コンパイルを
  2 個までにする)。CI は `CARGO_BUILD_JOBS: default` で並列数の上限を外している
- **aws-sdk-* を足すときは rustc の最大メモリを測る** (2GB を大きく超えるなら feature の後ろに置く)。
  実測 (dev profile): ec2 3.1GB 以上 (完走しない) / rds 2.2GB / ssm 2.0GB / iam 1.8GB / s3 1.6GB /
  sesv2 1.3GB
- 依存が全部作り直しになる操作 (`cargo clean`、profile や feature の変更、別の target dir) を
  試すときは、上限を超えたら固まらずに止まるよう
  `systemd-run --user --scope -p MemoryMax=3600M -p MemorySwapMax=0 cargo ...` の中で実行する
- dev profile や feature の構成を変える commit では、workflow の rust-cache の `prefix-key` を上げる
  (古い cache の上にフルビルドが載ると runner のディスクが尽きる)

## 検査の置き場所 (pre-commit / pre-push / CI)

- **pre-commit** (commit 毎、数秒): ruff / ruff format / pyright / `cargo fmt --check` 等の軽い検査
- **pre-push**: gitleaks (scan-leaks) だけ。push は数秒で終わる
- **CI の `test` workflow** (`.github/workflows/test.yml`、dev push と PR で起動):
  semgrep (`just semgrep-all`) / clippy (`just lint-rust`) / pytest + cargo test (`just test`)。
  push しただけでは検査が終わっていないので、区切り (リリース・main への ff・完了報告) の前に
  `gh run list --workflow test.yml --branch dev` で対象 commit の結果を確かめる。
  dev が一時的に赤になるのは許容するが、赤のまま tag や main へ進めない

pre-push に重い検査を戻さないこと。push 中に tracked ファイルを編集すると pre-commit が
`files were modified by this hook` で偽失敗し、検査対象も push する commit ではなく作業ツリーになる。

## リリース手順

リリース (version バンプ → CHANGELOG 確定 → tag push → PyPI publish) と、リリース後に
毎回行うフォローアップ (example の pin 更新・GitHub Release 作成・main の fast-forward) は
**`docs/releasing.md` の手順に従う**。手順はそのページが正本で、ここには要約を置かない
(要約に無い手順が抜ける事故を防ぐため)。
