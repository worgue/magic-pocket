"""`pocket deploy` / `destroy` を moto server 相手に確かめる。

moto は CloudFront・ACM・WAF・SES の stack や Lambda の image を扱えないので、ここでは
S3 だけの最小の stage で、パイプライン全体 (state bucket の確保 → 状態の判定 → 作成 →
state の記録 → 削除) と、Python 版との相互運用 (同じ state・同じ bucket) を見る。
リソースごとの書き込みは部品の e2e (`test_*_write_e2e.py`)、手順の順と分岐は Rust の
テスト (fake) が持つ。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import boto3
import pytest

pytestmark = pytest.mark.usefixtures("moto_versioning_root")

REGION = "ap-southeast-1"

_PY_STATE = """
import json, sys
from pocket.context import Context
from pocket_cli.resources.aws.state import create_state_store
ctx = Context.from_toml(stage=sys.argv[1])
store = create_state_store(ctx)
if len(sys.argv) > 2:
    store.record(json.loads(sys.argv[2]))
print(json.dumps(store.load()))
"""

_PY_DEPLOY = """
import sys
from pocket_cli.cli.deploy_cli import run_deploy
run_deploy(sys.argv[1], yes=True)
"""


def _client(service: str, endpoint: str):
    return boto3.client(
        service,
        region_name=REGION,
        endpoint_url=endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 moto 用のダミー
    )


def _project(tmp_path: Path, project: str, body: str = "[s3]\n") -> Path:
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
    return {**pocket_rs.env, "AWS_DEFAULT_REGION": REGION}


def _rs(pocket_rs, root: Path, command: str, *args: str):
    return subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs.binary), command, "--stage", "prod", *args],
        cwd=root,
        env=_env(pocket_rs),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _ok(pocket_rs, root: Path, command: str, *args: str):
    result = _rs(pocket_rs, root, command, *args)
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
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else ""


def _buckets(endpoint: str, prefix: str) -> list[str]:
    names = [b["Name"] for b in _client("s3", endpoint).list_buckets()["Buckets"]]
    return sorted(name for name in names if name.startswith(prefix))


def _state(endpoint: str, bucket: str) -> dict:
    body = _client("s3", endpoint).get_object(Bucket=bucket, Key="resources.json")
    return json.loads(body["Body"].read())


def test_deploy_destroy_redeploy(pocket_rs, moto_endpoint, tmp_path):
    root = _project(tmp_path, "dplcycle")
    bucket, state_bucket = "prod-dplcycle-pocket", "prod-dplcycle-pocket-state"

    result = _ok(pocket_rs, root, "deploy")
    assert "Creating S3..." in result.stderr
    assert _buckets(moto_endpoint, "prod-dplcycle") == [bucket, state_bucket]
    expected = {"version": 1, "resources": {"s3": {"bucket_name": bucket}}}
    assert _state(moto_endpoint, state_bucket) == expected
    assert "S3 status: COMPLETED" in _ok(pocket_rs, root, "status").stderr

    # 2 回目は何も書かない
    result = _ok(pocket_rs, root, "deploy")
    assert "S3 is already the latest version." in result.stderr
    assert "Creating" not in result.stderr

    # 確認に答えられない (stdin が閉じている) ときは消さない
    result = _rs(pocket_rs, root, "destroy")
    assert result.returncode != 0
    assert f"  - S3 バケット: {bucket}" in result.stderr
    assert "Aborted!" in result.stderr
    assert bucket in _buckets(moto_endpoint, "prod-dplcycle")

    result = _ok(pocket_rs, root, "destroy", "-y")
    assert "stage 'prod' の全リソースを削除しました。" in result.stderr
    # state bucket は --with-state-bucket を付けたときだけ消す
    assert _buckets(moto_endpoint, "prod-dplcycle") == [state_bucket]
    result = _ok(pocket_rs, root, "destroy", "-y")
    assert "削除対象のリソースが見つかりません。" in result.stderr

    # 消した後にもう一度 deploy できる
    assert "Creating S3..." in _ok(pocket_rs, root, "deploy").stderr
    assert _buckets(moto_endpoint, "prod-dplcycle") == [bucket, state_bucket]
    result = _ok(pocket_rs, root, "destroy", "-y", "--with-state-bucket")
    assert "  - ステートバケット" in result.stderr
    assert _buckets(moto_endpoint, "prod-dplcycle") == []


def test_state_is_shared_with_python(pocket_rs, moto_endpoint, tmp_path):
    root = _project(tmp_path, "dplstate")
    state_bucket = "prod-dplstate-pocket-state"

    # Python 版が deploy した stage を Rust が引き継ぐ: 何も作り直さない
    _py(pocket_rs, root, _PY_DEPLOY)
    by_python = _state(moto_endpoint, state_bucket)
    result = _ok(pocket_rs, root, "deploy")
    assert "S3 is already the latest version." in result.stderr
    assert _state(moto_endpoint, state_bucket) == by_python

    # Python 版が書いた別の記録を、Rust の記録が消さない (種別ごとに merge する)
    other = {"upstash": {"database_name": "kept"}}
    _py(pocket_rs, root, _PY_STATE, json.dumps(other))
    _ok(pocket_rs, root, "destroy", "-y")
    _ok(pocket_rs, root, "deploy")
    merged = json.loads(_py(pocket_rs, root, _PY_STATE))
    assert merged["resources"]["upstash"] == {"database_name": "kept"}
    assert merged["resources"]["s3"] == {"bucket_name": "prod-dplstate-pocket"}
    # Rust が書いた state のテキストは Python 版の書き方 (indent=2) と同じ
    raw = _client("s3", moto_endpoint).get_object(
        Bucket=state_bucket, Key="resources.json"
    )
    assert raw["Body"].read().decode() == json.dumps(merged, indent=2)


def test_broken_state_stops_deploy(pocket_rs, moto_endpoint, tmp_path):
    root = _project(tmp_path, "dplbroken")
    state_bucket = "prod-dplbroken-pocket-state"
    _ok(pocket_rs, root, "deploy")
    s3 = _client("s3", moto_endpoint)
    s3.put_object(Bucket=state_bucket, Key="resources.json", Body=b"{broken")
    # 壊れた state を空として扱わない (残存リソースの検出が効かなくなる)
    result = _rs(pocket_rs, root, "deploy")
    assert result.returncode != 0
    assert "resources.json が JSON ではありません" in result.stderr
    assert s3.get_object(Bucket=state_bucket, Key="resources.json")["Body"].read() == (
        b"{broken"
    )


def test_promote_requires_container(pocket_rs, moto_endpoint, tmp_path):
    root = _project(tmp_path, "dplpromote")
    result = _rs(pocket_rs, root, "promote", "--commit-hash", "abc1234")
    assert result.returncode != 0
    assert "container がこの stage に設定されていません。" in result.stderr
    assert _buckets(moto_endpoint, "prod-dplpromote") == []
