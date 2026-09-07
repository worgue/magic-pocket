"""django 未導入環境での CLI 起動の回帰テスト (feedback KN1281)。

0.32.0 で django-storages が [django] extra へ移動し、django が transitive に
入らなくなった結果、main_cli の `from pocket_cli import django_cli` (module top で
django を import する) が ModuleNotFoundError になり、`pocket --help` すら
起動不能になっていた。django 未導入でも django 非依存のサブコマンドは動き、
`pocket django ...` だけが install 案内 + 非ゼロ exit になることを検証する。

テスト環境には django が入っているため、sys.meta_path で django を隠した
subprocess で実際の import 経路を通して確認する。
"""

import subprocess
import sys

_BLOCKED_SCRIPT = """
import sys

class _BlockDjango:
    def find_spec(self, name, path=None, target=None):
        if name == "django" or name.startswith("django."):
            raise ModuleNotFoundError("No module named %r" % name, name=name)
        return None

sys.meta_path.insert(0, _BlockDjango())

from click.testing import CliRunner
from pocket_cli.cli.main_cli import main

runner = CliRunner()

r = runner.invoke(main, ["--help"])
assert r.exit_code == 0, "pocket --help failed: %s" % r.output
assert "django" in r.output, "django command missing from help: %s" % r.output

r = runner.invoke(main, ["version"])
assert r.exit_code == 0, "pocket version failed: %s" % r.output

r = runner.invoke(main, ["django", "build", "--stage=dev"])
assert r.exit_code != 0, "pocket django should fail without django"
assert "magic-pocket[django]" in r.output, "install guide missing: %s" % r.output
assert "Traceback" not in r.output

print("OK")
"""


def test_cli_works_without_django():
    res = subprocess.run(  # noqa: S603 sys.executable + 固定スクリプト
        [sys.executable, "-c", _BLOCKED_SCRIPT],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert "OK" in res.stdout


def test_cli_with_django_registers_real_group():
    """django が入っている環境では従来どおり実体の group が登録される。"""
    from pocket_cli import django_cli
    from pocket_cli.cli.main_cli import main

    assert main.commands["django"] is django_cli.django
