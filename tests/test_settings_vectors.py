"""共有 test vector (tests/data/settings_vectors) が Python の設定モデルと一致すること。

Python のモデルを変えたら `just vectors` で vector を作り直し、Rust 側
(`crates/pocket-settings`) を直して両方のテストを green にする。
"""

import subprocess
import sys
from pathlib import Path

GENERATOR = Path(__file__).resolve().parent / "vectors/generate_settings_vectors.py"


def test_settings_vectors_are_up_to_date():
    res = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
