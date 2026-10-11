# 引数にスペースが含まれても分解されないよう位置パラメータを有効化する
# (例: just test -k "a or b")。pass-through するレシピの body では
# {{args}} ではなく "$@" を使うこと
set positional-arguments

docs:
    uv run zensical serve -a 0.0.0.0:8080

# テスト実行 (引数は pytest にパススルー。"$@" で "a or b" 等の空白引数を保つ)
test *args: _target-gc-guard
    uv run pytest "$@"
    cargo test --all-features

# target/ を作り直し、フルビルド直後のサイズを baseline として記録する
target-gc:
    #!/usr/bin/env bash
    set -uo pipefail
    state=".forge/vm/target-gc"
    mkdir -p "$(dirname "$state")"
    now=$(date +%s)
    echo "[target-gc] cargo clean → フルビルドで baseline を取り直します (数分かかります)"
    cargo clean
    if cargo build --workspace --all-targets; then
        kb=$(du -sk target | cut -f1)
        printf 'baseline_kb=%s\nlast_gc=%s\nlast_check=%s\n' "$kb" "$now" "$now" > "$state"
        echo "[target-gc] baseline = $((kb / 1024)) MiB を記録しました"
    else
        # 編集途中の壊れたツリーで発火した場合に、失敗ビルドの途中サイズを
        # baseline として過小記録しない (gc 時刻だけ更新して次回に回す)
        base=$(sed -n 's/^baseline_kb=//p' "$state" 2>/dev/null | tail -1)
        printf 'baseline_kb=%s\nlast_gc=%s\nlast_check=%s\n' "${base:-0}" "$now" "$now" > "$state"
        echo "[target-gc] ビルド失敗のため baseline は更新していません" >&2
        exit 1
    fi

# cargo を呼ぶレシピの前段ガード。TARGET_GC=never で無効化、TARGET_GC_RATIO で倍率変更。
# CI (GitHub Actions は CI=true) では発火させない
_target-gc-guard:
    #!/usr/bin/env bash
    set -uo pipefail
    [ "${TARGET_GC:-}" = "never" ] && exit 0
    [ -n "${CI:-}" ] && exit 0
    state=".forge/vm/target-gc"
    now=$(date +%s)
    if [ ! -f "$state" ]; then
        echo "[target-gc] baseline 未確立のため target-gc を実行します"
        exec just target-gc
    fi
    baseline_kb=$(sed -n 's/^baseline_kb=//p' "$state" | tail -1)
    last_gc=$(sed -n 's/^last_gc=//p' "$state" | tail -1)
    last_check=$(sed -n 's/^last_check=//p' "$state" | tail -1)
    if [ $(( now - ${last_gc:-0} )) -gt $(( 30 * 86400 )) ]; then
        echo "[target-gc] 前回 gc から 30 日超のため target-gc を実行します"
        exec just target-gc
    fi
    # du -s target は数秒かかるので 1 日 1 回に間引く
    [ $(( now - ${last_check:-0} )) -lt 86400 ] && exit 0
    kb=$(du -sk target 2>/dev/null | cut -f1)
    sed -i "s/^last_check=.*/last_check=$now/" "$state"
    ratio=${TARGET_GC_RATIO:-5}
    if [ "${baseline_kb:-0}" -gt 0 ] && [ "${kb:-0}" -gt $(( baseline_kb * ratio )) ]; then
        echo "[target-gc] target/ が baseline の ${ratio} 倍を超えました ($((kb / 1024)) MiB)"
        exec just target-gc
    fi
