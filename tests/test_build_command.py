"""Step A: build once 用の build & push のテスト。

- get_commit_hash: COMMIT_HASH env 優先 / git フル hash
- Ecr.build_and_push: tag 上書き (案 a)。省略時は stage タグ
- AwsContainer.build: ensure_exists + build_and_push(tag) に委譲
"""

import boto3
import pytest
from moto import mock_aws
from pocket_cli.resources.aws.ecr import Ecr
from pocket_cli.resources.awscontainer import AwsContainer

from pocket.context import Context, get_commit_hash

REGION = "ap-southeast-1"


class _FakeBuilder:
    def __init__(self):
        self.targets = []

    def build_and_push(self, *, target, dockerfile_path, platform):
        self.targets.append(target)

    def delete(self):
        pass


def test_get_commit_hash_env_override(monkeypatch):
    """COMMIT_HASH 環境変数があればそれを返す (git に触らない)。"""
    monkeypatch.setenv("COMMIT_HASH", "deadbeefcafe")
    assert get_commit_hash() == "deadbeefcafe"


def test_get_commit_hash_uses_git(monkeypatch):
    """COMMIT_HASH 未設定なら git rev-parse HEAD の結果を返す。"""
    monkeypatch.delenv("COMMIT_HASH", raising=False)

    class _Result:
        returncode = 0
        stdout = "abc123def456\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Result())
    assert get_commit_hash() == "abc123def456"


def test_get_commit_hash_raises_outside_git(monkeypatch):
    """git 取得失敗時は黙って unknown にせず例外で落とす。"""
    monkeypatch.delenv("COMMIT_HASH", raising=False)

    class _Result:
        returncode = 128
        stdout = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Result())
    with pytest.raises(RuntimeError):
        get_commit_hash()


@mock_aws
def test_ecr_build_and_push_tag_override():
    """build_and_push(tag=...) は uri:tag を builder に渡す。省略時は stage タグ。"""
    client = boto3.client("ecr", region_name=REGION)
    client.create_repository(repositoryName="myrepo")
    builder = _FakeBuilder()
    ecr = Ecr(REGION, "myrepo", "dev", "Dockerfile", "linux/amd64", builder=builder)

    ecr.build_and_push(tag="abc123")
    assert builder.targets[-1].endswith(":abc123")

    ecr.build_and_push()  # tag 省略 → stage 名 (self.tag = "dev")
    assert builder.targets[-1].endswith(":dev")


@mock_aws
def test_awscontainer_build_delegates_to_ecr(use_toml, tmp_path, monkeypatch):
    """AwsContainer.build は runtime config 生成 + ensure_exists + build_and_push(tag)。

    pocket.runtime.toml は Dockerfile の COPY で image に焼き込まれるため、
    deploy_init と同様に build 前の生成が必須 (欠けると stale な設定が焼かれる)。
    """
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
    context = Context.from_toml(stage="dev")
    assert context.awscontainer
    ac = AwsContainer(context.awscontainer)

    calls = {}
    monkeypatch.setattr(
        "pocket_cli.resources.awscontainer.generate_runtime_config",
        lambda path: calls.__setitem__("runtime_config", path),
    )
    monkeypatch.setattr(
        Ecr, "ensure_exists", lambda self: calls.__setitem__("ensure", True)
    )
    monkeypatch.setattr(
        Ecr, "build_and_push", lambda self, tag=None: calls.__setitem__("tag", tag)
    )

    ac.build("abc123")
    assert calls["ensure"] is True
    assert calls["tag"] == "abc123"
    assert "runtime_config" in calls
