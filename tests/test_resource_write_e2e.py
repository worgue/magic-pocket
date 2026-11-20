"""`pocket-rs resource` の書き込み系を moto server 相手に確かめる。

部品 (`deploy/*.rs`) の中身は `test_*_write_e2e.py` (`deploy-parts` 経由) で見る。
ここは公開の入口の分岐・確認・文言と、Python 版が読める結果になることを見る。
moto で作れない stack (CloudFront / container) の作成は対象外。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

REGION = "ap-southeast-1"

# Python 版の実装で managed secret を読む (store ごとの {key: 値})
_PY_SECRETS = """
import json, sys
from pocket.context import Context
ctx = Context.from_toml(stage=sys.argv[1])
views = ([ctx.secrets] if ctx.secrets else [])
views += [c.secrets for c in ctx.container.values() if c.secrets]
print(json.dumps({v.pocket_key: v.pocket_store.secrets for v in views}, sort_keys=True))
"""

pytestmark = pytest.mark.usefixtures("moto_versioning_root")


def _client(service: str, endpoint: str):
    return boto3.client(
        service,
        region_name=REGION,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 moto 用のダミー
    )


def _project(tmp_path: Path, project: str, body: str) -> Path:
    root = tmp_path / "prj"
    root.mkdir(exist_ok=True)
    (root / "pocket.toml").write_text(
        "[general]\n"
        f'region = "{REGION}"\n'
        f'project_name = "{project}"\n'
        'stages = ["prod"]\n\n' + body
    )
    (root / "pyproject.toml").write_text('[project]\nname = "e2e-prj"\n')
    return root


def _env(pocket_rs) -> dict[str, str]:
    return {k: v for k, v in pocket_rs.env.items() if k != "DEPLOY_HASH"}


def _rs(pocket_rs, root: Path, *args: str, stdin: str = ""):
    return subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs.binary), "resource", *args],
        cwd=root,
        env=_env(pocket_rs),
        input=stdin,
        capture_output=True,
        text=True,
    )


def _ok(pocket_rs, root: Path, *args: str, stdin: str = ""):
    result = _rs(pocket_rs, root, *args, stdin=stdin)
    assert result.returncode == 0, result.stderr + result.stdout
    return result


def _py_secrets(pocket_rs, root: Path) -> dict:
    result = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", _PY_SECRETS, "prod"],
        cwd=root,
        env=_env(pocket_rs),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---- s3 ----


def test_s3_create_reconcile_and_destroy(tmp_path, pocket_rs, moto_endpoint):
    project = "rss3"
    root = _project(tmp_path, project, "[s3]\n")
    bucket = f"prod-{project}-pocket"
    s3 = _client("s3", moto_endpoint)

    result = _ok(pocket_rs, root, "s3", "create", "--stage", "prod")
    assert f"Created: bucket {bucket}" in result.stderr
    result = _ok(pocket_rs, root, "s3", "create", "--stage", "prod")
    assert f"Reconciled: bucket {bucket} (PAB / CORS / versioning / lifecycle)" in (
        result.stderr
    )
    s3.put_object(Bucket=bucket, Key="a.txt", Body=b"x")

    # 断れば何も消さない (-y は無い)
    result = _rs(pocket_rs, root, "s3", "destroy", "--stage", "prod", stdin="n\n")
    assert result.returncode != 0
    assert "Aborted!" in result.stderr
    assert "S3バケットの全データが失われます。" in result.stderr
    assert f"バケット '{bucket}' を削除しますか？ [y/N]" in result.stdout
    s3.head_bucket(Bucket=bucket)

    result = _ok(pocket_rs, root, "s3", "destroy", "--stage", "prod", stdin="y\n")
    assert "S3 bucket was deleted." in result.stderr
    with pytest.raises(s3.exceptions.ClientError):
        s3.head_bucket(Bucket=bucket)
    result = _ok(pocket_rs, root, "s3", "destroy", "--stage", "prod")
    assert "No S3 bucket found." in result.stderr


# ---- cloudfront prune ----

_PRUNE = """
[s3]

