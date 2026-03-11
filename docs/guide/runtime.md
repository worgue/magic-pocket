# 実行環境

Lambda上のアプリケーションは、`pocket.toml` とAWSリソースから設定情報を取得します。

---

## POCKET_STAGE

`POCKET_STAGE` は2つの用途で使用される環境変数です。

- **Lambda ランタイム**: Lambda環境ではCloudFormationにより自動設定され、実行環境のステージを判定します
- **CLI デフォルトステージ**: `pocket` コマンドの `--stage` オプションのデフォルト値として参照されます（[CLI](cli.md#pocket_stage-環境変数) を参照）

各フレームワークのユーティリティ関数（Django の `set_envs()` や Rust の `set_envs()`）は、この変数の有無でローカル環境と Lambda 環境を判別します。`POCKET_STAGE` が未設定の場合、AWS リソースへのアクセスはスキップされます。

---

## セットされる環境変数

`set_envs()` は、Django・Rust いずれの実装でも、以下の環境変数をセットします。

| 環境変数 | 説明 |
|----------|------|
| `POCKET_PROJECT_NAME` | プロジェクト名 |
| `POCKET_REGION` | AWS リージョン |
| `POCKET_HOSTS` | API Gateway ホスト一覧 |
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

| 関数 | 引数 | 戻り値 | 説明 |
|------|------|--------|------|
| `generate_token(user_id)` | `user_id: str`, `secret: str\|None`, `max_age: int` | `str` | HMAC-SHA256 トークンを生成 |
| `verify_token(token)` | `token: str`, `secret: str\|None` | `str\|None` | トークンを検証し、有効なら user_id を返す |
| `spa_login(response, user_id)` | `response`, `user_id: str`, `secret: str\|None`, `max_age: int` | — | レスポンスにトークン Cookie をセット |
| `spa_logout(response)` | `response` | — | レスポンスからトークン Cookie を削除 |

- `secret` を省略すると `os.environ["SPA_TOKEN_SECRET"]` を使用します
- `max_age` のデフォルトは `604800`（7日間）です
- Cookie 名は `pocket-spa-token` で、`HttpOnly`, `Secure`, `SameSite=Lax` が設定されます
