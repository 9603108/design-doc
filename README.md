# design-doc

## 概要

`design-doc` は、Web アプリのソースから、画面ごとの詳細設計書(.docx)を自動生成する Claude Code のスキルです。HTML・JavaScript・Python のバックエンドを読み、画面1つにつき設計書1つを作ります。入力の単位は、HTML 1枚、または React の画面コンポーネント(tsx)1つと、画面を構成するソースの一覧(`source_files`)の組のどちらでもかまいません。`source_files` の書き方は `SKILL.md` を参照してください。

- ソースの解析と中間データの作成は、Python のスクリプト群が行います。
- docx の生成は、Node.js の `docx` パッケージが行います。
- 出力は日本語固定です。出力先は `target/output/詳細設計書_{機能名}.docx` です。
- 特定の案件の設定は入っていません。架空の「受注入力」画面のサンプルが `examples/sample/` にあります。

## 対応する構成

- 画面は、素の JavaScript(バニラ JS)の HTML か、React(TSX)の画面です。Vue など、React 以外のフレームワークは範囲外です。React の画面の設定(`source_files` など)は `SKILL.md` を参照してください。
- 画面は、共通の関数 `apiCall(...)` で API を呼ぶ形を前提にしています。`call('<action>', ...)` のような関数で呼ぶ形も、その関数名を project_config の `api_call_functions` に書けば扱えます。
- バックエンドは、1つの入口ファイルが、受け取った `action` の値で処理を振り分ける REST の形を前提にしています。URL のパスごとに処理を分ける API などは、画面と API の呼出の突き合わせで検出されません。

詳しい前提は `SKILL.md` の冒頭にあります。

## 必要なもの

bash で動かす前提です。Linux・macOS・WSL が対象で、Windows ネイティブ(PowerShell のみ)は対象外です。

| 要るもの | 補足 |
|:---|:---|
| Node.js 18 以上と npm | `docx` パッケージを使います |
| Python 3 | 解析のスクリプトを動かします |
| npm の `docx` パッケージ | `npm install -g docx`。実行時は `NODE_PATH=$(npm root -g)` を付けます |
| cairosvg と日本語フォント(任意) | 構成図を PNG にするときだけ必要です。無いと、構成図の章は PNG の代わりに文字の図になります |

動作を確かめた版は、Node.js 22.17.0、Python 3.10.12、docx 9.6.1 です。`generate_docx.js` のコメントが docx 9.6.1 の仕様に触れているため、docx はこの版を勧めます。

## インストール

```bash
git clone https://github.com/9603108/design-doc.git ~/.claude/skills/design-doc
npm install -g docx@9.6.1
```

- `~/.claude/skills/design-doc` が既にある場合は、上書きせず、退避してから clone してください。
- 構成図を PNG にするなら、`pip install cairosvg` と日本語フォント(Ubuntu・Debian では `fonts-noto-cjk`)も入れます。cairosvg は cairo ライブラリに依存します。
- インストールのあと、Claude Code を開き直してください(新しいセッションを始めます)。`/` を入力して、候補に `design-doc` が出れば完了です。出ないときは、`~/.claude/skills/design-doc/SKILL.md` の位置と、先頭の `name:` を確かめてください。

## 動作確認

スクリプトは、生成物をスキルのフォルダの `target/` の下へ書きます。インストール先を汚さないため、確認は一時フォルダへ複製して行います。

```bash
T=$(mktemp -d)
cp -r ~/.claude/skills/design-doc "$T/design-doc"
cd "$T/design-doc"
for f in target/scripts/*.py; do python3 -m py_compile "$f" || echo "NG: $f"; done
node --check target/scripts/generate_docx.js && echo "node ok"
cd ~ && rm -rf "$T"
```

`NG:` が1件も出ず、`node ok` が出れば合格です。

## 使い方

1. 設定2つを、`examples/sample/` から `target/intermediate/` へコピーします。ファイル名の `受注入力` は、自分の機能名に変えます。
2. 設定の値を、自分の案件に合わせて書き換えます。
3. Phase 1(ソースを読んで中間データを作る作業)を済ませます。
4. `FEATURE_NAME=<機能名>` を指定して、`SKILL.md` の「実行順序」のスクリプトを流します。

設定をコピーしただけでは、中身の無い骨組みの設計書になります。Phase 1 を先に行ってください。手順の詳細は `SKILL.md` と `examples/sample/README.md` にあります。

## 設定ファイル

設定は、画面ごとに2つあります。どちらも `target/intermediate/` に置きます。

- `{機能名}_screen_config.json`: 画面別の設定
- `{機能名}_project_config.json`: 案件共通の設定

見本は `examples/sample/` にあります。書き換える箇所と各キーの意味は、`examples/sample/README.md` と `SKILL.md` を参照してください。

## ライセンス

ライセンスはまだ定めていません。
