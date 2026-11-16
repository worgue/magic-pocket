"""`pocket context` (Python 版) と `pocket-rs context` の出力が一致すること。

代表的な fixture を project dir (pocket.toml + pyproject.toml) に置き、両方の CLI を
moto server 向けの同じ環境で実行して stdout をバイト単位で比べる。Route53 の
hosted zone と、一部の managed secret (Secrets Manager) は moto に作っておく
(作っていない secret は「未作成」の経路を通る)。pocket-rs のバイナリは
tests/conftest.py の fixture が cargo build する。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import boto3
import pytest
from conftest import MOTO_REGION

REPO_ROOT = Path(__file__).resolve().parent.parent

# credential の環境変数 (親プロセスのものを持ち込まない)
_CREDENTIAL_ENV = (
    "NEON_API_KEY",
    "TIDB_PUBLIC_KEY",
    "TIDB_PRIVATE_KEY",
    "UPSTASH_EMAIL",
    "UPSTASH_API_KEY",
)
# Context が読む環境変数 (case ごとに明示する)
_CONTEXT_ENV = (
    "DEPLOY_HASH",
    "POCKET_PERMISSIONS_BOUNDARY_ARN",
    "FORGE_PERMISSIONS_BOUNDARY_ARN",
    "AWS_LAMBDA_FUNCTION_NAME",
)

HOSTED_ZONES = ("example.com", "sandbox.magic-pocket.example.com", "project.com")
# moto に作っておく managed secret (SecretId = pocket_key)。作らないものは未作成
EXISTING_SECRETS = ("dev-cf-pocket", "dev-ctx-web-pocket")

CASES = [
    ("tests/data/toml_vectors/cloudfront_full.toml", "dev", {}),
    ("tests/data/toml_vectors/cloudfront_full.toml", "prod", {}),
    (
        "tests/data/toml_vectors/context_full.toml",
        "dev",
        {
            "DEPLOY_HASH": "env-hash-1",
            "POCKET_PERMISSIONS_BOUNDARY_ARN": "arn:aws:iam::123456789012:policy/b",
        },
    ),
    ("tests/data/toml_vectors/context_full.toml", "live", {}),
    (
        "tests/data/toml_vectors/context_runtime.toml",
        "dev",
        {
            "AWS_LAMBDA_FUNCTION_NAME": "fn",
            "FORGE_PERMISSIONS_BOUNDARY_ARN": "arn:aws:iam::123456789012:policy/f",
        },
    ),
    ("tests/data/toml_vectors/misc.toml", "prod", {}),
    ("tests/data/toml_vectors/db_backup.toml", "prod", {}),
    ("tests/data/toml_vectors/provisioning.toml", "dev", {}),
    ("tests/data/toml_vectors/s3_vpc.toml", "shared", {}),
    ("tests/data/toml/scheduler.toml", "dev", {}),
    ("example-dsql/pocket.toml", "sandbox", {}),
]


@pytest.fixture(scope="module")
def moto_aws(moto_endpoint) -> str:
    """hosted zone と managed secret を moto に作る (何度呼ばれても同じ状態)"""
    kwargs = {
        "region_name": MOTO_REGION,
        "endpoint_url": moto_endpoint,
        "aws_access_key_id": "testing",
        "aws_secret_access_key": "testing",  # noqa: S106 moto 用のダミー
    }
    route53 = boto3.client("route53", **kwargs)
    existing = {z["Name"] for z in route53.list_hosted_zones()["HostedZones"]}
    for name in HOSTED_ZONES:
        if name + "." not in existing:
            route53.create_hosted_zone(Name=name, CallerReference=name)
    sm = boto3.client("secretsmanager", **kwargs)
    for key in EXISTING_SECRETS:
        try:
            sm.describe_secret(SecretId=key)
        except sm.exceptions.ResourceNotFoundException:
            sm.create_secret(Name=key, SecretString="{}")
    return moto_endpoint


def _project(tmp_path: Path, toml: str) -> Path:
    root = tmp_path / "prj"
    root.mkdir()
    shutil.copy(REPO_ROOT / toml, root / "pocket.toml")
    (root / "pyproject.toml").write_text('[project]\nname = "parity-prj"\n')
    return root


def _env(pocket_rs, extra: dict[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in pocket_rs.env.items()
        if k not in _CREDENTIAL_ENV and k not in _CONTEXT_ENV
    }
    env["UPSTASH_API_KEY"] = "from-env"
    return {**env, **extra}


@pytest.mark.parametrize(("toml", "stage", "extra_env"), CASES)
def test_context_output_matches_python(
    tmp_path, pocket_rs, moto_aws, toml, stage, extra_env
):
    root = _project(tmp_path, toml)
    env = _env(pocket_rs, extra_env)
    py = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [
            sys.executable,
            "-c",
            "from pocket_cli.cli.main_cli import main; main()",
            "context",
            "--stage",
            stage,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    rs = subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs.binary), "context", "--stage", stage],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert py.returncode == 0, py.stderr
    assert rs.returncode == 0, rs.stderr
    assert rs.stdout == py.stdout


def test_version_matches_python(pocket_rs_bin):
    py = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", "from pocket_cli.cli.main_cli import main; main()"]
        + ["version"],
        capture_output=True,
        text=True,
    )
    rs = subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs_bin), "version"], capture_output=True, text=True
    )
    assert py.returncode == 0, py.stderr
    assert rs.returncode == 0, rs.stderr
    assert rs.stdout == py.stdout
