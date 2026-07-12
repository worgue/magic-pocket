# AGENTS.md

このファイルはコーディングエージェント (Claude Code / Codex 等) 向けの
プロジェクト共通ルール。リポジトリで作業する際は本ファイルの内容に従うこと。

## 言語設定
**このプロジェクトは日本語が基準です。Claude Codeとのやり取りは日本語で行ってください。**
コメント、ドキュメント、コミットメッセージなど、すべて日本語で記述してください。

## ファイルフォーマット
- 文字エンコーディング: UTF-8
- すべてのファイルは改行（LF）で終わる
- 行末の空白は削除する

## Python

### コード品質

**重要**: uvによるパッケージ管理は以下の方法を厳守してください。

- `uv add <package>`: パッケージを追加
- `uv remove <package>`: パッケージを削除
- `uv sync --all-groups`: 依存関係を同期

**重要**: Pythonファイル（.py）を編集・作成した後は、必ず以下のチェックを実行してください。

1. **フォーマット**: `uv run ruff format` でコードを整形
2. **リントチェック**: `uv run ruff check` でエラーを検出
3. **typeチェック**: `uv run pyright` で型エラーを検出
4. **エラー修正**: エラーが検出された場合は、エラー内容を報告し、修正すること

**重要**:

- Ruffエラーを無視する設定（`# noqa`, `# ruff: noqa`, `pyproject.toml`でのignore設定など）は、勝手に追加してはいけない
- エラーを無視する必要がある場合は、必ずユーザーに報告して判断を委ねること
- エラーを残したままタスクを完了しないこと

```bash
# 1. フォーマット実行（Pythonファイル編集後は必須）
uv run ruff format .

# 2. リントチェック（エラー検出）
uv run ruff check .

# 特定のファイルのみチェック
uv run ruff check <file_path>

# 3. typeチェック（型エラー検出）
uv run pyright
```

### 型チェックのガイドライン

- **型エラーの解決方法**: `typing.cast()` は使用せず、ユーザーに確認したうえで、型アノテーション + `# type: ignore` で対処してください。
- **理由**:
  - `cast()` よりもシンプルで読みやすい
  - 実行時のオーバーヘッドがない（インポート不要）
  - 意図が明確（型チェッカーの判断が厳しすぎる場合の無視）
- **例**:
  ```python
  # 良い例
  result: tuple[int] = cursor.fetchone()  # type: ignore
  count = result[0]

  # 避ける例
  result = cast(tuple[int], cursor.fetchone())
  count = result[0]
  ```

### Pythonの制約事項

- except Exception は絶対に使用してはいけません。他にも曖昧な例外キャッチは避け、特定の例外をキャッチしてください。出来ない場合、raiseでプログラムが止まって構いません。
- 例外は重要な情報を含むので、無理にexceptしないでください。以下の様な、単にエラーメッセージを表示してsys.exit(1)するだけの例外処理は作らず自然に失敗させて問題ありません。
  ```python
  except mysql.connector.Error as e:
      print(f"MySQLエラー: {e}", file=sys.stderr)
      sys.exit(1)
  ```
- **sys.pathへの動的追加禁止**: `sys.path.append()`や`sys.path.insert()`でパスを動的に追加してはならない。パッケージ構造とインストールで解決すること。
- **インポートはファイル先頭で行う**: すべてのimport文はファイルの先頭に配置する。関数内での遅延インポートが必要な場合は、事前に確認すること。

### Pythonの推奨事項

- 引数を取るPythonスクリプトには、Clickライブラリを使用してください。argparseは使用しないでください。
- 続けて5行以上の情報をprintする場合、print()を何度も呼ばず、print_xxx()関数を作成してまとめてください。4行以下なら分ける必要はありません。print_xxx()関数はファイルの最後にまとめて配置してください。xxxには、表示内容が分かる名前を入れてください。長くても構いません。

### パッケージ構成と依存方針

このリポジトリは 2 パッケージ構成です。依存を追加・移設する際は、どちらに属する変更かを必ず意識してください。

- **`magic-pocket` (runtime, リポジトリルート)**: Lambda ランタイムに同梱される。全ユーザーの Lambda にバンドルされるため、**依存は最小限に保つ**こと。新規の実行時依存を安易に追加しない。
- **`magic-pocket-cli` (`packages/magic-pocket-cli/`)**: deploy / CLI 専用。`requests` 等の重い依存はこちらに置く。

CLI 専用だったロジックを runtime 側へ移設して公開 API 化する場合（例: 外部 provisioner から import 共有させる）、その依存を runtime に持ち込まないこと。**HTTP なら `requests` ではなく stdlib `urllib` で書き換える**（`pocket.provisioning.neon` はこの方針で実装済み）。やむを得ず runtime に依存を足す判断が要る場合は、**ユーザーに確認**してください。

なお CLI が runtime の新モジュールを import（re-export 含む）するようになったら、`packages/magic-pocket-cli/pyproject.toml` の `magic-pocket>=X.Y.Z` pin を、そのモジュールが載る版へ**必ず引き上げる**こと（古い runtime との組み合わせで import 落ちを防ぐ）。
