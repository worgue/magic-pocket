"""s3 / cloudfront 系の書き込み (pocket) を moto server 相手に確かめる。

部品の入口は非表示の `pocket deploy-parts` (通しの deploy は moto で作れない
stack で止まるため、部品ごとに通す)。
Rust が書いたものを Python 版の実装で読み、Python 版が書いたものを Rust が壊さないことを
見る (同じ bucket / policy を両方の CLI が扱う移行期間の前提)。

moto の CloudFormation は CloudFront / ACM / WAF のリソースを作れず、KVS の API も無い。
cloudfront の stack は、最新と判定される印 (template の hash) と output を付けた単純な
stack をテスト側で置く。stack の作成・更新と KVS の書き込みは Rust の単体テスト (fake)
で固定し、実機の確認は deploy 結合の課題で行う。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

REGION = "ap-southeast-1"

# Python 版の実装で S3 bucket の状態を判定する
_PY_S3_STATUS = """
import sys
from pocket.context import Context
from pocket_cli.resources.s3 import S3
ctx = Context.from_toml(stage=sys.argv[1])
print(S3(ctx.s3, cloudfront_contexts=ctx.cloudfront).status)
"""

# Python 版の実装で S3 bucket を作る
_PY_S3_CREATE = """
import sys
from pocket.context import Context
from pocket_cli.resources.s3 import S3
ctx = Context.from_toml(stage=sys.argv[1])
S3(ctx.s3, cloudfront_contexts=ctx.cloudfront).create()
"""

# Python 版の実装で cloudfront の stack 名と bucket policy の状態を見る
_PY_CLOUDFRONT = """
import json, sys
from pocket.context import Context
from pocket_cli.resources.cloudfront import CloudFront
ctx = Context.from_toml(stage=sys.argv[1])
cf = CloudFront(ctx.cloudfront[sys.argv[2]])
if sys.argv[3] == "stack-name":
    print(cf.stack.name)
elif sys.argv[3] == "policy-require-update":
    print(json.dumps(cf.bucket_policy_require_update))
elif sys.argv[3] == "ensure-policy":
    cf._ensure_bucket_policy()
elif sys.argv[3] == "upload":
    cf.upload(skip_build=True)
elif sys.argv[3] == "managed-assets":
    cf.upload_managed_assets()
"""


pytestmark = pytest.mark.usefixtures("moto_versioning_root")


def _client(service: str, endpoint: str, region: str = REGION):
    return boto3.client(
        service,
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 moto 用のダミー
    )


def _toml(project: str, body: str) -> str:
    return (
        "[general]\n"
        f'region = "{REGION}"\n'
        f'project_name = "{project}"\n'
        'stages = ["prod"]\n\n' + body
    )


def _project(tmp_path: Path, toml: str) -> Path:
    root = tmp_path / "prj"
    root.mkdir(exist_ok=True)
    (root / "pocket.toml").write_text(toml)
    (root / "pyproject.toml").write_text('[project]\nname = "e2e-prj"\n')
    return root


def _env(pocket_rs) -> dict[str, str]:
    return {k: v for k, v in pocket_rs.env.items() if k != "DEPLOY_HASH"}


def _part(pocket_rs, root: Path, part: str, *args: str):
    return subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs.binary), "deploy-parts", part, "--stage", "prod", *args],
        cwd=root,
        env=_env(pocket_rs),
        capture_output=True,
        text=True,
    )


def _ok(pocket_rs, root: Path, part: str, *args: str):
    result = _part(pocket_rs, root, part, *args)
    assert result.returncode == 0, result.stderr + result.stdout
    return result


def _py(pocket_rs, root: Path, script: str, *args: str):
    result = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", script, "prod", *args],
        cwd=root,
        env=_env(pocket_rs),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result


def _bucket(project: str) -> str:
    return f"prod-{project}-pocket"


# ---- S3 ----

_S3_FULL = """
[s3]
versioning = true
cors = { cloudfront = ["web"], methods = ["GET", "PUT"] }
lifecycle_rules = [
    { id = "tmp", prefix = "tmp/", expiration_days = 7 },
    { id = "old", prefix = "", noncurrent_version_expiration_days = 30 },
]

[cloudfront.web]
[[cloudfront.web.routes]]
is_default = true
origin_path = "/web"
"""

_S3_PLAIN = """
[s3]

