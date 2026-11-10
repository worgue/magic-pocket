"""共有 test vector (tests/data/settings_vectors) が Python の設定モデルと一致すること。

Python のモデルを変えたら `just vectors` で vector を作り直し、Rust 側
(`crates/pocket-settings`) を直して両方のテストを green にする。
"""

import json
import subprocess
import sys
import typing
from pathlib import Path

from pydantic import BaseModel

from pocket.settings import Settings

TESTS_DIR = Path(__file__).resolve().parent
GENERATOR = TESTS_DIR / "vectors/generate_settings_vectors.py"
VECTORS_DIR = TESTS_DIR / "data/settings_vectors"


def test_settings_vectors_are_up_to_date():
    res = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr


def _hints(model: type[BaseModel]) -> dict:
    """field の型。文字列の前方参照が残っていれば model_rebuild で解決する"""
    if any(
        isinstance(f.annotation, typing.ForwardRef | str)
        for f in model.model_fields.values()
    ):
        model.model_rebuild(force=True)
    return {name: f.annotation for name, f in model.model_fields.items()}


def _model_types(annotation) -> list[type[BaseModel]]:
    """注釈に含まれるモデル (Optional / dict / list / union / Annotated を開く)"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    out = []
    for arg in typing.get_args(annotation):
        out.extend(_model_types(arg))
    return out


def _all_fields(model: type[BaseModel], seen: set) -> None:
    if model in seen:
        return
    seen.add(model)
    for annotation in _hints(model).values():
        for sub in _model_types(annotation):
            _all_fields(sub, seen)


def _children(annotation, value) -> list[tuple[type[BaseModel], dict]]:
    """value のうちモデルに当たる部分 (モデル, dump) の一覧"""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [(annotation, value)] if isinstance(value, dict) else []
    if origin is typing.Annotated:
        return _children(args[0], value)
    if origin is dict and isinstance(value, dict):
        return [c for v in value.values() for c in _children(args[1], v)]
    if origin is list and isinstance(value, list):
        return [c for v in value for c in _children(args[0], v)]
    if args:  # union / Optional: scheduler のタグで選ぶ、他は候補全部を試す
        models = [a for a in args if isinstance(a, type) and issubclass(a, BaseModel)]
        if isinstance(value, dict) and "scheduler" in value and len(models) > 1:
            models = [
                m
                for m in models
                if m.model_fields["scheduler"].default == value["scheduler"]
                or value["scheduler"] in typing.get_args(_hints(m)["scheduler"])
            ]
        out = []
        for arg in args:
            if arg in models or not (
                isinstance(arg, type) and issubclass(arg, BaseModel)
            ):
                out.extend(_children(arg, value))
        return out
    return []


def _default_json(field) -> object:
    default = field.get_default(call_default_factory=False)
    if isinstance(default, BaseModel):
        return default.model_dump(mode="json")
    return default


def _covered(model: type[BaseModel], data: dict, found: set) -> None:
    hints = _hints(model)
    for name, field in model.model_fields.items():
        if name not in data:
            continue
        value = data[name]
        if (
            field.is_required()
            or field.default_factory is not None
            or value != _default_json(field)
        ):
            found.add((model.__name__, name))
        for sub, sub_value in _children(hints[name], value):
            _covered(sub, sub_value, found)


def test_every_field_has_non_default_vector():
    """全モデルの全 field に、既定でない値を与える vector が 1 件以上あること"""
    models: set = set()
    _all_fields(Settings, models)
    # 値が 1 つだけの Literal (LambdaScheduleEntry.scheduler 等) は既定以外を書けない
    wanted = {
        (m.__name__, name)
        for m in models
        for name, f in m.model_fields.items()
        if not (
            typing.get_origin(f.annotation) is typing.Literal
            and len(typing.get_args(f.annotation)) == 1
        )
    }
    found: set = set()
    for path in VECTORS_DIR.rglob("*.json"):
        vector = json.loads(path.read_text(encoding="utf-8"))
        _covered(Settings, vector["settings"], found)
    missing = sorted(wanted - found)
    assert not missing, "既定でない値の vector が無い field:\n" + "\n".join(
        "  %s.%s" % m for m in missing
    )
