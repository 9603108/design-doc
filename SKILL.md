---
name: design-doc
description: ソースコードから詳細設計書（.docx）を自動生成する。ディレクトリを指定すると、HTML単位で設計書を作成する。
---

# 詳細設計書生成スキル

## 対象範囲・前提条件・設定の書き方

design-doc = 詳細設計書、basic-design = 概要設計書（対になる別のスキル。混同しない）。

**対象範囲:**

- 対象は、素の JavaScript（バニラ JS）の画面（HTML）+ REST API の Web アプリと、React（TSX）の画面 + REST API の Web アプリ。Vue など、ほかのビルドを伴うフレームワークは範囲外
- 出力は日本語固定
- 入力の単位は HTML 1枚 = 設計書1つ。React（TSX）の画面では、画面コンポーネント（tsx）1つ + screen_config の `source_files`（画面を構成するソースのパスの配列）= 設計書1つ
- 画面は、共通の関数 `apiCall(...)` で API を呼ぶ形（例: `apiCall('POST', '/api/orders', { action: 'order_save', ... })`）を前提にしている。project_config の `api_call_functions` に呼出関数名を書けば、`call('<action>', ...)` のような関数ラッパーの呼出（action は第1引数の文字列リテラル）も拾える。バックエンドは、1つの入口ファイルが、受け取った `action` の値で処理を振り分ける REST の形を前提にしている。これ以外の書き方（URL のパスごとに処理を分ける API など）は、画面と API の呼出の突き合わせで検出されない
- React(TSX) の画面は、次のように設定を書く。(1) screen_config.json の `source_files` に、画面本体の tsx・API ラッパーの ts（例: `api.ts`）・CSS などのパスを書く（`${HOME}` と `~` を展開する。HTML を持たないので `source_html_path` は空でよい）。(2) project_config.json の `api_call_functions` に API 呼出の関数名（例: `call`）を書く。(3) 同じ action を複数のイベントから呼ぶ多対一は、Phase 1 で `db_operations[].api_endpoint` の中のキー `trigger_events`（event_code の配列）に書く（単数 `trigger_event` も従来どおり使える。5.7(d) の process_flows[] の旧キー `trigger_events` とは別物）。(4) useEffect 経由の暗黙の呼出の扱いは、02_イベント一覧.md と 04_機能別処理.md の React の節を見る。キーの詳細は後段の「画面別カスタマイズ」「project_config.json」の表を見る

**前提条件:**

| 要るもの | 補足 |
|:---|:---|
| Node.js 18 以上 + npm の `docx` パッケージ | `npm install -g docx`。実行時は `NODE_PATH=$(npm root -g)` を付ける |
| Python 3 | パイプラインのスクリプト（`target/scripts/`）を動かす |
| cairosvg + 日本語フォント（Noto Sans CJK JP など） | 構成図を PNG にするときだけ。無ければ PNG が作れず、処理構成図は主要な業務イベントの箇条書き、画面構成図は ASCII の図で代替される（構成図の章は省かれない） |
| diagram-design スキル（任意） | 無くてよい。構成図は `target/scripts/` の Python スクリプト（実行順序の Phase 5）が直接作る。導入されていれば代わりに使える（17_構成図生成.md） |

- 公式 docx スキルは不要。docx は `target/scripts/generate_docx.js` が `docx` パッケージで直接生成する
- スキル本体は特定の MCP・環境の道具に依存しない
- `SendUserFile` ツールがある環境では、完了時にそれで docx を送る。無い環境では、出力ファイルの絶対パスを報告する
- general-purpose サブエージェントがある環境では、HTML 単位で並列に処理できる（Phase 1 の成果を固定名の partial 4本で渡す場合は並列にできない。Step 3「Phase 1 の成果物の渡し方」を参照）。無い環境では、メインセッションで1画面ずつ順に処理する
- この文書のコマンド例は、カレントディレクトリがこのスキルのフォルダ（SKILL.md のある場所）である前提で書いている

**設定の書き方:**

設定ファイルは画面ごとに2つ。どちらも `target/intermediate/` に置く。

- `target/intermediate/{機能名}_screen_config.json` — 画面別の設定（パイプラインを実行する前に用意する）
- `target/intermediate/{機能名}_project_config.json` — 案件共通の設定

初めて使うときは、`examples/sample/` の設定2つ（`受注入力_screen_config.json` と `受注入力_project_config.json`）を `target/intermediate/` へコピーして書き換える。サンプルのソース（`examples/sample/src/`）を対象に動かすときは、設定をコピーしたうえで `FEATURE_NAME=受注入力` を指定する。ただし、設定を置いただけでは中身の無い骨組みの設計書になる。先に Phase 1（Step 3。ソースを読んで中間データ = partial 4本を作る作業）を済ませてから、実行順序のスクリプトを流す（`examples/sample/README.md` を参照）。

機能名は、全スクリプトが第1引数か環境変数 `FEATURE_NAME` で受け取る。既定の機能名は無い（無ければ「feature_name 未指定」で止まる）。

主なキー（全キーと使用スクリプトは、後段の「画面別カスタマイズ」「project_config.json」の節を参照）:

| ファイル | キー | 内容 | 既定値 |
|:---|:---|:---|:---|
| screen_config | `feature_name` / `screen_name` | 機能名 / 画面の正式名称。`screen_name` は「〜画面」で終わる正式名で、機能名 +「画面」と一致させる（例: 機能名 `受注入力` なら `受注入力画面`。機能名が既に「画面」を含むなら機能名そのまま）。`verify_intermediate.py` が、トリガーの発生場所の画面名をこの形と照合する | 必須 |
| screen_config | `source_html_path` | 解析対象 HTML のパス（`${HOME}` と `~` を展開する） | HTML の画面では必須。`source_files` を書く React(TSX) の画面では空でよい |
| screen_config | `source_files` | （任意）画面を構成するソースのパスの配列（tsx/ts/css など。`${HOME}` と `~` を展開する） | `[]` |
| screen_config | `project_root` | 案件のソースの最上位フォルダ。無ければ `common/` ディレクトリを手がかりに自動検出する | 自動検出 |
| screen_config | `backend_files` | バックエンドのソースのパスの配列 | `[]` |
| screen_config | `project_pattern` / `profile` | プロジェクト構成 / プロファイルの判定結果 | `""` |
| project_config | `forbidden_terms_map.terms` | {禁止する旧名: 置き換える業務語彙} の辞書 | 禁止語なし |
| project_config | `shared_module_patterns` | 共通モジュールとみなすパスの断片 | `["common/"]` |
| project_config | `backend_entry_patterns` | バックエンドの入口ファイルとみなすパスの断片 | `["handler"]` |
| project_config | `allowed_project_patterns` / `allowed_profiles` | project_pattern / profile の許容値 | 検査しない |
| project_config | `api_wrapper_classes` | API 呼出を包む共通部品のクラス名 | `[]` |
| project_config | `api_call_functions` | （任意）API 呼出の関数名の配列（例: `call`）。`call('<action>', ...)` 形の呼出の第1引数の文字列リテラルを action として拾う | `[]` |
| project_config | `verify_overrides.noise_words` | 呼出グラフの突き合わせで無視する一般語 | `[]` |
| project_config | `step_kind_keywords` | 処理ステップの分類（`branch` / `calculation`）に足す案件固有の語 | 空 |
| project_config | `logging.observation_points` | 観測点ログの観測点の定義。無ければ観測点ログのステップ挿入を省く | 定義なし |
| project_config | `docx_output.doc_title` / `font` / `font_size_pt` / `toc_depth` | 文書種別名 / 本文の字体 / 本文の大きさ / 目次の深さ | `"詳細設計書"` / `'Meiryo UI'` / `8` / `'1-3'` |
| project_config（手順書だけが参照） | `execution_locations` | 動作場所として書いてよい語の一覧 | `["ブラウザ", "サーバー(API)", "データベース", "定義のみ"]` |
| project_config（手順書だけが参照） | `naming.legacy_system` | 旧システム由来の命名規則。null なら適用しない | `null` |
| project_config（手順書だけが参照） | `logical_name_sources` | 論理名の根拠として使う資料の一覧 | `[{"rank": "A", "kind": "customer_docs", "paths": []}, {"rank": "B", "kind": "source_code"}]` |

設定を書くときの決まり（どれも、外すと検証で ERROR になるか、画面側とバックエンドの紐付けが抜ける）:

- **イベントの番号**: `process_names_by_event_code` と `trigger_dispatch` のキーは、Phase 1 の成果物の `events[].event_code` である（`EV00` が初期表示、以降は `EV01`、`EV02`… の連番）。Phase 1 の出力では events[] に必ず event_code を付ける。パイプラインの後半（Phase 5.85 の `migrate_v49_event_unify.py`）が、これを `no` / `legacy_event_code` の形へ移す。event_code を持たない形は、パイプラインを通った後の最終形である
- **処理の番号**: `trigger_dispatch` の `then_call` と `process_overview_overrides` のキーは、`migrate_v17_to_v18.py` が振る `F001` 形式の番号である。初期処理が `initial_processes.processes[].process_id`（1件目は `F001`）、それ以外は event_processes の並び順（= EV00 を除いた events の並び順）に `F002` から
- **概要の長さ**: 処理の概要（overview）は30字以内（25字が目安）で、物理名（英字の変数名・関数名）を含めない。30字を超えると `verify_intermediate.py` が ERROR にする。フロント処理の概要は `events[].content` の最初の句点までが入り、初期処理の概要は「名前。URLパラメータ条件: …」の形で自動生成されるので、長くなるときは `process_overview_overrides`（キーは `F001` などの処理の番号）で短い1文に上書きする。バックエンド処理の概要は `api_spec[].description` がそのまま入るので、Phase 1 で30字以内に書く
- **初期処理からバックエンドを呼ぶ step**: `initial_processes.processes[].steps[]` のうちバックエンドを呼ぶ step には、`"kind": "backend_call"` と `"backend_call": {"backend_id": "B001"}` を書く。これが画面側の処理とバックエンドの処理を結ぶ。`backend_id` は「B」+ 3桁の通し番号で、Phase 1 の `api_spec[]` の並び順に対応する（1件目が `B001`。action 名ではない）。Phase 5.8 で `BE1` の形に変換される
- **初期処理以外のイベントからバックエンドを呼ぶ step**: 設定には書かない。Phase 1 の `db_operations[].api_endpoint` の `action`（呼ぶ API）と `trigger_event`（呼ぶ側のイベントの event_code。多対一は `trigger_events`（event_code の配列）に書き、単数 `trigger_event` も従来どおり使える）、および `event_processes[].details` の呼出の行から、`migrate_v17_to_v18.py` が自動で `backend_call.backend_id` を補う（書き方は Step 3「Phase 1 の成果物の渡し方」）
- **`_meta.format_version`**: 利用者は書かなくてよい（未設定でも `verify_intermediate.py` は WARN にしない。docx には既定の版が出る）

## このスキルの位置付け

**目的:** 対象アプリの HTML（フロントエンド）と関連バックエンドを読み、詳細設計書を `.docx` 形式で自動生成する。出力先は `target/output/詳細設計書_{機能名}.docx`。

**意味合い:** ソース解析・中間データ化（Phase 1 の Agent と Python スクリプト群）と、docx 化（`target/scripts/generate_docx.js` が `docx` パッケージで直接生成）を分離した二段構成。中間JSON を境界にすることで、解析の規則と docx の体裁を別々に直せる。

**接続情報:**
- **docx 出力:** `target/scripts/generate_docx.js`（最終 .docx 化。手順と体裁は 16_docx出力ハンドオフ.md）
- **共通の規則:** 業務担当者の語彙、論理名変換、日付時刻フォーマット（YYYY/MM/DD HH:MM）、旧システムの物理名の禁止（禁止する語は project_config の `forbidden_terms_map.terms`）
- **データ契約:** 15_中間JSONスキーマ.md（中間データ形式）

## ディレクトリ構造

```
（このスキルのフォルダ）/               ← スキル本体（別プロジェクトへ丸ごとコピー可能）
├── SKILL.md                            ← スキル説明（本ファイル）
├── 01_画面レイアウト.md 〜 17_構成図生成.md  ← サブスキル（汎用、案件非依存）
├── id_scheme.json                      ← ID 体系規約（汎用、全プロジェクト共通）
├── examples/
│   └── sample/                         ← 設定の雛形と解析対象のソース（架空の受注入力画面）
│       ├── README.md                      ← サンプルの説明と使い方
│       ├── 受注入力_screen_config.json    ← 画面別の設定の雛形
│       ├── 受注入力_project_config.json   ← 案件共通の設定の雛形
│       ├── schema.sql                     ← テーブル定義（DDL）
│       └── src/                           ← 解析対象のソース（project_root として自動検出される）
│           ├── order_detail.html             ← 受注入力画面
│           ├── js/order_detail.js            ← 画面の処理
│           ├── common/js/api.js              ← 共通の apiCall の定義
│           ├── common/python/db_utils.py     ← DB 接続の共通モジュール
│           └── order-handler/handler.py      ← バックエンドの入口ファイル（action で処理を振り分ける）
└── target/
    ├── scripts/                        ← パイプラインスクリプト群（配布物。汎用、Python + Node.js、外部依存は docx パッケージ + cairosvg のみ）
    ├── intermediate/                   ← 中間データ（生成物。配布しない）
    │   ├── {機能名}.json                  ← 中間 JSON（パイプライン途中産物）
    │   ├── {機能名}_screen_config.json    ← 画面別ハードコード集約（案件 + 画面別、要作成）
    │   └── {機能名}_project_config.json   ← プロジェクト固有規約（ロギング/監査/認証、案件別、要作成）
    ├── diagrams/                       ← 構成図 HTML/SVG/PNG（生成物。配布しない）
    └── output/                         ← 最終 docx 出力（生成物。配布しない）
```