[cloudfront.web]
[[cloudfront.web.routes]]
is_default = true
origin_path = "/web"
"""


def _s3_settings(s3, bucket: str) -> dict:
    """bucket の 4 設定 (比較用。無いものは None)"""

    def get(call, key):
        try:
            return call(Bucket=bucket).get(key)
        except s3.exceptions.ClientError:
            return None

    return {
        "block": get(s3.get_public_access_block, "PublicAccessBlockConfiguration"),
        "cors": get(s3.get_bucket_cors, "CORSRules"),
        "versioning": get(s3.get_bucket_versioning, "Status"),
        "lifecycle": get(s3.get_bucket_lifecycle_configuration, "Rules"),
    }


def test_bucket_created_by_rust_is_completed_for_python(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "s3rs"
    root = _project(tmp_path, _toml(project, _S3_FULL))
    s3 = _client("s3", moto_endpoint)

    _ok(pocket_rs, root, "s3")
    assert _py(pocket_rs, root, _PY_S3_STATUS).stdout.strip() == "COMPLETED"
    settings = _s3_settings(s3, _bucket(project))
    assert settings["versioning"] == "Enabled"
    assert settings["cors"][0]["AllowedOrigins"] == ["https://*.cloudfront.net"]
    assert [rule["ID"] for rule in settings["lifecycle"]] == ["tmp", "old"]
    location = s3.get_bucket_location(Bucket=_bucket(project))
    assert location["LocationConstraint"] == REGION

    # 設定を外す: CORS と lifecycle は消え、versioning は Suspended になる
    (root / "pocket.toml").write_text(_toml(project, _S3_PLAIN))
    assert _py(pocket_rs, root, _PY_S3_STATUS).stdout.strip() == "REQUIRE_UPDATE"
    result = _ok(pocket_rs, root, "s3")
    assert "CORS 設定を削除しました" in result.stderr
    assert "Versioning を Suspended にしました" in result.stderr
    assert "Lifecycle 設定を削除しました" in result.stderr
    assert _py(pocket_rs, root, _PY_S3_STATUS).stdout.strip() == "COMPLETED"
    settings = _s3_settings(s3, _bucket(project))
    assert (settings["cors"], settings["lifecycle"]) == (None, None)
    assert settings["versioning"] == "Suspended"

    # 変更が無ければ何も書かない
    result = _ok(pocket_rs, root, "s3")
    assert "Public access block is already configured properly." in result.stderr
    assert "しました" not in result.stderr


def test_bucket_created_by_python_is_left_alone_by_rust(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "s3py"
    root = _project(tmp_path, _toml(project, _S3_FULL))
    s3 = _client("s3", moto_endpoint)

    _py(pocket_rs, root, _PY_S3_CREATE)
    before = _s3_settings(s3, _bucket(project))
    result = _ok(pocket_rs, root, "s3")
    assert "しました" not in result.stderr, result.stderr
    assert "Update public access block" not in result.stderr
    assert _s3_settings(s3, _bucket(project)) == before


def test_bucket_delete_removes_versions_across_pages(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "s3del"
    root = _project(tmp_path, _toml(project, _S3_FULL))
    s3 = _client("s3", moto_endpoint)
    bucket = _bucket(project)
    _ok(pocket_rs, root, "s3")
    # 一覧の 1 ページ (1000 件) を超える object と、version・delete marker
    for i in range(1005):
        s3.put_object(Bucket=bucket, Key=f"bulk/{i:04}", Body=b"x")
    s3.put_object(Bucket=bucket, Key="doc.txt", Body=b"v1")
    s3.put_object(Bucket=bucket, Key="doc.txt", Body=b"v2")
    s3.delete_object(Bucket=bucket, Key="doc.txt")

    result = _ok(pocket_rs, root, "s3-delete")
    assert f"{bucket}: deleted" in result.stdout
    with pytest.raises(s3.exceptions.ClientError):
        s3.head_bucket(Bucket=bucket)
    assert f"{bucket}: not found" in _ok(pocket_rs, root, "s3-delete").stdout


# ---- cloudfront: bucket policy ----

_CLOUDFRONT = """
[s3]

[cloudfront.web]
managed_assets = "fonts"

[[cloudfront.web.routes]]
is_default = true
is_spa = true
origin_path = "/spa"
build = { dir = "dist", cmd = "sh build.sh" }

[[cloudfront.web.routes]]
path_pattern = "/assets/*"
versioning = "content_hash"
versioned_max_age = 3600
upload_dir = "build/assets"

