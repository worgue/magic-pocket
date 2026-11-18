"""container 系の書き込み (pocket-rs) を moto server 相手に確かめる。

部品の入口は非表示の `pocket-rs deploy-parts` (通しの deploy は moto で作れない
stack で止まるため、部品ごとに通す)。
Rust が書いたものを Python 版の実装で読み、Python 版が書いたものを Rust が壊さないことを
見る (同じ store / stack を両方の CLI が扱う移行期間の前提)。

moto の CloudFormation は Lambda / API Gateway を含む template を作れないので、stack の
作成・更新は handler の無い container で通す。Lambda の更新は、function と stack を
テスト側で moto に置いてから `container-update` を呼ぶ。
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

REGION = "ap-southeast-1"

# Python 版の実装で managed secret を読む (pocket_key → {key: 値})
_PY_READ = """
import json, sys
from pocket.context import Context
ctx = Context.from_toml(stage=sys.argv[1])
views = ([ctx.secrets] if ctx.secrets else [])
views += [c.secrets for c in ctx.container.values() if c.secrets]
print(json.dumps({v.pocket_key: v.pocket_store.secrets for v in views}, sort_keys=True))
"""

# Python 版の実装で managed secret を用意する
_PY_ENSURE = """
import sys
from pocket.context import Context
from pocket_cli.mediator import Mediator
Mediator(Context.from_toml(stage=sys.argv[1])).ensure_pocket_managed_secrets()
"""

# Python 版の実装で container の stack の状態を判定する
_PY_STACK_STATUS = """
import sys
from pocket.context import Context
from pocket_cli.resources.aws.cloudformation import ContainerStack
ctx = Context.from_toml(stage=sys.argv[1])
print(ContainerStack(ctx.container[sys.argv[2]]).status)
"""


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


def _env(pocket_rs, extra: dict[str, str] | None = None) -> dict[str, str]:
    drop = ("DEPLOY_HASH", "POCKET_PERMISSIONS_BOUNDARY_ARN")
    drop += ("FORGE_PERMISSIONS_BOUNDARY_ARN", "CODEBUILD_PERMISSIONS_BOUNDARY")
    env = {k: v for k, v in pocket_rs.env.items() if k not in drop}
    return {**env, **(extra or {})}


def _rs(pocket_rs, root: Path, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs.binary), *args],
        cwd=root,
        env=_env(pocket_rs, env),
        capture_output=True,
        text=True,
    )


def _part(pocket_rs, root: Path, part: str, *args: str, env=None):
    return _rs(pocket_rs, root, "deploy-parts", part, "--stage", "prod", *args, env=env)


def _py(pocket_rs, root: Path, script: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", script, "prod", *args],
        cwd=root,
        env=_env(pocket_rs),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _py_secrets(pocket_rs, root: Path) -> dict[str, dict]:
    return json.loads(_py(pocket_rs, root, _PY_READ))


def _context(pocket_rs, root: Path) -> dict:
    result = _rs(pocket_rs, root, "context", "--stage", "prod")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _secrets_toml(project: str, store: str, *, extra_key: bool = True) -> str:
    managed = [
        'SECRET_KEY = { type = "password", options = { length = 50 } }',
        'SHARED_TOKEN = { type = "spa_token_secret", shared = true }',
        'SIGNING = { type = "rsa_pem_base64" }',
    ]
    if extra_key:
        managed.append('EXTRA = { type = "password" }')
    return _toml(
        project,
        '[container.main]\ndockerfile_path = "Dockerfile"\n\n'
        f'[container.main.secrets]\nstore = "{store}"\n\n'
        "[container.main.secrets.managed]\n" + "\n".join(managed) + "\n",
    )


@pytest.mark.parametrize("store", ["sm", "ssm"])
def test_secrets_written_by_rust_are_read_by_python(tmp_path, pocket_rs, store):
    project = f"rswrite{store}"
    root = _project(tmp_path, _secrets_toml(project, store))
    result = _part(pocket_rs, root, "secrets")
    assert result.returncode == 0, result.stderr

    secrets = _py_secrets(pocket_rs, root)
    shared = secrets[f"prod-{project}-pocket"]
    main = secrets[f"prod-{project}-main-pocket"]
    assert set(shared) == {"SHARED_TOKEN"}
    assert len(shared["SHARED_TOKEN"]) == 64
    assert set(main) == {"SECRET_KEY", "SIGNING", "EXTRA"}
    assert len(main["SECRET_KEY"]) == 50
    assert len(main["EXTRA"]) == 16
    pem = base64.b64decode(main["SIGNING"]["pem"]).decode()
    pub = base64.b64decode(main["SIGNING"]["pub"]).decode()
    assert pem.startswith("-----BEGIN RSA PRIVATE KEY-----\n")
    assert pub.startswith("-----BEGIN PUBLIC KEY-----\n")

    # もう一度実行しても値は変わらない
    result = _part(pocket_rs, root, "secrets")
    assert result.returncode == 0, result.stderr
    assert _py_secrets(pocket_rs, root) == secrets

    # 定義から外した key だけが消える
    _project(tmp_path, _secrets_toml(project, store, extra_key=False))
    result = _part(pocket_rs, root, "secrets")
    assert result.returncode == 0, result.stderr
    assert f"(prod-{project}-main-pocket): EXTRA" in result.stderr
    after = _py_secrets(pocket_rs, root)
    del main["EXTRA"]
    assert after == secrets


@pytest.mark.parametrize("store", ["sm", "ssm"])
def test_secrets_created_by_python_are_kept_by_rust(tmp_path, pocket_rs, store):
    project = f"pywrite{store}"
    root = _project(tmp_path, _secrets_toml(project, store))
    _py(pocket_rs, root, _PY_ENSURE)
    before = _py_secrets(pocket_rs, root)
    assert set(before[f"prod-{project}-main-pocket"]) == {
        "SECRET_KEY",
        "SIGNING",
        "EXTRA",
    }

    result = _part(pocket_rs, root, "secrets")
    assert result.returncode == 0, result.stderr
    assert _py_secrets(pocket_rs, root) == before


def test_user_secret_must_exist_before_deploy(tmp_path, pocket_rs):
    toml = _toml(
        "usersecret",
        '[neon]\nproject_name = "p"\n\n'
        '[container.main]\ndockerfile_path = "Dockerfile"\n\n'
        "[container.main.secrets.user]\n"
        'DATABASE_URL = { type = "neon_database_url" }\n',
    )
    root = _project(tmp_path, toml)
    result = _part(pocket_rs, root, "secrets", env={"NEON_API_KEY": "dummy"})
    assert result.returncode != 0
    assert "stored mode の user secret が見つかりません。" in result.stderr


def _put_image(ecr, repository: str, tag: str) -> str:
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "size": 1,
                "digest": "sha256:" + hashlib.sha256(tag.encode()).hexdigest(),
            },
            "layers": [],
        }
    )
    image = ecr.put_image(
        repositoryName=repository, imageManifest=manifest, imageTag=tag
    )
    return image["image"]["imageId"]["imageDigest"]


def test_promote_moves_stage_tag_without_build(tmp_path, pocket_rs, moto_endpoint):
    toml = _toml("promote", '[container.main]\ndockerfile_path = "Dockerfile"\n')
    root = _project(tmp_path, toml)
    repository = _context(pocket_rs, root)["container"]["main"]["ecr_name"]
    args = ("container-init", "--container", "main", "--promote", "abc1234")

    result = _part(pocket_rs, root, *args)
    assert result.returncode != 0
    assert f"ECR repository '{repository}' が存在しません。" in result.stderr

    ecr = _client("ecr", moto_endpoint)
    ecr.create_repository(repositoryName=repository)
    result = _part(pocket_rs, root, *args)
    assert result.returncode != 0
    assert f"image :abc1234 が ECR repository '{repository}' に存在しません。" in (
        result.stderr
    )

    digest = _put_image(ecr, repository, "abc1234")
    for _ in range(2):  # 2 回目は tag が同じ image を指している
        result = _part(pocket_rs, root, *args)
        assert result.returncode == 0, result.stderr
        details = ecr.describe_images(
            repositoryName=repository, imageIds=[{"imageTag": "prod"}]
        )["imageDetails"]
        assert details[0]["imageDigest"] == digest


def _template_hash(pocket_rs, root: Path, env=None) -> str:
    result = _part(
        pocket_rs, root, "container-template", "--container", "main", env=env
    )
    assert result.returncode == 0, result.stderr
    return hashlib.sha256(result.stdout.encode()).hexdigest()[:16]


def _stack(cfn, name: str) -> dict:
    return cfn.describe_stacks(StackName=name)["Stacks"][0]


def _stack_tag(cfn, name: str, key: str) -> str | None:
    tags = {t["Key"]: t["Value"] for t in _stack(cfn, name).get("Tags", [])}
    return tags.get(key)


def test_container_stack_create_and_update(tmp_path, pocket_rs, moto_endpoint):
    project = "stackprj"
    body = '[container.main]\ndockerfile_path = "Dockerfile"\n'
    secrets = '\n[container.main.secrets.managed]\nSECRET_KEY = { type = "password" }\n'
    root = _project(tmp_path, _toml(project, body + secrets))
    cfn = _client("cloudformation", moto_endpoint)
    name = f"prod-{project}-container-main"

    result = _part(pocket_rs, root, "container-create", "--container", "main")
    assert result.returncode == 0, result.stderr + result.stdout
    assert _stack(cfn, name)["StackStatus"] == "CREATE_COMPLETE"
    created = _stack_tag(cfn, name, "pocket:template_hash")
    assert created == _template_hash(pocket_rs, root)
    # secret は stack の前に用意されている
    assert set(_py_secrets(pocket_rs, root)[f"prod-{project}-main-pocket"]) == {
        "SECRET_KEY"
    }
    # Python 版も、Rust が作った stack を最新と判定する
    assert _py(pocket_rs, root, _PY_STACK_STATUS, "main").strip() == "COMPLETED"

    # template が同じなら stack を更新しない
    result = _part(pocket_rs, root, "container-update", "--container", "main")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Update stack" not in result.stdout

    # 設定を変えると stack を更新し、タグの hash も変わる
    policy = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"
    changed = body + f'\n[container.main.iam]\nmanaged_policy_arns = ["{policy}"]\n'
    _project(tmp_path, _toml(project, changed + secrets))
    assert _py(pocket_rs, root, _PY_STACK_STATUS, "main").strip() == "REQUIRE_UPDATE"
    result = _part(pocket_rs, root, "container-update", "--container", "main")
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Update stack" in result.stdout
    updated = _stack_tag(cfn, name, "pocket:template_hash")
    assert updated != created
    assert updated == _template_hash(pocket_rs, root)
    assert _py(pocket_rs, root, _PY_STACK_STATUS, "main").strip() == "COMPLETED"


def test_container_update_replaces_image_and_syncs_deploy_hash(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "lambdaprj"
    toml = _toml(
        project,
        '[container.main]\ndockerfile_path = "Dockerfile"\n\n'
        '[container.main.handlers.wsgi]\ncommand = "app.wsgi"\n\n'
        '[container.main.handlers.worker]\ncommand = "app.worker"\n',
    )
    root = _project(tmp_path, toml)
    env = {"DEPLOY_HASH": "newhash"}
    container = _context(pocket_rs, root)["container"]["main"]
    wsgi = container["handlers"]["wsgi"]["function_name"]

    ecr = _client("ecr", moto_endpoint)
    uri = ecr.create_repository(repositoryName=container["ecr_name"])["repository"][
        "repositoryUri"
    ]
    _put_image(ecr, container["ecr_name"], "prod")
    iam = _client("iam", moto_endpoint)
    role = iam.create_role(
        RoleName=f"{project}-fn",
        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
    )["Role"]["Arn"]
    lambda_client = _client("lambda", moto_endpoint)
    # wsgi だけが在る (worker は「無いので飛ばす」経路)
    lambda_client.create_function(
        FunctionName=wsgi,
        Role=role,
        PackageType="Image",
        Code={"ImageUri": uri + ":old"},
        Environment={"Variables": {"DEPLOY_HASH": "oldhash", "KEEP": "1"}},
    )
    # stack は moto が作れないので、最新と判定される印 (template の hash) を付けて置く
    cfn = _client("cloudformation", moto_endpoint)
    cfn.create_stack(
        StackName=f"prod-{project}-container-main",
        TemplateBody=json.dumps(
            {"Resources": {"Topic": {"Type": "AWS::SNS::Topic", "Properties": {}}}}
        ),
        Tags=[
            {
                "Key": "pocket:template_hash",
                "Value": _template_hash(pocket_rs, root, env=env),
            }
        ],
    )

    result = _part(pocket_rs, root, "container-update", "--container", "main", env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "function worker was not found and skipped." in result.stdout
    assert f"lambda function {wsgi} was updated." in result.stdout
    assert "Update stack" not in result.stdout
    assert "[wsgi] Lambda env DEPLOY_HASH を同期します (oldhash → newhash)" in (
        result.stderr
    )
    function = lambda_client.get_function(FunctionName=wsgi)
    assert function["Code"]["ImageUri"] == uri + ":prod"
    assert function["Configuration"]["Environment"]["Variables"] == {
        "DEPLOY_HASH": "newhash",
        "KEEP": "1",
    }

    # 揃っていれば環境変数は触らない
    result = _part(pocket_rs, root, "container-update", "--container", "main", env=env)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "DEPLOY_HASH を同期します" not in result.stderr


def test_api_roles_are_created_and_deleted(tmp_path, pocket_rs, moto_endpoint):
    project = "roleprj"
    toml = _toml(
        project,
        '[container.main]\ndockerfile_path = "Dockerfile"\nbuild = "codebuild"\n',
    )
    root = _project(tmp_path, toml)
    iam = _client("iam", moto_endpoint)

    result = _part(pocket_rs, root, "roles")
    assert result.returncode == 0, result.stderr
    names = [line.split(": ")[0] for line in result.stdout.splitlines()]
    assert len(names) == 1, result.stdout
    role = iam.get_role(RoleName=names[0])["Role"]
    statement = role["AssumeRolePolicyDocument"]["Statement"][0]
    assert statement["Principal"] == {"Service": "codebuild.amazonaws.com"}
    assert iam.list_role_policies(RoleName=names[0])["PolicyNames"]

    result = _part(pocket_rs, root, "roles", "--delete")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{names[0]}: deleted"
    with pytest.raises(iam.exceptions.NoSuchEntityException):
        iam.get_role(RoleName=names[0])
    result = _part(pocket_rs, root, "roles", "--delete")
    assert result.stdout.strip() == f"{names[0]}: not found"


@pytest.mark.parametrize("store", ["sm", "ssm"])
def test_container_delete_removes_stack_repository_and_secrets(
    tmp_path, pocket_rs, moto_endpoint, store
):
    project = f"delprj{store}"
    toml = _toml(
        project,
        '[container.main]\ndockerfile_path = "Dockerfile"\n\n'
        f'[container.main.secrets]\nstore = "{store}"\n\n'
        "[container.main.secrets.managed]\n"
        'SECRET_KEY = { type = "password" }\n',
    )
    root = _project(tmp_path, toml)
    name = f"prod-{project}-container-main"
    key = f"prod-{project}-main-pocket"
    repository = _context(pocket_rs, root)["container"]["main"]["ecr_name"]
    ecr = _client("ecr", moto_endpoint)
    ecr.create_repository(repositoryName=repository)
    _put_image(ecr, repository, "prod")
    result = _part(pocket_rs, root, "container-create", "--container", "main")
    assert result.returncode == 0, result.stderr + result.stdout
    first = _py_secrets(pocket_rs, root)[key]["SECRET_KEY"]

    for _ in range(2):  # 2 回目は消すものが無い
        result = _part(pocket_rs, root, "container-delete", "--container", "main")
        assert result.returncode == 0, result.stderr + result.stdout
        cfn = _client("cloudformation", moto_endpoint)
        live = [
            s
            for s in cfn.describe_stacks()["Stacks"]
            if s["StackName"] == name and s["StackStatus"] != "DELETE_COMPLETE"
        ]
        assert live == []
        with pytest.raises(ecr.exceptions.RepositoryNotFoundException):
            ecr.describe_repositories(repositoryNames=[repository])

    # 消した後に用意し直すと、新しい値が入る (SM は削除予約から復元される)
    result = _part(pocket_rs, root, "secrets")
    assert result.returncode == 0, result.stderr
    second = _py_secrets(pocket_rs, root)[key]["SECRET_KEY"]
    assert len(second) == 16
    assert second != first