`target/intermediate/`・`target/output/`・`target/diagrams/` は生成物の置き場で、配布物には含まれない（無ければ作る）。

**別プロジェクトでの再利用手順:**

1. `examples/sample/` の設定2つ（`受注入力_screen_config.json` / `受注入力_project_config.json`）を `target/intermediate/` へコピーし、ファイル名の `受注入力` を対象の機能名に変えて、対象の画面に合わせて書き換える（サンプルのソースを対象に動かすときは、ファイル名を変えずに `FEATURE_NAME=受注入力` を指定する）:
   - `target/intermediate/{機能名}_screen_config.json` — 画面別ハードコード（source_html_path / areas / process_overview_overrides / trigger_dispatch / initial_processes 等）
   - `target/intermediate/{機能名}_project_config.json` — プロジェクト固有規約（logging.observation_points / audit_logging / auth / forbidden_terms_map 等）
2. Phase 1（Step 3）を行う。対象のソースを読み、中間データ（partial 4本）を作る。設定を置いただけでパイプラインを流すと、partial が無いので中身の無い骨組みの設計書になる
3. `FEATURE_NAME` を指定してパイプラインを実行する（後段の「実行順序」）
4. `target/output/詳細設計書_{機能名}.docx` が出力される

**スキル本体は特定の MCP・環境の道具に依存しない**。標準 Python + Node.js（docx パッケージ + cairosvg）のみで動作する。

## 使い方

```
/design-doc <ディレクトリパス>              # 全HTMLファイルの設計書を生成
/design-doc <ディレクトリパス> <ファイル名>   # 特定HTMLファイルのみ
/design-doc <ディレクトリパス> --profile report  # プロファイル手動指定
```

## 実行手順

### Step 0: 環境チェック + ディレクトリ走査

**環境チェック（実行開始時に一度だけ）:**

| 項目 | 確認コマンド | 不足時の対処 |
|:---|:---|:---|
| Node.js | `node --version` | 18以上必須。nvm 等で導入 |
| npm | `npm --version` | Node.js 同梱 |
| `docx` パッケージ（global） | `npm list -g docx` | `npm install -g docx` を一度だけ実行（実行時は `NODE_PATH=$(npm root -g)` を付ける） |
| Python 3 | `python3 --version` | 導入する |
| cairosvg（構成図を付けるときだけ） | `python3 -c "import cairosvg"` | `pip install cairosvg`。無ければ PNG が作れず、処理構成図は主要な業務イベントの箇条書き、画面構成図は ASCII の図で代替される |
| 設定2ファイル | `target/intermediate/{機能名}_screen_config.json` / `{機能名}_project_config.json` があること | `examples/sample/` の `受注入力_screen_config.json` / `受注入力_project_config.json` を `target/intermediate/` へコピーして書き換える |

**ディレクトリ走査:**

1. 指定ディレクトリ内のファイルを走査する
2. HTMLファイルを列挙 → 各HTMLが 1機能 = 1設計書の単位（React(TSX) の画面は、画面コンポーネント(tsx)1つが単位。`source_files` を持つ screen_config から決まる）
3. 引数でファイル名が指定されている場合はそのファイルのみ対象
4. `target/intermediate/` と `target/output/` が存在することを確認（なければ作成）

### Step 1: プロジェクト構成の判定

以下のパターンを自動判定する。判定結果は中間JSONの `meta.project_pattern` に格納:

| パターン | 判定基準 | バックエンド追跡 |
|:---|:---|:---|
| **フロント単体型** | HTMLファイルのfetch/axios先が外部URL（バックエンドのソースが手元に無い） | API仕様は対象外（エンドポイント一覧のみ記載） |
| **バックエンド統合型** | 同一ディレクトリにバックエンドのソース（Pythonファイル等）があり、HTML内にAPIパスが含まれる | バックエンドのソース内のSQL/ORMを追跡 |
| **フロント+バックエンド分離型** | 別ディレクトリにバックエンドコードがある | ユーザーにバックエンドのパスを確認 |

上の3つは汎用の呼び名である。`meta.project_pattern` に書いてよい値は、project_config の `allowed_project_patterns` で案件ごとに決められる（キーが無ければ検査しない。Step 2 のプロファイルの `allowed_profiles` も同じ）。判定結果は screen_config の `project_pattern` / `profile` に書いておく（`merge_partials.py` が中間JSON の meta へ写す）。

### Step 2: プロファイル判定

ソースコードの特徴からアプリケーションプロファイルを判定する。判定結果は中間JSONの `meta.profile` に格納:

| プロファイル | 判定基準 |
|:---|:---|
| **CRUD型** | フォーム入力 + POST/PUT/DELETE API呼出しあり |
| **レポート型** | GET API呼出しのみ + テーブル表示中心 + データ変更なし |
| **ハイブリッド型** | 上記の両方の特徴を持つ |

`--profile` オプションで手動指定も可能。

### Step 3: HTML 単位の並列処理

**並列の単位:**

- 複数HTML処理時: **HTML単位でAgent並列起動** が標準（Phase 1 の成果を固定名の partial 4本で渡す場合は、画面どうしで partial が衝突するので並列にしない。後述「Phase 1 の成果物の渡し方」）
- 各エージェント内部の Wave 1〜4 は **順次実行**（並列にしない）
- 単一HTMLの場合は Agent サブエージェント不要、メインセッションで順次実行
- general-purpose サブエージェントが無い環境では、複数HTMLでもメインセッションで1画面ずつ順に処理する（下のプロンプトの手順を、そのままメインセッションで実行する）

**Agent 並列起動の例（複数HTMLの場合）:**

各HTMLについて、以下のプロンプトでサブエージェント（general-purpose）を起動する。複数同時起動可。

