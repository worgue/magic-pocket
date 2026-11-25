"""vpc / efs / rds / backup の書き込み (pocket) を moto server 相手に確かめる。

部品の入口は非表示の `pocket deploy-parts` (通しの deploy は moto で作れない
stack で止まるため、部品ごとに通す)。
Rust が書いたものを Python 版の実装で読み、Python 版が書いたものを Rust が壊さないことを
見る。

moto で通せない範囲:

- DSQL: moto に cluster の一覧が無い (cluster を Name タグで探せない)
- backup の selection: moto に API が無い。vault・role・plan の作成までを確かめ、
  selection の手前で止まることを期待値にする
- EFS 付きの VPC の stack: moto が作れない (EFS の無い VPC は作れる)
- RDS は security group (EC2) を使う。開発用 VM のバイナリは EC2 抜きなので、RDS の
  テストは CI でだけ動く
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

REGION = "ap-southeast-1"

# 開発用 VM (FORGE=1) の pocket は EC2 の API 抜きでビルドされる
# (conftest と同じ条件)
needs_ec2 = pytest.mark.skipif(
    os.environ.get("FORGE") == "1" and not os.environ.get("CI"),
    reason="EC2 抜きでビルドした pocket では RDS の security group を扱えない",
)

_PY_EFS = """
import sys
from pocket.context import Context
from pocket_cli.resources.aws.efs import Efs
ctx = Context.from_toml(stage=sys.argv[1])
context = ctx.container["main"].vpc.efs
if sys.argv[2] == "create":
    Efs(context).ensure_exists()
print(Efs(context).status)
"""

_PY_RDS = """
import json, sys
from pocket.context import Context
from pocket_cli.resources.rds import Rds
from pocket_cli.resources.vpc import Vpc
ctx = Context.from_toml(stage=sys.argv[1])
rds = Rds(ctx.rds)
if sys.argv[2] == "create":
    Vpc(ctx.rds.vpc).create()
    rds.create()
    rds.clear_cache()
elif sys.argv[2] == "credentials":
    print(rds._read_credential_from_store(ctx.rds.secret_store))
    sys.exit(0)
print(rds.status)
"""

_PY_BACKUP = """
import json, sys
import boto3
from pocket.context import Context
from pocket_cli.resources.backup import Backup, _plan_matches
ctx = Context.from_toml(stage=sys.argv[1])
backup = Backup(ctx.backup, dsql_context=ctx.dsql, rds_context=ctx.rds)
result = {}
for plan in ctx.backup.plans:
    plan_id = backup._find_plan_id(plan.plan_name)
    current = backup._backup.get_backup_plan(BackupPlanId=plan_id)["BackupPlan"]
    result[plan.plan_name] = _plan_matches(current, backup._plan_document(plan))
print(json.dumps(result))
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


def _env(pocket_rs) -> dict[str, str]:
    drop = ("DEPLOY_HASH", "POCKET_PERMISSIONS_BOUNDARY_ARN")
    drop += ("FORGE_PERMISSIONS_BOUNDARY_ARN", "CODEBUILD_PERMISSIONS_BOUNDARY")
    return {k: v for k, v in pocket_rs.env.items() if k not in drop}


def _part(pocket_rs, root: Path, part: str):
    return subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs.binary), "deploy-parts", part, "--stage", "prod"],
        cwd=root,
        env=_env(pocket_rs),
        capture_output=True,
        text=True,
    )


def _ok(pocket_rs, root: Path, part: str):
    result = _part(pocket_rs, root, part)
    assert result.returncode == 0, result.stderr + result.stdout
    return result


