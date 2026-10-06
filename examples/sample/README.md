# サンプル: 受注入力

## このサンプルは何か

架空の「受注入力」画面を1つだけ持つ、小さな Web アプリのソースと、それを design-doc スキルで解析するための設定2つである。目的は次の2つ。

- 設定の書き方を真似るための雛形（自分の案件では、設定2つを複製して値を書き換える）。
- このスキルが手元で最後まで動くかを確かめるための入力。

業務の内容・テーブル・データはすべて架空である。ソースは、スクリプトが解析して筋の通った結果を出せるように書いてあるが、動く完成品ではない（Web サーバや画面の配信の仕組みは含まない）。

## フォルダ構成

```
examples/sample/
├── README.md                      この文書
├── 受注入力_screen_config.json    画面別の設定（雛形）
├── 受注入力_project_config.json   プロジェクト共通の設定（雛形）
├── schema.sql                     4テーブルの DDL（列のコメントが論理名）
└── src/                           解析対象のソース（プロジェクトルート）
    ├── order_detail.html          受注入力画面
    ├── js/
    │   └── order_detail.js        画面の処理
    ├── common/
    │   ├── js/
    │   │   └── api.js             API 呼出の共通関数 apiCall
    │   └── python/
    │       └── db_utils.py        データベース接続の共通モジュール
    └── order-handler/
        └── handler.py             バックエンドの入口ファイル
```

プロジェクトルートは設定に書いていない。スクリプトが、HTML のあるフォルダから上へたどり、`common` フォルダを持つ最初の階層（このサンプルでは `src/`）を自動でプロジェクトルートにする。

## 使い方

1. 設定2つ（`受注入力_screen_config.json` と `受注入力_project_config.json`）を、スキルの `target/intermediate/` へ、ファイル名を変えずにコピーする。
2. `FEATURE_NAME=受注入力` で、`SKILL.md` の手順を実行する。

設定をコピーしてスクリプトを実行するだけでは、中身の無い空の骨組みの設計書になる。設定は解析の前提を与えるだけで、画面の項目・イベント・処理の内容は入っていないためである。先に `SKILL.md` の Phase 1 で、ソースを読んで中間データを作る必要がある。順序は次のとおり。

1. Phase 1 の成果を partial 4本（`_partial_screen_events` / `_partial_calc_state` / `_partial_backend` / `_partial_peripheral`）として作る。
2. `merge_partials.py` で1つの中間データにまとめる。
3. `event_processes[].details` に、イベントごとの処理ステップを補う（まとめた直後は空である）。

partial のどのキーに何を書くか、および 3 の手順は、`SKILL.md` の「Phase 1 の成果物の渡し方」の節にある。

`受注入力_screen_config.json` の `source_html_path` は `${HOME}/.claude/skills/design-doc/examples/sample/src/order_detail.html` と書いてある。このスキルを `~/.claude/skills/design-doc` に置いた場合は、そのまま動く。別の場所に置いた場合は、この値を、置いた場所に合わせて書き換える（`${HOME}` と `~` は展開される）。

## サンプルのソースの作り

このスキルが呼出の突き合わせで検出できる書き方に合わせてある。自分の案件のソースが同じ形かどうかを見比べる手がかりにしてほしい。

- 画面は素の JavaScript。`order_detail.html` が、`<script src="/common/js/api.js">` と `<script src="/js/order_detail.js">` を、プロジェクトルートからのパス（`/` 始まり）で読み込む。CSS は HTML の中の `style` に書いてあり、外部の CSS ファイルは無い。
- 画面から API への呼出は、すべて共通の関数を通す。形は `apiCall('POST', '/api/orders', { action: 'order_get', ... })` で、メソッド・パス・`action` の3つを文字列で直接書いている。
- バックエンドは、1つの入口ファイル `order-handler/handler.py` が、REST の POST で受け取った `action` の値で処理を振り分ける。`action = body.get('action')` で取り出し、`if action == 'order_get':` / `elif action == 'customer_get':` の形で分岐する。
- 入口ファイルの判定は、`backend_entry_patterns` の既定値 `["handler"]` を使っている（パスに `handler` を含むファイルが入口）。`common/python/db_utils.py` はパスに `handler` を含まないので入口にならず、`common/` を含むので共通モジュールとして扱われる。

`action` は次の4つ。

| action | 内容 | 画面側の呼出元 |
|:---|:---|:---|
| `order_get` | 受注番号で、受注のヘッダと明細を取得する | 初期表示、検索ボタン |
| `customer_get` | 顧客コードで、顧客名を取得する | 顧客コード入力 |
| `item_get` | 商品コードで、商品名と単価を取得する | 商品コード入力 |
| `order_save` | 入力チェックのうえ、受注と受注明細を保存する | 保存 |