[cloudfront.web]
[[cloudfront.web.routes]]
is_default = true
origin_path = "/web"

[[cloudfront.web.routes]]
path_pattern = "/assets/*"
versioning = "content_hash"
upload_dir = "build/assets"
"""


def test_prune_deletes_only_stale_keys(tmp_path, pocket_rs, moto_endpoint):
    project = "rsprune"
    root = _project(tmp_path, project, _PRUNE)
    bucket = f"prod-{project}-pocket"
    s3 = _client("s3", moto_endpoint)
    _ok(pocket_rs, root, "s3", "create", "--stage", "prod")
    assets = root / "build" / "assets" / "js"
    assets.mkdir(parents=True)
    (assets / "app.123.js").write_text("new")
    for key in ["assets/js/app.123.js", "assets/js/app.old.js", "assets/z.css"]:
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")
    s3.put_object(Bucket=bucket, Key="web/index.html", Body=b"x")

    result = _ok(pocket_rs, root, "cloudfront", "prune", "--stage", "prod", "--dry-run")
    assert "[web] /assets/*: 旧版 2 件" in result.stderr
    assert f"  s3://{bucket}/assets/js/app.old.js" in result.stderr
    assert "--dry-run のため削除しません (2 件)" in result.stderr

    result = _rs(pocket_rs, root, "cloudfront", "prune", "--stage", "prod", stdin="n\n")
    assert result.returncode != 0
    assert "上記 2 件を削除しますか？ [y/N]" in result.stdout

    result = _ok(pocket_rs, root, "cloudfront", "prune", "--stage", "prod", "-y")
    assert f"削除: s3://{bucket}/assets/z.css" in result.stderr
    assert "2 件を削除しました" in result.stderr
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket)["Contents"])
    assert keys == ["assets/js/app.123.js", "web/index.html"]
    result = _ok(pocket_rs, root, "cloudfront", "prune", "--stage", "prod", "-y")
    assert "削除対象の旧版はありません" in result.stderr

    result = _rs(
        pocket_rs, root, "cloudfront", "prune", "--stage", "prod", "--route", "/x/*"
    )
    assert result.returncode != 0
    assert (
        "route '/x/*' はアップロード対象 (build / upload_dir を持つ route) にありません"
        in result.stderr
    )


def test_prune_stops_without_upload_dir(tmp_path, pocket_rs, moto_endpoint):
    """build していない (upload_dir が無い) ときに全 key を旧版とみなさない"""
    project = "rsprunenodir"
    root = _project(tmp_path, project, _PRUNE)
    bucket = f"prod-{project}-pocket"
    s3 = _client("s3", moto_endpoint)
    _ok(pocket_rs, root, "s3", "create", "--stage", "prod")
    s3.put_object(Bucket=bucket, Key="assets/a.js", Body=b"x")

    result = _rs(pocket_rs, root, "cloudfront", "prune", "--stage", "prod", "-y")
    assert result.returncode != 0
    assert "がありません。build の出力先を確認してください。" in result.stderr
    assert s3.list_objects_v2(Bucket=bucket)["KeyCount"] == 1


# ---- container secrets / reload-env ----

_SECRETS = """
[container.main]
dockerfile_path = "Dockerfile"

[container.main.handlers.wsgi]
command = "app.wsgi"

[container.main.secrets]
store = "ssm"

