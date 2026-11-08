import os
import shutil
import socket
import subprocess
from pathlib import Path

import boto3
import pytest
from moto.server import ThreadedMotoServer

from pocket import settings
from pocket.runtime import get_context
from pocket.utils import get_hosted_zone_id_from_domain, get_hosted_zones


@pytest.fixture
def use_toml(monkeypatch):
    """テスト用の pocket.toml パスを設定するフィクスチャ"""

    def _use(path: str):
        p = Path(path)
        monkeypatch.setattr("pocket.settings.get_toml_path", lambda: p)
        monkeypatch.setattr("pocket.general_settings.get_toml_path", lambda: p)
        monkeypatch.setattr("pocket.general_context.get_toml_path", lambda: p)

    return _use


@pytest.fixture
def base_settings():
    return settings.Settings.model_validate(
        {
            "stage": "test",
            "general": {
                "region": "ap-southeast-1",
                "project_name": "testprj",
                "stages": ["dev", "prod"],
            },
        }
    )


@pytest.fixture
def aws_settings():
    management_name = "pocket.django.lambda_handlers.management_command_handler"
    return settings.Container.model_validate(
        {
            "dockerfile_path": "Dockerfile",
            "handlers": {
                "wsgi": {"command": "pocket.django.lambda_handlers.wsgi_handler"},
                "management": {"command": management_name},
            },
        }
    )


@pytest.fixture(autouse=True)
def cleaned_cache():
    get_hosted_zones.cache_clear()
    get_hosted_zone_id_from_domain.cache_clear()
    get_context.cache_clear()
    return True


@pytest.fixture(autouse=True)
def aws_test_env(monkeypatch):
    """テストを AWS 環境変数に依存させない (hermetic 化)。

    製品コードには Lambda runtime が `AWS_REGION` を注入する前提で region 未指定の
    boto3 クライアントを作るパスがあり、AWS env の無い環境 (CI runner 等) では
    NoRegionError になる。region を固定し、あわせて dummy credentials を設定して
    moto 外で実 AWS に触れる事故も防ぐ。

    region はこのパスを通すテストが使う tests/data/toml/rds.toml の region に
    合わせる (Lambda runtime では AWS_REGION = デプロイ region になるため)。
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


# --- pocket-rs バイナリを moto server 相手に叩く e2e 用 (KN2215) ---
#
# Rust 版 CLI (`pocket-rs`) をコマンドとして実行し、S3 等は moto server
# (ThreadedMotoServer) で受ける。static の e2e (tests/test_static_e2e.py) のほか、
# 設定モデル・読み取り系の移植でも同じ fixture を使う。

REPO_ROOT = Path(__file__).resolve().parent.parent
MOTO_REGION = "ap-northeast-1"


@pytest.fixture(scope="session")
def pocket_rs_bin() -> Path:
    """`cargo build` した pocket-rs のパス。

    cargo が無い環境では skip するが、CI (`CI` 環境変数あり) では失敗にする
    (e2e 未実行で緑にしない)。
    """
    if shutil.which("cargo") is None:
        if os.environ.get("CI"):
            pytest.fail("CI に cargo がありません (pocket-rs の e2e を実行できません)")
        pytest.skip("cargo が無いため pocket-rs の e2e を skip します")
    subprocess.run(  # noqa: S603 shell=False + 制御された引数
        ["cargo", "build", "-p", "magic-pocket-cli-rs"],  # noqa: S607 cargo は PATH 前提
        cwd=REPO_ROOT,
        check=True,
        env={**os.environ, "TARGET_GC": "never"},
    )
    target_dir = Path(os.environ.get("CARGO_TARGET_DIR", REPO_ROOT / "target"))
    binary = target_dir / "debug" / "pocket-rs"
    assert binary.is_file(), binary
    return binary


@pytest.fixture(scope="session")
def moto_endpoint():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        yield "http://127.0.0.1:%d" % port
    finally:
        server.stop()


@pytest.fixture
def moto_s3(moto_endpoint):
    """moto server に繋いだ S3 client (テスト側の準備・観測用)。"""
    return boto3.client(
        "s3",
        region_name=MOTO_REGION,
        endpoint_url=moto_endpoint,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 moto 用のダミー
    )


class PocketRs:
    """pocket-rs を moto server 向けの環境変数で実行する。"""

    def __init__(self, binary: Path, endpoint: str):
        self.binary = binary
        self.env = {
            **os.environ,
            "AWS_ENDPOINT_URL": endpoint,
            "AWS_ACCESS_KEY_ID": "testing",
            "AWS_SECRET_ACCESS_KEY": "testing",
            "AWS_DEFAULT_REGION": MOTO_REGION,
            "COLUMNS": "500",
        }
        self.env.pop("AWS_SESSION_TOKEN", None)
        self.env.pop("POCKET_DEPLOY_STAGE", None)

    def run(
        self, cwd: Path, *args: str, stdin: str = "", env: dict | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 shell=False + 制御された引数
            [str(self.binary), *args],
            cwd=cwd,
            env={**self.env, **(env or {})},
            capture_output=True,
            text=True,
            input=stdin,
        )


@pytest.fixture
def pocket_rs(pocket_rs_bin, moto_endpoint) -> PocketRs:
    return PocketRs(pocket_rs_bin, moto_endpoint)
