# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 言語設定
**このプロジェクトは日本語が基準です。Claude Codeとのやり取りは日本語で行ってください。**
コメント、ドキュメント、コミットメッセージなど、すべて日本語で記述してください。

## ファイルフォーマット
- 文字エンコーディング: UTF-8
- すべてのファイルは改行（LF）で終わる
- 行末の空白は削除する

## 重要な制約事項
- ファイル編集のスコープ（範囲）制限: あなたのファイル編集範囲は、コマンドが実行されたディレクトリによって厳しく制限されます。このルールを必ず遵守してください。
  - worktree内での作業時: 起動されたディレクトリが .worktrees/ 以下（例: .worktrees/feature/new-feature）の場合、編集対象はそのディレクトリ内部のファイルのみに限定されます。../ を使用して親ディレクトリのファイルにアクセスし、編集することは固く禁止します。
  - プロジェクトルートでの作業時: 起動されたディレクトリがプロジェクトのルート（.worktreesディレクトリが見える階層）の場合、.worktrees/ ディレクトリ全体を完全に無視してください。このディレクトリは各機能開発用の一時的な作業環境であり、あなたの編集対象ではありません。
- git操作の制限:
  - **許可**: `git status`, `git diff`, `git log`, `git show`, `git mv`, `git restore`, `git rm`, `git bisect`, `git grep`（参照・軽微な操作）
  - **禁止**: `git add`, `git commit`, `git push`, `git pull`, `git checkout`, `git reset`, `git rebase`, `git merge`, `git stash`, `git clone`, `git init`, `git branch`, `git switch`, `git tag`, `git fetch`（変更・履歴操作）
  - コミット操作はユーザーが手動で行います

## Python

### コード品質

**重要**: uvによるパッケージ管理は以下の方法を厳守してください。

- `uv add <package>`: パッケージを追加
- `uv remove <package>`: パッケージを削除
- `uv sync --all-groups`: 依存関係を同期（venv作成は不要、プロジェクトルートの.venvを使用）
- `uv venv` は使用しない（既存の.venvを使用）

**重要**: Pythonファイル（.py）を編集・作成した後は、必ず以下のチェックを実行してください。

1. **フォーマット**: `uv run ruff format` でコードを整形
2. **リントチェック**: `uv run ruff check` でエラーを検出
3. **typeチェック**: `uv run pyright` で型エラーを検出
4. **エラー修正**: エラーが検出された場合は、エラー内容を報告し、修正すること

**重要**:

- Ruffエラーを無視する設定（`# noqa`, `# ruff: noqa`, `pyproject.toml`でのignore設定など）は、Claude Codeが勝手に追加してはいけない
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
