# 実行環境

Lambda上のアプリケーションは、`pocket.toml`（または `pocket.runtime.toml`）とAWSリソースから設定情報を取得します。

`pocket.runtime.toml` が存在する場合はそちらが優先されます。`pocket runtime-config` コマンドでビルド専用設定を除外したランタイム用 TOML を生成できます（[設定ファイル - pocket runtime-config](configuration.md#pocket-runtime-config) を参照）。

!!! warning "CLI と runtime のバージョンは揃える（新機能を使ったら lock も上げる）"
    デプロイを叩く `pocket` CLI（`magic-pocket-cli`）と、Lambda コンテナ内で動く runtime（`magic-pocket`）は**別パッケージで、同一バージョン系列で lockstep リリース**されます。CLI は host 側（`uvx` 等）で最新を使いがちですが、runtime は**プロジェクトの `uv.lock` に固定**され、独立して古いままになり得ます。

    CLI が新しいと `pocket.runtime.toml` に新スキーマ（新機能）を書き込みますが、**古い runtime はそれを解釈できず Lambda の INIT フェーズで失敗**します。この場合 CloudWatch には原因の分かりにくい `Runtime.Unknown` しか出ません。

    **pocket.toml で新しめの機能を使ったら、プロジェクトの `magic-pocket` も同じ版以上に上げてください**：

    ```bash
    uv add 'magic-pocket[django]>=X.Y.Z'   # CLI と同じ版に揃える
    ```

    magic-pocket 0.11.0 以降の runtime は、生成元 CLI 版が自身より新しいことを検知したら `Runtime.Unknown` の代わりに上記の対処を促す明快なエラーを出します（古い runtime には遡って効かないため、この結合関係の理解が最優先の予防策です）。

---

## POCKET_STAGE

`POCKET_STAGE` は **Lambda ランタイムでの実行ステージ判定** に使われる環境変数です。
CloudFormation が Lambda 関数の環境変数として自動設定するため、通常はユーザーが手動で設定する必要はありません。

各フレームワークのユーティリティ関数（Django の `set_envs()` や Rust の `set_envs()`）は、この変数の有無でローカル環境と Lambda 環境を判別します。`POCKET_STAGE` が未設定の場合、AWS リソースへのアクセスはスキップされます。

!!! note "デプロイ対象指定とは別物です"
    `pocket` コマンドの `--stage` オプションのデフォルト値は `POCKET_DEPLOY_STAGE` 環境変数から読まれます（[CLI](cli.md#pocket_deploy_stage-環境変数) を参照）。
    `POCKET_STAGE` をローカルで設定すると、ローカル実行プロセスが「自分は Lambda 上のそのステージで動いている」と誤解する原因になります。デプロイ対象を環境変数で指定したい場合は `POCKET_DEPLOY_STAGE` を使ってください。

---

## セットされる環境変数

`set_envs()` は、Django・Rust いずれの実装でも、以下の環境変数をセットします。

| 環境変数 | 説明 |
|----------|------|
| `POCKET_PROJECT_NAME` | プロジェクト名 |
| `POCKET_REGION` | AWS リージョン |
| `POCKET_HOSTS` | API Gateway ホスト一覧（カンマ区切り。Django では `ALLOWED_HOSTS` に追加される） |
| `POCKET_{HANDLER}_HOST` | 各ハンドラーのホスト |
| `POCKET_{HANDLER}_ENDPOINT` | 各ハンドラーの URL |
| `POCKET_{HANDLER}_QUEUEURL` | SQS キュー URL |
| `POCKET_CLOUDFRONT_{NAME}_DOMAIN` | CloudFront ディストリビューションのドメイン名 |
| `POCKET_DSQL_ENDPOINT` | DSQL クラスターのエンドポイント |
| `POCKET_DSQL_REGION` | DSQL クラスターのリージョン |
| `POCKET_DSQL_TOKEN` | DSQL IAM 認証トークン（`set_envs()` 呼び出し時に生成） |
| シークレットキー | Secrets Manager / SSM のシークレット値 |

---

## SPA トークン認証 {: #spa-トークン認証 }

CloudFront 配信の SPA にログイン必須機能を追加する場合、SPA トークン認証モジュールを使用します。
HMAC-SHA256 トークンを Cookie にセットし、CloudFront Function で検証します。

!!! info "gate は default behavior の全リクエストに効く (asset 配信も含む)"
    CloudFront Function (viewer-request) は `require_token = true` のルートの
    default behavior に attach されるため、**そのルート配下の全リクエスト**
    (`/_app/*.js` 等の拡張子付き asset も含む) に対して token 検証が走ります。
    asset 単位で gate を bypass する設定は入っていません。

    一見「token expired のユーザーは asset の数だけ redirect される
    (thundering herd 的にループ寸前)」のように見えますが、実際にはなりません:
    ブラウザは **index.html を先に取得 → パース → 必要な asset を要求** の順で
    動くため、document 取得時点で middleware が token を refresh すれば、
    後続の asset リクエストはすべて valid token で 200 を返します。**1 ユーザー
    につき 1 回の token refresh で済む** モデルです。

### Python (Django)

```python
from pocket.django.spa_auth import generate_token, verify_token, spa_login, spa_logout

# トークン生成（ログイン時）
token = generate_token("user123")  # デフォルト有効期限: 7日

# トークン検証（任意のバックエンド処理で）
user_id = verify_token(token)  # 有効なら user_id、無効なら None
```

ログイン・ログアウトでは、Django の View でレスポンスに Cookie をセットします。

```python
from django.http import HttpResponseRedirect
from pocket.django.spa_auth import spa_login, spa_logout

def login_view(request):
    # Django 認証でユーザーを検証後...
    response = HttpResponseRedirect(request.GET.get("next", "/"))
    spa_login(response, str(request.user.id))
    return response

def logout_view(request):
    response = HttpResponseRedirect("/")
    spa_logout(response)
    return response
```

#### `SpaTokenCookieMiddleware` (必須)

`require_token = true` のルートを公開するときは、`SpaTokenCookieMiddleware`
を `MIDDLEWARE` に追加してください。**入れないと無限 redirect ループに
陥ります**。

```python
# settings.py
MIDDLEWARE = [
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "pocket.django.spa_auth.SpaTokenCookieMiddleware",
    # ...
]
```

middleware の動作:

- 認証済み response: `pocket-spa-token` cookie が無い / 期限切れなら `spa_login()`
  で発行 (= self-heal)
- 未認証 response: 残存 cookie があれば `spa_logout()` で削除
- `SPA_TOKEN_SECRET` 環境変数が未設定の環境 (gating 未 deploy のローカル等)
  は no-op

##### なぜ必要か (詳細)

Django session のデフォルト寿命 (`SESSION_COOKIE_AGE` = 14 日) が SPA token
の寿命 (`DEFAULT_MAX_AGE` = 7 日) より長いため、**8 日目以降に「session 有り
+ token 無し」の状態が全ユーザーに発生**します。この状態で `require_token`
ルートにアクセスすると:

1. CloudFront Function が token 無を検出 → `login_path` (`/accounts/login/?next=/`) に 302
2. ログインページが「既にログイン済み」を検出し、ログイン処理を通さずに `next` (`/`) へ 302
   (`allauth.RedirectAuthenticatedUserMixin` や Django 標準 `LoginView.redirect_authenticated_user=True` の挙動)
3. → 1 へ戻り **無限ループ**

middleware が 2 の bounce response 経路で必ず token を補填するため、1 往復
余分に bounce するだけでループが断ち切られます (ユーザーには visible な
追加遷移には見えません)。

##### 拡張 (subclass で sliding refresh や短命 token に対応)

`SpaTokenCookieMiddleware` は次の 2 つの拡張点を持ちます:

| メソッド | デフォルト | 用途 |
|---------|-----------|------|
| `_should_issue(request) -> bool` | cookie 無 or `verify_token` で失効判定 | 残り寿命が短い時にも発行する sliding refresh 等 |
| `_max_age() -> int` | `DEFAULT_MAX_AGE` (7 日) | 短命 token を使う場合に settings 等から返す |

例: 残り寿命が半分を切ったら sliding refresh する subclass:

```python
import time
from django.conf import settings
from pocket.django.spa_auth import (
    COOKIE_NAME,
    SpaTokenCookieMiddleware as BaseMiddleware,
)


class SpaTokenCookieMiddleware(BaseMiddleware):
    def _should_issue(self, request):
        if super()._should_issue(request):
            return True
        token = request.COOKIES[COOKIE_NAME]
        remaining = int(token.split(":")[1]) - time.time()
        return remaining < self._max_age() / 2

    def _max_age(self):
        return settings.SPA_TOKEN_MAX_AGE
```

`MIDDLEWARE` で指定するパスを subclass 側に差し替えれば、デフォルトの
挙動はそのまま保ちつつ拡張できます。

### Rust (Loco)

Rust アプリケーションでは `pocket-spa-auth` crate を使用できます。

```rust
use pocket_spa_auth::{generate_token, verify_token, login_cookie_value, logout_cookie_value};

let secret = std::env::var("SPA_TOKEN_SECRET").unwrap();

// トークン生成
let token = generate_token("user123", &secret, 604800);

// トークン検証
if let Some(user_id) = verify_token(&token, &secret) {
    println!("認証成功: {}", user_id);
}

// Cookie 値の生成
let set_cookie = login_cookie_value(&token, 604800);
let delete_cookie = logout_cookie_value();
```

### API リファレンス

**Python**

| 関数 / クラス | 引数 | 戻り値 | 説明 |
|------|------|--------|------|
| `generate_token(user_id)` | `user_id: str`, `secret: str\|None`, `max_age: int` | `str` | HMAC-SHA256 トークンを生成 |
| `verify_token(token)` | `token: str`, `secret: str\|None` | `str\|None` | トークンを検証し、有効なら user_id を返す |
| `spa_login(response, user_id)` | `response`, `user_id: str`, `secret: str\|None`, `max_age: int` | — | レスポンスにトークン Cookie をセット |
| `spa_logout(response)` | `response` | — | レスポンスからトークン Cookie を削除 |
| `SpaTokenCookieMiddleware` | Django middleware | — | 認証済み response に token 自動補填、未認証 response から残存 cookie 削除 (詳細は上記) |

- `secret` を省略すると `os.environ["SPA_TOKEN_SECRET"]` を使用します
- `max_age` のデフォルトは `604800`（7日間）です
- Cookie 名は `pocket-spa-token` で、`HttpOnly`, `Secure`, `SameSite=Lax` が設定されます