テーブルは `customers`（顧客）・`items`（商品）・`orders`（受注）・`order_items`（受注明細）の4つ。定義は `schema.sql`、SQL 文は `handler.py` に文字列で持つ。

画面のイベントは EV00〜EV07 の8つで、`order_detail.js` の各処理の直前に `// EV01: 顧客コード入力` の形でコメントを付けてある。`受注入力_screen_config.json` の `process_names_by_event_code` と同じ名前である。

## 自分の案件に合わせて書き換える箇所

設定2つを `target/intermediate/` へコピーし、ファイル名の `受注入力` を自分の機能名に変えてから、次の値を書き換える。各キーの意味は `SKILL.md` の「画面別カスタマイズ」の節を参照。

### `{機能名}_screen_config.json`

| キー | 書き換える内容 |
|:---|:---|
| `feature_name` | 機能名（ファイル名の先頭、`FEATURE_NAME` と同じ値） |
| `screen_name` | 「〜画面」で終わる画面の正式名。機能名に「画面」を付けた名前にする（サンプルは `受注入力画面`） |
| `source_html_path` | 対象の画面の HTML のパス |
| `project_pattern` / `profile` | 対象の構成と画面の型（サンプルは `バックエンド統合型` / `CRUD型`） |
| `backend_files` | バックエンドのファイル（プロジェクトルートからの相対パス） |
| `shared_modules` / `file_descriptions` | 共通モジュールと、JS・CSS のファイルの説明 |
| `areas` | 画面のエリア（サンプルはヘッダ・明細・フッタ・ボタンの4つ） |
| `process_names_by_event_code` | イベントコードと処理名の対応 |
| `initial_processes` | 初期表示の処理。バックエンドを呼ぶ step には `backend_call.backend_id` を書く |
| `process_overview_overrides` | 処理の概要を短く上書きする文（処理の番号ごと。必要なときだけ） |
| `trigger_dispatch` | イベントごとの、条件と呼び出す処理の対応 |
| `diagrams` | 図の定義（サンプルは処理構成図を1つ） |
| `_meta` | この設定の説明（目的・意味合い・接続情報の覚え書き） |

`trigger_dispatch` の `then_call`（`F002` など）は、処理の番号である。サンプルの番号は、初期表示が `F001`、以降はイベントの並び順に `F002` から、という前提で書いてある。自分の案件では、処理の並びに合わせて番号を直す。

`screen_name` は、機能名に「画面」を付けた名前と一致させる（機能名がすでに「画面」を含む場合は、機能名そのまま）。検査のスクリプトは、この名前を画面の正式名として、トリガー文「{画面名}の…」の先頭と突き合わせる。「〜画面」で終わっていても、機能名と違う名前（機能名が `受注入力` なのに `注文入力画面` など）ではエラーになる。

`initial_processes` の、バックエンドを呼ぶ step の `backend_call.backend_id` は、画面側の処理とバックエンドの処理を結ぶ値である。値は action の名前ではなく、Phase 1 で作る `api_spec` の並び順の通し番号（1件目が `B001`、以降 `B002`…）である。サンプルは `api_spec` を `order_get`・`customer_get`・`item_get`・`order_save` の順に並べる前提で、初期表示が呼ぶ `order_get` を `B001` と書いてある。`api_spec` の並びを変えたら、この番号も合わせて直す。

処理の概要は、30字を超えると検査でエラーになる（25字以内を勧める）。初期表示の概要は処理名と `url_param_match` から自動で作られるので、超えやすい。超えるときは `process_overview_overrides` に、処理の番号をキーにして短い文を書くと、その文で上書きされる（サンプルは `F001` などで使っている）。文には、項目やパラメータの物理名を書かない。

### `{機能名}_project_config.json`

サンプルは、既定値のままで足りるキーを書いていない。書いてあるのは次の2つだけである。

| キー | 書き換える内容 |
|:---|:---|
| `docx_output.doc_title` | 文書種別名。機能名の後ろに付いて「受注入力 詳細設計書」のように表題とヘッダーに出るので、機能名は書かない（サンプルは `詳細設計書`） |
| `forbidden_terms_map.terms` | 設計書に出したくない語と、置き換える語の対応（サンプルは空） |

次のキーは、自分の案件が既定値と合わないときだけ足す。

| キー | 足す場合 |
|:---|:---|
| `backend_entry_patterns` | 入口ファイルのパスが `handler` を含まない場合（例: 入口が `app.py` なら `["app.py"]`） |
| `shared_module_patterns` | 共通モジュールの置き場所が `common/` でない場合 |

ほかのキー（ログ・認証の観測点など）は、`SKILL.md` の project_config の表を参照。
