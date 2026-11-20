"""`pocket permissions` の出力の共有 test vector を生成する (`just vectors`)。

settings の vector と同じ入力 (toml × その stages) について、Python 版の
`pocket permissions list` (text / json)・`role-names`・`roles` の stdout を
`tests/data/permissions_vectors/<group>/<名前>.<stage>.json` に書く。Rust 版
(`pocket-rs permissions`) は同じ入力から同じ文字列を出すことを確かめる
(`crates/pocket-cli/src/permissions.rs` のテスト)。

- `roles` は `--account-id` に固定の fake を渡す (sts を呼ばない)。Context の外部入力は
  settings の vector と同じ fake で、SM の managed secret は全 stage で「作成済み」に
  する (template の vector と同じ)。使った値を `external` に同梱する。Context を組めない
  入力は `roles` を null にする
- `groups` は付与した action group の名前 (Rust の判定がずれていないかを、出力が
  たまたま同じになる場合も含めて確かめる。網羅の検査は
  `tests/test_permissions_vectors.py`)

`--check` は書かずに、commit 済みの vector と一致するか (不足・余分を含む) を見る。
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import CliRunner
from generate_settings_vectors import (
    REPO_ROOT,
    _place_managed_assets,
    fake_external,
    input_context,
    isolated_load,
    iter_inputs,
)
from pocket_cli.cli.permissions_cli import permissions
from pydantic import ValidationError

from pocket.permissions import _enabled_groups
from pocket.settings import Settings
from pocket.utils import tomllib

OUT_DIR = REPO_ROOT / "tests/data/permissions_vectors"
FAKE_ACCOUNT = "123456789012"


def _stdout(args: list[str]) -> str:
    result = CliRunner().invoke(permissions, args, catch_exceptions=False)
    if result.exit_code != 0:
        raise click.ClickException(
            "pocket permissions %s が失敗しました:\n%s"
            % (" ".join(args), result.output)
        )
    return result.stdout


def _roles_stdout(stage: str) -> str | None:
    """`roles` の stdout。Context を組めない入力は None"""
    try:
        return _stdout(["roles", "--stage", stage, "--account-id", FAKE_ACCOUNT])
    except (ValueError, RuntimeError, ValidationError):
        return None


def build_vector(toml_path: Path, stage: str, context: dict) -> dict:
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    with isolated_load(toml_path, context):
        groups = _enabled_groups(Settings.from_toml(stage=stage))
        list_text = _stdout(["list", "--stage", stage])
        list_json = _stdout(["list", "--stage", stage, "--format", "json"])
        role_names = _stdout(["role-names", "--stage", stage])
        dir_files = _place_managed_assets(data)
        with fake_external(True) as external:
            roles = _roles_stdout(stage)
        external = {**external, "dir_files": dir_files}
    return {
        "input": toml_path.relative_to(REPO_ROOT).as_posix(),
        "stage": stage,
        "context": context,
        "external": external,
        "account_id": FAKE_ACCOUNT,
        "groups": groups,
        "list_text": list_text,
        "list_json": list_json,
        "role_names": role_names,
        "roles": roles,
    }


def render(vector: dict) -> str:
    return json.dumps(vector, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def expected_files() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for group, name, path in iter_inputs():
        text = path.read_text(encoding="utf-8")
        context = input_context(text)
        for stage in tomllib.loads(text)["general"]["stages"]:
            vector = build_vector(path, stage, context)
            files[OUT_DIR / group / ("%s.%s.json" % (name, stage))] = render(vector)
    return files


def existing_files() -> set[Path]:
    return set(OUT_DIR.rglob("*.json"))


def diff_files(expected: dict[Path, str]) -> list[str]:
    """commit 済みの vector との差 (相対 path と理由)"""
    problems = []
    for path, text in sorted(expected.items()):
        rel = path.relative_to(REPO_ROOT)
        if not path.is_file():
            problems.append("%s: ありません" % rel)
        elif path.read_text(encoding="utf-8") != text:
            problems.append("%s: 内容が違います" % rel)
    for path in sorted(existing_files() - set(expected)):
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
                "permissions vector が古いです (`just vectors` で再生成):\n  "
                + "\n  ".join(problems)
            )
        return
    for path in existing_files() - set(expected):
        path.unlink()
    for path, text in expected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    click.echo("%d 件の vector を書きました" % len(expected), err=True)


if __name__ == "__main__":
    main()
