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

エラーの vector: `tests/data/toml_errors/<名前>.toml` (1 ファイル 1 欠陥。stage は
先頭の `# stage: <名前>` コメント、無ければ `[general] stages` の先頭) を Python で
読んだときのエラーを `<名前>.expected.json` に書く。エラーがちょうど 1 件でない
fixture は不正として止める。kind は Missing / Extra / Type / Value (独自の検証と
前処理) / Syntax。path から pydantic の union のタグは除く。

Context の vector: 同じ入力 × stage を `Context.from_settings` で組み立て、
`model_dump(mode="json")` を `tests/data/context_vectors/<group>/<名前>.<stage>.json`
に書く (Rust 側は `crates/pocket-settings/tests/context_vectors.rs`)。外部入力
(Route53 の hosted zone / SM の managed secret の ARN / git の short hash /
managed_assets のディレクトリ) は固定の fake に差し替え、Python が実際に引いた値を
vector の `external` に同梱する (Rust は同じ値を注入する)。SM の managed secret は
各入力の先頭の stage だけ「作成済み」とし、2 番目以降は未作成 (PocketSecretIsNotReady)
にする。managed_assets を持つ入力では cwd の一時ディレクトリに
`<managed_assets>/default/robots.txt` を置く。`masked_text` は `pocket context` の
stdout (マスク後の `json.dumps(indent=2)`) と同じ文字列で、key の順まで固定する。

`--check` は書かずに、commit 済みの vector と一致するか (不足・余分を含む) を見る。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import click
from pydantic import ValidationError

import pocket.utils
from pocket.context import Context
from pocket.resources.aws.secretsmanager import PocketSecretIsNotReady
from pocket.settings import Settings
from pocket.utils import mask_secret_values, tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "tests/data/settings_vectors"
CONTEXT_OUT_DIR = REPO_ROOT / "tests/data/context_vectors"
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
}


# Context の外部入力の fake (Route53 の list_hosted_zones の HostedZones と同じ形)。
# 最長一致の確認のため、親子関係の zone を両方置く
FAKE_HOSTED_ZONES = [
    {"Name": "example.com.", "Id": "/hostedzone/ZEXAMPLE"},
    {"Name": "sandbox.magic-pocket.example.com.", "Id": "/hostedzone/ZSANDBOX"},
    {"Name": "project.com.", "Id": "/hostedzone/ZPROJECT"},
]
FAKE_GIT_SHORT_HASH = "abc1234"
FAKE_ASSET_FILE = "robots.txt"


def fake_secret_arn(pocket_key: str, region: str) -> str:
    return "arn:aws:secretsmanager:%s:123456789012:secret:%s-AbCdEf" % (
        region,
        pocket_key,
    )


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


def build_vector(toml_path: Path, stage: str, context: dict) -> dict:
    """toml_path を読んだ vector。"""
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


@contextlib.contextmanager
def fake_external(secret_ready: bool) -> Iterator[dict]:
    """Context の外部入力を fake にし、Python が引いた値を記録する"""
    secret_arns: dict[str, str | None] = {}

    class FakeSecretsManager:
        def __init__(self, context) -> None:
            self.context = context

        @property
        def arn(self) -> str:
            key = self.context.pocket_key
            arn = fake_secret_arn(key, self.context.region) if secret_ready else None
            secret_arns[key] = arn
            if arn is None:
                raise PocketSecretIsNotReady("Pocket secrets not found")
            return arn

    def fake_run(args, **kwargs):
        if args != ["git", "rev-parse", "--short", "HEAD"]:
            raise AssertionError("想定外の subprocess: %s" % args)
        return subprocess.CompletedProcess(args, 0, FAKE_GIT_SHORT_HASH + "\n", "")

    pocket.utils.get_hosted_zone_id_from_domain.cache_clear()
    with (
        mock.patch("pocket.context.SecretsManager", FakeSecretsManager),
        mock.patch("pocket.utils.get_hosted_zones", lambda: FAKE_HOSTED_ZONES),
        mock.patch("subprocess.run", fake_run),
    ):
        yield {
            "hosted_zones": FAKE_HOSTED_ZONES,
            "git_short_hash": FAKE_GIT_SHORT_HASH,
            "secret_arns": secret_arns,
        }
    pocket.utils.get_hosted_zone_id_from_domain.cache_clear()


def _place_managed_assets(data: dict) -> dict[str, list[str]]:
    """cwd に managed_assets の default ディレクトリを作る (dir → ファイル名)"""
    dirs: dict[str, list[str]] = {}
    for cf in (data.get("cloudfront") or {}).values():
        assets = cf.get("managed_assets") if isinstance(cf, dict) else None
        if not assets:
            continue
        directory = Path(assets) / "default"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / FAKE_ASSET_FILE).write_text("", encoding="utf-8")
        dirs[directory.as_posix()] = [FAKE_ASSET_FILE]
    return dirs


def _context_error_message(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return error.errors()[0]["msg"].removeprefix("Value error, ")
    return str(error)


def build_context_vector(
    toml_path: Path, stage: str, context: dict, secret_ready: bool
) -> dict:
    """toml_path を Context にした vector。"""
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    result: dict = {
        "input": toml_path.relative_to(REPO_ROOT).as_posix(),
        "stage": stage,
        "context": context,
    }
    with isolated_load(toml_path, context):
        dir_files = _place_managed_assets(data)
        with fake_external(secret_ready) as external:
            settings = Settings.from_toml(stage=stage)
            try:
                dumped = Context.from_settings(settings).model_dump(mode="json")
            except (ValueError, RuntimeError) as e:
                dumped = None
                result["error"] = _context_error_message(e)
        result["external"] = {**external, "dir_files": dir_files}
    if dumped is not None:
        masked = mask_secret_values(dumped)
        result["result"] = dumped
        result["masked_text"] = json.dumps(masked, indent=2)
    return result


def render(vector: dict) -> str:
    return json.dumps(vector, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def expected_files() -> dict[Path, str]:
    """書くファイル。"""
    files: dict[Path, str] = {}
    for group, name, path in iter_inputs():
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text)
        context = input_context(text)
        for stage in data["general"]["stages"]:
            vector = build_vector(path, stage, context)
            files[OUT_DIR / group / ("%s.%s.json" % (name, stage))] = render(vector)
    return files


def expected_context_files() -> dict[Path, str]:
    files: dict[Path, str] = {}
    for group, name, path in iter_inputs():
        text = path.read_text(encoding="utf-8")
        context = input_context(text)
        stages = tomllib.loads(text)["general"]["stages"]
        for i, stage in enumerate(stages):
            vector = build_context_vector(path, stage, context, secret_ready=i == 0)
            out = CONTEXT_OUT_DIR / group / ("%s.%s.json" % (name, stage))
            files[out] = render(vector)
    return files


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
    files: dict[Path, str] = {}
    for path in sorted(ERRORS_DIR.glob("*.toml")):
        vector = build_error_vector(path, DEFAULT_CONTEXT)
        files[path.with_suffix(".expected.json")] = render(vector)
    return files


def existing_files() -> set[Path]:
    return (
        set(OUT_DIR.rglob("*.json"))
        | set(CONTEXT_OUT_DIR.rglob("*.json"))
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
    expected = expected_files()
    expected.update(expected_error_files())
    expected.update(expected_context_files())
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
