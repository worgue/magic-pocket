"""`pocket permissions` の共有 test vector (tests/data/permissions_vectors) の検査。

Python の action group の表や判定を変えたら `just vectors` で vector を作り直し、
Rust 側 (`crates/pocket-cli/src/permissions.rs`) を直して両方のテストを green にする。
"""

import json
import subprocess
import sys
from pathlib import Path

from pocket.permissions import action_groups

TESTS_DIR = Path(__file__).resolve().parent
GENERATOR = TESTS_DIR / "vectors/generate_permissions_vectors.py"
VECTORS_DIR = TESTS_DIR / "data/permissions_vectors"
SETTINGS_VECTORS_DIR = TESTS_DIR / "data/settings_vectors"


def _all_groups() -> list[list[str]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))["groups"]
        for path in sorted(VECTORS_DIR.rglob("*.json"))
    ]


def test_permissions_vectors_are_up_to_date():
    res = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr


def test_every_group_is_enabled_and_disabled_somewhere():
    """どの group も付く vector と (core 以外は) 付かない vector が 1 件以上あること"""
    vectors = _all_groups()
    names = list(action_groups())
    never_enabled = [n for n in names if not any(n in g for g in vectors)]
    always_enabled = [n for n in names if n != "core" and all(n in g for g in vectors)]
    assert not never_enabled, "付く vector が無い group: %s" % never_enabled
    assert not always_enabled, "付かない vector が無い group: %s" % always_enabled


def test_alternative_conditions_are_covered():
    """複数の条件で付く group の、それぞれの条件の vector があること"""
    vectors = _all_groups()
    cases = {
        # [dsql] 無しの [backup.rds]
        "backup without dsql": lambda g: "backup" in g and "dsql" not in g,
        # 同じ stage で sm と ssm の両方
        "secretsmanager and ssm": lambda g: "secretsmanager" in g and "ssm" in g,
        "ssm only": lambda g: "ssm" in g and "secretsmanager" not in g,
    }
    missing = [name for name, case in cases.items() if not any(map(case, vectors))]
    assert not missing, "vector が無い組み合わせ: %s" % missing


def _has_enabled_dead_letter_alert(settings: dict) -> bool:
    return any(
        ((h.get("sqs") or {}).get("dead_letter_alert") or {}).get("enabled")
        for c in settings["container"].values()
        for h in c["handlers"].values()
    )


def test_sqs_alert_by_inbound_only_is_covered():
    """[inbound] だけで sqs_alert が付く vector があること

    (dead_letter_alert が全部無効の stage)。inbound の handler は sqs handler に
    限られるので group の組み合わせでは見分けられず、同じ入力の settings vector で
    条件を見る。"""
    found = []
    for path in sorted(VECTORS_DIR.rglob("*.json")):
        settings_path = SETTINGS_VECTORS_DIR / path.relative_to(VECTORS_DIR)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))["settings"]
        groups = json.loads(path.read_text(encoding="utf-8"))["groups"]
        if "sqs_alert" in groups and not _has_enabled_dead_letter_alert(settings):
            found.append(path.name)
    assert found, "[inbound] だけで sqs_alert が付く vector がありません"
