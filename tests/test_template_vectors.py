"""共有 test vector (tests/data/template_vectors) が Python の template と一致すること。

template (`pocket_cli/templates/cloudformation/`) や template に渡す変数を変えたら
`just vectors` で vector を作り直し、Rust 側 (`crates/pocket-cli` の stack module) を
直して両方のテストを green にする。
"""

import subprocess
import sys
from pathlib import Path

GENERATOR = Path(__file__).resolve().parent / "vectors/generate_template_vectors.py"


def test_template_vectors_are_up_to_date():
    res = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
