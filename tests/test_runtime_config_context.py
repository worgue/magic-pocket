"""`pocket runtime-config` の生成物から runtime の Context が組めること。

runtime (Lambda の INIT) は pocket.runtime.toml を Settings で読み、さらに Context を
組む。設定 vector (tests/test_settings_vectors.py) は Settings までしか見ないので、
EFS の mount 先や RDS の vpc のように Context で初めて使う key を runtime-config が
消すと、vector では気づけないまま INIT で落ちる。元の pocket.toml で Context が
組める入力は、runtime.toml からも組めることをここで確かめる。
"""

import os
import re
from pathlib import Path
from unittest import mock

import pytest
from pocket_cli.cli.runtime_config_cli import _clean_data, _to_toml

from pocket.context import Context
from pocket.utils import tomllib

REPO_ROOT = Path(__file__).resolve().parents[1]
INPUT_PATTERNS = (
    "tests/data/toml/*.toml",
    "example-*/pocket.toml",
    "tests/data/toml_vectors/*.toml",
)

# credential を環境変数から読む section 用 (値は何でもよい)
DEFAULT_ENV = {
    "NEON_API_KEY": "test-neon-api-key",
    "TIDB_PUBLIC_KEY": "test-tidb-public-key",
    "TIDB_PRIVATE_KEY": "test-tidb-private-key",
    "UPSTASH_EMAIL": "test@example.com",
    "UPSTASH_API_KEY": "test-upstash-api-key",
}


def _input_env(text: str) -> dict[str, str]:
    """入力の `# vector-env: KEY=VALUE` 行 (settings vector と同じ規約) の環境変数"""
    lines = re.findall(r"^# vector-env:(.*)$", text, re.MULTILINE)
    if not lines:
        return DEFAULT_ENV
    return dict(line.strip().split("=", 1) for line in lines if line.strip())


def _cases() -> list:
    cases = []
    for pattern in INPUT_PATTERNS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            stages = tomllib.loads(path.read_text(encoding="utf-8"))["general"][
                "stages"
            ]
            rel = path.relative_to(REPO_ROOT).as_posix()
            cases.extend(pytest.param(path, s, id="%s-%s" % (rel, s)) for s in stages)
    return cases


def _build_context(toml_path: Path, stage: str, env: dict, use_toml) -> Context:
    use_toml(str(toml_path))
    with mock.patch.dict(os.environ, env, clear=True):
        return Context.from_toml(stage=stage)


@pytest.mark.parametrize(("path", "stage"), _cases())
def test_runtime_toml_builds_context(path, stage, use_toml, tmp_path, monkeypatch):
    text = path.read_text(encoding="utf-8")
    env = _input_env(text)
    # project_name の既定は cwd の pyproject.toml から取る
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test-prj"\n')
    monkeypatch.chdir(tmp_path)
    try:
        _build_context(path, stage, env, use_toml)
    except (ValueError, RuntimeError):
        # 元の pocket.toml でも Context が組めない入力 (Settings 用の fixture) は対象外
        pytest.skip("元の pocket.toml で Context が組めない")
    runtime_path = tmp_path / "pocket.runtime.toml"
    runtime_path.write_text(_to_toml(_clean_data(tomllib.loads(text))).strip() + "\n")
    _build_context(runtime_path, stage, env, use_toml)