```
Agent({
  description: "{機能名} 設計書中間JSON生成",
  prompt: "対象HTML: {絶対パス}\n本タスクは design-doc スキルの内部処理として、{機能名} の中間JSON を生成し target/intermediate/{機能名}.json に保存することです。\n手順:\n1. このスキルのフォルダの 15_中間JSONスキーマ.md を読んでデータ形式を把握（v49 拡張仕様: response_spec.items[].source/transform_backend、front_processes[].steps[].response_mapping）\n2. **非機能横断要素の抽出仕様確認**: target/intermediate/{機能名}_project_config.json があれば logging.observation_points[].trigger_function / audit_logging.trigger_function / auth.claims を必ず読み、ソース内の該当呼出を網羅抽出する対象に含める\n3. 以下のサブスキルを順次実行: 10_論理名変換.md → 01_画面レイアウト.md → 02_イベント一覧.md → 05_DB操作.md → 06_パラメータ.md → 07_バリデーション.md → 03_初期処理.md → 04_機能別処理.md → 12_API仕様.md (SPA時のみ) → 13_共通ロジック.md (該当時のみ)\n4. **呼出グラフ・観測点ログ・監査ログを必ず抽出**:\n   (a) フロント JS の apiCall('METHOD','/path',{action:'xxx',...}) 形式の呼出を網羅。各呼出について、呼ぶ側のイベントの event_code と action を、その API が実行する db_operations[].api_endpoint の {action, trigger_event} に書く（trigger_event = 呼ぶ側の events[].event_code）。同じ action を複数イベントから呼ぶ多対一は api_endpoint.trigger_events（event_code の配列）で書く。単数 trigger_event も従来どおり。初期表示（EV00）からの呼出は、screen_config の initial_processes.processes[].steps[] の backend_call.backend_id（B001 形式 = api_spec[] の並び順の通し番号）で結ぶ。front_processes.steps[].backend_call は Phase 4 のスクリプトがここから作るので、直接は書かない（書いても残らない）\n   (b) バックエンドの action 分岐（if action == 'xxx': / elif action == 'xxx': や、action 名をキーにした辞書ディスパッチ）を網羅。api_spec[].action と一致させる（backend_processes と called_by_front_processes[] は後段のスクリプトが作る）\n   (c) project_config.logging.observation_points[].trigger_function（project_config に書かれた関数名。案件ごとに違う）をフロント JS / バックエンドのソースから grep し、呼出箇所を把握する（中間 JSON の logging_calls[] と観測点ログの step は Phase 5.6 / 5.7 のスクリプトが作るので、Agent は書かない）。観測点は project_config の logging.observation_points に定義したもの（数は案件による）。定義が無ければ (c) と、観測点ログのステップ挿入（Phase 5.7 / apply_logging_steps.py）は省く\n   (d) audit_logging.trigger_function (例: write_audit_log) の呼出の有無を確認する（中間 JSON への記録は Phase 5.6 の build_call_graph.py が行うので、Agent は書かない）\n   (e) 認証 (auth.claims) を参照する箇所も同様\n5. **v49 必須: バック処理レスポンスのデータ起源と加工内容を抽出**（書く場所は api_spec[]。backend_processes は Phase 4 のスクリプトが api_spec[] と db_operations[] から作り直すので、backend_processes へ直接書いた内容は残らない）:\n   (a) `api_spec[].response_items[].source`（Phase 4 で backend_processes[].response_spec.items[].source になる）を { type: 'request'|'db'|'calculated'|'merged'|'const', source_ref: '②-X データセット名', source_column: 'テーブル.列名' } 形式で抽出。JSON キーの各値が「リクエストパラメータ透過 / DB SELECT 直結 / Python 計算 / 複数 SELECT 統合 / 固定値」のどれかを判定\n   (b) `transform_backend` にバック側 Python での加工内容を業務語彙で記述（例: \\\"date → 'YYYY/MM/DD' 書式変換\\\"、\\\"数量 × 単価 × 税率 ÷ 100（小数切り捨て）\\\"、\\\"区分単位で辞書化、明細配列にネスト\\\"）。何も加工しない場合は \\\"そのまま\\\" を明示\n   (c) 画面項目マッピングは `api_spec[].response_to_screen_mapping[]` に [{ api_field_path, screen_area_no, screen_item_name, transform }] の形で書く。Phase 5.9 の migrate_v48_to_v49.py が、その API を呼ぶ backend_call の step があるフロント処理へ、kind='response_mapping' の step として移す（item_ref_no もそのとき screen_layout.items[] から逆引きで付く）。screen_item_name は `screen_layout.items[].item_name` と一致させること。front_processes[].steps[] へ response_mapping の step を直接書いても、Phase 4 で消える\n   (d) `db_op_detail.sub_no`（②-1, ②-2 の番号）は Phase 5.9 のスクリプトが採番するので書かない。【②DB問合せ】に出す DB 操作は、`api_spec[].related_db_operations[]` に db_operations[].id を実行順に挙げる（1件も無い API は、業務固有の step が無いとして verify_intermediate.py が ERROR にする）\n5.55. **events[] と event_processes[] の対応（Phase 1 の出力では event_code で結ぶ）**:\n   (a) **event_code**: events[] の全件に `event_code` を付ける。初期表示が `EV00`、以降は `EV01`、`EV02`… の連番（2桁以上のゼロ埋め）。screen_config の process_names_by_event_code / trigger_dispatch のキーと、db_operations[].api_endpoint.trigger_event は、この値でイベントを指す。同じ action を複数イベントから呼ぶ多対一は api_endpoint.trigger_events（この値の配列）で書く（単数 trigger_event も従来どおり）。`event_ref` と、events[].no の `EV1` 形式は、パイプラインの後半（Phase 5.8 / 5.85）が作る最終形なので、Phase 1 では書かない。\n   (b) **event_processes[]**: EV00 以外の events[] 1件につき1件を、events[] と同じ並び順で持たせる（この並び順がフロント処理の番号 F002、F003… になる）。partial で渡す場合は merge_partials.py が events[] から作るので Agent は作らず、merge の後に details だけを書く。{機能名}.json を直接保存する場合は Agent が作る。\n   (c) **構造**:\n      ```json\n      {\n        \"event_code\": \"EV05\",\n        \"pattern\": \"（未分類）\",\n        \"overview\": \"（events[].content と同じ文。最初の1文は業務担当者語彙で 30 字以内。例: 「商品コードのフォーカス外しでマスタ照合する」）\",\n        \"details\": [\"（処理ステップを1行1 step で書く）\"],\n        \"db_operations_ref\": [],\n        \"parameter_refs\": []\n      }\n      ```\n   (d) **overview**: 最初の句点までが、フロント処理の概要として docx に出る。30 字を超えると verify_intermediate.py が ERROR にする（超えるときは screen_config の process_overview_overrides で上書きする）。実装語彙（関数名 / camelCase / blur / focus 等）は禁止、業務担当者の操作語彙（クリック / 押下 / 変更 / フォーカス外し）に変換する。\n   (e) **details**: これがフロント処理のステップの実体になる（空のままだと、overview を句点で区切った文がステップになる）。バックエンドを呼ぶ行にだけ「呼出」「API」「リクエスト」のいずれかの語を入れる（Phase 4 のスクリプトがその行を backend_call の step と判定し、db_operations[].api_endpoint の {action, trigger_event} から backend_id を補う。api_endpoint.trigger_events に挙げた各イベントでも同様に補う）。\n   (f) **書かないもの**: front_processes[] / trigger_groups[] / called_by_triggers は、Phase 4 の migrate_v17_to_v18.py が events[] と event_processes[] から作るので書かない（書いても残らない）。対応する event_processes[] が無い events[] が残った場合は、Phase 4.5 の _fill_event_processes.py が仮埋め（overview 1行、details は空）を足すが、Phase 4 の後に足されるのでフロント処理にはならない。Phase 1 で漏れなく揃える。\n\n5.5. **v50.5 必須: バックエンド処理の材料（api_spec / db_operations）と validations / calculations / common_logic / messages / parameters の「中身」を実コードから抽出（key だけ作って value 空のスケルトン禁止）**。backend_processes は Phase 4 のスクリプトが api_spec[] と db_operations[] から作り直すので、(a)〜(f) は次の入口へ書く（backend_processes へ直接書いた内容は残らない）:\n   (a) **DB 操作・キャッシュ操作（→ backend_processes[].processing の step）**: バックエンドのソースを API（action）単位で実コード読みし、実行する DB 操作を db_operations[] に書いて、その id を `api_spec[].related_db_operations[]` に実行順に挙げる。キャッシュ操作は `api_spec[].related_cache_operations[]` に {operation, key_pattern, ttl_sec, purpose} で書く。どちらも1件も無い API は、verify_intermediate.py が「業務固有の step が無い」ERROR にする。DB 操作・キャッシュ操作以外の業務 step（計算、外部サービスの呼出 など）は、今は Phase 1 の成果から docx へ届かない（要るときは、Phase 4 の後に中間 JSON の backend_processes[].processing[] へ書く）。\n   (b) DB 操作の dataset_name、キャッシュ操作の purpose は、業務担当者語彙で「何をするか」を書く（例: '受注明細データを受注月＋商品コードで検索'、'排他制御用のロックを 30 秒の有効期限で取得'）。実コードを読まずにテンプレ文言を使うのは禁止。\n   (c) **api_spec[].description（→ backend_processes[].overview）**: 実コードを読んで「この API が業務的に何を達成するか」を 30 字以内の 1 文で記述。空文字は ERROR、30 字超も ERROR。\n   (d) **api_spec[].request_params（→ backend_processes[].request_spec.params）**: バックエンドがリクエストからパラメータを取り出している箇所（リクエスト本文の action や各パラメータを読む箇所。書き方は案件のバックエンドの作りによる）を実コードから全件列挙、{name, type, required, description} 4 フィールドを埋める。\n   (e) **api_spec[].response_items（→ backend_processes[].response_spec.items）**: return 文の dict / JSON 構造から全 key を列挙、{no, key, type, source, transform_backend, description} を埋める。\n   (f) **エラーパターン**: backend_processes[].error_patterns は、今は Phase 1 の成果から docx へ届かない（Phase 4 のスクリプトが空で作る。空でも ERROR にはならず WARN）。raise / return error の各箇所のメッセージは messages[] に漏れなく挙げる。エラーパターンを docx に出すときは、Phase 4 の後に中間 JSON の backend_processes[].error_patterns[] へ {condition, status_code, message_id} を書く（docx の表は 発生条件／ステータス／メッセージID の3列で、ステータス列は status_code を読む。business_reason は書いても docx の表には出ない）。\n   (g) **validations[]**: {field, rule, applied_when, message_id} を実コード（HTML の data-validation 属性、JS のチェック関数、バックエンド側の validation 関数）から抽出。field / rule / applied_when が全て空のエントリ作成は禁止。\n   (h) **calculations[]**: {name, formula, inputs[], outputs[], remarks} を実コード（JS の calculation engine、バックエンド側の計算関数）から抽出。name と formula が両方空のエントリ作成は禁止。\n   (i) **common_logic[]**: {name, description, used_by_processes[]} を実コードから抽出。name と description が両方空のエントリ作成は禁止。\n   (j) **messages[]**: {message_id, code, message, severity, emitted_by_api[]} を実コード（messages.json / メッセージ定義モジュール）から抽出。message 本文が空のエントリ作成は禁止。\n   (k) **parameters[]**: {name, type, passed_between[], description} を実コードから抽出。name と description が両方空のエントリ作成は禁止。\n   (l) verify_intermediate.py の check_backend_process_substance / check_section_substance が空テンプレを検出する（v50.5 で追加）。バックエンド処理は、概要（= api_spec[].description）が空のとき、または業務固有の step（= related_db_operations / related_cache_operations から作られる step）が1件も無いときに ERROR。request_params / response_items / error_patterns が空のときは WARN。(g)〜(k) は、禁止と書いた空のエントリが ERROR。\n5.6. **v51 必須: 設計書フォーマット憲法準拠**（このスキルのフォルダの `16_docx出力ハンドオフ.md` 全文を読んで遵守）:\n   (a) 章構成: §1 文書情報 / §2 構成図 / §3 画面レイアウト / §4 トリガー / §5 処理詳細 / §6 メッセージ。§7 以降は将来拡張。\n   (b) 全処理セクション（FR/BE/VL/CA/CL/LG/MS/DB）は共通 7 項目で記述: 概要 / 動作場所 / 呼出元 / 呼出先 / 入力 / 処理 / 出力 / エラー。該当しない項目は「（該当なし）」明記、空白禁止。\n   (c) 入出力表共通列: No / 種別 / 項目・名称 / 型 / 用途。BE 出力のみ JSON 構造インデント + 項番 + ソース + 変換 の固有フォーマット。\n   (d) 接頭辞ルール: 2 文字 = グローバル参照（FR1, BE1, VL1, CA1, CL1, LG1, MS1, DB1, AR1, IT1, EV1, TR1）/ 1 文字 = ローカル（E1, E2 …エラー）/ 接頭辞無し = 表内連番。\n   (e) Step 種別は動詞付き ID 名併記（例: `[LG3] バック←受信記録 を呼出`）。エラーチェックは「判定」種別で書き、ローカル [E#] と紐付け。\n   (f) DB セクションは §5.6 で独立。SQL は構造化表（取得テーブルと結合 / 絞込条件 / SELECT 項目「集約・関数・式」列 / グルーピング / 並び順）で記述、生 SQL 文字列は禁止。\n   (g) 物理名・実装名禁止: 旧システムの物理名（project_config の forbidden_terms_map.terms を参照。そこに書かれた語）/ DB 物理列名（unit_price/order_month 等）/ JS 関数名（onNewProductCodeBlur 等）/ CSS クラス名 は出力に含めない。業務語彙化必須。API キー（JSON パス）はそのまま残す。\n   (h) 件数表記禁止: 章見出しに「（N 件）」を付けない。\n   (i) 動作場所: 動作場所は project_config の execution_locations に定義した語だけを使う。既定は ブラウザ / サーバー(API) / データベース / 定義のみ。辞書に無い語を動作場所に書かない。\n   (j) 揺れ防止辞書（許容値のみ使用、辞書外の表記禁止）: Step 種別 / 入力種別 / 出力種別 / 結合種別 / 集約・関数・式 / 演算子 / 並び順方向 / 型 は `16_docx出力ハンドオフ.md §9` に定義。\n   (k) CL は **システム全体スコープ** で使われるロジックに限定。当該画面のみで使われるロジックは CA に統合。\n   (l) 責務分離: 呼ばれる側は呼出元の業務文脈情報を持たない。BE 概要に「当該画面の初期表示の起点」のような呼出元視点記述は禁止。\n5.7. **v50 Phase 2 必須: 参照フィールド命名規約 {category}_ref / {category}_refs**:\n   (a) 単数参照は `{category}_ref`、複数参照（配列）は `{category}_refs`。category は対応する ID カテゴリ名（front_process なら front、backend_process なら backend、logging なら logging 等）。\n   (b) 新名のみで出力（旧名を使わない）: `front_refs`（配列、FR#）/ `logging_ref`（単数、LG#、kind='logging_call' step 内）/ `db_op_refs`（配列、DB#、process_flows.steps 内）。\n   (c) 旧名は出力禁止: `calls` / `call_id` / `db_op_ref`（後段に rename するスクリプトは無い。Agent 側で最初から新名を出す）。**例外**: `backend_call` / `response_mapping` の `backend_id` と、`response_mapping.mappings[]` の `item_ref_no` は、15_中間JSONスキーマ.md のとおり、この名前のまま使う（screen_config の initial_processes の step に書く backend_call.backend_id も同じ。migrate_v17_to_v18.py と migrate_v48_to_v49.py がこの名前だけを読み書きするため。新名の `backend_ref` / `item_ref` は verify_intermediate.py / verify_call_graph.py / generate_docx.js なら読めるが、この2本は読まないので紐付けが抜ける）。\n   (d) 維持される命名（rename 対象外）: `area_ref` / `api_refs` / `validation_refs` / `calculation_refs` / `common_logic_refs` / `message_ref` / `trigger_events`（process_flows[] の旧キー）/ `called_by_front` / `db_operations_ref` / `parent_call_id` / `then_call`。**イベントを指すキーは、Phase 1 の出力では `event_code`**（events[].event_code と同じ値を、screen_layout.items[].event_code / event_processes[].event_code / db_operations[].api_endpoint.trigger_event に書く）。api_endpoint の中の `trigger_events`（多対一で使う event_code の配列。Phase 1 の値のまま変換されない）は、上の process_flows[] の旧キー `trigger_events` とは別物である。`event_ref` は、Phase 5.85 の migrate_v49_event_unify.py が event_code から作る最終形の名前なので、Phase 1 では書かない。\n   (e) 適用例: `{\"kind\": \"backend_call\", \"backend_call\": {\"backend_id\": \"B001\", \"request_summary\": \"受注月\"}}`（screen_config の initial_processes の step。backend_id は api_spec[] の並び順の B001 形式で書き、Phase 5.8 で BE1 の形に変換される）、`{\"kind\": \"logging_call\", \"logging_ref\": \"LG3\", \"observation_no\": 3}`。\n6. **v42 設計徹底: 初期処理 FRX の steps（screen_config の initial_processes.processes[].steps[]）に「URL パラメータを解析して mode/id を確認する」等の判定 step を書かない**。URL パラメータ判定は「4 トリガー」の章の自動トリガー（画面表示時）の dispatch (trigger_dispatch.EV00) で完結させ、呼ばれる側 FRX は「呼ばれたら無条件で実行する純粋処理」とする\n7. 各サブスキルの出力を中間JSON の対応キーに集約。**docx へ届く入口は events / event_processes（details を含む）/ screen_layout / api_spec / db_operations / validations / calculations / common_logic / messages / parameters**。trigger_groups / front_processes / backend_processes は Phase 4 のスクリプトがこれらから作り直すので、直接は書かない（SKILL.md「Phase 1 の成果物の渡し方」の対応表を参照）\n8. 14_チェックリスト.md の整合性ルールで自己検証、問題があれば再生成。**特に「呼ばれていない F00X / B00X」「呼んでるのに存在しない処理参照」「project_config に定義された trigger_function なのに抽出ゼロ」はゼロ件を目標**\n9. target/intermediate/{機能名}.json に保存して完了報告\n共通の規則: 業務担当者の語彙、論理名変換、日付時刻フォーマット(YYYY/MM/DD HH:MM)。旧システムの物理名（project_config の forbidden_terms_map.terms に書かれた語）は出力に含めない。**非機能横断要素（ログ・監査・認証）は業務処理 step に直書きせず、project_config に定義された共通処理として参照する**"
})
```

**Phase 1 の成果物の渡し方（実行順序へ進む前に必ず確認する）:**

後段のスクリプトは、Phase 1 の成果物を次の形で受け取る。上のプロンプトは `target/intermediate/{機能名}.json` へ直接保存するが、そのまま実行順序の Phase 2 から流すと成果が上書きされるので、どちらの形で渡すかを先に決める。

- `merge_partials.py`（Phase 2 の先頭）は、固定名の partial 4本を読んで `target/intermediate/{機能名}.json` を新しく書き出す。同名のファイルが既にあれば上書きする。partial が無いときは警告を出すだけで空として続けるので、**Agent が `{機能名}.json` を直接保存した後に実行すると、その内容は空に近い中間JSON で置き換わる**
- `migrate_v17_to_v18.py`（Phase 4）は、`events` / `event_processes` / `initialization` / `api_spec` / `db_operations` から `trigger_groups` / `front_processes` / `backend_processes` を作り直して上書きする（v17 のキーは残す）。この3キーを Phase 4 より前に書いても、その内容は残らない

| partial（`target/intermediate/` 直下。機能名に依らない固定名） | `merge_partials.py` が読むキー → 中間JSON のキー |
|:---|:---|
| `_partial_screen_events.json` | `screen_layout` / `events` |
| `_partial_calc_state.json` | `calculations` / `validations_frontend` → `validations` / `common_logic` |
| `_partial_backend.json` | `logical_names` / `db_operations` / `api_spec` / `messages_backend` → `messages` / `parameters_backend` → `parameters` |
| `_partial_peripheral.json` | `events_peripheral` → `events` の末尾に連結 / `common_logic_peripheral` → `common_logic` の末尾に連結 |

各キーの中身の形は、15_中間JSONスキーマ.md の対応する節（`events` / `validations` / `messages` など）に従う。`initialization` と `event_processes` は、`merge_partials.py` が `events` と `db_operations` から最小限の内容で作る（`event_processes[].details` は空で作られる。partial に details を渡すキーは無い）。

**Phase 1 で書いた内容が docx のどこへ届くか（担当が抽出した内容を届ける経路は、この表の1本だけ）:**

| 書く場所 | 書く内容 | 届く先（docx の章） |
|:---|:---|:---|
| partial の `screen_layout` | 画面項目（`items[]`）。エリア（`areas[]`）は screen_config の `areas[]` から `add_screen_areas.py` が付ける | 3 画面レイアウト |
| partial の `events[]` / `events_peripheral[]` | `event_code`（`EV00` = 初期表示、以降 `EV01`… の連番。必ず付ける）/ `trigger`（「{画面名}の…」の型の1文。{画面名} は screen_config の `screen_name`）/ `content`（処理の内容。最初の1文は30字以内）/ `classification`。`no` は `merge_partials.py` が 1 からの通し番号に振り直し、`location` / `action` / `source_kind` は `convert_events_location_action.py` が `trigger` から付ける | `trigger` → 4 トリガー。`content` → `event_processes[].overview` → 5.1 フロント処理の概要（最初の句点まで。`process_overview_overrides` があればそちら）。フロント処理の名前は screen_config の `process_names_by_event_code`。`EV00` の event はフロント処理にならず、初期処理は screen_config の `initial_processes` から作られる |
| 中間 JSON の `event_processes[].details`（**`merge_partials.py` の後、Phase 4 の前に書く**） | 処理ステップを1行1 step の文字列の配列で書く。これが 5.1 フロント処理のステップの実体になる。空のままだと、概要を「。」で区切った文がステップになる。ステップの種別は文の語から推定される（下の「details の書き方」） | 5.1 フロント処理の各処理のステップ表 |
| partial の `api_spec[]` | `action` / `method` / `path` / `description`（概要。30字以内。空だと ERROR）/ `request_params[]` / `related_db_operations[]`（この API が実行する `db_operations[].id` の配列。**1件も無いと「業務固有の step が無い」ERROR になる**）/ `related_cache_operations[]` / `response_items[]`（`source` / `transform_backend` を含む）/ `response_to_screen_mapping[]`。並び順が `B001`、`B002`… の番号になる | 5.2 バックエンド処理（【①リクエスト受信】【②DB問合せ】【③JSONレスポンス】）。`response_to_screen_mapping` は、その API を呼ぶ `backend_call` の step があるフロント処理にだけ、画面項目マッピング（`response_mapping` の step）として届く（Phase 5.9） |
| partial の `db_operations[]` | `id` / `operation_type` / `dataset_name` / `dataset` / `joins` / `where` / `order_by` など。`api_endpoint` に `{action, trigger_event}`（`action` = この DB 操作を実行する API、`trigger_event` = その API を呼ぶ画面側のイベントの event_code。多対一は `api_endpoint.trigger_events`（event_code の配列）、単数 `trigger_event` も従来どおり）を書く | 5.2 バックエンド処理の【②DB問合せ】（`api_spec[].related_db_operations` に id を挙げたもの）。`api_endpoint` は、初期処理以外のイベントのフロント処理とバックエンド処理の紐付けに使われる |
| partial の `validations_frontend[]` | `{no, field, rule, applied_when, message_id}` | 5.3 バリデーション |
| partial の `calculations[]` | `{no, name, formula, inputs[{name, source}], outputs[{name, destination}], remarks}` | 5.4 計算式 |
| partial の `common_logic[]` / `common_logic_peripheral[]` | `{name, description, …}`（13_共通ロジック.md） | 5.5 共通ロジック（1件以上あるときだけ章が出る） |
| partial の `messages_backend[]` | `{message_id, code, severity, message, emitted_by_api[]}` | 6 メッセージ一覧 |
| partial の `parameters_backend` | 配列 `[{no, name, type, passed_between[], description}]` | 中間 JSON の `parameters`（docx に独立した章は出ない。`verify_intermediate.py` が中身の有無を見る） |
| partial の `logical_names[]` | 物理名と論理名の対応 | 章は出ない。Phase 5.5 の `apply_logical_name_substitution.py` が本文の物理名を「論理名[物理名]」へ置き換えるのに使う |

**details の書き方（`event_processes[].details`）:**

- 1行に1つの処理を、業務担当者の語彙で書く。行頭の「・」「-」「*」は取り除かれる
- バックエンドを呼ぶ行にだけ、「呼出」「呼び出し」「API」「リクエスト」のいずれかの語を入れる（例: 「顧客情報の取得 API を呼出」）。`migrate_v17_to_v18.py` がその行を `backend_call` の step と判定し、`db_operations[].api_endpoint` の `trigger_event`（多対一なら `trigger_events` の配列の要素。単数 `trigger_event` も従来どおり）がそのイベントの event_code と一致する `action` の `backend_id` を、step の並び順に補う。1つのイベントが複数の API を呼ぶときは、`db_operations` の並び順と、呼出の行の並び順を合わせる
- バックエンドを呼ばない行には、上の語を書かない（呼出と判定されて、割り当てが1つずれる）

**Phase 1 で書いても docx に届かないもの（書く場所が別にある）:**

| 項目 | 実際の入口 |
|:---|:---|
| `trigger_groups` / `front_processes` / `backend_processes` を Phase 4 より前に書いた内容の全部 | 届かない。Phase 4 の `migrate_v17_to_v18.py` が上の表のキーから毎回作り直す。書くなら上の表の入口へ書く |
| フロント処理のステップ（`front_processes[].steps[]`） | `event_processes[].details`（上の表）。初期処理は screen_config の `initial_processes.processes[].steps[]` |
| フロント処理の step の `backend_call.backend_id` | 初期処理: 設定に書く（`initial_processes.processes[].steps[]` の `backend_call.backend_id`）。それ以外のイベント: Phase 4 のスクリプトが作る（`db_operations[].api_endpoint` と details の呼出の行から） |
| `response_mapping` の step（画面項目マッピング） | Phase 5.9 の `migrate_v48_to_v49.py` が、`api_spec[].response_to_screen_mapping` から作る |
| `backend_processes[].called_by_front` / `called_by_front_processes[]` | Phase 4 のスクリプト（`backend_call.backend_id` からの逆引き）と、Phase 5.6 の `build_call_graph.py` が作る |
| `logging_calls[]` / `audit_calls[]` / 観測点ログの step | Phase 5.6 の `build_call_graph.py` と Phase 5.7 の `apply_logging_steps.py` が作る（project_config に定義があるとき） |
| `backend_processes[].processing[]` のうち、DB 操作・キャッシュ操作以外の業務 step（計算など） | 今は partial からは届かない。要るときは Phase 4 の後に中間 JSON の `backend_processes[].processing[]` へ書く |
| `backend_processes[].error_patterns[]` | 今は partial からも設定からも届かない（Phase 4 は空で作る）。要るときは Phase 4 の後に中間 JSON へ `{condition, status_code, message_id}` を書く（docx の表は 発生条件／ステータス／メッセージID の3列で、ステータス列は `status_code` を読む。`business_reason` は書いても docx の表には出ない）。空のままでも ERROR にはならない（`verify_intermediate.py` は WARN を出す） |
| docx の「5.6 DB 操作」の章 | 今は Phase 1 の `db_operations` の形からは届かない。この章は `summary` / `business_title` / `runtime_location` / `used_by_api` / `operation` を読み、全件がどれも持たなければ章ごと省かれる。DB 操作の内容は 5.2 の【②DB問合せ】に出る |

Phase 4 の後に中間 JSON へ手で書いた内容は、Phase 4（`migrate_v17_to_v18.py`）をやり直すと消える。やり直す前に中間 JSON の控えを取る。

渡し方:

- **partial 4本で渡す場合（実行順序の Phase 2 からそのまま流す）**: 上のプロンプトの手順7・9の保存先を、最初の表の partial とキーに読み替える。`event_processes[].details` は partial に入口が無いので、`merge_partials.py` の後、Phase 4 の前に `{機能名}.json` へ書く。partial は画面どうしで共有されるので、複数画面を同時に処理しない（1画面ぶんの partial 4本を作り、その画面の `merge_partials.py` を済ませてから、次の画面の Phase 1 に進む）
- **`{機能名}.json` を直接保存した場合**: `merge_partials.py` は実行しない（実行順序は次の `convert_events_location_action.py` から始める）。このとき meta の `backend_files` / `project_pattern` / `profile` は screen_config から写されないので、15_中間JSONスキーマ.md の meta のとおりに Agent が書く。`events` / `event_processes`（`event_code` と `details` を含む）/ `initialization` / `api_spec` / `db_operations` も、上の表の形で Agent が書く（Phase 4 が読むのはこれらのキー）
- どちらの場合も、`trigger_groups` / `front_processes` / `backend_processes` は Phase 4 で作り直される。直接保存でも同じで、この3キーに書いた内容は残らない

**抽出対象の網羅範囲（v41 で仕様化、Critical）:**

| カテゴリ | 抽出対象 | 中間 JSON 反映先 | 検証スクリプト |
|:---|:---|:---|:---|
| 業務的構造 | 画面項目 / トリガー / 処理フロー / DB操作 | screen_layout / trigger_groups / front_processes / backend_processes | verify_intermediate.py |
| **API 呼出グラフ** | apiCall(...) / action ディスパッチ | steps[].backend_call.backend_id / backend_processes.called_by_front_processes[] | **verify_call_graph.py** |
| **観測点ログ** | project_config.logging.observation_points[].trigger_function | steps[].logging_calls[] / processing[].logging_calls[] | **verify_call_graph.py** |
| **監査ログ** | project_config.audit_logging.trigger_function | front_processes[].audit_calls[] / backend_processes[].audit_calls[] | **verify_call_graph.py** |
| **認証** | project_config.auth.claims | （初期処理のみ。案件の、認証情報を受け取る共通処理経由） | verify_intermediate.py |

非機能横断要素を「業務処理 step に直書きしない」設計の根拠: project_config の `logging.observation_points` に定義した観測点（数は案件による）を、独立した処理 [LN]（サブ観測点は [LNa] 等）として docx の「5.7 観測点ログ処理（共通）」に集約。定義が無ければ、観測点ログのステップ挿入（Phase 5.7 / `apply_logging_steps.py`）は省く。各 F00X / B00X の steps からは「[LN] を呼ぶ」の 1 行で参照される構造を維持。

**各エージェント内の Wave 順序（v18 体系）:**

設計思想（v18 で確定）: 呼ぶ側（処理実行条件）と呼ばれる側（処理本体）を対にし、フロント／バックエンドの責務を章レベルで分離する。詳細は 15_中間JSONスキーマ.md「設計思想（v18 で確定）」セクション参照。

| Wave | サブスキル | 出力先（中間JSONキー） | docx での位置（実際の章） |
|:---|:---|:---|:---|
| Wave 1 | 10_論理名変換.md | `logical_names` | メタ情報（章は出ない。本文の物理名の置き換えに使う） |
| Wave 1 | 01_画面レイアウト.md | `screen_layout` | 3 画面レイアウト |
| Wave 1 | 02_イベント一覧.md | `events`（Phase 1 が書く。`event_code` つき）→ Phase 4 が `trigger_groups` を作る | 4 トリガー（呼ぶ側） |
| Wave 2 | 06_パラメータ.md | `parameters` | 章は出ない（検証だけが見る） |
| Wave 2 | 07_バリデーション.md | `validations` / `messages` / `calculations` | 5.3 バリデーション / 6 メッセージ一覧 / 5.4 計算式 |
| Wave 3 | 03_初期処理.md | screen_config の `initial_processes` / `trigger_dispatch.EV00` → Phase 4 が `trigger_groups[kind=auto]` の 1 トリガー + `front_processes[F001]` を作る | 4 トリガー（自動トリガー）+ 5.1 フロント処理の初期処理 |
| Wave 3 | 04_機能別処理.md | `event_processes`（Phase 1 が書く。`details` が処理ステップ）→ Phase 4 が `front_processes` を作る | 5.1 フロント処理 |
| Wave 3 | 05_DB操作.md ＋ 12_API仕様.md（v18 で統合） | `db_operations` / `api_spec`（Phase 1 が書く）→ Phase 4 が `backend_processes` を作る | 5.2 バックエンド処理 |
| Wave 3 | 13_共通ロジック.md（該当時のみ） | `common_logic` | 5.5 共通ロジック（有るときだけ） |
| Wave 3.5 | 17_構成図生成.md（任意） | 図の spec — screen_config の `diagrams.{diagram_id}`（id をキーにしたオブジェクト）に書く。中間 JSON の `diagrams[]` は `add_diagrams_to_intermediate.py` が写して作るので、担当が直接書かない（図そのものは実行順序 Phase 5 の Python スクリプトが作る。diagram-design スキルは任意の代替） | 2 構成図（処理構成図 = 2.1、IPO 図 = 2.2、画面遷移図 = 2.3）/ 3.1 画面構成図 |
| Wave 4 | 14_チェックリスト.md | 整合性検証（v18 整合性ルール12-17 を含む） | 全体検証 |

**docx 章構成（generate_docx.js が実際に出力する章立て）:**

```
1 文書情報
  1.1 フロントエンド構成ファイル / 1.2 バックエンド構成ファイル / 1.3 本書の色の読み方