def _py(pocket_rs, root: Path, script: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", script, "prod", *args],
        cwd=root,
        env=_env(pocket_rs),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


# ---- EFS ----


def _efs_toml(project: str) -> str:
    return _toml(
        project,
        f'[vpc]\nref = "{project}"\nzone_suffixes = ["a"]\nefs = {{}}\n\n'
        '[container.main]\ndockerfile_path = "Dockerfile"\n',
    )


def _file_systems(efs) -> list[dict]:
    return efs.describe_file_systems()["FileSystems"]


def test_efs_created_by_rust_is_completed_for_python(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "efsrs"
    root = _project(tmp_path, _efs_toml(project))
    efs = _client("efs", moto_endpoint)

    result = _ok(pocket_rs, root, "vpc-init")
    assert "Created EFS: fs-" in result.stdout
    assert _py(pocket_rs, root, _PY_EFS, "status") == "COMPLETED"
    created = [fs for fs in _file_systems(efs) if fs["Name"].startswith(project)]
    assert len(created) == 1
    assert created[0]["Encrypted"] is True
    policies = efs.describe_lifecycle_configuration(
        FileSystemId=created[0]["FileSystemId"]
    )["LifecyclePolicies"]
    assert policies == [
        {"TransitionToIA": "AFTER_30_DAYS"},
        {"TransitionToPrimaryStorageClass": "AFTER_1_ACCESS"},
    ]

    # 2 回目は作らない
    result = _ok(pocket_rs, root, "vpc-init")
    assert "Created EFS" not in result.stdout
    assert len([fs for fs in _file_systems(efs) if fs["Name"].startswith(project)]) == 1


def test_efs_created_by_python_is_left_alone_by_rust(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "efspy"
    root = _project(tmp_path, _efs_toml(project))
    efs = _client("efs", moto_endpoint)

    assert _py(pocket_rs, root, _PY_EFS, "create") == "COMPLETED"
    before = [fs for fs in _file_systems(efs) if fs["Name"].startswith(project)]
    result = _ok(pocket_rs, root, "vpc-init")
    assert "Created EFS" not in result.stdout
    after = [fs for fs in _file_systems(efs) if fs["Name"].startswith(project)]
    assert [fs["FileSystemId"] for fs in after] == [fs["FileSystemId"] for fs in before]


# ---- backup ----

_BACKUP = """
[vpc]
ref = "{project}"
zone_suffixes = ["a", "c"]

[container.main]
dockerfile_path = "Dockerfile"

[rds]

[backup]
timezone = "Asia/Tokyo"

[backup.rds]
weekly = {{ cron = "0 2 ? * 7 *", delete_after_days = 180 }}
monthly = {{ cron = "0 2 1 * ? *", delete_after_days = 730 }}
"""


def _place_cluster(moto_endpoint, project: str):
    """backup の対象になる cluster を moto に置く (名前は pocket の命名)"""
    rds = _client("rds", moto_endpoint)
    rds.create_db_cluster(
        DBClusterIdentifier=f"prod-{project}-pocket-aurora",
        Engine="aurora-postgresql",
        MasterUsername="postgres",
        ManageMasterUserPassword=True,
    )


def test_backup_vault_role_and_plan_are_created(tmp_path, pocket_rs, moto_endpoint):
    project = "bkrs"
    root = _project(tmp_path, _toml(project, _BACKUP.format(project=project)))
    backup = _client("backup", moto_endpoint)

    # 対象の cluster が無いうちは何も作らない
    _ok(pocket_rs, root, "backup-post")
    assert backup.list_backup_plans()["BackupPlansList"] == []

    _place_cluster(moto_endpoint, project)
    # moto は AWS 管理 policy (AWSBackupServiceRolePolicyFor...) を持たないので、role は
    # 先に置く (在る role はそのまま使われる)
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "backup.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    _client("iam", moto_endpoint).create_role(
        RoleName="prod-bkrs-pocket-backup-role",
        AssumeRolePolicyDocument=json.dumps(trust),
    )
    # moto に selection の API が無いので、plan を作った後の selection の確認で止まる
    result = _part(pocket_rs, root, "backup-post")
    assert "Created backup vault: prod-bkrs-pocket-backup" in result.stderr
    assert "Creating backup plan: prod-bkrs-pocket-backup-rds" in result.stderr
    assert result.returncode != 0
    assert "list_backup_selections" in result.stderr

    vaults = [
        v["BackupVaultName"] for v in backup.list_backup_vaults()["BackupVaultList"]
    ]
    assert "prod-bkrs-pocket-backup" in vaults
    # Rust が作った plan を、Python 版は宣言どおりと判定する
    matches = json.loads(_py(pocket_rs, root, _PY_BACKUP))
    assert matches == {"prod-bkrs-pocket-backup-rds": True}

    # plan が在る状態でもう一度: vault も plan も作り直さない
    result = _part(pocket_rs, root, "backup-post")
    assert "Created backup vault" not in result.stderr
    assert "Creating backup plan" not in result.stderr
    assert "Updating backup plan" not in result.stderr
    assert len(backup.list_backup_plans()["BackupPlansList"]) == 1


# ---- RDS ----

_RDS = """
[vpc]
ref = "{project}"
zone_suffixes = ["a", "c"]

[container.main]
dockerfile_path = "Dockerfile"

[rds]
{rds}
"""


def _cluster(moto_endpoint, project: str) -> dict | None:
    rds = _client("rds", moto_endpoint)
    try:
        found = rds.describe_db_clusters(
            DBClusterIdentifier=f"prod-{project}-pocket-aurora"
        )
    except rds.exceptions.DBClusterNotFoundFault:
        return None
    return found["DBClusters"][0]


@needs_ec2
def test_rds_created_by_rust_is_completed_for_python(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "rdsrs"
    toml = _RDS.format(project=project, rds="min_capacity = 1\nmax_capacity = 4")
    root = _project(tmp_path, _toml(project, toml))

    _ok(pocket_rs, root, "rds-init")
    _ok(pocket_rs, root, "vpc-create")
    result = _ok(pocket_rs, root, "rds-create")
    assert "Aurora cluster is now available." in result.stderr
    assert _py(pocket_rs, root, _PY_RDS, "status") == "COMPLETED"
    cluster = _cluster(moto_endpoint, project)
    assert cluster is not None
    assert cluster["ServerlessV2ScalingConfiguration"] == {
        "MinCapacity": 1.0,
        "MaxCapacity": 4.0,
    }
    assert cluster["DatabaseName"]
    assert "MasterUserSecret" in cluster

    # やり直しても作り直さない
    result = _ok(pocket_rs, root, "rds-create")
    assert "already exists; skipping creation." in result.stderr

    # scaling と保持日数の変更は 1 回の modify で反映する
    changed = _RDS.format(
        project=project,
        rds="min_capacity = 1\nmax_capacity = 8\nbackup = { retention_days = 14 }",
    )
    (root / "pocket.toml").write_text(_toml(project, changed))
    assert _py(pocket_rs, root, _PY_RDS, "status") == "REQUIRE_UPDATE"
    result = _ok(pocket_rs, root, "rds-update")
    assert result.stderr.count("RDS cluster configuration updated.") == 1
    assert _py(pocket_rs, root, _PY_RDS, "status") == "COMPLETED"
    result = _ok(pocket_rs, root, "rds-update")
    assert "Updating RDS cluster configuration" not in result.stderr

    _ok(pocket_rs, root, "rds-delete")
    assert _cluster(moto_endpoint, project) is None
    ec2 = _client("ec2", moto_endpoint)
    groups = ec2.describe_security_groups(
        Filters=[{"Name": "tag:Name", "Values": [f"prod-{project}-pocket-aurora-rds"]}]
    )
    assert groups["SecurityGroups"] == []


@needs_ec2
@pytest.mark.parametrize("store", ["sm", "ssm"])
def test_rds_static_credentials_are_readable_by_python(
    tmp_path, pocket_rs, moto_endpoint, store
):
    project = f"rdsst{store}"
    body = _RDS.format(project=project, rds='password_strategy = "static"')
    body += f'\n[container.main.secrets]\nstore = "{store}"\n'
    root = _project(tmp_path, _toml(project, body))

    _ok(pocket_rs, root, "vpc-create")
    _ok(pocket_rs, root, "rds-create")
    assert _py(pocket_rs, root, _PY_RDS, "status") == "COMPLETED"
    credentials = json.loads(_py(pocket_rs, root, _PY_RDS, "credentials"))
    assert credentials["username"] == "postgres"
    assert len(credentials["password"]) == 32
    assert credentials["engine"] == "postgres"
    assert credentials["dbClusterIdentifier"] == f"prod-{project}-pocket-aurora"
    cluster = _cluster(moto_endpoint, project)
    assert cluster is not None
    assert credentials["host"] == cluster["Endpoint"]
    assert "MasterUserSecret" not in cluster

    # 削除で認証情報も消える
    _ok(pocket_rs, root, "rds-delete")
    assert _py(pocket_rs, root, _PY_RDS, "credentials") == "None"


@needs_ec2
def test_rds_created_by_python_is_left_alone_by_rust(
    tmp_path, pocket_rs, moto_endpoint
):
    project = "rdspy"
    root = _project(tmp_path, _toml(project, _RDS.format(project=project, rds="")))

    assert _py(pocket_rs, root, _PY_RDS, "create") == "COMPLETED"
    before = _cluster(moto_endpoint, project)
    result = _ok(pocket_rs, root, "rds-update")
    assert "Updating RDS cluster configuration" not in result.stderr
    assert "Migrating master password" not in result.stderr
    after = _cluster(moto_endpoint, project)
    assert before is not None and after is not None
    for key in ("ServerlessV2ScalingConfiguration", "BackupRetentionPeriod", "Status"):
        assert after[key] == before[key]