[container.main.secrets.managed]
SECRET_KEY = { type = "password", options = { length = 20 } }
"""


def test_pocket_managed_secrets_create_and_delete(tmp_path, pocket_rs, moto_endpoint):
    project = "rssecrets"
    root = _project(tmp_path, project, _SECRETS)
    pocket_key = f"prod-{project}-main-pocket"

    _ok(
        pocket_rs,
        root,
        "container",
        "secrets",
        "create-pocket-managed",
        "--stage",
        "prod",
    )
    created = _py_secrets(pocket_rs, root)[pocket_key]
    assert len(created["SECRET_KEY"]) == 20
    # 既にあるものは作り直さない
    _ok(
        pocket_rs,
        root,
        "container",
        "secrets",
        "create-pocket-managed",
        "--stage",
        "prod",
    )
    assert _py_secrets(pocket_rs, root)[pocket_key] == created

    listed = _ok(pocket_rs, root, "container", "secrets", "list", "--stage", "prod")
    assert "SECRET_KEY: password {'length': 20}\n  - CREATED\n" in listed.stdout

    args = ("container", "secrets", "delete-pocket-managed", "--stage", "prod")
    result = _rs(pocket_rs, root, *args, stdin="n\n")
    assert result.returncode != 0
    assert " - SECRET_KEY (main)" in result.stderr
    assert _py_secrets(pocket_rs, root)[pocket_key] == created

    result = _ok(pocket_rs, root, *args, stdin="y\n")
    assert f"Deleting pocket secrets via SSM {pocket_key} ..." in result.stderr
    assert _py_secrets(pocket_rs, root)[pocket_key] == {}


def test_reload_env_overlays_fresh_secrets(tmp_path, pocket_rs, moto_endpoint):
    project = "rsreload"
    root = _project(tmp_path, project, _SECRETS)
    _ok(
        pocket_rs,
        root,
        "container",
        "secrets",
        "create-pocket-managed",
        "--stage",
        "prod",
    )
    secret = _py_secrets(pocket_rs, root)[f"prod-{project}-main-pocket"]["SECRET_KEY"]
    function = f"prod-{project}-pocket-main-wsgi"
    iam = _client("iam", moto_endpoint)
    role = iam.create_role(
        RoleName=f"{project}-fn",
        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
    )["Role"]["Arn"]
    lambda_client = _client("lambda", moto_endpoint)
    lambda_client.create_function(
        FunctionName=function,
        Role=role,
        PackageType="Image",
        Code={"ImageUri": "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/x:prod"},
        Environment={"Variables": {"KEEP": "1", "SECRET_KEY": "old"}},
    )

    result = _ok(pocket_rs, root, "container", "status-env", "--stage", "prod")
    assert "  ~ SECRET_KEY (Lambda 値が古い、reload-env で更新)" in result.stderr

    result = _ok(pocket_rs, root, "container", "reload-env", "--stage", "prod")
    assert (
        "[wsgi] env を更新しました (1/1 秘密値を反映、warm container は再生成)"
        in result.stderr
    )
    assert "  - SECRET_KEY" in result.stderr
    variables = lambda_client.get_function_configuration(FunctionName=function)[
        "Environment"
    ]["Variables"]
    assert variables == {"KEEP": "1", "SECRET_KEY": secret}

    result = _ok(pocket_rs, root, "container", "reload-env", "--stage", "prod")
    assert "[wsgi] 差分なし (handler 内 env は既に最新)" in result.stderr
    result = _ok(pocket_rs, root, "container", "status-env", "--stage", "prod")
    assert "drift なし。Lambda env と secrets は同期されています。" in result.stderr

    result = _rs(
        pocket_rs, root, "container", "reload-env", "--stage", "prod", "--handler", "x"
    )
    assert result.returncode != 0
    assert "handler 'x' が見つかりません。利用可能: wsgi" in result.stderr


# ---- 確認を断ると何もしない ----


@pytest.mark.parametrize(
    ("args", "question"),
    [
        (
            ("cloudfront", "destroy"),
            "stage 'prod' の CloudFront distribution を削除しますか？",
        ),
        (
            ("cloudfront-keys", "destroy"),
            "stage 'prod' の CloudFront signing key リソースを削除しますか？"
            "(署名済み URL が無効になります)",
        ),
        (
            ("cloudfront-waf", "destroy"),
            "stage 'prod' の WAF WebACL を削除しますか？(IP 制限が解除されます)",
        ),
    ],
)
def test_edge_destroy_asks_first(tmp_path, pocket_rs, moto_endpoint, args, question):
    root = _project(tmp_path, "rsedge", _PRUNE)
    result = _rs(pocket_rs, root, *args, "--stage", "prod", stdin="n\n")
    assert result.returncode != 0
    assert f"{question} [y/N]" in result.stdout
    assert "Aborted!" in result.stderr
