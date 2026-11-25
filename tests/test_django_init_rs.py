"""`pocket django init` が Python 版 `pocket django init` と同じファイルを書くか。

SECRET_KEY は乱数なので行ごと伏せて比べる (Rust 側は形だけ確かめる)。
django-environ の有無は、Python 版は pocket 自身の Python で、Rust 版は project の
Python (PATH の `uv`) に聞くので、Python 版は find_spec を、Rust 版は PATH の `uv` を
差し替えて揃える。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from pocket_cli import django_cli

FILES = ["pocket.toml", "pocket.Dockerfile", "myproj/settings.py", ".env"]
SECRET_KEY_LINE = re.compile(r"^SECRET_KEY=(.*)$", re.MULTILINE)
SECRET_KEY_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789!@#$%^&*(-_=+)"


def _project(root: Path, dotenv: str | None, existing: bool = False) -> Path:
    project = root / "myproj"
    (project / "myproj").mkdir(parents=True)
    if dotenv is not None:
        (project / ".env").write_text(dotenv)
    if existing:
        for name in FILES[:3]:
            (project / name).write_text("KEEP\n")
    return project


def _snapshot(project: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in FILES:
        path = project / name
        result[name] = path.read_bytes().decode() if path.exists() else None
    return result


def _run_python(project: Path, answers: str, environ: bool, monkeypatch) -> None:
    real_find_spec = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        if name == "environ":
            return object() if environ else None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(django_cli.importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(django_cli, "get_random_secret_key", lambda: "x" * 50)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(django_cli.django, ["init"], input=answers)
    assert result.exit_code == 0, result.output


def _run_rust(
    binary: Path, project: Path, answers: str, environ: bool, tmp_path: Path
) -> None:
    # Rust 版は PATH の uv で `uv run python -c …` を実行して environ の有無を聞く
    fake_bin = tmp_path / ("bin-yes" if environ else "bin-no")
    fake_bin.mkdir(exist_ok=True)
    uv = fake_bin / "uv"
    uv.write_text("#!/bin/sh\nexit %d\n" % (0 if environ else 1))
    uv.chmod(0o755)
    env = {**os.environ, "PATH": "%s:%s" % (fake_bin, os.environ["PATH"])}
    subprocess.run(  # noqa: S603 shell=False + 制御された引数
        [str(binary), "django", "init"],
        cwd=project,
        input=answers.encode(),
        env=env,
        check=True,
        capture_output=True,
    )


def _mask_secret_key(files: dict[str, str | None]) -> dict[str, str | None]:
    dotenv = files[".env"]
    if dotenv is None:
        return files
    return {**files, ".env": SECRET_KEY_LINE.sub("SECRET_KEY=<masked>", dotenv)}


@pytest.mark.parametrize(
    ("dotenv", "answers", "environ", "existing"),
    [
        # .env が無い: 内容を見ずに作る
        (None, "n\ny\n", True, False),
        # .env が無い: 内容を見て作らない
        (None, "y\nn\n", True, False),
        # .env がある: 上書きする (消す内容も表示する)
        ("OLD=1\n", "n\ny\ny\ny\n", True, False),
        # .env がある: 上書きしない
        ("OLD=1\n", "n\ny\nn\n", True, False),
        # django-environ が無い: settings.py と .env は書かない
        ("OLD=1\n", "", False, False),
        # 既存の pocket.toml は残す・pocket.Dockerfile は上書き・settings.py は残す
        (None, "n\ny\nn\nn\nn\n", True, True),
        # django-environ が無い: 既存の pocket.toml / pocket.Dockerfile を上書きする
        (None, "y\ny\n", False, True),
    ],
)
def test_init_writes_same_files_as_python(
    pocket_rs_bin, tmp_path, monkeypatch, dotenv, answers, environ, existing
):
    py_project = _project(tmp_path / "py", dotenv, existing)
    rs_project = _project(tmp_path / "rs", dotenv, existing)
    _run_python(py_project, answers, environ, monkeypatch)
    _run_rust(pocket_rs_bin, rs_project, answers, environ, tmp_path)

    py_files = _snapshot(py_project)
    rs_files = _snapshot(rs_project)
    assert rs_files["pocket.toml"] is not None
    assert _mask_secret_key(rs_files) == _mask_secret_key(py_files)
    if rs_files[".env"] is not None and rs_files[".env"] != dotenv:
        match = SECRET_KEY_LINE.search(rs_files[".env"])
        assert match is not None
        key = match.group(1)
        assert len(key) == 50
        assert set(key) <= set(SECRET_KEY_CHARS)
