#!/bin/sh
# Rust 版 CLI `pocket-rs` を GitHub Release から入れる。Python は要らない。
#
#   sh install.sh X.Y.Z
#   curl -fsSL https://github.com/worgue/magic-pocket/raw/main/install.sh | sh -s -- X.Y.Z
#
# - 版 (X.Y.Z) は必須。project の pin と同じ版を入れる (省略時に latest を入れない)
# - 置き場所は ${POCKET_RS_INSTALL_DIR:-$HOME/.local/bin}/pocket-rs。既存があれば版を表示して上書きする
# - tar.gz は同じ Release の .sha256 で検証してから置く
# - 依存は curl / tar / sha256sum か shasum だけ
set -eu

BIN="pocket-rs"

die() {
    echo "install.sh: $*" >&2
    exit 1
}

detect_target() {
    os=$(uname -s)
    arch=$(uname -m)
    case "$os/$arch" in
        Darwin/arm64 | Darwin/aarch64)
            echo "aarch64-apple-darwin"
            ;;
        Linux/x86_64 | Linux/amd64)
            # glibc があれば gnu、無ければ (Alpine 等) 静的リンクの musl
            if ldd --version >/dev/null 2>&1; then
                echo "x86_64-unknown-linux-gnu"
            else
                echo "x86_64-unknown-linux-musl"
            fi
            ;;
        Linux/aarch64 | Linux/arm64)
            # aarch64 linux は musl (静的リンク) だけを配っている
            echo "aarch64-unknown-linux-musl"
            ;;
        *)
            die "この環境 ($os/$arch) 向けのバイナリは配っていません (aarch64-apple-darwin / x86_64-unknown-linux-gnu / x86_64-unknown-linux-musl / aarch64-unknown-linux-musl)"
            ;;
    esac
}

sha256_check() {
    # $1 = .sha256 ファイル (中身は "<hash>  <file名>")。同じディレクトリで実行する
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -c "$1"
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c "$1"
    else
        die "sha256sum も shasum もありません (sha256 を検証できないため止めます)"
    fi
}

[ $# -eq 1 ] || die "使い方: install.sh X.Y.Z (project の pin と同じ版を指定してください)"
version=$1
case "$version" in
    [0-9]*.[0-9]*.[0-9]*) ;;
    *) die "版は X.Y.Z の形で指定してください: $version" ;;
esac
command -v curl >/dev/null 2>&1 || die "curl がありません"
command -v tar >/dev/null 2>&1 || die "tar がありません"

target=$(detect_target)
name="$BIN-$target"
base="https://github.com/worgue/magic-pocket/releases/download/$version"
install_dir=${POCKET_RS_INSTALL_DIR:-$HOME/.local/bin}
dest="$install_dir/$BIN"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
trap 'exit 130' INT TERM

echo "取得: $base/$name.tar.gz"
curl -fsSL -o "$tmp/$name.tar.gz" "$base/$name.tar.gz" ||
    die "$name.tar.gz を取得できません (版 $version の Release に asset があるか確かめてください)"
curl -fsSL -o "$tmp/$name.tar.gz.sha256" "$base/$name.tar.gz.sha256" ||
    die "$name.tar.gz.sha256 を取得できません"
(cd "$tmp" && sha256_check "$name.tar.gz.sha256") || die "sha256 が一致しません"
tar xzf "$tmp/$name.tar.gz" -C "$tmp"
[ -f "$tmp/$name/$BIN" ] || die "アーカイブに $name/$BIN がありません"

if [ -e "$dest" ]; then
    old=$("$dest" --version 2>/dev/null || echo "版不明")
    echo "既存の $dest ($old) を上書きします"
fi
mkdir -p "$install_dir"
# 実行中のバイナリを壊さないよう、同じディレクトリに置いてから rename する
cp "$tmp/$name/$BIN" "$dest.tmp.$$"
chmod 755 "$dest.tmp.$$"
# 置き場所で実行できること (noexec・共有ライブラリ不足でない) を確かめてから置き換える
if ! new=$("$dest.tmp.$$" --version); then
    rm -f "$dest.tmp.$$"
    die "$install_dir で $BIN を実行できません (noexec の mount か、必要なライブラリが無い可能性があります)。既存のファイルは変えていません"
fi
mv -f "$dest.tmp.$$" "$dest"

echo "入れました: $dest ($new)"
case ":$PATH:" in
    *":$install_dir:"*) ;;
    *) echo "注意: $install_dir が PATH にありません" >&2 ;;
esac
