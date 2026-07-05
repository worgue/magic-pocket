# Changelog
全ての重要な変更はこのファイルに記録されます。

書き方は[Keep a Changelog](http://keepachangelog.com/en/1.0.0/)に基づきます。<br>
バージョンは[Semantic Versioning](http://semver.org/spec/v2.0.0.html)に従います。

## [Unreleased]

### Changed
- **stored user secret の保存先を `type` 基準の正準名に変更しました（破壊的変更）**。
  従来 `type` 指定の user secret（stored mode）は、保存先の SSM/SM 名を
  **consumer の env var 名（`[awscontainer.secrets.user]` の辞書キー）**から導出して
  いました（`/{pocket_key}-user/{ENV_KEY}`）。このため env var のリネームや backend
  の付け替えで保存先が動き、backend 移行（`neon`→`tidb` 等）で「保存済み URL を引け
  ない」問題の温床になっていました。これを **`type` 基準**（`/{pocket_key}-user/{type}`、
  例 `/{pocket_key}-user/neon_database_url`）に変更しました:
  - 保存 identity が env var 名から独立し、リネームや付け替えで動きません。
  - 同一 `type` の user secret は **1 stage につき 1 個まで**に制限されます（保存先が
    衝突するため。設定ロード時のバリデーションで検出）。
  - `pocket resource <db> url` は、consumer の `DATABASE_URL` が別 backend を指して
    いても **`type` 基準でその backend の保存 URL を解決**できるようになりました
    （宣言が無くても正準パスから直接読む）。
  - **移行**: 0.11 以前で provision 済みの環境は、アップグレード後に
    `pocket migrate secret-paths --stage <stage>`（または引数なしの `pocket migrate`）
    を実行して旧パス→新パスへ値を移設してください（copy→検証→旧削除、冪等）。
    ※ backend の cutover を既に済ませて `type` 宣言が消えている旧値は自動移設の対象外
    です（旧キーを導出できないため）。その場合は `store-url` の再実行、または旧パスの
    値を手動で新パスへ copy してください。
- **`redirect_from` を CloudFront Function 方式に作り替えました**。従来は
  リダイレクト元ドメインごとに「専用 ACM 証明書 + 専用 S3 website バケット
  （`RedirectAllRequestsTo`）+ 専用ディストリビューション」を作っていましたが、
  これを次の構成に置き換えました:
  - リダイレクト元ドメインを**メインディストリビューションの Alias** に追加し、
    証明書も**メイン証明書の SAN** にまとめる（専用証明書・専用配信を作らない）。
  - canonical ドメインへの **301 リダイレクトを viewer-request の CloudFront
    Function** で返す（path・query を保持）。既存の viewer-request Function
    （API host / SPA fallback / deploy-hash strip 等）には同等の redirect
    prelude を注入し、Function を持たない behavior にのみ専用の redirect
    Function を割り当てるため、**全 behavior で確実にリダイレクト**されます。
  - これにより、専用 S3 website バケット作成に起因する
    **`IllegalLocationConstraintException`（bucket region 不整合）**と、
    リダイレクト専用証明書の**論理名バグ**の温床が構造的に解消されます。
  - 既存環境の更新時、旧実装が残した S3 website バケットは配信更新後に**冪等に
    削除**します（別アカウント所有等で消せない場合は警告のみ）。
  - `pocket.toml` の設定（`redirect_from = [{ domain = ... }]`）は**従来どおり**で、
    移行のための記述変更は不要です。

### Added
- **`pocket migrate` をサブコマンド構成に整理しました**。
  - `pocket migrate secret-paths`: stored user secret を旧キー基準パスから新 `type`
    基準パスへ移設します（0.11→0.12。copy→検証→旧削除、冪等。`--dry-run` で確認可）。
  - `pocket migrate template-hash`: 従来の `pocket migrate`（スタックのテンプレート
    ハッシュタグ一括付与）です。
  - 引数なしの `pocket migrate` は上記を **secret-paths → template-hash の順に冪等実行**
    します（template-hash がテンプレ差分で中断しても secret-paths は完了済みで、
    `pocket deploy` 後の再実行が安全）。
  - **破壊的変更**: 従来の bare `pocket migrate`（テンプレートハッシュ付与）は
    `pocket migrate template-hash` へ移動しました。

## [0.11.0](https://github.com/worgue/magic-pocket/releases/tag/0.11.0) - 2026-07-05

### Added
- **CLI と runtime のバージョン不整合を、分かりやすいエラーで検知するようにしました**。
  デプロイを叩く `pocket` CLI（`magic-pocket-cli`）と Lambda 内 runtime（`magic-pocket`）は
  別パッケージで版が独立に固定されるため、CLI が新機能スキーマの `pocket.runtime.toml` を
  書いても runtime が古いと解釈できず、Lambda の INIT フェーズで `Runtime.Unknown` として
  不透明に失敗していました。
  - `pocket.runtime.toml` の先頭に**生成元 CLI 版を TOML コメントとして刻み**ます。コメント
    なので**古い runtime（tomllib）は無視**し、後方互換は壊れません。
  - runtime は設定読込時に「生成元版 > 自身の版」を検出したら、`Runtime.Unknown` の代わりに
    **対処（`uv add 'magic-pocket[django]>=X.Y.Z'`）を促す明快な例外**を出します（この検査を
    含む 0.11.0 以降の runtime で有効）。
  - deploy の management ステップが **INIT フェーズ失敗**（`Runtime.Unknown` /
    `INIT_REPORT ... Status: error`）を検出した場合、「アプリの traceback を確認」ではなく
    **runtime 側（バージョン不整合を含む）を疑う案内**に切り替え、切り分けの誤誘導を減らします。
  - あわせて deploy ガイド（実行環境）に CLI/runtime の版結合と lock 更新手順を明記しました。

## [0.10.0](https://github.com/worgue/magic-pocket/releases/tag/0.10.0) - 2026-07-05

### Added
- **`pocket resource neon url` / `pocket resource tidb url` を追加しました**。指定 stage の
  backend 接続 URL を **純 URL のみ** stdout に出力します（診断・警告は stderr）。backend
  移行ツール（Neon→TiDB 等）が接続 URL を app 側で自前解決（SSM パラメータ名のハードコード
  や `neonctl` 直叩き）せずに、`$(pocket resource neon url --stage prod)` で取得できます。
  - 解決方式は既定で **stored-first**（`type = "<db>_database_url"` の user secret を読む。
    副作用が無く、consumer が実際に使う URL と一致）。未 provision の場合のみ provider API
    での live 算出に fallback します。`--live` で常に provider API から算出します。
  - TiDB は password reveal API が無く URL 算出が root password を rotate するため、既定を
    stored-first にして rotate を回避しています（`--live` 指定時のみ rotate。consumer の
    redeploy が前提）。
  - 移行中に `[neon]` と `[tidb]` を **併記**（dual-declaration）した状態で、resource ごとに
    `neon` / `tidb` を呼び分ければ source/target 双方の URL を解決できます。

## [0.9.1](https://github.com/worgue/magic-pocket/releases/tag/0.9.1) - 2026-07-05

### Fixed
- **`redirect_from` の ACM 証明書 CFn 論理名にハイフン等の非英数字が残り deploy が
  失敗する不具合を修正しました**。`RedirectFromContext.yaml_key` が `domain.split(".")`
  + `capitalize()` で論理名を組み立てていたため、ハイフンを含む domain
  （`apex→www` の定番 redirect でごく一般的）では CFn 論理 ID に非英数字が残り
  `Template format error: Resource name ... is non alphanumeric` で UpdateStack が
  失敗していました。`RouteContext` / `_camel` と同様に非英数字を境界にして除去する
  ように揃えました。
  - **既存デプロイへの影響なし**: 非英数字を含まない domain では旧実装と同一の論理名に
    なる（挙動不変）ため、影響を受けるのはハイフン等を含む domain のみで、それらは
    そもそも従来 deploy できていませんでした。

## [0.9.0](https://github.com/worgue/magic-pocket/releases/tag/0.9.0) - 2026-07-04

### Added
- **S3 route で `origin_path` を省略できるようにしました**（`path_pattern` が prefix を持つ
  route のみ）。S3 の key prefix は `origin_path + path_pattern` で計算されるため、
  `path_pattern = "/media/*"` の route に `origin_path = "/media"` を付けると S3 実キーが
  `media/media/...` の**二重階層**になっていました。`origin_path` を省略すると `path_pattern`
  由来の**単一 prefix**（`media/...`）になり、`aws s3 sync` 等でバケットを直接操作する運用で
  prefix が直感的になります。catch-all（`path_pattern = ""` / `"/*"`）は prefix を持たないため
  `origin_path` は必須のままです。
  - あわせて、S3 prefix 重複検査と OAC バケットポリシーの prefix 計算にも `origin_path` 省略
    route を含めるよう修正しました（含めないと空 origin route のオブジェクトがポリシー範囲外に
    なり CloudFront が 403 になる不具合を回避）。
  - **既存デプロイへの影響**: 既存 route から `origin_path` を外すと S3 key prefix が変わり
    既存オブジェクトが参照できなくなるため、単一 prefix へ移行する場合は既存オブジェクトの
    移送が必要です（opt-in。既存 toml はそのままなら挙動不変）。

## [0.8.1](https://github.com/worgue/magic-pocket/releases/tag/0.8.1) - 2026-07-03

### Fixed
- **RDS `create()` を冪等化**しました。途中で失敗した deploy の再実行や、一部リソース
  だけ先行作成済みのケースで、DB Subnet Group / Security Group / クラスタ / インスタンスが
  `...AlreadyExists` で落ちていたのを、既存を検出して再利用・skip するようにしました。
  static パスワードの再生成や password 切替 modify は「新規作成/復元したセッションのみ」
  実施するため、再実行で認証情報が作り直されることもありません。
- **RDS の snapshot 復元で、`modify_db_cluster`（master password 切替）の前にインスタンスが
  `available` になるまで待つ**ようにしました。従来はクラスタの available のみ待っており、
  インスタンスが `creating` の状態で modify が走って反映されない/失敗する可能性がありました。
- **management command（migrate 等）の失敗が「緑」で通っていた（false green）のを修正**
  しました。ハンドラは非同期 (`InvocationType="Event"`) で invoke され戻り値/例外が CLI に
  伝わらないため、成功時のみ出力するセンチネルを導入し、CLI 側 (`show_logs`) が REPORT
  までにそれを観測できなければ `ManagementCommandFailed` で**非ゼロ終了**するようにしました。
  これにより `pocket deploy` 中の migrate 失敗が握り潰されず deploy が止まります。

## [0.8.0](https://github.com/worgue/magic-pocket/releases/tag/0.8.0) - 2026-07-03

### Changed
- **RDS の既定 DB 名の順序を `{project}_{stage}` から `{stage}_{project}` に変更しました**
  （例 `myapp_prod` → `prod_myapp`）。クラスタ識別子や Subnet Group など他の RDS リソース名は
  すべて `{stage}-{project}` 順（`resource_prefix` 由来）だったのに、DB 名だけ project 先頭で
  順序が食い違っていたのを揃えるものです。
  - **既存の RDS プロジェクトへの影響**: 既にデプロイ済みのクラスタは DB 名が旧順序
    （`{project}_{stage}`）のままのため、本バージョンにアップグレードして接続すると
    `FATAL: database "..." does not exist` になります。**旧 DB 名を維持したい場合は、新設の
    `[rds] database` で旧名を明示的に固定してください**（下記 Added 参照）。新規プロジェクトは
    そのまま新順序で作成されます。

### Added
- `[rds] database` を追加し、RDS の DB 名を明示的に上書きできるようにしました
  （`managed = true` 時のみ）。主用途は snapshot からの復元です。
  `RestoreDBClusterFromSnapshot` は `DatabaseName` を無視するため、復元後のクラスタには
  snapshot 元の DB 名がそのまま残ります。元ツールが別命名（例 `{project}_{stage}`）だった場合、
  `database` で実 DB 名を指すことで復元後の接続失敗を防げます。

## [0.7.2](https://github.com/worgue/magic-pocket/releases/tag/0.7.2) - 2026-07-03

### Fixed
- TiDB backend の TLS CA バンドルパスを Debian/Ubuntu 命名
  (`/etc/ssl/certs/ca-certificates.crt`) でハードコードしていたため、Amazon Linux 2023
  ベースの Lambda（`public.ecr.aws/lambda/python`、CA は `/etc/pki/tls/certs/ca-bundle.crt`）
  では CA ファイルを開けず、`ssl_mode = VERIFY_IDENTITY` の DB 接続が deploy 後に失敗する
  問題を修正しました。候補パスを順に存在チェックする実装に変更し、AL2023 / RHEL 系と
  Debian/Ubuntu の双方を吸収します。

### Added
- TiDB backend で `CONN_MAX_AGE = None` / `CONN_HEALTH_CHECKS = True` を標準デフォルト化
  しました。Lambda は実行環境（コンテナ）を再利用するため、持続接続で warm リクエストの
  TLS handshake を省けます。idle 切断された接続は再利用前の health check で検知して
  張り直すため安全です。

## [0.7.1](https://github.com/worgue/magic-pocket/releases/tag/0.7.1) - 2026-07-03

### Fixed
- Neon の `provisioning = "deploy"` で、既存ブランチ（Neon プロジェクト作成時に自動生成
  される default `main` を含む）があっても branch を無条件に作成しようとして
  `409 branch already exists` で初回 deploy が失敗する問題を修正しました。既存ブランチが
  ある場合は作成をスキップし、その上に role / database を ensure します（`create()` /
  `create_branch()` を冪等化）。これにより default ブランチを使う stage の初回 deploy が
  409 にならず、既存ブランチへの db/role bootstrap も deploy で完結します。

## [0.7.0](https://github.com/worgue/magic-pocket/releases/tag/0.7.0) - 2026-07-02

### Features
- staticfiles の **publish を deploy から分離**できるようにしました。staticfiles 宣言に
  `publish = "command"` を指定すると、`pocket django deploy` / `promote` は静的ファイルに
  一切触れず、publish は `pocket django deploystatic` に一任されます（DB/KVS の
  `provisioning = "command"` と同じ思想の静的版。大容量資産を out-of-band 管理し、CI は
  コードのみデプロイする構成に対応。既定は従来どおり `publish = "deploy"`）。
- `pocket django deploystatic` に `--link` を追加しました。collectstatic に `--link` を
  渡し、大容量資産の複製コストを削減します（`aws s3 sync` は symlink を追うため upload 互換）。

### Changed
- `pocket django deploystatic` の **S3 上の不要ファイル削除を opt-in** にしました
  （`--delete` フラグ新設）。従来は `aws s3 sync --delete` 固定で、旧デプロイのアセットを
  参照中のリクエスト（キャッシュ済み HTML / 切替前の Lambda が返すページの hash 付き
  ファイル名）や過去 commit への rollback を壊す時間窓がありました。`pocket django deploy` /
  `promote` 内の静的アップロードも同様に削除なしになります。不要ファイルの掃除は
  `pocket django deploystatic --delete` を明示実行してください。

## [0.6.0](https://github.com/worgue/magic-pocket/releases/tag/0.6.0) - 2026-06-28

### Features
- DB / KVS の **provisioning を deploy から分離**できるようにしました。`[neon]` / `[tidb]` /
  `[upstash]` に `provisioning = "command"` を指定すると、**deploy は当該リソースに一切触れません**
  （管理 API call ゼロ / credential 不要）。provisioning は新コマンド
  `pocket resource <neon|tidb|upstash> store-url --stage <stage>` に分離し、
  branch/cluster/role/db (Upstash は database) を ensure して接続 URL を stored user secret
  （`[awscontainer.secrets.user]` の `type`）の正準名へ保存します。これにより「provisioning は
  管理 API key を持つ host / 特権 CI」「deploy は credential なし」という custody 分離が
  素直に成立します（既定は従来どおり `provisioning = "deploy"`）。
  - user secret の `type` に `upstash_redis_url` を追加しました（`neon_database_url` /
    `tidb_database_url` と同様の stored mode）。
  - `store-url` は既存 secret があると no-op で、`--force` で上書きします。複数候補があるときは
    `--key` で対象を指定します。
  - **TiDB の注意**: TiDB Serverless は password の reveal API が無いため、`tidb store-url` は
    実行のたびに root password をローテーションします（Neon / Upstash は冪等）。実行後は
    consumer の再デプロイが前提です。

### Changed / Deprecated
- DB / KVS 接続 URL の **computed mode**（`[awscontainer.secrets.managed]` に
  `{ type = "neon_database_url" / "tidb_database_url" / "upstash_redis_url" }`）を
  **deprecated** にしました。deploy 時に warning を出します。`[<db>] provisioning` + stored
  user secret（`[awscontainer.secrets.user]` の `type`）へ移行してください。computed と
  `provisioning = "deploy"` は「deploy が ensure し URL を供給する」点で挙動が同じで、差分は
  保存先のみ（computed = managed pocket_store、stored = user secret 名）です。

### Removed
- `[neon]` / `[tidb]` / `[upstash]` の **`skip_check_existing` を削除**しました
  （`provisioning = "command"` へ置換）。残っていると deploy 前に **fail-fast** で移行を案内します。
- **実行時フラグ `--skip-check-existing` を削除**しました（`pocket deploy` / `pocket promote` /
  `pocket django deploy` / `pocket django promote`）。credential-less deploy は
  `[<db>] provisioning = "command"` に一本化されました。
  - 移行手順: `[<db>] skip_check_existing = true` を `[<db>] provisioning = "command"` に置換し、
    接続 URL を `[awscontainer.secrets.user]` の `type` で宣言、deploy 前に
    `pocket resource <db> store-url --stage <stage>` を一度実行してください。

## [0.5.0](https://github.com/worgue/magic-pocket/releases/tag/0.5.0) - 2026-06-28

### Features
- `[neon]` で使用するブランチを選択できるようにしました。これまで Neon の
  `branch_name` は stage 名にハードコードされていましたが、`branch_name` を省略すると
  project の **default ブランチ (通常 `main`)** を使うようになり、stage = ブランチ名の
  暗黙の結合を解消しました。`[<stage>.neon]` で per-stage に上書きでき、
  `{stage}`/`{project}`/`{namespace}` を展開できるので、環境ごとに別ブランチを払い出す
  使い方もできます。あわせて `parent_branch_name` を追加し、ブランチを新規作成する際の
  親ブランチを指定できます (省略時は Neon の default ブランチから分岐する従来挙動)。
  既存の stage 名ブランチ運用は `branch_name = "<stage>"` を明示すれば維持できます。

## [0.4.0](https://github.com/worgue/magic-pocket/releases/tag/0.4.0) - 2026-06-22

### Features
- DB 接続 URL の **stored mode** を追加しました。`[awscontainer.secrets.user]` に
  `DATABASE_URL = { type = "tidb_database_url" }` / `{ type = "neon_database_url" }` と
  書くと、deploy 時に provider の管理 API を叩いて URL を計算する computed mode
  (`secrets.managed`) の代わりに、**事前 provision して secret store に保存済みの接続 URL を
  参照するだけ**になります。deploy 環境に cluster を作成・削除できる管理 API key を持ち込まず
  に済み (least privilege)、deploy が外部 API に依存しません。`type` 指定時は pocket が
  secret 名を自動導出し、未 provision のまま deploy すると正準名を示して deploy 時にエラーで
  止めます (runtime まで遅延しません)。`name` と `type` は排他です。RDS は元々管理 API key
  非依存かつパスワードローテーション追従のため対象外です。

## [0.3.0](https://github.com/worgue/magic-pocket/releases/tag/0.3.0) - 2026-06-16

### Features
- `[cloudfront.<name>].enable_origin_verify` を追加しました。CloudFront 配下の
  origin (lambda / API Gateway) に対し、(1) origin 直叩き防止の secret custom header
  (`X-Pocket-Origin-Verify`) を CloudFront → origin に付与しつつ同値を Lambda runtime
  env に注入、(2) 詐称耐性のある client IP (CloudFront が TCP から取得する
  `event.viewer.ip`) を `X-Pocket-Viewer-Ip` header で origin に転送、(3) 検証 +
  `REMOTE_ADDR` 正規化を行う Django middleware
  (`pocket.django.origin_verify.OriginVerifyMiddleware`) の同梱、を一括で有効化します。
  secret は managed secret (`type = "origin_verify_secret"`) として自動生成・管理され、
  利用者は flag を立てて middleware を最前段に置くだけで済みます。
  viewer IP 転送自体は flag 非依存で lambda route に常時入ります (キャッシュ無影響・
  純加算のため。origin request policy は `AllViewerExceptHostHeader` のまま据え置き、
  CloudFront Function 経由で付与するので API GW の Host 整合性も壊しません)。

## [0.2.2](https://github.com/worgue/magic-pocket/releases/tag/0.2.2) - 2026-06-15

### Bug Fixes
- `versioning = "deploy_hash"` 構成で 2 回目以降の deploy 時に Lambda の環境変数
  `DEPLOY_HASH` が旧値に固着し、Django が古い hash の static URL を生成して
  CloudFront 側 (毎 deploy 追従) と乖離 → 静的アセットが全滅 (403) する不具合を
  修正しました。`pocket` の Lambda 更新は `update_function_code` (コードのみ) で
  Environment を更新せず、env は CFn `stack.update()` 経由でしか書き換わらないため、
  stack 更新が `yaml_synced` / `wait_status` timeout 等でスキップされると env が
  古いまま残るのが原因でした。deploy フロー末尾の post-deploy hook
  (`AwsContainer.ensure_post_deploy_state`) で、CloudFront の KVS 書き込みと同じ
  philosophy により Lambda env の `DEPLOY_HASH` を side-channel で冪等に同期する
  ようにしています (既存 env / secret は保持)。

### Security
- Rust crate (`magic-pocket-rs`) の依存ツリーから legacy TLS スタック
  (rustls 0.21 / hyper 0.14 系) を除去しました。`aws-sdk-*` の default feature
  `rustls` を無効化し、既定の HTTP client (rustls 0.23 + aws-lc) のみを使用します。
  動作は変わりません。git 依存で利用している場合は `cargo update magic-pocket-rs`
  で取り込めます。

## [0.2.1](https://github.com/worgue/magic-pocket/releases/tag/0.2.1) - 2026-06-10

### Bug Fixes
- `pocket version` が古いバージョン (0.1.1) を表示する問題を修正しました。
  `__version__` を手書き定数からパッケージメタデータ由来に変更し、
  pyproject.toml との二重管理を廃止しています (同期の回帰テスト付き)。

## [0.2.0](https://github.com/worgue/magic-pocket/releases/tag/0.2.0) - 2026-06-10

0.1.1 以降の全面的な機能拡張リリースです。runtime ライブラリ (`magic-pocket`) と
deploy CLI (`magic-pocket-cli`) の 2 パッケージ構成になりました。

### Breaking Changes
- **パッケージを 2 分割しました。** deploy CLI (`pocket` コマンド) は新パッケージ
  `magic-pocket-cli` に移動し、`magic-pocket` は Lambda runtime ライブラリのみに
  なりました。デプロイ環境には `magic-pocket-cli` を、Lambda image には従来どおり
  `magic-pocket` をインストールしてください。
- **AWS リソース系コマンドを `resource` group 配下へ再配置しました。** 旧トップレベル
  コマンド `pocket awscontainer` / `neon` / `tidb` / `dsql` / `rds` / `s3` / `vpc` /
  `cloudfront` 等は廃止され、`pocket resource awscontainer ...` のように `resource` を
  挟む新 path になりました（旧 path には alias を残していないため `No such command`
  で失敗します）。CLI を呼び出すスクリプト・上位ツールは新 path への追従が必要です。
  例: `pocket awscontainer reload-env` → `pocket resource awscontainer reload-env`。
- **`pocket.django.lambda_handlers.shell_handler` を `dangerous_shell_handler` に
  リネームしました。** 任意文字列を `shell=True` で実行する危険な handler である
  ことを名前で明示する目的です（capability 自体は維持）。`pocket.toml` の handler
  に旧名を指定している場合は新名への追従が必要です。SQS 駆動でコマンドを安全に
  完走させる用途には新設の `BaseCommandHandler` を利用してください。
- **deploy 時の stage 指定環境変数を `POCKET_DEPLOY_STAGE` に分離しました。**
  `POCKET_STAGE` は Lambda runtime 専用になり、ローカルで runtime helper と
  deploy CLI の stage 指定が干渉しなくなりました。
- **Route の `type = "api"` を `type = "lambda"` にリネームしました**（旧値は起動時に
  分かりやすいエラーで失敗します）。
- **`is_versioned` を廃止し `versioning` に統一しました**（`"content_hash"` = 旧
  `is_versioned = true` 相当 / `"deploy_hash"` = git hash を URL prefix に付与する
  方式を新設）。
- **VPC 設定をトップレベル `[vpc]` セクションへ移動しました。** 外部 VPC 参照
  (`manage = false`) と VPC 共有 (`sharable = true` + consumer タグ管理) も
  サポートします。
- **Route に `origin_path` を導入し、storage の location を自動計算するように
  しました**（旧 `spa.origin_path_format` の設定体系は廃止）。
- **CloudFront 専用 S3 バケットを廃止し、プロジェクトの S3 バケットに統合しました。**
- **Neon の `project_name` を pocket.toml で必須指定に変更しました。**
- **secrets セクションを再編しました**:
  `[awscontainer.secretsmanager.pocket_secrets]` → `[awscontainer.secrets.managed]`、
  `[awscontainer.secretsmanager.secrets]` → `[awscontainer.secrets.user]`。
  保存先 store として Secrets Manager に加え SSM Parameter Store
  (`store = "ssm"`) を選択可能になりました。

### Features
- **データベース / キャッシュの選択肢を拡張**: Neon に加えて TiDB Serverless
  (`[tidb]`) / RDS Aurora Serverless v2 (`[rds]`、既存クラスター参照可・static
  パスワード管理対応) / Aurora DSQL (`[dsql]`、IAM 認証・VPC 不要) /
  Upstash Redis (`[upstash]`) をサポート。
- **Rust (Loco) 対応**: `magic-pocket-rs` crate を追加し、Django 以外に Loco app を
  同じ pocket.toml 体系でデプロイできるようになりました。
- **CloudFront 統合を全面拡張**: `[cloudfront.<name>]` で複数ディストリビューション、
  routes (S3 / lambda)、SPA ルーティング、署名付き URL (`signing_key`)、SPA トークン
  認証 (`require_token` + CloudFront Function + KeyValueStore)、WAF IP allowlist
  (`waf`)、ステージ別アセット配信 (`managed_assets`)、`deploy_hash` versioning に
  よるキャッシュバスティングをサポート。
- :material-console: build once + commit hash 昇格をサポート。`pocket django build` で
  作業ツリーを一度ビルドして git commit hash（full）タグで ECR へ push し、
  `pocket promote` / `pocket django promote --commit-hash <sha>` で同一イメージを
  再ビルドなしで各ステージへ昇格できます（`:<stage>` タグの付け替え + Lambda 更新）。
  `[awscontainer].ecr_name` で ECR リポジトリ名を上書きでき、同一アカウント内の
  ステージ間でリポジトリを共有可能（明示指定したリポジトリは `pocket destroy` で
  削除されません）。通常の `pocket django deploy` の挙動は不変です。
- SQS 駆動の安全な command worker 基盤 `pocket.command_handler.BaseCommandHandler`
  を追加。SQS イベントを別 Lambda invocation の本体として受け、`build_argv` で固定
  した実行ファイルを `shell=False` の list argv で完走させ、出力 / ステータスを sink
  hook（`on_start` / `on_output` / `on_finish` / `on_crash`）に委譲します。long-running
  job を wsgi tier から worker tier に逃がす定石を共通化し、Lambda の freeze による
  「ステータスが running 固着」を構造的に防ぎます。crash 時は `try/finally` で
  `on_crash` を呼んでから例外を re-raise（握りつぶさない）。`dangerous_shell_handler`
  の安全な後継です。
- **EventBridge Scheduler サポート** (`[scheduler]`): cron / rate での定期実行を
  CloudFormation 管理で構成。Django management command を呼ぶショートカット
  entry (`pocket.django.management_lambda_scheduler`) もあります。
- **VPC + EFS サポート**: NAT / Internet Gateway 構成、EFS マウント、Django
  キャッシュの EFS 利用 (`store = "efs"`)。
- **デプロイ権限の可視化**: `pocket permissions list` CLI と Python API
  (`pocket.permissions.compute_actions()` / `action_groups()`) で、pocket.toml の
  構成に必要な IAM Action 一覧を機械可読に提供。デプロイ用 IAM Role の最小権限
  プロビジョニングに使えます。
- **ビルドバックエンドの選択**: `[awscontainer.build]` で codebuild（既定）/
  docker / depot を選択可能。ローカル Docker なしでデプロイできます。
- **IAM Permissions Boundary 対応** (`[awscontainer].permissions_boundary`)。
  Lambda 実行ロールと CodeBuild ロールに適用されます。
- **`pocket runtime-config`**: ビルド専用設定を除外した `pocket.runtime.toml` を
  生成し、Lambda image に焼き込む仕組みを導入。
- **SES メール送信** (`[ses]`): Django email backend の自動構成つき。
- **`pocket waf ip` CLI**: WAF IPSet の side-channel 即時更新（add / remove / list）。
- **`pocket resource awscontainer reload-env` / `status-env`**: SSM / Secrets Manager
  の最新値で Lambda 環境変数を即時更新（CFn を介さない）/ 宣言値との drift 表示。
- :material-console: `pocket django deploy` でインフラデプロイ + ローカル
  collectstatic + Lambda 上での migrate を対話形式で一括実行。
- :material-console: `pocket django resetdb`でデータベースの public スキーマをリセット（`DROP SCHEMA public CASCADE`）
- S3 バケット名のカスタマイズ (`[s3].bucket_name_format`) とステージ別
  `[<stage>.general]` 上書き（region 等）。

### Bug Fixes
- `pocket permissions list` / `compute_actions()` に deploy が実際に必要とする
  Action の宣言漏れが 5 件あったのを修正。権限を絞ったデプロイ用ロールで
  該当構成を deploy すると `AccessDenied` になっていた:
  `dsql:*`（`[dsql]` 構成の cluster 操作）/ `scheduler:*`（`[scheduler]` 構成の
  CFn `AWS::Scheduler::Schedule` 作成）/ `tag:TagResources`・`tag:UntagResources`
  （外部 VPC 参照時の consumer タグ付け外し）/ `iam:ListRolePolicies`
  （CodeBuild ロール削除時の inline policy 列挙）/ `ssm:GetParameter`・
  `ssm:PutParameter`・`ssm:DeleteParameter`（`[rds]` の static master password
  管理。`secrets.store` とは独立に必要）。`action_groups()` に `dsql` /
  `scheduler` / `tag` グループを追加（キー追加のみの非破壊変更）。
- `POCKET_HOSTS` 環境変数が複数ホストをセパレータなしで連結していたのを
  カンマ区切りに修正（Python / Rust 両ランタイム）。apigateway 付き handler を
  2 つ以上定義すると、Django の `ALLOWED_HOSTS` に壊れたホスト名が入り
  2 つ目以降のホストが `DisallowedHost` になっていました（消費側の
  `add_or_append_env` は元々カンマ結合を前提としており、handler 1 つの構成では
  影響ありません）。
- `pocket resource awscontainer reload-env` / `status-env` が Lambda 関数名から
  namespace（既定 `pocket`）を取りこぼし、default namespace のデプロイで常に
  「Lambda function が見つかりません」で失敗していたのを修正（deploy 側と同じ
  正準 `function_name` を参照）。あわせて `status-env` の drift 警告が案内する
  コマンドが旧 path のままだったのを新 path に修正。

### Improvements
- deploy コードと `compute_actions()` の同期検証テストを追加
  (`tests/test_permissions_sync.py`)。boto3 呼び出しの AST 静的解析と
  CloudFormation テンプレートのリソース型解析の 2 系統で、deploy が必要とする
  Action の宣言漏れを CI で検知する（過去に 3 回再発した「権限を絞った deploy
  ロールが本番で AccessDenied」の構造的な再発防止。未知の CFn リソース型の
  追加時はテストが fail し権限の検討を強制する）。同期方針は
  `docs/permissions/aws.md` に記載。
- S3バケットのCORS設定を`pocket.toml`で宣言可能に（CloudFrontドメイン自動解決）
- `pocket destroy`がデフォルトでシークレットも削除するように変更（`--without-secrets`で残す）
- `pocket destroy`でCloudFrontスタック削除の完了を待機するように修正
- `pocket deploy`時にSSM/SMの不要なシークレットを自動クリーンアップ

## [0.1.1](https://github.com/worgue/magic-pocket/releases/tag/0.1.1) - 2024-10-16

**Full Changelog**: https://github.com/worgue/magic-pocket/compare/0.1.0...0.1.1

### Bug Fixes
- spa用のリソース作成時にリダイレクトするためのリソースが作られないバグを修正

## [0.1.0](https://github.com/worgue/magic-pocket/releases/tag/0.1.0) - 2024-10-11

### Dependencies
- click>=8.1.7
- tomli>=1.1.0 ; python_version < '3.11'
- mergedeep>=1.3.4
- pydantic>=2.5.3
- pydantic-settings>=2.1.0
- boto3>=1.34.28
- rich>=13.7.0
- deepdiff>=6.7.1
- pyyaml>=6.0.1
- python-on-whales>=0.68.0
- jinja2>=3.1.3
- awslambdaric>=2.0.10
- apig_wsgi>=2.18.0
- django-storages>=1.14.2,!=1.14.3

### Features
- :material-console: `pocket status`で環境の作成状況を確認
- :material-console: `pocket deploy`でデプロイ
    - :material-database: NeonへのDB作成
    - :simple-awssecretsmanager: SecretsManagerへのNeon DBの接続情報登録
    - :simple-amazons3: ストレージ用にS3を作成し権限を設定
    - :simple-docker: コンテナイメージを作成しECRへアップロード
    - :material-language-javascript: フロントエンドSPAのビルドデータをアップロードするS3を作成
    - CF: Lambdaに関わるCloudFormationを登録・更新
        - LambdaのIAM Role, SecurityGroup, Function
        - API Gateway の LogGroup, Api, ApiGatewayManagedOverrides, Route, Integration, lambda Permission, Certificate, DomainName, RecordSet, ApiMapping
        - API Gatewayのhost名のoutput
    - CF: SPAに関わるCloudFormationを登録・更新
        - CloudFrontのOriginAccessControl, Certificate, CloudFrontFunction, Distribution, RecordSet
- :material-language-python: `settings.py`での情報取得
    - :simple-awssecretsmanager: AWS SecretsManagerから情報を取得(1)
    - :simple-toml: `pocket.toml`からdjangoの`STORAGES`, `CACHES`を取得
    - CF: CloudFormationのoutputからdjangoの`ALLOWED_HOSTS`を取得
- :simple-toml: デプロイ環境ごとのdjango settings登録
- :material-console: `pocket django manage COMMAND ARGS` で管理コマンドを実行
- :material-console: `pocket django storage upload STORAGE` でローカルのFileSystemStorageから対象ステージのS3Boto3Storageへデータをsync
- :material-console: `pocket resource awscontainer status`でLambdaの作成状況を確認
- :material-console: `pocket resource awscontainer secretsmanager list`でSecretsManagerの値を確認
- :material-console: `pocket resource awscontainer yaml`でCloudFormation用のyaml ファイルを確認
- :material-console: `pocket resource awscontainer yaml-diff`でCloudFormation用のyamlファイルの差分を確認
- :material-console: `pocket resource neon status`でNeonの作成状況を確認
- :material-console: `pocket resource s3 status`でS3バケットの作成状況を確認
- :material-console: `pocket resource spa status`でspaアップロード先S3バケットの作成状況を確認
