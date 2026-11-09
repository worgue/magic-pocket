"""pocket.toml の設定モデルの共有 test vector を生成する (`just vectors`)。

Python の `Settings.from_toml` で各入力 (toml × その stages) を読み、
`model_dump(mode="json")` を `tests/data/settings_vectors/<group>/<名前>.<stage>.json`
に書く。Rust 側 (`crates/pocket-settings/tests/vectors.rs`) は同じ入力を読んで
この JSON と一致することを確かめる。

ファイル・環境に依らない読み込み (Rust の純粋層 `Settings::from_parts` と同じ入力)
にするため、読み込みは空の一時ディレクトリを cwd にし (`.env` を読まない)、
環境変数を vector の context だけに差し替えて行う。project_name の既定は一時
ディレクトリに置いた pyproject.toml から取らせる。

`--check` は書かずに、commit 済みの vector と一致するか (不足・余分を含む) を見る。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import click

from pocket.settings import Settings
from pocket.utils import mask_secret_values, tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "tests/data/settings_vectors"

# 入力の group 名と glob (repo root からの相対)
INPUT_GROUPS = (
    ("toml", "tests/data/toml/*.toml"),
    ("example", "example-*/pocket.toml"),
    ("toml_vectors", "tests/data/toml_vectors/*.toml"),
)

DEFAULT_CONTEXT: dict = {
    "project_name_fallback": "vector-prj",
    "env": {
        "NEON_API_KEY": "vector-neon-api-key",
        "TIDB_PUBLIC_KEY": "vector-tidb-public-key",
        "TIDB_PRIVATE_KEY": "vector-tidb-private-key",
        "UPSTASH_EMAIL": "vector@example.com",
        "UPSTASH_API_KEY": "vector-upstash-api-key",
    },
    "runtime_version": None,
}


def iter_inputs() -> Iterator[tuple[str, str, Path]]:
    """(group, 名前, toml の path)"""
    for group, pattern in INPUT_GROUPS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            name = path.parent.name if path.name == "pocket.toml" else path.stem
            yield group, name, path


@contextlib.contextmanager
def isolated_load(toml_path: Path, context: dict) -> Iterator[None]:
    """toml_path を pocket.toml として、context の入力だけで読める状態にする。"""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "pyproject.toml").write_text(
            '[project]\nname = "%s"\n' % context["project_name_fallback"],
            encoding="utf-8",
        )
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with (
                mock.patch.dict(os.environ, context["env"], clear=True),
                mock.patch("pocket.settings.get_toml_path", lambda: toml_path),
                mock.patch("pocket.general_settings.get_toml_path", lambda: toml_path),
            ):
                yield
        finally:
            os.chdir(cwd)


def build_vector(toml_path: Path, stage: str, context: dict) -> dict:
    with isolated_load(toml_path, context):
        settings = Settings.from_toml(stage=stage)
    dumped = settings.model_dump(mode="json")
    return {
        "input": toml_path.relative_to(REPO_ROOT).as_posix(),
        "stage": stage,
        "context": context,
        "settings": dumped,
        "masked": mask_secret_values(dumped),
    }


def render(vector: dict) -> str:
    return json.dumps(vector, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def expected_files() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for group, name, path in iter_inputs():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for stage in data["general"]["stages"]:
            vector = build_vector(path, stage, DEFAULT_CONTEXT)
            files[OUT_DIR / group / ("%s.%s.json" % (name, stage))] = render(vector)
    return files


def diff_files(expected: dict[Path, str]) -> list[str]:
    """commit 済みの vector との差 (相対 path と理由)"""
    problems = []
    existing = set(OUT_DIR.rglob("*.json"))
    for path, text in sorted(expected.items()):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file():
            problems.append("%s: ありません" % rel)
        elif path.read_text(encoding="utf-8") != text:
            problems.append("%s: 内容が違います" % rel)
    for path in sorted(existing - set(expected)):
        problems.append("%s: 入力がありません (余分)" % path.relative_to(REPO_ROOT))
    return problems


@click.command()
@click.option("--check", is_flag=True, help="書かずに commit 済みの vector と比べる")
def main(check: bool):
    expected = expected_files()
    if check:
        problems = diff_files(expected)
        if problems:
            raise click.ClickException(
                "settings vector が古いです (`just vectors` で再生成):\n  "
                + "\n  ".join(problems)
            )
        return
    for path in set(OUT_DIR.rglob("*.json")) - set(expected):
        path.unlink()
    for path, text in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    click.echo("%d 件の vector を書きました" % len(expected), err=True)


if __name__ == "__main__":
    main()
