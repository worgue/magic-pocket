"""build context のファイル permission チェック。

Lambda の実行ユーザーは非 root (sbx_user) のため、other-read の無いファイル
(編集操作の副作用で生まれる mode 600 等) が image にそのまま COPY されると
読めず、全 handler が INIT フェーズで失敗する (wsgi も同居するためサイトごと
500)。表面のエラーは Runtime.Unknown で原因に辿り着きにくいので、build 前に
context を走査して legible に警告する。runtime が INIT で読む
pocket.toml / pocket.runtime.toml はエラーに昇格する。

codebuild backend は source zip 作成時に mode を 0644/0755 へ正規化する
(_add_source_file) ため対象外。docker / depot backend は context を生の
permission のまま送るため、build 前にこのチェックを通す。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pocket.utils import echo
from pocket_cli.resources.aws.builders.dockerignore import (
    iter_source_files,
    load_dockerignore,
)

_MAX_LISTED = 10

# この process の build で警告した other-read 無しファイル ("path (mode)" 形式)。
# build ログに警告が埋もれて INIT 失敗まで気付けない実害があったため、deploy
# 終了時の再掲 (deploy_cli) と INIT 失敗エラーの原因提示 (lambdahandler) に使う。
# multi-container で同じ context を複数回 build しても重複しては積まない。
_warned_files: list[str] = []

# runtime が INIT フェーズで読む設定ファイル。other-read が無いまま COPY される
# と確実に INIT 失敗する (Rust runtime は pocket.toml を上方探索、Python runtime
# は pocket.runtime.toml を読む) ため、警告でなくエラーに昇格する。
_INIT_CRITICAL_NAMES = frozenset({"pocket.toml", "pocket.runtime.toml"})


def find_files_without_world_read(context_dir: Path) -> list[str]:
    """dockerignore 適用後の build context から other-read の無いファイルを列挙する。

    壊れた symlink (実体なし) は stat できないため対象外 (zip 側と同様 skip)。
    """
    spec = load_dockerignore(context_dir)
    return [
        rel
        for abs_path, rel in iter_source_files(context_dir, spec)
        if os.path.exists(abs_path) and not os.stat(abs_path).st_mode & stat.S_IROTH
    ]


def warn_files_without_world_read(context_dir: Path) -> list[str]:
    """other-read の無いファイルを警告する (検出リストを返す)。

    一般ファイルを error にしないのは、Dockerfile 側で `COPY --chmod` や
    `RUN chmod` により image 内で正規化している構成では false positive に
    なるため。ただし runtime が INIT で読む pocket.toml / pocket.runtime.toml
    (_INIT_CRITICAL_NAMES) は該当 container が確実に INIT で死ぬうえ、非 secret
    の設定ファイルで local を `chmod 644` して困る構成が無いため error とする。
    """
    unreadable = find_files_without_world_read(context_dir)
    critical = [r for r in unreadable if Path(r).name in _INIT_CRITICAL_NAMES]
    if unreadable:
        with_mode = [
            "%s (%o)" % (rel, os.stat(context_dir / rel).st_mode & 0o777)
            for rel in unreadable
        ]
        for entry in with_mode:
            if entry not in _warned_files:
                _warned_files.append(entry)
        listed = ", ".join(with_mode[:_MAX_LISTED])
        if len(with_mode) > _MAX_LISTED:
            listed += " (他 %d 件)" % (len(with_mode) - _MAX_LISTED)
        echo.warning(
            "build context に other-read の無いファイルがあります (%d 件): %s。"
            "Lambda の実行ユーザーは非 root のため、このまま image に COPY されると"
            "読めず INIT フェーズで失敗します (wsgi も同じ image のためサイトごと "
            "500)。`chmod 644 <file>` で修正するか、Dockerfile の COPY に "
            "`--chmod` を付けて build 段で正規化してください。"
            % (len(unreadable), listed)
        )
    if critical:
        raise RuntimeError(
            "build context の %s に other-read がありません。runtime が INIT "
            "フェーズで読む設定ファイルのため、このまま image に COPY されると"
            "該当 container は確実に INIT で失敗します (PermissionDenied)。"
            "`chmod 644 <file>` で修正してください (COPY --chmod で image 内を"
            "正規化している場合も、local 側を 644 にして問題ありません)。"
            "image に COPY しない場合は .dockerignore に追加してください。"
            % ", ".join(critical)
        )
    return unreadable


def warned_files_with_mode() -> list[str]:
    """この process の build で警告済みの other-read 無しファイル ("path (mode)")"""
    return list(_warned_files)


def resummarize_world_read_warnings() -> None:
    """build 時の other-read 警告を deploy 終了時に再掲する。

    build ログの途中に出る警告は大量の出力に埋もれて気付けず、INIT 失敗まで
    到達してしまう実害があった (2026-08-26 受領 feedback)。deploy の最後に
    ファイル名 + mode を再掲して、その場で chmod できるようにする。
    """
    if not _warned_files:
        return
    listed = ", ".join(_warned_files[:_MAX_LISTED])
    if len(_warned_files) > _MAX_LISTED:
        listed += " (他 %d 件)" % (len(_warned_files) - _MAX_LISTED)
    echo.warning(
        "注意: build context に other-read の無いファイルがありました (%d 件): "
        "%s。Dockerfile で `COPY --chmod` 等により正規化していない場合、Lambda の"
        "非 root 実行ユーザーが読めず INIT フェーズで失敗します。"
        "`chmod 644 <file>` で修正してください。" % (len(_warned_files), listed)
    )
