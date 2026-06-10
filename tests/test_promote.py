"""Step C: promote (再ビルドなし deploy) のテスト。

- Ecr.retag: タグ付け替え / source 不在エラー / 冪等性
- AwsContainer.deploy_init: promote_commit_hash 設定時は build せず retag
- is_working_tree_dirty: build の dirty チェック用ヘルパ
"""

import json

import boto3
import pytest
from moto import mock_aws
from pocket_cli.resources.aws.ecr import Ecr
from pocket_cli.resources.awscontainer import AwsContainer

from pocket.context import Context, is_working_tree_dirty

REGION = "ap-southeast-1"

_MANIFEST = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": 7023,
            "digest": "sha256:" + "a" * 64,
        },
        "layers": [],
    }
)


def _make_ecr(client, *, name="myrepo", tag="dev"):
    client.create_repository(repositoryName=name)
    return Ecr(REGION, name, tag, "Dockerfile", "linux/amd64")


def _digest_of(client, repo, tag):
    for detail in client.describe_images(repositoryName=repo)["imageDetails"]:
        if tag in detail.get("imageTags", []):
            return detail["imageDigest"]
    return None


@mock_aws
def test_retag_moves_stage_tag():
    """retag で :<sha> の image に :<stage> タグが付き、同じ digest を指す。"""
    client = boto3.client("ecr", region_name=REGION)
    ecr = _make_ecr(client)
    client.put_image(
        repositoryName="myrepo", imageManifest=_MANIFEST, imageTag="abc123"
    )

    ecr.retag("abc123", "dev")
    assert _digest_of(client, "myrepo", "dev") == _digest_of(client, "myrepo", "abc123")


@mock_aws
def test_retag_missing_source_raises():
    """source tag の image が無ければ ValueError (黙って進まない)。"""
    client = boto3.client("ecr", region_name=REGION)
    ecr = _make_ecr(client)
    with pytest.raises(ValueError, match="存在しません"):
        ecr.retag("nonexistent", "dev")


@mock_aws
def test_retag_missing_repository_raises():
    """repository 自体が無ければ ValueError。"""
    ecr = Ecr(REGION, "norepo", "dev", "Dockerfile", "linux/amd64")
    with pytest.raises(ValueError, match="存在しません"):
        ecr.retag("abc123", "dev")


@mock_aws
def test_retag_is_idempotent():
    """同じ昇格を 2 回実行してもエラーにならない (再 promote の冪等性)。"""
    client = boto3.client("ecr", region_name=REGION)
    ecr = _make_ecr(client)
    client.put_image(
        repositoryName="myrepo", imageManifest=_MANIFEST, imageTag="abc123"
    )
    ecr.retag("abc123", "dev")
    ecr.retag("abc123", "dev")  # 2 回目: ImageAlreadyExists を握って no-op
    assert _digest_of(client, "myrepo", "dev") is not None


def _make_context(use_toml, tmp_path):
    toml_path = tmp_path / "pocket.toml"
    toml_path.write_text(
        """
[general]
region = "ap-southeast-1"
project_name = "testprj"
stages = ["dev"]

[awscontainer]
dockerfile_path = "Dockerfile"

[awscontainer.handlers.wsgi]
command = "pocket.django.lambda_handlers.wsgi_handler"
"""
    )
    use_toml(str(toml_path))
    return Context.from_toml(stage="dev")


@mock_aws
def test_deploy_init_promotes_without_build(use_toml, tmp_path, monkeypatch):
    """promote_commit_hash 設定時の deploy_init は retag のみ。

    build (ecr.sync) も runtime config 生成も行わない (image は build 時に
    焼き込み済みのため)。
    """
    context = _make_context(use_toml, tmp_path)
    assert context.awscontainer
    context.awscontainer.promote_commit_hash = "abc123"
    ac = AwsContainer(context.awscontainer)

    calls = {}
    monkeypatch.setattr(
        "pocket_cli.resources.awscontainer.generate_runtime_config",
        lambda path: calls.__setitem__("runtime_config", True),
    )
    monkeypatch.setattr(Ecr, "sync", lambda self: calls.__setitem__("sync", True))
    monkeypatch.setattr(
        Ecr,
        "retag",
        lambda self, src, dest: calls.__setitem__("retag", (src, dest)),
    )

    ac.deploy_init()
    assert calls == {"retag": ("abc123", "dev")}


@mock_aws
def test_deploy_init_default_builds(use_toml, tmp_path, monkeypatch):
    """promote_commit_hash 未設定 (通常 deploy) は従来どおり runtime config + sync。"""
    context = _make_context(use_toml, tmp_path)
    assert context.awscontainer
    ac = AwsContainer(context.awscontainer)

    calls = {}
    monkeypatch.setattr(
        "pocket_cli.resources.awscontainer.generate_runtime_config",
        lambda path: calls.__setitem__("runtime_config", True),
    )
    monkeypatch.setattr(Ecr, "sync", lambda self: calls.__setitem__("sync", True))
    monkeypatch.setattr(
        Ecr,
        "retag",
        lambda self, src, dest: calls.__setitem__("retag", (src, dest)),
    )

    ac.deploy_init()
    assert calls == {"runtime_config": True, "sync": True}


def test_is_working_tree_dirty(monkeypatch):
    """porcelain 出力の有無で dirty を判定。git 外 (非 0 終了) は False。"""

    def _fake(stdout, returncode=0):
        class _Result:
            pass

        r = _Result()
        r.stdout = stdout
        r.returncode = returncode
        return r

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _fake(" M foo.py\n"))
    assert is_working_tree_dirty() is True

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _fake(""))
    assert is_working_tree_dirty() is False

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _fake("", returncode=128))
    assert is_working_tree_dirty() is False
