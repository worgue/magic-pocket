"""`pocket settings` (Python 版) と `pocket-rs settings` の出力が一致すること。

代表的な fixture を project dir (pocket.toml + pyproject.toml、必要なら .env) に
置き、両方の CLI を実行して stdout をバイト単位で比べる。pocket-rs のバイナリは
tests/conftest.py の fixture が cargo build する。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# credential の環境変数 (親プロセスのものを持ち込まない)
_CREDENTIAL_ENV = (
    "NEON_API_KEY",
    "TIDB_PUBLIC_KEY",
    "TIDB_PRIVATE_KEY",
    "UPSTASH_EMAIL",
    "UPSTASH_API_KEY",
)

CASES = [
    ("tests/data/toml/default.toml", "dev", None),
    ("tests/data/toml/default.toml", "prod", None),
    ("tests/data/toml_vectors/general_full.toml", "prod", None),
    ("tests/data/toml_vectors/general_minimal.toml", "dev", None),
    ("tests/data/toml_vectors/s3_vpc.toml", "shared", None),
    ("tests/data/toml_vectors/container_full.toml", "prod", None),
    ("tests/data/toml_vectors/cloudfront_full.toml", "dev", None),
    ("tests/data/toml_vectors/db_backup.toml", "prod", None),
    ("tests/data/toml_vectors/misc.toml", "dev", None),
    # credential は .env から (環境変数より弱い)
    (
        "tests/data/toml_vectors/provisioning.toml",
        "dev",
        "NEON_API_KEY=from-dotenv\nUPSTASH_EMAIL='dot@example.com'\n",
    ),
    ("example-dsql/pocket.toml", "sandbox", None),
]


def _project(tmp_path: Path, toml: str, dotenv: str | None) -> Path:
    root = tmp_path / "prj"
    root.mkdir()
    shutil.copy(REPO_ROOT / toml, root / "pocket.toml")
    (root / "pyproject.toml").write_text('[project]\nname = "parity-prj"\n')
    if dotenv is not None:
        (root / ".env").write_text(dotenv)
    return root


def _env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _CREDENTIAL_ENV and k != "POCKET_DEPLOY_STAGE"
    }
    env["UPSTASH_API_KEY"] = "from-env"
    return env


@pytest.mark.parametrize(("toml", "stage", "dotenv"), CASES)
def test_settings_output_matches_python(tmp_path, pocket_rs_bin, toml, stage, dotenv):
    root = _project(tmp_path, toml, dotenv)
    env = _env()
    py = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [
            sys.executable,
            "-c",
            "from pocket_cli.cli.main_cli import main; main()",
            "settings",
            "--stage",
            stage,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    rs = subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(pocket_rs_bin), "settings", "--stage", stage],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert py.returncode == 0, py.stderr
    assert rs.returncode == 0, rs.stderr
    assert rs.stdout == py.stdout