[[cloudfront.web.routes]]
path_pattern = "/assetsx/*"
upload_dir = "build/assetsx"
"""


def _distribution(moto_endpoint, bucket: str) -> str:
    """moto に distribution を作り、その id を返す (invalidation の宛先)"""
    cloudfront = _client("cloudfront", moto_endpoint)
    response = cloudfront.create_distribution(
        DistributionConfig={
            "CallerReference": bucket,
            "Comment": "",
            "Enabled": True,
            "Origins": {
                "Quantity": 1,
                "Items": [
                    {
                        "Id": "s3",
                        "DomainName": f"{bucket}.s3.amazonaws.com",
                        "S3OriginConfig": {"OriginAccessIdentity": ""},
                    }
                ],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": "s3",
                "ViewerProtocolPolicy": "allow-all",
                "MinTTL": 0,
                "ForwardedValues": {
                    "QueryString": False,
                    "Cookies": {"Forward": "none"},
                },
            },
        }
    )
    return response["Distribution"]["Id"]


def _place_cloudfront_stack(pocket_rs, root: Path, moto_endpoint, project: str) -> str:
    """bucket と、deploy 済みと判定される cloudfront の stack を置く。

    戻り値は distribution の id。
    """
    _ok(pocket_rs, root, "s3")
    distribution_id = _distribution(moto_endpoint, _bucket(project))
    name = _py(pocket_rs, root, _PY_CLOUDFRONT, "web", "stack-name").stdout.strip()
    template = _ok(pocket_rs, root, "cloudfront-template", "--cloudfront", "web").stdout
    _client("cloudformation", moto_endpoint).create_stack(
        StackName=name,
        TemplateBody=json.dumps(
            {
                "Parameters": {"Dist": {"Type": "String"}},
                "Resources": {"Topic": {"Type": "AWS::SNS::Topic", "Properties": {}}},
                "Outputs": {"DistributionId": {"Value": {"Ref": "Dist"}}},
            }
        ),
        Parameters=[{"ParameterKey": "Dist", "ParameterValue": distribution_id}],
        Tags=[
            {
                "Key": "pocket:template_hash",
                "Value": hashlib.sha256(template.encode()).hexdigest()[:16],
            }
        ],
    )
    return distribution_id


def _policy(s3, bucket: str) -> dict | None:
    try:
        return json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    except s3.exceptions.ClientError:
        return None


_OTHER_STATEMENT = {
    "Sid": "Other",
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::123456789012:root"},
    "Action": "s3:ListBucket",
    "Resource": "arn:aws:s3:::placeholder",
}


def test_bucket_policy_written_by_rust_satisfies_python(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "cfpol"
    root = _project(tmp_path, _toml(project, _CLOUDFRONT))
    s3 = _client("s3", moto_endpoint)
    bucket = _bucket(project)
    distribution_id = _place_cloudfront_stack(pocket_rs, root, moto_endpoint, project)
    # 先にある他の statement は残す
    other = {**_OTHER_STATEMENT, "Resource": f"arn:aws:s3:::{bucket}"}
    s3.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps({"Version": "2012-10-17", "Statement": [other]}),
    )

    result = _ok(pocket_rs, root, "cloudfront-post", "--cloudfront", "web")
    assert f"Updating bucket policy for {bucket}." in result.stderr
    policy = _policy(s3, bucket)
    assert policy is not None
    assert policy["Statement"][0] == other
    own = policy["Statement"][1]
    assert own["Resource"] == f"arn:aws:s3:::{bucket}/*"
    assert own["Condition"]["StringEquals"]["AWS:SourceArn"].endswith(
        f":distribution/{distribution_id}"
    )
    check = _py(pocket_rs, root, _PY_CLOUDFRONT, "web", "policy-require-update")
    assert json.loads(check.stdout) is False

    # 2 回目は書かない
    result = _ok(pocket_rs, root, "cloudfront-post", "--cloudfront", "web")
    assert "Bucket policy is already configured properly." in result.stderr
    assert _policy(s3, bucket) == policy

    # 削除: 自分の statement だけ外し、stack を消す (bucket は残す)
    result = _ok(pocket_rs, root, "cloudfront-delete", "--cloudfront", "web")
    assert _policy(s3, bucket) == {"Version": "2012-10-17", "Statement": [other]}
    s3.head_bucket(Bucket=bucket)
    # moto server は他のテストと共有なので、この project の stack だけを見る
    stacks = _client("cloudformation", moto_endpoint).describe_stacks()["Stacks"]
    assert [
        s
        for s in stacks
        if s["StackName"].startswith(f"prod-{project}-")
        and s["StackStatus"] != "DELETE_COMPLETE"
    ] == []


def test_bucket_policy_written_by_python_is_kept_by_rust(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "cfpolpy"
    root = _project(tmp_path, _toml(project, _CLOUDFRONT))
    s3 = _client("s3", moto_endpoint)
    bucket = _bucket(project)
    _place_cloudfront_stack(pocket_rs, root, moto_endpoint, project)

    _py(pocket_rs, root, _PY_CLOUDFRONT, "web", "ensure-policy")
    before = s3.get_bucket_policy(Bucket=bucket)["Policy"]
    result = _ok(pocket_rs, root, "cloudfront-post", "--cloudfront", "web")
    assert "Bucket policy is already configured properly." in result.stderr
    assert s3.get_bucket_policy(Bucket=bucket)["Policy"] == before

    # 自分の statement だけの policy は、削除で policy ごと消える
    _ok(pocket_rs, root, "cloudfront-delete", "--cloudfront", "web")
    assert _policy(s3, bucket) is None


def test_post_state_is_skipped_until_stack_is_deployed(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "cfnostack"
    root = _project(tmp_path, _toml(project, _CLOUDFRONT))
    _ok(pocket_rs, root, "s3")
    _ok(pocket_rs, root, "cloudfront-post", "--cloudfront", "web")
    assert _policy(_client("s3", moto_endpoint), _bucket(project)) is None


# ---- cloudfront: upload ----


def _write(root: Path, files: dict[str, str]):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _site(root: Path):
    """build の出力 (dist は build コマンドが作る) と upload_dir の中身"""
    _write(
        root,
        {
            "build.sh": "mkdir -p dist/js && cp src/index.html dist/"
            " && cp src/app.js dist/js/ && pwd > built-in.txt\n",
            "src/index.html": "<html>v1</html>",
            "src/app.js": "console.log(1)",
            "build/assets/a.css": "a{}",
            "build/assets/img/b.png": "png",
            "build/assetsx/x.txt": "x",
        },
    )


def _objects(s3, bucket: str, prefix: str = "") -> dict[str, dict]:
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
    return {obj["Key"]: s3.head_object(Bucket=bucket, Key=obj["Key"]) for obj in listed}


def _invalidations(moto_endpoint, distribution_id: str) -> list[list[str]]:
    cloudfront = _client("cloudfront", moto_endpoint)
    listed = cloudfront.list_invalidations(DistributionId=distribution_id)
    paths = []
    for item in listed["InvalidationList"].get("Items", []):
        detail = cloudfront.get_invalidation(
            DistributionId=distribution_id, Id=item["Id"]
        )
        paths.append(detail["Invalidation"]["InvalidationBatch"]["Paths"]["Items"])
    return paths


def test_upload_by_rust_is_skipped_by_python(tmp_path, pocket_rs, moto_endpoint):
    project = "cfup"
    root = _project(tmp_path, _toml(project, _CLOUDFRONT))
    s3 = _client("s3", moto_endpoint)
    bucket = _bucket(project)
    distribution_id = _place_cloudfront_stack(pocket_rs, root, moto_endpoint, project)
    _site(root)
    # 隣の prefix (assetsx) と、route の外の object には触らない
    s3.put_object(Bucket=bucket, Key="other/keep.txt", Body=b"keep")

    result = _ok(pocket_rs, root, "upload", "--cloudfront", "web")
    # build は pocket.toml のディレクトリで実行される
    assert (root / "built-in.txt").read_text().strip() == str(root.resolve())
    objects = _objects(s3, bucket)
    assert sorted(objects) == [
        "assets/a.css",
        "assets/img/b.png",
        "assetsx/x.txt",
        "other/keep.txt",
        "spa/index.html",
        "spa/js/app.js",
    ]
    assert objects["spa/index.html"]["CacheControl"] == "no-cache, no-store"
    assert objects["spa/index.html"]["ContentType"] == "text/html"
    assert objects["spa/js/app.js"]["CacheControl"] == "max-age=31536000"
    assert objects["assets/a.css"]["CacheControl"] == "public, max-age=3600, immutable"
    assert objects["assets/a.css"]["ContentType"] == "text/css"
    assert objects["assets/img/b.png"]["ContentType"] == "image/png"
    assert "CacheControl" not in objects["assetsx/x.txt"]
    # 変更があった 3 route を 1 回の invalidation にまとめる
    assert _invalidations(moto_endpoint, distribution_id) == [
        ["/*", "/assets/*", "/assetsx/*"]
    ]
    assert "CloudFront キャッシュ無効化をリクエストしました" in result.stderr

    # Python 版は全件を skip する (ETag = MD5 が一致)
    upload = _py(pocket_rs, root, _PY_CLOUDFRONT, "web", "upload")
    assert upload.stderr.count("0 ファイルをアップロードしました") == 3, upload.stderr
    assert len(_invalidations(moto_endpoint, distribution_id)) == 1

    # Rust も 2 回目は何も上げず、invalidation も出さない
    result = _ok(pocket_rs, root, "upload", "--cloudfront", "web", "--skip-build")
    assert result.stderr.count("0 ファイルをアップロードしました") == 3, result.stderr
    assert len(_invalidations(moto_endpoint, distribution_id)) == 1


def test_upload_by_python_is_skipped_by_rust_and_stale_is_handled(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "cfuppy"
    root = _project(tmp_path, _toml(project, _CLOUDFRONT))
    s3 = _client("s3", moto_endpoint)
    bucket = _bucket(project)
    distribution_id = _place_cloudfront_stack(pocket_rs, root, moto_endpoint, project)
    _site(root)
    subprocess.run(["sh", "build.sh"], cwd=root, check=True)  # noqa: S603, S607

    _py(pocket_rs, root, _PY_CLOUDFRONT, "web", "upload")
    before = len(_invalidations(moto_endpoint, distribution_id))
    result = _ok(pocket_rs, root, "upload", "--cloudfront", "web", "--skip-build")
    assert result.stderr.count("0 ファイルをアップロードしました") == 3, result.stderr
    assert len(_invalidations(moto_endpoint, distribution_id)) == before

    # ローカルから消えたファイル: spa (delete_stale) は消し、content_hash は旧版を残す
    (root / "dist/js/app.js").unlink()
    (root / "build/assets/a.css").unlink()
    result = _ok(pocket_rs, root, "upload", "--cloudfront", "web", "--skip-build")
    assert "削除 1、prefix: spa" in result.stderr
    assert "旧版を保持 1、prefix: assets)" in result.stderr
    objects = _objects(s3, bucket)
    assert "spa/js/app.js" not in objects
    assert "assets/a.css" in objects
    # 削除だけでも、その route は invalidation する (残しただけの route はしない)
    assert _invalidations(moto_endpoint, distribution_id)[before:] == [["/*"]]


def test_upload_stops_on_build_failure_and_missing_upload_dir(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "cfupng"
    root = _project(tmp_path, _toml(project, _CLOUDFRONT))
    s3 = _client("s3", moto_endpoint)
    bucket = _bucket(project)
    distribution_id = _place_cloudfront_stack(pocket_rs, root, moto_endpoint, project)
    s3.put_object(Bucket=bucket, Key="spa/old.html", Body=b"old")

    # build の出力先が無い: prefix の object を消さずに止める
    result = _part(pocket_rs, root, "upload", "--cloudfront", "web", "--skip-build")
    assert result.returncode != 0
    assert "がありません" in result.stderr
    assert "spa/old.html" in _objects(s3, bucket)

    # build の失敗: 何も上げずに止める
    _write(root, {"build.sh": "exit 3\n"})
    result = _part(pocket_rs, root, "upload", "--cloudfront", "web")
    assert result.returncode != 0
    assert "build コマンドが失敗しました (exit 3): sh build.sh" in result.stderr
    assert sorted(_objects(s3, bucket)) == ["spa/old.html"]
    assert _invalidations(moto_endpoint, distribution_id) == []


def test_managed_assets_follow_stage_directory(tmp_path, pocket_rs, moto_endpoint):
    project = "cfma"
    root = _project(tmp_path, _toml(project, _CLOUDFRONT))
    s3 = _client("s3", moto_endpoint)
    bucket = _bucket(project)
    _ok(pocket_rs, root, "s3")

    # ディレクトリが無ければ警告だけ
    result = _ok(pocket_rs, root, "managed-assets", "--cloudfront", "web")
    assert "managed_assets ディレクトリが見つかりません" in result.stderr

    _write(root, {"fonts/default/a.woff2": "default", "fonts/default/sub/x": "no"})
    s3.put_object(Bucket=bucket, Key="pocket_managed/stale.woff2", Body=b"stale")
    _ok(pocket_rs, root, "managed-assets", "--cloudfront", "web")
    objects = _objects(s3, bucket, "pocket_managed/")
    assert sorted(objects) == ["pocket_managed/a.woff2"]
    assert objects["pocket_managed/a.woff2"]["ContentType"] == "font/woff2"

    # stage のディレクトリがあればそちらだけを使う
    _write(root, {"fonts/prod/b.woff2": "prod"})
    _ok(pocket_rs, root, "managed-assets", "--cloudfront", "web")
    assert sorted(_objects(s3, bucket, "pocket_managed/")) == ["pocket_managed/b.woff2"]

    # Python 版が上げた結果と同じ
    _py(pocket_rs, root, _PY_CLOUDFRONT, "web", "managed-assets")
    assert sorted(_objects(s3, bucket, "pocket_managed/")) == ["pocket_managed/b.woff2"]
