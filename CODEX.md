# CODEX.md

@AGENTS_COMMON.md

上の行は共通指示 (agent 非依存の正本) の import です。`forge codex run` が起動前に
この行を AGENTS_COMMON.md の内容へ展開して Codex に読ませます (Codex 自身は `@path` を
解釈しないため、**先頭の import 行は動かさない**)。

Codex 固有の指示は現状ありません。追加するときは AGENTS_COMMON.md ではなく
このファイルに書いてください。
