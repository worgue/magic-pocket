"""確認プロンプトの非対話制御 (assume yes)。

`-y` / `--yes` を受け取ったコマンドが :func:`set_assume_yes` を呼ぶと、以降の
:func:`confirm` はプロンプトを出さずに承認扱いになる。

素の ``click.confirm`` ではなくこのモジュールを経由するのは、``pocket deploy``
のように「そのコマンド自身は確認を出さないが、配下の処理が確認を出しうる」
経路でも `-y` の意味が保たれるようにするため。CI や LLM からの非対話実行では
確認が 1 つでも残ると停止するので、確認の追加側が `-y` の存在を意識しなくても
済む形にしておく。
"""

import click

_assume_yes = False


def set_assume_yes(value: bool) -> None:
    """以降の :func:`confirm` を無条件承認にするか設定する。"""
    global _assume_yes
    _assume_yes = value


def assume_yes() -> bool:
    """現在 assume yes が有効かを返す。"""
    return _assume_yes


def confirm(text: str, *, default: bool = True, abort: bool = False) -> bool:
    """assume yes を尊重する ``click.confirm``。

    assume yes が有効なら、プロンプトを出さず True (続行) を返す。
    ``abort=True`` の呼び出しでも中断せずに続行する。
    """
    if _assume_yes:
        return True
    return click.confirm(text, default=default, abort=abort)
