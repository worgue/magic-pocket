"""CI が赤になることの確認用 (直後に削除する)。"""

import subprocess


def run(cmd: str) -> int:
    return subprocess.call(cmd, shell=True)