2 構成図
  2.1 業務フロー全体（処理構成図。PNG が無ければ主要な業務イベントの箇条書きで代替）
  2.2 IPO データフロー図（screen_config の diagrams に ipo_flowchart があるときだけ）
  2.3 画面遷移図（screen_transition の PNG があるときだけ）
3 画面レイアウト
  3.1 画面構成図 / 3.2 画面エリア一覧 / 3.3 画面項目一覧（3.3.N エリア別）
4 トリガー（呼ぶ側。画面操作 / 自動 / 外部の種類ごと。各トリガーが呼ぶフロント処理 [FR#] を示す）
5 処理詳細（呼ばれる側）
  5.1 フロント処理 → [FR#] xxx処理
  5.2 バックエンド処理 → [BE#] xxx処理（【①リクエスト受信】【②DB問合せ】【③JSONレスポンス】）
  5.3 バリデーション
  5.4 計算式
  5.5 共通ロジック（common_logic があるときだけ）
  5.6 DB 操作（db_operations が章の読む項目を持つとき、または data_flow の図があるときだけ）
  5.7 観測点ログ処理（共通）（project_config に観測点の定義があるときだけ）
6 メッセージ一覧
付録A ID 索引
```

改訂履歴・論理名対応表・パラメータの章は出ない。

順次実行とする理由: 並列にしてもサブエージェント間でのコンテキスト引き渡しが必要になり、複雑性が上回るため。HTML単位の並列で十分なスループットが出る。

### Step 4: docx 出力

各HTMLの中間JSON が `target/intermediate/{機能名}.json` に揃ったら、`target/scripts/generate_docx.js` を実行して `.docx` を生成する。docx は、このスクリプトが npm の `docx` パッケージで直接生成する（体裁の規則は 16_docx出力ハンドオフ.md）。

**実行例:**

```bash
INPUT_JSON="target/intermediate/${FEATURE_NAME}.json" \
  DOCX_OUTPUT_PATH="target/output/詳細設計書_${FEATURE_NAME}.docx" \
  NODE_PATH=$(npm root -g) node target/scripts/generate_docx.js
```

`INPUT_JSON` と `DOCX_OUTPUT_PATH` は必須（無いと止まる）。出力ファイル名は `DOCX_OUTPUT_PATH` で決まる。project_config は `INPUT_JSON` と同じフォルダから読むので、`INPUT_JSON` は `target/intermediate/` の中を指す。中間JSON から docx までの全手順は、後段の「実行順序」を参照。

複数HTMLの場合は HTML ごとに実行を繰り返す。

### Step 5: 完了報告 + docx 送付

業務担当者の語彙で短く完了報告する。docx の渡し方は環境で2通り:

- **`SendUserFile` ツールがある環境**: 生成した docx を `SendUserFile({ files: ["target/output/詳細設計書_{機能名}.docx"], caption: "...", status: "normal" })` で送付する。ユーザーがリモート環境にいてもチャット履歴経由で即座にダウンロードできる
- **無い環境**: 出力ファイルの絶対パスを報告する

例:

> 顧客マスタ画面の詳細設計書を生成しました。
> 出力先: `target/output/詳細設計書_顧客マスタ.docx`
> 中間JSON: `target/intermediate/顧客マスタ.json`

## 注意事項

- 各サブスキルファイルは、このスキルのフォルダ（SKILL.md と同じ場所）に配置されている
- サブスキルファイルは、その環境にあるファイル読込の手段で読む（スキル本体は特定の MCP・環境の道具に依存しない）
- 記述ルールはサブスキルファイル配下に閉じる。外部の仕様書を実行時に参照しない（仕様の二重管理を避けるため）
- ソースコード内の物理名は必ず論理名に変換する（10_論理名変換.md 参照）
- プロファイル/中間JSON 該当キーの空状態に応じて不要セクションは自動的にスキップされる
- **構成図はASCIIではなく PNG**（v5以降）。SVG経由ではなく PNG 経由で埋め込む（Wordバージョン互換性、17_構成図生成.md 参照）
- **イベント処理索引の章は無い**（v15以降）。呼ぶ側は「4 トリガー」、呼ばれる側は「5 処理詳細」で完結する。バックエンド処理が実行する DB 操作は「5.2 バックエンド処理」の【②DB問合せ】に出る
- **EV00（初期表示）は画面操作のトリガーには出ず、自動トリガー（画面表示時）と「5.1 フロント処理」の初期処理で扱う**（v14以降）。「初期処理 = ユーザーが画面を開いてからアイドル状態に到達するまでに自動実行される処理」と定義
- **WebSocket 受信系は「4 トリガー」の画面操作以外（自動 / 外部）のトリガー**（v15以降）。`classify_source_kind` は action/location だけでなく trigger/content 全文で判定
- **業務担当者の語彙を優先**。旧システムの物理名の出力は禁止（禁止する語は project_config の `forbidden_terms_map.terms` に書く）
- **日付時刻フォーマット** は `YYYY/MM/DD HH:MM`

## target/scripts/ 配下スクリプト群（v47 最新版、次画面でも再利用する汎用部品）

設計書生成パイプラインの実装は `target/scripts/` 配下のスクリプトに集約されている。次画面で /design-doc を起動した時、これらをそのまま再利用する。

**v30 で完全汎用化、v41 で非機能横断要素も網羅化**: 画面別ハードコード + プロジェクト固有規約（観測点ログのモデル等）を `screen_config.json` + `project_config.json` の 2 ファイルに集約。次画面追加時はこの 2 ファイルを書くだけで、業務処理 + 呼出グラフ + 観測点ログ + 監査ログ + 認証 を Phase 1 Agent が網羅抽出し、Phase 5.6 build_call_graph で機械補完、Phase 6 verify_call_graph で見逃しゼロ保証。

| スクリプト | 役割 | 汎用/画面別 | 導入版 |
|:---|:---|:---|:---|
| **外部設定読込ヘルパー（v30 で新設）** | | | |
| `_config_loader.py` | `{機能名}_screen_config.json` 読込ヘルパー。`get_areas()` / `get_process_names_by_event_code()` / `get_diagram_spec(id)` 等のセクション別アクセサを提供 | 汎用 | **v30** |
| **基盤スクリプト（v17 以前から）** | | | |
| `merge_partials.py` | 4 Agent の出力（_partial_*.json）を統合し中間JSON 本体生成 | 汎用 | v1 |
| `convert_events_location_action.py` | events[].trigger を location/action/source_kind に分解 | 汎用 | v10 |
| `replace_hatsuka.py` | 「発火」を「呼出/実行」に置換（業務語彙統一） | 汎用 | v11 |
| **v18-v23 体系拡張（v30 で screen_config.json 経由に汎用化）** | | | |
| `migrate_v17_to_v18.py` | v17 中間JSON → v18+ 形式に変換（trigger_groups/front_processes/backend_processes 生成、業務名マッピング、steps 句点分解） | 汎用（`process_names_by_event_code` / `front_area_keywords` は screen_config.json 経由） | v18-v30 |
| `add_screen_areas.py` | screen_layout.areas[] 追加 + items[].area_ref 付与（画面エリア定義の権威ソース） | 汎用（`areas[]` は screen_config.json 経由） | v20-v30 |
| `normalize_screen_terminology.py` | events/trigger_groups の location 用語を screen_layout 権威語彙に正規化 | 汎用（`area_aliases` / `screen_name_aliases` / `area_keyword_fallback` は screen_config.json 経由） | v20-v30 |
| `extract_source_files.py` | HTML 解析で frontend_files[] 自動抽出 + backend_files[] 拡張。screen_config.source_files があればそれも frontend_files に入れる（HTML なしでも可。tsx/ts は kind=js） | 汎用（`source_html_path` / `source_files` / `shared_modules` / `file_descriptions` は screen_config.json 経由） | v21-v30 |
| `apply_trigger_dispatch.py` | 主要 trigger に dispatch[] 付与 + 対応 F00X 本体から事前チェック step を完全削除 | 汎用（`trigger_dispatch` は screen_config.json 経由） | v23-v30 |
| **構成図パイプライン（v30 で screen_config.json 経由に汎用化）** | | | |
| `add_diagrams_to_intermediate.py` | diagrams[] spec を中間JSON に追加（処理構成図 / 画面構成図 / IPO Flowchart） | 汎用（`diagrams.{id}.spec` は screen_config.json 経由、ファイルパスは命名規約から組立） | v5-v30 |
| `build_ipo_flowchart_svg.py` | IPO データフロー図 SVG/HTML を Python 直接生成（diagram-design API 不要）。screen_config の `diagrams` に `ipo_flowchart` があるときだけ作る。無ければ「IPO 図の定義が無いので省く」と表示して正常終了する | 汎用（`diagrams.ipo_flowchart.svg_layout` は screen_config.json 経由、COLOR スキンは Python 側残置） | v26-v30 |
| `extract_svg.py` | HTML から SVG 抽出 + font-family 強制 + `<style>` 注入（v24 で cairosvg 文字化け対策強化） | 汎用 | v5-v24 |
| `svg_to_png.py` | cairosvg で SVG → PNG 変換（高DPI 1200px〜） | 汎用 | v5 |
| **物理名置換（v31 で新設）** | | | |
| `apply_logical_name_substitution.py` | 中間JSON テキスト内の DB 物理名（`order_month` 等）を「論理名[物理名]」表記に置換（冪等）。logical_names テーブルから物理名→論理名マップを自動構築、業務担当者向けフィールドのみに限定 | 汎用 | **v31** |
| **ID 体系再設計（v43 で新設、スキル付属設定）** | | | |
| `apply_id_scheme.py` | スキル付属 `id_scheme.json` を読み、中間 JSON 全体の ID を新体系（2文字接頭辞 + 1始まり可変桁、AR#/IT#/EV#/TR#/FR#/BE#/LG#/ST#/AP#/DB#/VL#/CA#/CL#/PR#/MS#/LN#）に一括変換。サブラベル付き ID（FR1-EDIT, LG4-a 等）は親と同じ番号を共有、クロスリファレンス（called_by_triggers / dispatch.then_call / backend_call.backend_id / area_ref 等）も整合的に更新、legacy_id 保持。新カテゴリ追加時は id_scheme.json の categories[] にエントリ追記のみでスキル本体改修不要 | 汎用 | **v43** |
| **v49 構造化（v49 で新設）** | | | |
| `migrate_v48_to_v49.py` | v48 中間 JSON を v49 構造に変換。(1) `backend_processes[].response_spec.response_to_screen_mapping[]` を `front_processes[].steps[]` の最終 `screen_update` 直後に kind='response_mapping' の新 step として挿入し、item_ref_no を `screen_layout.items[]` から逆引き付与、(2) `response_spec.items[]` に `source / transform_backend` を空のテンプレートで追加（後段 Agent or 手動で埋める）、(3) `db_op_detail.sub_no` を採番（②-N 表示用）、(4) 初期処理 FRX に残る URL パラメータ判定 step を削除（v42 設計逸脱の解消）。バックアップは `/tmp/{機能名}_v48_backup_YYYYMMDD-HHMMSS.json` に保存 | 汎用 | **v49** |
| **呼出グラフ抽出（v41 で新設、本質対応）** | | | |
| `build_call_graph.py` | フロント JS の apiCall ↔ バックエンドの action ディスパッチ（`if action == 'xxx':` / 辞書ディスパッチ）を正規表現で双方向突合。`called_by_front_processes[]` / `api_callsite_count` / `logging_callsites[]` / `audit_callsites[]` を機械補完。**2 段階解析**: Stage 1 で `apiCall(...)` の直接呼出を拾い（project_config の `api_call_functions` に書いた関数の呼出 `call('<action>', ...)` も Stage 1 と同じ扱いで拾う。走査対象は screen_config の `source_files`（あれば。.ts/.tsx を含む）か、無ければ HTML の script src）、Stage 2 で全 JS から `action: 'xxx'` を網羅抽出して、Stage 1 の呼出位置の ±15 行に入らない参照を間接経路として `called_by_front_processes[].source='indirect_action'` で記録する（直接経路は `source='apiCall'`）。間接経路には `context`（`fetch_direct` = fetch の直叩き / project_config の `api_wrapper_classes` に書いたクラス名 = 共通部品経由 / `other`）も付く。HTML/JS/バックエンドのパスは screen_config.json（`source_html_path` / `source_files` / `project_root`）+ meta.backend_files から動的取得（プロジェクト固有名を持たない） | 汎用 | **v41-v48** |
| **観測点ログ処理挿入（v39-v40 で新設、project_config 経由）** | | | |
| `apply_logging_steps.py` | project_config.json logging.observation_points を読み、`common_logging_processes[]` に、定義された観測点（とサブ観測点）ごとの独立処理 [LN] を新設。各 F00X / B00X の steps に「[LN] を呼ぶ」呼出 step（kind='logging_call'）を機械挿入。冪等。observation_points の定義が無ければスキップ（汎用性確保） | 汎用 | **v39-v40** |
| **検証 + 出力** | | | |
| `verify_intermediate.py` | 中間JSON 業務的構造の整合性検証（ルール 1-20、v20 で画面用語 cross-reference 検証追加） | 汎用 | v1-v20 |
| `verify_call_graph.py` | 呼出グラフ整合性検証（v41 で新設）。「呼ばれていない F00X / B00X」「呼んでるのに存在しない処理参照」「project_config 定義の trigger_function 抽出ゼロ」を ERROR / WARN / INFO で報告。終了コード=ERROR 件数（CI 連携可） | 汎用 | **v41** |
| `generate_docx.js` | 中間JSON を読んで docx-js で `.docx` 出力。章立ては Step 3 の「docx 章構成」のとおり（1 文書情報 / 2 構成図 / 3 画面レイアウト / 4 トリガー / 5 処理詳細 / 6 メッセージ一覧 / 付録A ID 索引）。主な体裁: 「3.3 画面項目一覧」と「5.1 フロント処理」のエリア別の分割、段落番号に合わせたインデント、フロント/バックの処理表は3列（Step / 種別 / 処理内容）、表ヘッダの ID 列の文言は id_scheme 経由（`getIdHeaderLabel`）、「5.2 バックエンド処理」は【①リクエスト受信】【②DB問合せ】【③JSONレスポンス】の3段構成（③ に「データ起源」「バック側の加工」列）、response_mapping の step ごとに画面項目マッピングの小節を自動生成、「5.7 観測点ログ処理（共通）」（`sectionCommonLoggingProcesses`） | 汎用 | v1-v49 |

### 実行順序（次画面用テンプレ、v52 完全版）

v52 で共通スクリプト 5 本（`_config_loader.py` / `apply_trigger_dispatch.py` / `apply_logging_steps.py` / `build_call_graph.py` / `verify_call_graph.py`）に画面別引数 / `FEATURE_NAME` 環境変数を追加。**1 コマンドラインで `FEATURE_NAME=受注照会` を切り替えるだけで複数画面パイプラインを切替可能**。`generate_docx.js` も既に `INPUT_JSON` / `DOCX_OUTPUT_PATH` 環境変数化済（v49 で対応）。

```bash
# === 環境変数で対象画面を指定（v52、複数画面切替対応） ===
export FEATURE_NAME="受注入力"   # 例: 受注入力 / 受注照会 / 顧客マスタ / 商品マスタ 等（既定の機能名は無い。必ず指定する）

# === Phase 1: ソース解析（Agent 並列） ===
# Agent 4本並列で _partial_*.json 生成（Step 3）。出力先: target/intermediate/_partial_*.json
# partial 4本の名前とキーは Step 3「Phase 1 の成果物の渡し方」を参照。partial は機能名に依らない固定名なので、1画面ずつ処理する
# Step 3 のプロンプトで {機能名}.json を直接保存した場合は、次の merge_partials.py を実行しない（実行すると空に近い中間JSON で上書きされる）
# Phase 1 の出力では events[] に event_code（EV00 = 初期表示、以降 EV01… の連番）を付ける。event_processes は merge_partials.py が events から作る（details は空）
# 処理ステップを docx に出すには、merge_partials.py の後・Phase 4 の前に event_processes[].details を埋める（Step 3「Phase 1 の成果物の渡し方」）

# === Phase 2: v17 形式中間JSON 構築 ===
python3 target/scripts/merge_partials.py "${FEATURE_NAME}"                       # _partial 統合
python3 target/scripts/convert_events_location_action.py "${FEATURE_NAME}"        # events に location/action/source_kind 付与
python3 target/scripts/replace_hatsuka.py "${FEATURE_NAME}"                       # 必要時のみ: 「発火」→「呼出」置換

# === Phase 3: v20-v21 拡張（画面エリア・構成ファイル網羅）===
python3 target/scripts/add_screen_areas.py "${FEATURE_NAME}"                      # screen_layout.areas[] 定義（screen_config.json から読込、裏方領域 screen_no="0" も含む）
python3 target/scripts/extract_source_files.py "${FEATURE_NAME}"                  # frontend_files / backend_files 抽出（screen_config.source_files も frontend_files へ）

# === Phase 4 の前（手作業）: event_processes[].details を埋める ===
# target/intermediate/${FEATURE_NAME}.json の event_processes[].details に、処理ステップを1行1 step で書く
# （空のままだと、概要を句点で区切っただけのステップになる。書き方は Step 3「Phase 1 の成果物の渡し方」）

# === Phase 4: v18 体系変換（呼ぶ側/呼ばれる側）===
python3 target/scripts/migrate_v17_to_v18.py "${FEATURE_NAME}"                    # trigger_groups / front_processes / backend_processes 生成（毎回作り直す）。初期処理以外のイベントの backend_call.backend_id もここで補う
python3 target/scripts/normalize_screen_terminology.py "${FEATURE_NAME}"          # location 用語を screen_layout 権威語彙に正規化（裏方領域 area_keyword_fallback も適用）。画面操作のトリガーへ events[] の area_ref を写す
python3 target/scripts/apply_trigger_dispatch.py "${FEATURE_NAME}"                # 主要 trigger に dispatch 付与 + F00X 事前チェック除去（v52: 画面別引数対応）

# === Phase 4.5: event_processes の補修 + 派生処理マーク（v52 で新設、汎用。2本とも無条件に流してよい） ===
# _fill_event_processes.py は冪等な補修。events[] のうち、対応する event_processes が無いもの（初期表示の EV00 を除く）にだけ
# 仮埋めを足す。既にある event_processes には足さず、書き換えもしない（merge_partials.py が全件を作った画面では「0 件追加」で終わる）。
# 突き合わせに events[].event_code を使うので、Phase 5.85（event_code を event_ref へ移す）より前に流す。
python3 target/scripts/_fill_event_processes.py "${FEATURE_NAME}"                 # 対応する event_processes が無い events にだけ仮埋め（overview 1行、details は空）を足す
python3 target/scripts/_fix_derived_dispatch.py "${FEATURE_NAME}"                 # screen_config.derived_processes[] を読み trigger_groups に派生処理マーク注入（定義が無ければ何もしない）

# === Phase 5: 構成図生成（v53: 固定3id 列挙 → 動的id列挙化。process_structure/screen_structure/ipo_flowchart
#     に加え screen_transition/data_flow/derivation_chain_* 等の可変個数 diagram_id を、
#     screen_config.json の diagrams[] キー追加のみでパイプラインに反映できるようにする） ===
python3 target/scripts/add_diagrams_to_intermediate.py "${FEATURE_NAME}"          # diagrams[] spec を中間JSON に追加
python3 target/scripts/build_ipo_flowchart_svg.py "${FEATURE_NAME}"               # IPO Flowchart SVG/HTML 生成（Python 直接、Agent 不要）。設定の diagrams に ipo_flowchart があるときだけ作る。無ければ「IPO 図の定義が無いので省く」と表示して正常終了する（そのまま次へ進む）

# diagram_id 一覧を screen_config.json の diagrams[] キーから動的取得（_config_loader.get_all_diagram_ids 経由）
DIAGRAM_IDS=$(python3 -c "
import sys; sys.path.insert(0, 'target/scripts')
from _config_loader import get_all_diagram_ids
ids = get_all_diagram_ids('${FEATURE_NAME}')
print('\n'.join(ids))
")

# ipo_flowchart 以外の全 diagram_id を build_generic_diagram_svg.py で個別生成（Python 直接、Agent 不要）
while IFS= read -r id; do
  [ -z "$id" ] && continue
  [ "$id" = "ipo_flowchart" ] && continue
  python3 target/scripts/build_generic_diagram_svg.py "${FEATURE_NAME}" "$id"
done <<< "$DIAGRAM_IDS"

# 図のファイル名は全画面で同じ書き方（"${FEATURE_NAME}_${id}" の接頭辞つき。IPO 図は "${FEATURE_NAME}_ipo_flowchart"）
while IFS= read -r id; do
  [ -z "$id" ] && continue
  SRC_HTML="target/diagrams/${FEATURE_NAME}_${id}.html"
  SRC_SVG="target/diagrams/${FEATURE_NAME}_${id}.svg"
  SRC_PNG="target/diagrams/${FEATURE_NAME}_${id}.png"
  python3 target/scripts/extract_svg.py "$SRC_HTML" "$SRC_SVG"
  python3 target/scripts/svg_to_png.py "$SRC_SVG" "$SRC_PNG" 1400
done <<< "$DIAGRAM_IDS"

# === Phase 5.5: 物理名置換（v31 で新設、v52 で画面別引数対応） ===
python3 target/scripts/apply_logical_name_substitution.py "${FEATURE_NAME}"        # DB 物理名 → 「論理名[物理名]」表記

# === Phase 5.6: 呼出グラフ抽出（v41 で新設、v52 で画面別引数対応） ===
python3 target/scripts/build_call_graph.py "${FEATURE_NAME}"                       # フロント apiCall・関数ラッパー（api_call_functions。走査対象は source_files か HTML の script src） / バック action / 観測点ログ呼出 / 監査ログ呼出 を抽出して中間JSON に補完（derived_processes は dead_code_candidate から除外）

# === Phase 5.7: 観測点ログ処理挿入（v40 で新設、project_config 経由、v52 で画面別引数対応） ===
# project_config の logging.observation_points に定義が無い案件では、この Phase は省く
python3 target/scripts/apply_logging_steps.py "${FEATURE_NAME}"                    # common_logging_processes を新設、各 F00X/B00X の steps に [LN] 呼出 step を挿入

# === Phase 5.8: ID 体系新体系化（v43 で新設、id_scheme.json 経由、v52 で画面別引数対応） ===
python3 target/scripts/apply_id_scheme.py "${FEATURE_NAME}"                        # 中間 JSON の全 ID を新体系（AR#/IT#/EV#/.../LN#）に一括変換、クロスリファレンス更新、legacy_id 保持

# === Phase 5.85: events event_code → event_ref 統一（v50 で新設、v52 で画面別引数対応） ===
# 意味合い: Phase 1 の出力は events[].event_code（EV00, EV01, ...）を持ち、screen_layout.items[] / event_processes[] などは
#           その event_code でイベントを指している。ここで、その参照を events[].no（新 ID EV1, EV2, ...）を指す event_ref に変換し、
#           events[].event_code は legacy_event_code へ退避する。event_code を持たない形は、ここを通った後の最終形。
#           apply_id_scheme.py の後、migrate_v48_to_v49.py の前に実行（v49 構造化が item_ref_no を引くため、参照側を先に新体系化）。
python3 target/scripts/migrate_v49_event_unify.py "${FEATURE_NAME}"                # events[].event_code 廃止、screen_layout.items[].event_ref / trigger_groups[].triggers[].event_ref に統一

# === Phase 5.9: v49 構造化（v49 で新設、v52 で画面別引数対応） ===
# 意味合い: backend_processes.response_spec.response_to_screen_mapping を front_processes.steps の response_mapping step に移動、
#           response_spec.items に source/transform_backend 追加、db_op_detail に sub_no 採番、URL パラメータ判定 step を除去
python3 target/scripts/migrate_v48_to_v49.py "${FEATURE_NAME}"                     # v48 中間 JSON を v49 構造に変換、front/back の責務を明確化

# === Phase 6: 整合性検証（v52 で画面別引数対応） ===
python3 target/scripts/verify_intermediate.py "${FEATURE_NAME}"                   # 業務的構造の整合性、ERROR 0 を目標
python3 target/scripts/verify_call_graph.py "${FEATURE_NAME}"                      # 呼出グラフの整合性（v41 で新設）、ERROR 0 を目標 / WARN は共通モジュール経由の許容範囲 / derived_processes は dead_code_candidate から除外

# === Phase 7: docx 出力（v49 で既に環境変数化済） ===
INPUT_JSON="target/intermediate/${FEATURE_NAME}.json" \
  DOCX_OUTPUT_PATH="target/output/詳細設計書_${FEATURE_NAME}.docx" \
  NODE_PATH=$(npm root -g) node target/scripts/generate_docx.js

# === Phase 7.5: docx レイアウト検証（A8 で新設、2026-07-18） ===
# 意味合い: 中間 JSON の整合性（Phase 6）だけでは検知できない、生成された .docx 自体の
#           レイアウト起因の可読性問題（図のはみ出し・広幅表・TOC未更新設定等）を機械検出する。
#           ユーザー送付前の最終ゲート。ERROR があれば Phase 7 の生成元（generate_docx.js /
#           中間JSON の embed_size 等）を見直してから再生成する。
python3 target/scripts/verify_docx_layout.py "target/output/詳細設計書_${FEATURE_NAME}.docx"

# === Phase 8: ユーザー送付 ===
# SendUserFile ツールがある環境では、メインセッションが SendUserFile({files:["target/output/詳細設計書_${FEATURE_NAME}.docx"], ...}) で送付。無い環境では、出力ファイルの絶対パスを報告する
```

**複数画面パイプライン例（v52、1 ループで 3 画面切替）:**

```bash
for FEATURE_NAME in 受注照会 顧客マスタ 商品マスタ; do
  export FEATURE_NAME
  # Phase 2 〜 Phase 7 をまとめて実行（個別画面の細部チューニングは screen_config.json 経由）
  # partial は固定名で画面どうしで共有されるので、この画面の partial 4本を用意してから merge_partials.py を実行する
  # （{機能名}.json を直接保存してある画面では、merge_partials.py の行を外す）
  python3 target/scripts/merge_partials.py "${FEATURE_NAME}"
  python3 target/scripts/convert_events_location_action.py "${FEATURE_NAME}"
  # ... 以下 Phase 6 まで、上の実行順序と同じ順に全部流す
  # （Phase 4 の前に event_processes[].details を埋める手作業が要る画面は、ループを Phase 3 までと Phase 4 以降に分ける。
  #   Phase 4.5 の _fill_event_processes.py / _fix_derived_dispatch.py は、どの画面でも無条件に流してよい）
  INPUT_JSON="target/intermediate/${FEATURE_NAME}.json" \
    DOCX_OUTPUT_PATH="target/output/詳細設計書_${FEATURE_NAME}.docx" \
    NODE_PATH=$(npm root -g) node target/scripts/generate_docx.js
done
```

### 画面別カスタマイズ（v30 で 1 ファイル集約済）

**新画面追加時の作業は `target/intermediate/{機能名}_screen_config.json` を1つ書くだけ。** v29 以前のように7スクリプトを書き換える必要はなくなった（v30 で全7スクリプトが `_config_loader.py` 経由で screen_config.json を読む方式に統一されたため）。

初めて使うときは、`examples/sample/` の設定2つ（架空の受注入力画面の `受注入力_screen_config.json` と `受注入力_project_config.json`）を `target/intermediate/` へコピーして、新画面用に書き換える（ファイル名の `受注入力` も機能名に変える）。サンプルのソース（`examples/sample/src/`）を対象に動かすときは、ファイル名を変えずに `FEATURE_NAME=受注入力` を指定する。どちらの場合も、設定を置いただけでは中身の無い骨組みになるので、先に Phase 1（Step 3。中間データの作成）を済ませる。サンプルの `source_html_path` は `${HOME}/.claude/skills/design-doc/examples/sample/src/order_detail.html` なので、このスキルを別の場所に置いた場合は書き換える。サンプルの作りと書き換える箇所は `examples/sample/README.md` を参照。

**screen_config.json は、パイプラインを実行する前に用意する。** `merge_partials.py` がパイプラインの先頭（Phase 2）でこのファイルを読み、`backend_files` / `project_pattern` / `profile` を中間 JSON の `meta` に書くためである。機能名に既定値は無い。全スクリプトは第1引数か環境変数 `FEATURE_NAME` で機能名を受け取り、無ければ「feature_name 未指定」で止まる。

| screen_config.json のキー | 用途 | 使用スクリプト |
|:---|:---|:---|
| `feature_name` / `screen_name` | 機能名 / 画面正式名称。`screen_name` は「〜画面」で終わる正式名で、機能名 +「画面」と一致させる（機能名が既に「画面」を含むなら機能名そのまま）。トリガー文は「{画面名}の…」の型で書き、{画面名} にこの値を使う | 全7スクリプト |
| `source_html_path` | 解析対象 HTML のパス（`${HOME}` と `~` を展開する。配布する設定には絶対パスを書かない）。HTML の画面では必須。`source_files` を書く React(TSX) の画面では空でよい | `extract_source_files.py` / `build_call_graph.py`（project_root の自動検出） |
| `source_files[]`（任意） | 画面を構成するソースのパスの配列（tsx/ts/css など。`${HOME}` と `~` を展開する。既定 `[]`）。あれば `extract_source_files.py` が `meta.frontend_files` に入れ（tsx/ts は `kind: "js"`）、`build_call_graph.py` が走査対象にする | `extract_source_files.py` / `build_call_graph.py` |
| `project_root`（任意） | 案件のソースの最上位フォルダ（`${HOME}` と `~` を展開する）。無ければ `build_call_graph.py` が `source_html_path` から、`common/` ディレクトリを手がかりに自動検出する。その構成でない案件は必ず書く | `build_call_graph.py` |
| `backend_files[]` | この画面が呼ぶバックエンドのソースのパス（文字列の配列、既定 `[]`）。中間 JSON の `meta.backend_files` に書かれる | `merge_partials.py` |
| `project_pattern` / `profile` | Step 1 / Step 2 の判定結果（文字列、既定 `""`）。中間 JSON の `meta.project_pattern` / `meta.profile` に書かれる | `merge_partials.py` |
| `shared_modules[]` | バックエンドが実際に import する共通モジュール一覧 | `extract_source_files.py` |
| `file_descriptions.css/js` | CSS/JS ファイル名 → 1 行説明マップ | `extract_source_files.py` |
| `areas[]` | 画面論理エリア定義（area_no / area_name / screen_no / description） | `add_screen_areas.py` |
| `area_pattern_subdivisions` (v34) | エリア配下の項目を業務軸（例 請求の区分: 税込 / 税抜 / 非課税）で更に細分化（`subdivision_axis_label` + `subdivisions[].remarks_keyword`）。`generate_docx.js` が「3.3 画面項目一覧」で共通+固有に分割表示 | `add_screen_areas.py` (中間JSON伝播) + `generate_docx.js` |
| `initial_processes.processes[]` (v42) | 画面初期表示時の URL パラメータ別分岐パターン（F001 / F001-EDIT / F001-VIEW / F001-MISC（その他取引先モード）等の各 process_id / name / url_param_match / preconditions / postconditions / steps）。N 個の独立処理として `migrate_v17_to_v18.py` が生成。**バックエンドを呼ぶ step には `kind: "backend_call"` と `backend_call.backend_id`（`B001` 形式 = `api_spec[]` の並び順の通し番号）を書く。これが画面側とバックエンドを結ぶ**（初期処理以外のイベントは、`db_operations[].api_endpoint` の `action` / `trigger_event`（多対一は `trigger_events`（event_code の配列））から自動で紐付く） | `migrate_v17_to_v18.py` (front_processes 生成) |
| `trigger_dispatch.EV00` (v42) | 画面表示時の URL パラメータ判定（キーの `EV00` は Phase 1 の初期表示イベントの event_code。`trigger_match_kind: 'auto'` + `dispatch[]` で initial_processes[] への分岐）。「呼ぶ側で判定、呼ばれる側は純粋処理」設計思想を初期処理にも適用 | `apply_trigger_dispatch.py` |
| `area_aliases` | エリア名エイリアス（原文 → 権威語彙） | `normalize_screen_terminology.py` |
| `screen_name_aliases` | 画面名エイリアス | `normalize_screen_terminology.py` |
| `area_keyword_fallback[]` | trigger パース失敗時のキーワード → エリア名フォールバック（順序保証） | `normalize_screen_terminology.py` |
| `process_names_by_event_code` | event_code → 業務担当者語彙の処理名。キーは Phase 1 の `events[].event_code`（`EV00` = 初期表示、以降 `EV01`… の連番） | `migrate_v17_to_v18.py` |
| `process_overview_overrides` (v31) | process_id（`F001` 形式）→ 業務目的1文の overview 上書き。概要は30字以内（25字が目安、物理名を含めない。超えると `verify_intermediate.py` が ERROR）なので、自動生成の概要（初期処理は「名前。URLパラメータ条件: …」、それ以外は `events[].content` の最初の句点まで）が長いときにここで短くする。`steps` との重複も避けられる | `migrate_v17_to_v18.py` |
| `front_area_keywords[]` | front_process のエリア分類用キーワード（順序保証） | `migrate_v17_to_v18.py` |
| `trigger_dispatch` | event_code → dispatch 定義（trigger_match_keyword / dispatch[] / strip_step_keywords）。キーは Phase 1 の `events[].event_code`、`then_call` は `F001` 形式の処理の番号（初期処理が `F001`、以降は event_processes の並び順に `F002` から） | `apply_trigger_dispatch.py` |
| `diagrams.{id}.spec` | 処理構成図 / 画面構成図 / IPO Flowchart の論理構造（nodes/edges/structure） | `add_diagrams_to_intermediate.py` |
| `diagrams.ipo_flowchart.svg_layout` | IPO Flowchart の SVG レイアウト座標（triggers/fronts/backends/dbs/scenarios）。`diagrams` に `ipo_flowchart` を書いたときだけ IPO 図が作られる（無ければスクリプトが省く旨を表示して正常終了し、docx の「2.2 IPO データフロー図」も出ない） | `build_ipo_flowchart_svg.py` |
| **`derived_processes[]` (v52)** | **派生処理マーク**（出力メニュー押下 / CSV ダウンロード / メール通知 等、trigger_groups に直接トリガーがないため `dead_code_candidate` と誤検出される処理を業務語彙で明示）。各エントリは `{trigger_event_ref, source_menu, kind, then_call[], then_message, derived_from}` 形式 | `apply_trigger_dispatch.py` / `verify_call_graph.py`（誤検出抑止） |
| **`areas[].screen_no="0"` / null（v52）** | **裏方領域パターン**（WebSocket 購読 / 排他通知 / 編集ロック通知 等、画面表示なしのバックグラウンド領域を `areas[]` に登録）。`description` には「裏方領域、画面表示なし」を必ず明記。`area_keyword_fallback[]` にも「WebSocket / 排他通知 / 編集ロック」キーワードのフォールバック対応を追加 | `add_screen_areas.py` / `normalize_screen_terminology.py` |

**`derived_processes[]` スキーマ例（v52、screen_config.json 内）:**

```json
"derived_processes": [
  {
    "trigger_event_ref": "TR21",
    "source_menu": "CSV ダウンロードメニュー",
    "kind": "output_menu_derived",
    "then_call": ["FR28", "FR29"],
    "then_message": "一覧画面で実行（本画面は参考表示のみ）",
    "derived_from": "出力メニュー（共通UI・一覧画面で実行）"
  },
  {
    "trigger_event_ref": "TR15",
    "source_menu": "メール通知メニュー",
    "kind": "external_link_derived",
    "then_call": [],
    "then_message": "外部 SaaS（メール配信サービス）画面遷移、本システム外で実行",
    "derived_from": "出力メニュー → メール通知リンク"
  }
]
```

**裏方領域 areas[] スキーマ例（v52、screen_config.json 内）:**

```json
"areas": [
  {
    "area_no": "A0",
    "area_name": "WebSocket購読エリア",
    "screen_no": "0",
    "description": "裏方領域、画面表示なし。編集ロック通知 / 排他制御通知を受信して画面状態を更新"
  }
],
"area_keyword_fallback": [
  {"keyword": "WebSocket", "area_name": "WebSocket購読エリア"},
  {"keyword": "排他通知", "area_name": "WebSocket購読エリア"},
  {"keyword": "編集ロック", "area_name": "WebSocket購読エリア"}
]
```

**設計意図（v52）:**

- **派生処理 (`derived_processes[]`)**: 「出力メニュー → CSV ダウンロード / メール通知 等」の派生処理は、画面に直接のトリガーが無いため `verify_call_graph.py` の dead_code_candidate 検出ロジックに誤って引っかかる。`derived_from` フィールドで業務的な派生関係を明示することで誤検出を抑止する。
- **裏方領域 (`areas[].screen_no="0"`)**: WebSocket 購読・編集ロック通知のように画面に表示されない処理が `screen_layout.areas[]` に登録されていないと、`normalize_screen_terminology.py` で「画面用語が見つからない」ERROR になる。裏方領域として登録すれば画面表示エリアと区別しつつクロスリファレンス整合性を保てる。

**Python 側に残置される画面共通スタイル**: `build_ipo_flowchart_svg.py` の `COLOR` 辞書（neutral stone paper + rust accent スキン）、SVG 描画プリミティブ（`cylinder` / `rect_node` / `pill_node` / `arrow`）、`extract_source_files.py` の `_describe_cdn`（tailwindcss/decimal.js 等の CDN URL 判定）。これらは画面横断の共通設計のため画面別 JSON には含めない。

### project_config.json（v39-v41 で新設、プロジェクト横断の非機能規約）

screen_config.json が「画面別の業務的構造」を持つのに対し、**project_config.json は「プロジェクト横断の非機能横断要素（ロギング/監査/認証）規約」と、案件ごとに変わる値（禁止語・共通モジュールの見分け方・文書の体裁など）** を画面非依存に定義する。案件固有の観測点モデルは project_config に集約し、スキル本体には固有名を含めない。置き場は `target/intermediate/{機能名}_project_config.json`（画面ごとに置く。同じ案件なら中身は同じでよい）。

**スクリプトが読むキー:**

| project_config.json のキー | 用途（既定値） | 使用スクリプト |
|:---|:---|:---|
| `logging.observation_points[]` | 観測点モデル定義（trigger_function / captures / triggered_when / business_meaning / sub_observations[]）。観測点の数は案件による。**定義が無ければ、観測点ログのステップ挿入（Phase 5.7 / `apply_logging_steps.py`）は省く** | `apply_logging_steps.py` / `build_call_graph.py` / `verify_call_graph.py` |
| `logging` 配下のその他のキー | 観測点ログに自動で付くメタ項目、ログの経路、運用の原則など、案件のロギング規約の仕様根拠。書式は案件の自由 | 仕様根拠として記載（スクリプトは値を使わない） |
| `audit_logging.trigger_function` | 監査ログ送信関数名（例: `write_audit_log`） | `build_call_graph.py` / `verify_call_graph.py` |
| `verify_overrides.legacy_event_code_patterns[]` | 案件で定義した旧形式のイベントコードの正規表現（文字列の配列）。いずれかに一致する `event_code` の残存を、旧形式として WARN にする（既定 `[]`。空なら `EV` + 数字の形式だけを見る） | `verify_intermediate.py` |
| `verify_overrides.business_ok_words[]` | 記述粒度の検査で、物理名として検出しない案件固有の語（文字列の配列。既定 `[]`） | `verify_intermediate.py` |
| `verify_overrides.business_ref_prefixes[]` | 記述粒度の検査で、業務参照 ID とみなす接頭辞に足す案件固有の接頭辞（文字列の配列。既定 `[]`） | `verify_intermediate.py` |
| `verify_overrides.extra_phrase_patterns[]` | 画面項目の備考の検査で、動的な挙動とみなさない案件固有の言い回しの正規表現（文字列の配列。既定 `[]`） | `verify_intermediate.py` |
| `verify_overrides.noise_words[]` | 呼出グラフの突き合わせで、観測点の `trigger_function` からログ関数名を取り出すときに無視する一般語に足す、案件固有の語（文字列の配列。既定 `[]`。`Function` / `Module` / `Handler` / `Method` / `Class` / `Object` は常に無視する） | `build_call_graph.py` / `verify_call_graph.py` |
| `auth.claims[]` | JWT クレーム定義（sub / name / sid / iat / exp） | 仕様根拠 |
| `forbidden_terms_map.terms` | {禁止する旧名: 置き換える業務語彙} の辞書。旧システムの物理名など、設計書に出してはいけない語を書く（無ければ禁止語なし） | `verify_intermediate.py`（検査）/ `apply_logical_name_substitution.py`（置換）/ `generate_docx.js`（置換） |
| `glossary` | 案件の用語集（業務語彙の統一に使う） | 仕様根拠 |
| `shared_module_patterns[]` | パスにいずれかの文字列を含めば共通モジュール（`kind: "shared_module"`）とみなす（既定 `["common/"]`） | `extract_source_files.py` |
| `backend_entry_patterns[]` | パスにいずれかの文字列を含めばバックエンドの入口ファイル（`kind: "backend"`）とみなす（既定 `["handler"]`）。`kind` を決めるときは、共通モジュールの判定（`shared_module_patterns[]`）が先 | `build_call_graph.py` / `extract_source_files.py` |
| `allowed_project_patterns[]` / `allowed_profiles[]` | `meta.project_pattern` / `meta.profile` に書いてよい値の一覧（キーが無ければ検査しない） | `verify_intermediate.py` |
| `api_wrapper_classes[]` | API 呼出を包む共通部品のクラス名（`new クラス名(` の形で API を呼ぶもの。既定 `[]`） | `build_call_graph.py` |
| `api_call_functions[]`（任意） | API 呼出の関数名の配列（例: `call`）。`call('<action>', ...)` 形の呼出の第1引数の文字列リテラルを action として拾う（既定 `[]`） | `build_call_graph.py` |
| `step_kind_keywords.branch[]` / `.calculation[]` | 処理ステップを「分岐」「計算」に分類するときに足す、案件固有の語（既定は空） | `migrate_v17_to_v18.py` |
| `docx_output.doc_title` | 文書種別名。表題とヘッダーに出る（既定 `"詳細設計書"`） | `generate_docx.js` |
| `docx_output.font` | 本文の字体（既定 `'Meiryo UI'`） | `generate_docx.js` |
| `docx_output.font_size_pt` | 本文の大きさ（既定 `8`。見出し・表ヘッダー・等幅は連動しない） | `generate_docx.js` |
| `docx_output.toc_depth` (A2, 2026-07-19) | 生成 docx の目次(TOC)に含める見出しレベル範囲（既定 `'1-3'`、現行互換）。例: `"1-4"` で h4 まで目次に含める | `generate_docx.js`（16_docx出力ハンドオフ.md §22 ナビゲーション規則） |
| `docx_output.cache_label` | キャッシュ操作の参照表記に出す、キャッシュの呼び名（既定 `"キャッシュ"`。案件で使う製品名に変えられる） | `generate_docx.js` |
| `logging.sub_label_by_kind` | 処理ステップの種類(kind)と、観測点の下位ラベルの対応を上書きする。既定は製品名を含まない語(キャッシュ操作・外部サービス呼出 など)。`apply_logging_steps.py` が読む | `apply_logging_steps.py` |

**手順書だけが参照するキー（スクリプトは読まない。設計書を書く担当が project_config から読んで従う）:**

| project_config.json のキー | 用途 | 既定値（キーが無いとき） |
|:---|:---|:---|
| `execution_locations[]` | 動作場所として書いてよい語の一覧。動作場所は、ここに定義した語だけを使う。辞書に無い語を動作場所に書かない（案件で足りなければこの配列に足す）。16_docx出力ハンドオフ.md §11 が参照する | `["ブラウザ", "サーバー(API)", "データベース", "定義のみ"]` |
| `naming.legacy_system` | 旧システム由来の命名規則（マスタ・コードの接尾辞、英数字の全角化など）。設定されているときだけ、10_論理名変換.md の旧システム前提の規則を適用する | `null`（旧システム前提の規則を適用しない） |
| `logical_name_sources[]` | 論理名の根拠として使う資料の一覧（rank の高い順に当たる）。10_論理名変換.md が参照する | `[{"rank": "A", "kind": "customer_docs", "paths": []}, {"rank": "B", "kind": "source_code"}]` |

この3つの書き方の例（値は架空。動作場所をクラウドのサービスの種類で書き分け、旧システム由来の命名規則を使う案件の場合）:

```json
{
  "execution_locations": ["ブラウザ", "アプリケーションサーバー", "RDB", "キャッシュサーバー", "オブジェクトストレージ", "定義のみ"],
  "naming": {
    "legacy_system": {"master_suffix": "マスタ", "code_suffix": "コード", "full_width_alnum": false}
  },
  "logical_name_sources": [
    {"rank": "A", "kind": "customer_docs", "paths": ["docs/用語集.xlsx"]},
    {"rank": "B", "kind": "source_code"}
  ]
}
```

別案件で再利用する際は `project_config.json` の logging / audit_logging / auth セクションを書き換えるだけ。OpenTelemetry / structlog / カスタム規約等への切替も同様にこのファイルで吸収可能。

### id_scheme.json（v43 で新設、スキル付属の ID 体系規約）

screen_config.json が「画面別」、project_config.json が「プロジェクト別」なのに対し、**id_scheme.json はスキル全体に適用される ID 体系規約** で、このスキルのフォルダの `id_scheme.json` に配置（全プロジェクト共通）。スキル本体（migrate / apply_logging_steps / apply_id_scheme / generate_docx 等）からハードコード接頭辞（"F", "B", "L" 等）を全廃するための単一拘束点。

| id_scheme.json のキー | 用途 | 使用スクリプト |
|:---|:---|:---|
| `categories[].key` | カテゴリ識別子（例 `front_process` / `backend_process` / `logging` / `area` 等の 16 種） | `apply_id_scheme.py` / `_config_loader.get_id_prefix()` 経由で全スクリプト |
| `categories[].prefix` | 2 文字接頭辞（AR/IT/EV/TR/FR/BE/LG/ST/AP/DB/VL/CA/CL/PR/MS/LN） | 上記同 |
| `categories[].display_name` | docx 表示用日本語名（例 "フロント処理" / "観測点ログ処理"） | ドキュメンテーションのみ（表ヘッダは下の `header_self` / `header_ref` を使う） |
| `categories[].header_self` / `header_ref` | 表の ID 列のヘッダ文言。`header_self` は自カテゴリの ID 列、`header_ref` は他カテゴリから参照されるときの列。「Step→ステップ」のように表記を変えたい案件は、このファイルを書き換えれば足りる | `generate_docx.js`（`getIdHeaderLabel()`） |
| `categories[].source_field` | 中間 JSON 内のパス（例 `front_processes[].process_id`）。apply_id_scheme.py が走査対象を特定 | `apply_id_scheme.py` |
| `categories[].legacy_format` | 旧体系の表記（例 "F###" / "L# + L#a-f"） | ドキュメンテーションのみ |
| `categories[].id_naming_convention` | サブラベル付き ID の命名規約（例: FR1-EDIT、LG4-a） | `apply_id_scheme.py` の 2 パス採番ロジック |
| `format.padding` | 桁数（"none" = 可変桁、数字 = ゼロパディング桁数） | `format_id()` ヘルパー |
| `format.start_number` | 連番開始番号（1 始まり） | 同上 |
| `format.sub_label_separator` | サブラベル区切り文字（"-"、例 LG4-a の "-"） | 同上 |
| `_extension_guide` | 新カテゴリ追加手順 + 例（RP 帳票 / BT バッチ / JB ジョブ / FL ファイル / EX 外部連携 等） | ドキュメンテーション（拡張時の参照） |

**スキル拡張の流れ（新カテゴリ追加時）:**

1. `id_scheme.json` の `categories[]` に新エントリ追加（key/prefix/display_name/source_field を定義）
2. prefix は既存 16 種と衝突しない 2 文字を選ぶ
3. 中間 JSON 該当セクションも追加（front_processes と並ぶ独立配列 or 既存配列の拡張）
4. generate_docx.js に表示処理を追加（既存 sectionXxx パターン）
5. `apply_id_scheme.py` が自動的に新カテゴリの ID を生成・参照を更新（**スキル本体改修不要**）

## 関連ファイル一覧

| ファイル | 役割 |
|:---|:---|
| 01_画面レイアウト.md | UI要素抽出 |
| 02_イベント一覧.md | イベントリスナー抽出 |
| 03_初期処理.md | 初期処理生成 |
| 04_機能別処理.md | イベント処理シート生成 |
| 05_DB操作.md | SQL/ORM 抽出と表形式化 |
| 06_パラメータ.md | パラメータフロー追跡 |
| 07_バリデーション.md | バリデーション/メッセージ/計算式抽出 |
| 10_論理名変換.md | 物理名→論理名対応表 |
| 11_共通記法.md | 設計書内参照記法 |
| 12_API仕様.md | API 仕様（SPA構成時） |
| 13_共通ロジック.md | 共通ビジネスロジック |
| 14_チェックリスト.md | 整合性検証 |
| 15_中間JSONスキーマ.md | 中間データ形式の契約 |
| 16_docx出力ハンドオフ.md | docx 出力の手順 |
| 17_構成図生成.md | 構成図SVG生成（Python スクリプトで生成。diagram-design 連携は任意） |
