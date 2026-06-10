"""`pocket version` とパッケージメタデータの同期検証。

過去に `pocket/__init__.py` の手書き `__version__` が version bump から漏れ、
`pocket version` が古いバージョンを表示したことがある (0.2.0 で発覚、0.2.1 で
メタデータ由来に変更)。手書き定数への先祖返りを検知する。
"""

from importlib.metadata import version

from click.testing import CliRunner
from pocket_cli.cli.main_cli import main


def test_version_command_matches_package_metadata():
    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == version("magic-pocket")
