"""pocket.toml の設定モデルの共有 test vector を生成する (`just vectors`)。

Python の `Settings.from_toml` で各入力 (toml × その stages) を読み、
`model_dump(mode="json")` を `tests/data/settings_vectors/<group>/<名前>.<stage>.json`
に書く。Rust 側 (`crates/pocket-settings/tests/vectors.rs`) は同じ入力を読んで
この JSON と一致することを確かめる。

入力の先頭に `# vector-env: KEY=VALUE` の行 (`# vector-env:` だけなら環境変数なし)
があれば、その入力の vector の環境変数をその行のものだけにする (credential の
空文字・未設定・大文字小文字の違いを固定するため)。

ファイル・環境に依らない読み込み (Rust の純粋層 `Settings::from_parts` と同じ入力)
にするため、読み込みは空の一時ディレクトリを cwd にし (`.env` を読まない)、
環境変数を vector の context だけに差し替えて行う。project_name の既定は一時
ディレクトリに置いた pyproject.toml から取らせる。

各入力から `pocket runtime-config` と同じ規則で作った pocket.runtime.toml
(`runtime/` 配下。生成元の版のマーカーは付けない) も入力にする (Lambda の runtime
が読む形を固定するため)。Python が読めない runtime.toml は vector にせず、理由を
stderr に出す。

エラーの vector: `tests/data/toml_errors/<名前>.toml` (1 ファイル 1 欠陥。stage は
先頭の `# stage: <名前>` コメント、無ければ `[general] stages` の先頭) を Python で
読んだときのエラーを `<名前>.expected.json` に書く。エラーがちょうど 1 件でない
fixture は不正として止める。kind は Missing / Extra / Type / Value (独自の検証と
前処理) / Syntax。path から pydantic の union のタグは除く。

`--check` は書かずに、commit 済みの vector と一致するか (不足・余分を含む) を見る。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import click
from pocket_cli.cli.runtime_config_cli import _clean_data, _to_toml
from pydantic import ValidationError

from pocket import __version__ as runtime_version
from pocket.settings import Settings
from pocket.utils import mask_secret_values, tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "tests/data/settings_vectors"
RUNTIME_DIR = OUT_DIR / "runtime"
ERRORS_DIR = REPO_ROOT / "tests/data/toml_errors"

# discriminated union のタグ (pydantic のエラーの loc に入る)
_UNION_TAGS = frozenset(
    {
        "pocket.lambda_scheduler",
        "pocket.django.management_lambda_scheduler",
        "pocket.sqs_scheduler",
    }
)

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


def input_context(text: str) -> dict:
    """入力の `# vector-env:` 行から vector の context を決める"""
    lines = re.findall(r"^# vector-env:(.*)$", text, re.MULTILINE)
    if not lines:
        return DEFAULT_CONTEXT
    env = {}
    for line in lines:
        if line.strip():
            key, value = line.strip().split("=", 1)
            env[key] = value
    return {**DEFAULT_CONTEXT, "env": env}


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


def runtime_toml_text(toml_path: Path) -> str:
    """`pocket runtime-config` と同じ規則の pocket.runtime.toml (版のマーカー無し)"""
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    return _to_toml(_clean_data(data)).strip() + "\n"


def build_vector(
    toml_path: Path, stage: str, context: dict, input_path: Path | None = None
) -> dict:
    """toml_path を読んだ vector。

    input_path は vector に書く入力の path (既定は toml_path)。
    """
    with isolated_load(toml_path, context):
        settings = Settings.from_toml(stage=stage)
    dumped = settings.model_dump(mode="json")
    return {
        "input": (input_path or toml_path).relative_to(REPO_ROOT).as_posix(),
        "stage": stage,
        "context": context,
        "settings": dumped,
        "masked": mask_secret_values(dumped),
    }


def render(vector: dict) -> str:
    return json.dumps(vector, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def expected_files() -> tuple[dict[Path, str], list[str]]:
    """(書くファイル, vector にしなかった runtime.toml とその理由)"""
    files: dict[Path, str] = {}
    skipped: list[str] = []
    for group, name, path in iter_inputs():
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        context = input_context(text)
        stages = data["general"]["stages"]
        for stage in stages:
            vector = build_vector(path, stage, context)
            files[OUT_DIR / group / ("%s.%s.json" % (name, stage))] = render(vector)
        runtime_path = RUNTIME_DIR / ("%s.%s.runtime.toml" % (group, name))
        runtime_text = runtime_toml_text(path)
        runtime_files: dict[Path, str] = {}
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / runtime_path.name
            tmp_path.write_text(runtime_text, encoding="utf-8")
            current = ""
            try:
                for stage in stages:
                    current = stage
                    vector = build_vector(
                        tmp_path, stage, context, input_path=runtime_path
                    )
                    out = RUNTIME_DIR / ("%s.%s.%s.json" % (group, name, stage))
                    runtime_files[out] = render(vector)
            except ValueError as e:
                # ValidationError / TOMLDecodeError も ValueError
                reason = " ".join(str(e).split())[:200]
                skipped.append("%s (%s): %s" % (runtime_path.name, current, reason))
                continue
        files[runtime_path] = runtime_text
        files.update(runtime_files)
    return files, skipped


def _error_kind(error_type: str) -> str:
    if error_type == "missing":
        return "Missing"
    if error_type == "extra_forbidden":
        return "Extra"
    if error_type == "value_error":
        return "Value"
    return "Type"


def _error_path(loc: tuple) -> list:
    """pydantic の loc から union のタグ (`function-after[...]` 等) を除く"""
    return [
        seg
        for seg in loc
        if not (
            isinstance(seg, str)
            and (seg in _UNION_TAGS or ("[" in seg and seg != "[key]"))
        )
    ]


def error_stage(text: str) -> str:
    m = re.search(r"^# stage: (\S+)", text, re.MULTILINE)
    if m:
        return m.group(1)
    with contextlib.suppress(tomllib.TOMLDecodeError, KeyError, IndexError, TypeError):
        return tomllib.loads(text)["general"]["stages"][0]
    return "dev"


def build_error_vector(toml_path: Path, context: dict) -> dict:
    text = toml_path.read_text(encoding="utf-8")
    stage = error_stage(text)
    result = {
        "input": toml_path.relative_to(REPO_ROOT).as_posix(),
        "stage": stage,
        "context": context,
    }
    with isolated_load(toml_path, context):
        try:
            Settings.from_toml(stage=stage)
        except ValidationError as e:
            errors = e.errors()
            if len(errors) != 1:
                raise click.ClickException(
                    "%s: エラーが %d 件あります (1 ファイル 1 欠陥にしてください):\n%s"
                    % (toml_path.name, len(errors), e)
                ) from e
            error = errors[0]
            kind = _error_kind(error["type"])
            message = error["msg"]
            if kind == "Value":
                message = message.removeprefix("Value error, ")
            return {
                **result,
                "path": _error_path(error["loc"]),
                "kind": kind,
                "message": message,
            }
        except tomllib.TOMLDecodeError as e:
            return {**result, "path": [], "kind": "Syntax", "message": str(e)}
        except ValueError as e:
            return {**result, "path": [], "kind": "Value", "message": str(e)}
    raise click.ClickException("%s: エラーになりません" % toml_path.name)


def expected_error_files() -> dict[Path, str]:
    context = {**DEFAULT_CONTEXT, "runtime_version": runtime_version}
    files: dict[Path, str] = {}
    for path in sorted(ERRORS_DIR.glob("*.toml")):
        vector = build_error_vector(path, context)
        files[path.with_suffix(".expected.json")] = render(vector)
    return files


def existing_files() -> set[Path]:
    return (
        set(OUT_DIR.rglob("*.json"))
        | set(RUNTIME_DIR.glob("*.runtime.toml"))
        | set(ERRORS_DIR.glob("*.expected.json"))
    )


def diff_files(expected: dict[Path, str]) -> list[str]:
    """commit 済みの vector との差 (相対 path と理由)"""
    problems = []
    existing = existing_files()
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
    expected, skipped = expected_files()
    expected.update(expected_error_files())
    if skipped:
        click.echo(
            "Python が読めない runtime.toml は vector にしていません:\n  "
            + "\n  ".join(skipped),
            err=True,
        )
    if check:
        problems = diff_files(expected)
        if problems:
            raise click.ClickException(
                "settings vector が古いです (`just vectors` で再生成):\n  "
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
