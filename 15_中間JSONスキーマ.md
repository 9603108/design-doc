# 中間JSONスキーマ定義

## このファイルの位置付け

**目的:** design-doc スキル内の全サブスキル（01_〜13_）が共通して書き込む中間データ形式を定義する。各サブスキルの出力を Markdown 連結ではなく構造化 JSON に統一することで、後段の docx 出力ハンドオフ（16_docx出力ハンドオフ.md）に渡しやすくする。

**意味合い:** スキル全体のデータ契約。サブスキル個別の出力フォーマット記述（01_〜13_ 内の「出力形式」セクション）は、最終的にこのスキーマのどのキーに入るかで読み替える。

**接続情報:**
- **書き手:** 01_画面レイアウト.md / 02_イベント一覧.md / 03_初期処理.md / 04_機能別処理.md / 05_DB操作.md / 06_パラメータ.md / 07_バリデーション.md / 10_論理名変換.md / 12_API仕様.md / 13_共通ロジック.md
- **読み手:** 14_チェックリスト.md（整合性検証）/ 16_docx出力ハンドオフ.md（最終出力）
- **保存先:** `target/intermediate/{機能名}.json`

## チェンジログ

**目的:** 本文中に散在する版数マーカー（vNN）を集約する。本文側の個別注記は「vNN」の参照のみとし、変更内容の詳細記述はこの表に一本化する（判断に迷う箇所は本文側の記述も残置）。

| 版 | 変更内容 |
|:---|:---|
| v10 | `events[].source_kind` 新設（`screen`/`internal` 分類、docx §5.1/5.2 分割用） |
| v15 | `api_spec[].related_db_operations` 新設 |
| v16 | `process_flows[]` 新設（§6 を業務フェーズ縦糸に再編）。`api_spec[].response_to_screen_mapping` / `related_cache_operations` 新設 |
| v17 | §10/§11/§12/§13/§14 を §6 配下に統合 |
| v18 | **呼ぶ側／呼ばれる側を対にする体系へ再定義（本ドキュメントの主軸）**。`trigger_groups[]` / `front_processes[]` / `backend_processes[]` 新設 |
| v20 | `screen_layout.areas[]` 新設、`items[].area_ref` 新設。画面用語 cross-reference 整合ルール確定 |
| v21 | `meta` を構成ファイル網羅型に拡張。`frontend_files[]` / `backend_files[]`（object[]）新設、`source_html_path` 削除 |
| v23 | `trigger_groups[].triggers[].dispatch[]` 新設（呼出元依存の事前チェック付き呼出ロジック） |
| v43 | `trigger_event_ref` の ID 体系（TR# 形式）確定 |
| v48 | `backend_processes[].response_spec.response_to_screen_mapping[]` が存在した旧形式（v49 で移動） |
| v49 | `steps[].kind: response_mapping` 新設。`response_to_screen_mapping[]` を `backend_processes` から `front_processes[].steps[]` へ移動（`migrate_v48_to_v49.py`）。`response_spec.items[].source` / `transform_backend` 新設。`db_op_detail.sub_no` 新設 |
| v52 | `derived_processes[]` 新設（派生処理マーク）。`screen_layout.areas[].screen_no` に裏方領域パターン（`"0"`/`null`）新設。`dispatch[].derived_from` 新設 |
| v53 | `db_operations[].api_endpoint.trigger_events`（event_code の配列。同じ action を複数イベントから呼ぶ多対一用。単数 `trigger_event` と併用可。`process_flows[]` の旧キーとは別物）を許容。`meta.frontend_files[]` は screen_config の `source_files` 指定時に HTML を介さず入る（`.tsx`/`.ts` は `kind: js`） |

---

## 設計思想（v18 で確定）

**呼ぶ側と呼ばれる側を対にする。** トリガー（呼ぶ側）と処理本体（呼ばれる側）を章レベルで分離し、参照IDで連結する。

- **呼ぶ側（処理実行条件）**: 画面操作・自動・外部 の3種のトリガー。網羅性で全処理の起動経路を保証
- **呼ばれる側（処理詳細）**: フロント処理（F001〜）／バックエンド処理（B001〜）の責務分離
- **連結**: トリガーが `calls: ["F00X"]` で呼出先を明示、フロント処理が `backend_call.backend_id: "B00X"` で呼出先を明示
- **採番**: 機能内連番。F001/F002/.../B001/B002/...
- **API粒度**: 1 API エンドポイント (method + path + action) = 1 バックエンド処理 = 1 process_id

過去経緯:
- v15 以前: §5 イベント一覧 + §6 初期処理 + §7 イベント処理索引 + §10 計算式 + §11 API仕様 + §12 DB操作（章構成が責務混在）
- v16: §6 を業務フェーズ縦糸（process_flows[]）に再編
- v17: §10/§11/§12/§13/§14 を §6 配下に統合（処理は全て §6 配下が論理的）
- **v18: 呼ぶ側／呼ばれる側 を対にする体系で再定義（本ドキュメントの主軸）**。今後の全画面で共通体系

## 全体構造（v18）

```json
{
  "meta": { ... },
  "logical_names": [ ... ],
  "screen_layout": { ... },
  "trigger_groups": [ ... ],       // v18 新設: 呼ぶ側（処理実行条件）
  "front_processes": [ ... ],       // v18 新設: 呼ばれるフロント処理（F001〜）
  "backend_processes": [ ... ],     // v18 新設: 呼ばれるバックエンド処理（B001〜）
  "derived_processes": [ ... ],    // v52 新設: 派生処理マーク（出力メニュー / CSV ダウンロード / メール通知 等）
  "validations": [ ... ],
  "calculations": [ ... ],
  "common_logic": [ ... ],
  "parameters": [ ... ],
  "messages": [ ... ],
  "diagrams": [ ... ],

  // 以下は v17 以前からのキー。process_flows のみ 15A_旧スキーマアーカイブ.md へ移動済み（デッドコード）。
  // 他は新規生成こそ v18 形式（trigger_groups/front_processes/backend_processes）が原則だが、
  // 現行描画または必須キーとして引き続き有効（下表「v17 以前からのキー」参照）
  "events": [ ... ],               // 引き続き有効。trigger_groups[] が空の場合の docx フォールバック描画で使用
  "initialization": { ... },        // 引き続き有効（必須キー）。現行描画では未参照だが検証対象
  "event_processes": [ ... ],       // 引き続き有効。現行描画では未参照だが複数の整合性ルールで参照
  "process_flows": [ ... ],         // 15A_旧スキーマアーカイブ.md へ移動済み。docx 出力対象外のデッドコード
  "db_operations": [ ... ],         // 引き続き有効。generate_docx.js の sectionDbOperations（§5.6）で現役描画
  "api_spec": [ ... ]               // 引き続き有効。generate_docx.js の sectionApiSpec で現役描画
}
```

### v18 必須キー

| キー | 必須 | 書き手サブスキル | 概要 |
|:---|:---:|:---|:---|
| `meta` | ◯ | SKILL.md (Step 1-2) | 機能名・ソースHTML・プロファイル等のメタ情報 |
| `logical_names` | ◯ | 10_論理名変換.md | 物理名→論理名対応表 |
| `screen_layout` | ◯ | 01_画面レイアウト.md | 画面UI要素一覧・画面# |
| `trigger_groups` | ◯ | 02_イベント一覧.md（v18 で改題予定: 処理実行条件） | 呼ぶ側。画面操作／自動／外部 の3種に分類してトリガー一覧 |
| `front_processes` | ◯ | 04_機能別処理.md（v18 で改題予定: フロント処理） | 呼ばれるフロント処理。F001 から連番採番、steps[] に backend_call で B00X を参照 |
| `backend_processes` | △ | 05_DB操作.md + 12_API仕様.md（v18 で統合: バックエンド処理） | 呼ばれるバックエンド処理。B001 から連番採番、1 endpoint = 1 process が原則 |

### v18 カタログキー（docx §5 処理詳細 配下のサブセクション）

| キー | 必須 | 書き手サブスキル | 概要 |
|:---|:---:|:---|:---|
| `validations` | △ | 07_バリデーション.md | フロント／バックエンド処理から参照されるバリデーション一覧 |
| `calculations` | △ | 07_バリデーション.md | 計算式一覧。process 内の `calculation_refs` から参照 |
| `common_logic` | △ | 13_共通ロジック.md | 複数処理で共有される業務ロジック |
| `parameters` | △ | 06_パラメータ.md | 処理間で受け渡す値の一覧（配列） |

### v18 メタキー

| キー | 必須 | 書き手サブスキル | 概要 |
|:---|:---:|:---|:---|
| `messages` | △ | 07_バリデーション.md | メッセージ一覧（独立章 §6） |
| `diagrams` | △ | 17_構成図生成.md | 処理構成図・画面遷移図（図は `target/scripts/` のスクリプトが作る） |

### v17 以前からのキー（現行での使用状況）

新規生成は v18 形式（trigger_groups/front_processes/backend_processes）が原則だが、以下のキーは **process_flows を除き廃止されていない**。現行の docx 描画または verify_intermediate.py の必須キーチェックで引き続き使用されているため、削除・移動は不可。

| キー | 現状 |
|:---|:---|
| `events` | **引き続き有効**。`trigger_groups[]` が空の場合の docx フォールバック描画（`sectionTriggerGroups`）で参照。verify_intermediate.py の必須キーチェック対象 |
| `initialization` | **引き続き有効**（必須キー）。現行 docx 描画では未参照だが、verify_intermediate.py の必須キー ERROR チェック対象 |
| `event_processes` | **引き続き有効**。現行 docx 描画では未参照だが、複数の整合性ルールで参照される必須キー |
| `process_flows` | **アーカイブ済み（15A_旧スキーマアーカイブ.md）**。docx 出力対象外、レンダラーから到達不能なデッドコード。将来再利用検討中のため中間JSON構造としては保持可 |
| `db_operations` | **引き続き有効**。generate_docx.js の `sectionDbOperations` で §5.6 に現役描画中 |
| `api_spec` | **引き続き有効**。generate_docx.js の `sectionApiSpec` で現役描画中。既存中間JSONに実データあり（response_to_screen_mapping/related_cache_operations を含む） |

「△」= プロファイル/特性により任意。出力時、空の場合はそのセクションを docx 化時にスキップする。

---

## 各セクションのJSONスキーマ

### meta（v21 で構成ファイル網羅型に拡張）

```json
{
  "feature_name": "顧客マスタ",
  "source_html": "customers.html",
  "frontend_files": [
    {"path": "customers.html", "kind": "html", "description": "メイン画面HTML"},
    {"path": "/common/css/common.css", "kind": "css", "description": "共通スタイル"},
    {"path": "/common/js/common.js", "kind": "js", "description": "共通スクリプト"},
    {"path": "https://cdn.tailwindcss.com", "kind": "external_cdn", "description": "TailwindCSS CDN"}
  ],
  "backend_files": [
    {"path": "customer-handler/app.py", "kind": "backend", "description": "顧客マスタAPI ハンドラ"},
    {"path": "common/python/app_logging.py", "kind": "shared_module", "description": "共通ログモジュール"}
  ],
  "project_pattern": "バックエンド統合型",
  "profile": "CRUD型",
  "generated_at": "2026-01-01T00:00:00+09:00",
  "attachments": [
    {"path": "target/attachments/customers_top.png", "description": "画面上部キャプチャ"}
  ]
}
```

| フィールド | 型 | 必須 | 説明 |
|:---|:---|:---:|:---|
| `feature_name` | string | ◯ | 設計書タイトルに用いる機能名（業務担当者の語彙で。HTMLファイル名そのままは禁止） |
| `source_html` | string | ◯ | 対象HTMLファイル名（パス除く） |
| `frontend_files` | object[] | ◯ | **画面を構成する全フロントエンドファイル**（v21 で新設、HTML + CSS + JS + 外部CDN）。`{path, kind, description}` の object。kind: `html` / `css` / `js` / `external_cdn` / `image`。screen_config の `source_files` を指定した画面（React(TSX) 等）は、そのファイルが HTML を介さず入る（HTML があればその抽出結果の後ろへ重複なく追記）。`.ts` / `.tsx` は kind=`js` として入り、kind の値は増やさない |
| `backend_files` | object[] | △ | **関連バックエンドファイル**（v21 で string[] から object[] に拡張）。バックエンド統合型/フロント+バックエンド分離型のみ。`{path, kind, description}`。kind: `backend` / `shared_module` / `infra`（`backend` = バックエンドの入口ファイル。パスに project_config の `backend_entry_patterns` のいずれかを含むもの） |
| `project_pattern` | string | ◯ | `"フロント単体型"` / `"バックエンド統合型"` / `"フロント+バックエンド分離型"`（汎用の呼び名。許容値は project_config の `allowed_project_patterns` で案件ごとに決める。キーが無ければ検査しない） |
| `profile` | string | ◯ | `"CRUD型"` / `"レポート型"` / `"ハイブリッド型"` |
| `generated_at` | string | ◯ | 生成日時。ISO8601 + JST。表示時は `YYYY/MM/DD HH:MM` に変換 |
| `attachments[]` | object[] | △ | 画面キャプチャ等の外部画像パス参照。各要素は `{path, description}`。フェーズB施策B3で新設、追加のみで既存フィールドへの影響なし |

**v21 変更履歴:**
- `source_html_path`（絶対パス）を**削除**。実行環境ごとに異なる個人パスのため、設計書として無価値だった
- `frontend_files[]`（HTML/CSS/JS 構成ファイル全網羅）を新設。HTML を解析して `<link rel="stylesheet">` / `<script src="...">` から自動抽出。ただし screen_config に `source_files`（画面を構成するソースのパス配列）があれば、それも HTML を介さず入る（HTML が無ければ source_files だけで作る）。`source_files` は screen_config のキーで、meta のキーではない
- `backend_files[]` を string[] → object[] に拡張。種別（backend / 共通モジュール）と説明を持たせ docx 表示で意味が分かるように

### logical_names

10_論理名変換.md の出力結果。物理名→論理名対応表。

```json
[
  {
    "physical_table": "customers",
    "logical_table": "顧客マスタ",
    "physical_column": "customer_code",
    "logical_column": "顧客コード"
  }
]
```

- `physical_table` ごとに列を網羅。
- project_config の `logical_name_sources` に定義した資料に名称があればそれを優先（rank A の資料が最優先。詳細は 10_論理名変換.md）。
- 上のサンプルの「顧客マスタ」「顧客コード」は、project_config の `naming.legacy_system` が既定（null）のときの値の例。`naming.legacy_system` を設定した案件では、その `master_suffix`・`code_suffix` に従った論理名になる（規則は 10_論理名変換.md。以降のサンプルも同じ）。

### screen_layout

```json
{
  "diagram_ascii": "+-------+\n| ...   |\n+-------+",
  "areas": [
    {
      "area_no": "A1",
      "area_name": "ヘッダエリア",
      "screen_no": "1",
      "description": "受注月選択・その他取引先入力モード切替・確定ボタン"
    },
    {
      "area_no": "A0",
      "area_name": "WebSocket購読エリア",
      "screen_no": "0",
      "description": "裏方領域、画面表示なし。編集ロック通知 / 排他制御通知を受信して画面状態を更新"
    }
  ],
  "items": [
    {
      "no": 1,
      "screen_no": "1",
      "area_ref": "A1",
      "item_name": "検索キーワード",
      "item_type": "Input",
      "event_code": "EV01",
      "format": "",
      "data_type": "string",
      "length": "30",
      "remarks": "",
      "example": "山田"
    }
  ]
}
```

- `diagram_ascii`: ASCII の画面レイアウト図（Mermaid 禁止）
- `areas[]`（新設経緯はチェンジログ参照）: 画面の論理エリア定義。トリガー・処理側から `area_name` で参照される画面用語の権威ソース。
  - `area_no`: 画面内ユニーク。形式 `A1`〜`A99` の3桁ゼロ埋め
  - `area_name`: 業務担当者語彙のエリア名（例: 「ワークフローステッパー」「受注明細グリッド」）
  - `screen_no`: items[].screen_no と同じ画面#（複数 area が同じ screen_no を共有してもよい）
  - **`screen_no: "0"` or `null`: 裏方領域パターン**。画面表示を持たないバックグラウンド領域（WebSocket 購読 / 排他通知 / 編集ロック通知 等）を表す。`description` 列に「裏方領域、画面表示なし」を必ず明記。`normalize_screen_terminology.py` の `area_keyword_fallback[]` にも「WebSocket / 排他通知 / 編集ロック」キーワードのフォールバック対応を追加することで、events / trigger_groups から裏方領域への参照を自動補完可能。
  - `description`: エリアの役割・含まれる主要項目（任意）
- `items[].area_ref`: その項目が属する `areas[].area_no` への参照
- `items[].item_type`: `Label` / `Input` / `Calendar` / `TimePicker` / `Checkbox` / `RadioButton` / `FileUpload` / `TextArea` / `ComboBox` / `Button` / `Grid` / `Link`
- `items[].event_code`: イベントが紐づく場合のみ。`events[].event_code` と一致すること
- `items[].example`（string、任意。フェーズB施策B3で新設）: 画面項目の入力例・表示例（例: 「2026/07」「山田太郎」）。追加のみで既存フィールドへの影響なし

**画面用語の権威ソース:**

screen_layout は「画面で使われる用語の権威ソース」。
events[].trigger / events[].location / trigger_groups[].triggers[].location / front_processes[].steps[].description /
backend_processes[].response_to_screen_mapping[].screen_item_name 等で画面項目に言及するときは、
必ず `areas[].area_name` / `items[].item_name` / 機能名（meta.feature_name + 「画面」）のいずれかと**厳密一致**させること。

不一致は機械検証で WARN／ERROR として検出される。「ワークフローステッパー[5]」のような番号付き表記、「テーブル」のような略称、screen_layout に未登録の用語は禁止。

---

## v18 メインスキーマ: 呼ぶ側／呼ばれる側 体系

### trigger_groups（呼ぶ側）

**意味合い:** 処理を起動するトリガーを「画面操作 / 自動 / 外部」の3種に分類して網羅する。各トリガーから `calls: ["F00X"]` でフロント処理の `process_id` を呼ぶ。docx §4 トリガー として出力。

```json
[
  {
    "kind": "screen_operation",
    "kind_label": "画面操作トリガー",
    "category": "受注入力画面",
    "triggers": [
      {
        "trigger_no": 1,
        "location": ["受注入力画面", "ワークフローステッパー[5]", "受注データ確定ボタン"],
        "action": "クリック",
        "description": "受注データ確定ボタンを押下する",
        "calls": ["F003"]
      }
    ]
  },
  {
    "kind": "auto",
    "kind_label": "自動トリガー",
    "category": "画面表示時",
    "triggers": [
      {
        "trigger_no": 1,
        "location": ["受注入力画面"],
        "action": "表示",
        "description": "業務担当者が受注入力画面のURLを開く",
        "calls": ["F001"]
      }
    ]
  },
  {
    "kind": "external",
    "kind_label": "外部トリガー",
    "category": "WebSocket受信",
    "triggers": [
      {
        "trigger_no": 1,
        "location": ["WebSocket購読", "editing 通知"],
        "action": "受信",
        "description": "他ユーザーが同じ受注月の受注データを編集中になる",
        "calls": ["F010"]
      }
    ]
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `kind` | ◯ | `screen_operation` / `auto` / `external` のいずれか |
| `kind_label` | ◯ | docx 表示用のラベル（例: 「画面操作トリガー」） |
| `category` | ◯ | 群の分類軸。kind=`screen_operation` なら画面名、`auto` なら「画面表示時」「タイマー」等、`external` なら「WebSocket受信」「URLパラメータ」等 |
| `triggers[]` | ◯ | 同一 group 内のトリガー一覧 |
| `triggers[].trigger_no` | ◯ | group 内連番（1から） |
| `triggers[].location` | ◯ | 発生場所の階層配列。docx §4 では `└` インデントで表示 |
| `triggers[].action` | ◯ | **動作種別の語彙のみ**（クリック / 変更 / 表示 / 受信 等）。長文記述は禁止 |
| `triggers[].calls` | ◯ | 起動するフロント処理の `process_id` 配列（例: `["F003"]`）。事前チェックなしの単純呼出時に使用 |
| `triggers[].dispatch` | △ | **呼出元依存の事前チェック付き呼出ロジック**。条件分岐を伴う呼出を表現する。形式: `[{condition, then_call?, then_message?, then_terminate?, message_ref?}, ...]`<br>例: `[{"condition": "閲覧モードでない", "then_call": "F004"}, {"condition": "閲覧モード", "then_terminate": true, "message_ref": "MSE00012"}]` |

**設計思想（アクションと処理内容の分離）:**

- **アクション** = 「業務担当者の操作種別（事象）」（クリック / 変更 / フォーカス外し 等）
- **処理内容**（docx §4 表示）= 「`calls` / `dispatch` から生成される呼出処理」（→ [F00X] name）。アクションの言い換えは禁止
- **F00X 本体の steps[]** = 「呼ばれたら無条件で実行する純粋な処理」。呼出元固有の事前チェックは含めない（複数箇所から呼ばれた際に矛盾するため）
- **呼出元固有の事前チェック** = トリガー側の `dispatch[]` に書く。例:「閲覧モードでは確定できません」は **受注データ確定ボタンクリックの dispatch** に書き、F004 受注データ確定処理本体には書かない

**dispatch[] の各要素:**

| サブフィールド | 必須 | 説明 |
|:---|:---:|:---|
| `condition` | ◯ | 業務的な条件（例: 「閲覧モードでない」「その他取引先モードかつ入力データあり」） |
| `then_call` | △ | 条件成立時に呼ぶ `front_processes[].process_id`（例: `"F004"`） |
| `then_message` | △ | 条件成立時のメッセージ表示（条件不成立で警告終了するケース） |
| `then_terminate` | △ | true なら処理終了（呼出しない）。`then_message` と併用可 |
| `message_ref` | △ | `messages[].message_id` への参照（メッセージ語彙を統一する場合） |
| **`derived_from`** | △ | **派生処理マーク**（出力メニュー / CSV ダウンロード / メール通知 等、共通 UI 経由で別画面から派生実行される処理）。値が存在すると `verify_call_graph.py` の `dead_code_candidate` 検出ロジックから除外される。例: `"出力メニュー（共通UI・一覧画面で実行）"` |

### derived_processes（派生処理マーク）

**意味合い:** 出力メニュー押下 / CSV ダウンロード / メール通知 等、`trigger_groups` に直接トリガーがなく `verify_call_graph.py` の `dead_code_candidate` 検出に誤って引っかかる処理を業務語彙で明示する。`screen_config.derived_processes[]` から `apply_trigger_dispatch.py` が読み込み、対応する trigger_groups エントリの `dispatch[].derived_from` 注入と `verify_call_graph.py` の除外リスト構築を行う。

**配置場所:** 中間 JSON 直下 `derived_processes[]` 配列 + `screen_config.derived_processes[]` 配列（後者が SSoT、前者は派生実行コピー）。

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
    "then_message": "外部のメール配信サービスの画面へ遷移、本システム外で実行",
    "derived_from": "出力メニュー → メール通知リンク"
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `trigger_event_ref` | ◯ | 対応する trigger_groups[].triggers[] への参照（TR# 形式） |
| `source_menu` | ◯ | 派生元メニュー名（業務担当者語彙、例: 「CSV ダウンロードメニュー」「メール通知メニュー」） |
| `kind` | ◯ | 派生種別。`output_menu_derived` / `external_link_derived` / `permission_request_derived` / `websocket_derived` 等の列挙値 |
| `then_call` | △ | 派生実行先の front_processes ID 配列（FR# 形式、空配列なら本システム外で実行） |
| `then_message` | △ | 業務担当者向け補足説明（例: 「一覧画面で実行（本画面は参考表示のみ）」） |
| `derived_from` | ◯ | 派生元の業務的説明（dispatch[].derived_from に注入される文字列） |

**設計意図（v52、複数画面を並行して処理したときの教訓）:**

複数の画面を並行して処理したとき、「出力メニュー → CSV ダウンロード / メール通知 等」の派生処理が `verify_call_graph.py` の dead_code_candidate 検出ロジックに誤って引っかかった。本セクションで業務的な派生関係を明示することで誤検出を抑止する。`source_menu` / `then_message` フィールドは docx 出力時に補足説明として表示可能。

### front_processes（呼ばれるフロント処理）

**意味合い:** トリガーから呼ばれるフロント側の処理本体。確認ダイアログ・クライアント計算・バックエンド呼出・画面更新の責務を持つ。docx §5.1 フロント処理 として出力。

```json
[
  {
    "process_id": "F003",
    "name": "受注データ確定処理",
    "overview": "編集中の受注データを保存し、確定済みの表示へ切り替える",
    "preconditions": ["編集モードである", "保存対象データが1件以上"],
    "postconditions": ["受注明細データが保存される", "ステップ②が活性化する"],
    "called_by_triggers": [
      { "group_kind": "screen_operation", "group_category": "受注入力画面", "trigger_no": 1 }
    ],
    "steps": [
      {
        "step_no": 1,
        "kind": "branch",
        "description": "閲覧モード判定",
        "branches": [
          { "condition": "閲覧モード", "then": "「閲覧モードでは確定できません」と表示して終了", "message_ref": "MSE00012" },
          { "condition": "編集モード", "then": "次ステップへ" }
        ]
      },
      {
        "step_no": 2,
        "kind": "validation",
        "description": "受注月チェック",
        "validation_ref": "V001"
      },
      {
        "step_no": 3,
        "kind": "data_load",
        "description": "保存データ収集（商品×顧客の全パターン分、計算結果を含む）"
      },
      {
        "step_no": 4,
        "kind": "backend_call",
        "description": "請求計算結果を受注明細データに一括 UPSERT する",
        "backend_call": {
          "backend_id": "B002",
          "request_summary": "order_month + items[商品×顧客×パターン]",
          "response_handling": [
            { "on": "data.updated_count + inserted_count > 0", "then": "ステップ①を ✓ に更新、ステップ②を活性化" },
            { "on": "エラー応答", "then": "エラーメッセージ表示、状態維持", "message_ref": "MSGE00001" }
          ]
        }
      },
      {
        "step_no": 5,
        "kind": "screen_update",
        "description": "ステップ表示を「確定済」に切替"
      }
    ],
    "validation_refs": ["V001"],
    "calculation_refs": ["C001", "C002"],
    "common_logic_refs": ["CL001"],
    "parameter_refs": [],
    "notes": [
      { "kind": "注意", "text": "確定後は同月の再編集不可（別途「解除」操作が必要）" }
    ]
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `process_id` | ◯ | `F` + 3桁ゼロ埋め連番。機能内ユニーク。例: `F001`, `F002`, ... |
| `name` | ◯ | **業務担当者語彙の短い処理名**（最大30文字、句点まで）。例: 「受注データを確定する」「商品行を追加する」。処理の目的は `overview` に30字以内で、手順の詳細は `steps[]` に書く |
| `area` | ◯ | docx §5.1 配下のエリア分類。同じ area を持つ front_process が H4 で1群にまとまる。標準語彙: 「初期処理」「ヘッダ操作」「ワークフロー操作」「パターン切替」「データ編集」「画面遷移」「編集権制御」「帳票出力」「ダイアログ操作」「サジェスト」「動作検証」等。画面のUI領域単位 or 業務操作カテゴリで分ける |
| `overview` | ◯ | 処理の目的を1文で。**上限30字（超えると verify_intermediate.py が ERROR）、推奨25字**。物理名（テーブル名・関数名など）を書かない。Phase 4 が作り直す値で、初期処理は「処理名。URLパラメータ条件: …」の形で自動生成されて長くなりやすく、他の処理は `events[].content` の最初の1文がそのまま入る。長くなるときは screen_config の `process_overview_overrides`（キー = `process_id`）で短い文に上書きする |
| `preconditions` | △ | 業務的前提条件の配列 |
| `postconditions` | △ | 正常終了時の業務的結果 |
| `called_by_triggers` | ◯ | 呼出元トリガーへの逆引き参照。`{group_kind, group_category, trigger_no}` の配列 |
| `steps[]` | ◯ | 処理ステップ。kind は process_flows と同じ語彙に + `backend_call` を追加 |
| `steps[].kind` | ◯ | `data_load` / `branch` / `validation` / `confirmation` / `backend_call` / `api_call` (表示ラベル:「API実行」) / `screen_update` / `response_mapping` / `state_update` / `calculation` / `transition` / `event_emit` / `logging_call` / `error_handling` |
| `steps[].backend_call` | △ | kind=`backend_call` のとき必須。`{backend_id, request_summary, response_handling[{on,then,message_ref?}]}` |
| `steps[].response_mapping` | △ | kind=`response_mapping` のとき必須。直前の `backend_call` の応答を画面項目に変換する。`{backend_id, mappings[{api_field_path, screen_area_no, item_ref_no, screen_item_name, transform}]}`。docx では §5.1 の配下に独立小節を生成し、表は 4 列（No / 画面項目（AR# / IT# / 名称）/ API フィールド / JS 変換）。**旧 backend_processes[].response_spec.response_to_screen_mapping[] は本フィールドに移動**（`migrate_v48_to_v49.py` で自動変換、詳細はチェンジログ参照） |
| `validation_refs` | △ | 使用するバリデーションID配列（`validations[].id`） |
| `calculation_refs` | △ | 使用する計算式ID配列（`calculations[].id`） |
| `common_logic_refs` | △ | 使用する共通ロジックID配列（`common_logic[].id`） |
| `parameter_refs` | △ | 使用するパラメータセット名配列 |
| `notes[]` | △ | 業務担当者への注意書き。各要素は `{kind: '注意'|'補足'|'制約', text: string}`。フェーズB施策B3で新設、追加のみで既存フィールドへの影響なし |

### backend_processes（呼ばれるバックエンド処理）

**意味合い:** フロント処理から呼ばれるバックエンド側の処理本体。1 API エンドポイント (method + path + action) = 1 backend_process が原則。リクエスト仕様／内部処理ステップ（バリデーション・DB操作・キャッシュ操作・計算）／レスポンス仕様 を持つ。docx §5.2 バックエンド処理 として出力。

```json
[
  {
    "process_id": "B002",
    "name": "受注データ確定（UPSERT）",
    "overview": "請求計算の結果を受注明細データへ一括で保存する",
    "called_by_front": ["F003"],
    "endpoint": {
      "method": "POST",
      "path": "/orders",
      "action": "order_save",
      "auth": "JWT"
    },
    "request_spec": {
      "params": [
        { "name": "action", "type": "string", "required": true, "location": "body", "validation": "固定値 'order_save'", "description": "アクション名" },
        { "name": "order_month", "type": "integer", "required": true, "location": "body", "validation": "YYYYMM形式", "description": "受注月" },
        { "name": "items", "type": "array", "required": true, "location": "body", "validation": "1件以上", "description": "商品×顧客×パターン のレコード配列" }
      ]
    },
    "processing": [
      {
        "step_no": 1,
        "kind": "validation",
        "description": "リクエストパラメータバリデーション（必須/型/件数）"
      },
      {
        "step_no": 2,
        "kind": "cache_op",
        "description": "編集ロック保持者確認（キャッシュ）",
        "cache_op": { "operation": "キャッシュ GET", "key_pattern": "lock:orders:{order_month}", "purpose": "自分が保持しているロックか確認" }
      },
      {
        "step_no": 3,
        "kind": "db_operation",
        "description": "受注明細データ UPSERT（送信商品ごと）",
        "db_op_detail": {
          "id": "db_op_order_save_upsert_order_items",
          "operation_type": "INSERT (ON CONFLICT UPDATE)",
          "dataset_name": "受注明細データ",
          "transaction": true,
          "insert_values": []
        }
      },
      {
        "step_no": 4,
        "kind": "db_operation",
        "description": "税率マスタ更新（既存顧客）",
        "db_op_detail": { "id": "db_op_order_save_update_invoices_existing", "operation_type": "UPDATE", "transaction": true }
      }
    ],
    "response_spec": {
      "pattern": "登録・更新成功",
      "schema": { "data": "object" },
      "items": [
        {
          "no": 1,
          "key": "data.message",
          "display_name": "完了メッセージ",
          "data_type": "string",
          "source": { "type": "const", "source_ref": null, "source_column": null },
          "transform_backend": "成功時に固定文言「受注データを確定しました」を組立"
        },
        {
          "no": 2,
          "key": "data.updated_count",
          "display_name": "更新件数",
          "data_type": "integer",
          "source": { "type": "calculated", "source_ref": "②-1 受注明細データ UPSERT", "source_column": null },
          "transform_backend": "UPSERT 実行結果の UPDATE 件数を集計"
        },
        {
          "no": 3,
          "key": "data.inserted_count",
          "display_name": "登録件数",
          "data_type": "integer",
          "source": { "type": "calculated", "source_ref": "②-1 受注明細データ UPSERT", "source_column": null },
          "transform_backend": "UPSERT 実行結果の INSERT 件数を集計"
        }
      ]
    },
    "error_patterns": [
      { "condition": "order_month 不正", "status_code": 400, "message_id": "MSGE00001" },
      { "condition": "編集ロック未保持", "status_code": 423, "message_id": "MSGE00002" }
    ],
    "notes": [
      { "kind": "制約", "text": "1回のリクエストで処理できる件数上限は1000件（それ以上は分割呼出が必要）" }
    ]
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `process_id` | ◯ | `B` + 3桁ゼロ埋め連番。機能内ユニーク。例: `B001`, `B002`, ... |
| `name` | ◯ | **業務担当者語彙の短い処理名**（最大30文字、句点まで）。例: 「受注データを保存する」「編集ロックを取得する」 |
| `area` | ◯ | docx §5.2 配下のエリア分類。同じ area を持つ backend_process が H4 で1群にまとまる。標準語彙: 「データ取得」「データ更新」「排他制御」「マスタ参照」「帳票生成」等。業務機能カテゴリで分ける |
| `overview` | ◯ | 処理の目的を1文で。**上限30字（超えると verify_intermediate.py が ERROR）、推奨25字**。物理名を書かない。Phase 4 が `api_spec[].description` から作るので、Phase 1 の `api_spec[].description` を30字以内で書く |
| `called_by_front` | ◯ | 呼出元フロント処理の `process_id` 配列 |
| `endpoint` | ◯ | `{method, path, action, auth}` |
| `request_spec.params[]` | △ | リクエストパラメータ一覧 |
| `processing[]` | ◯ | 処理ステップ |
| `processing[].kind` | ◯ | `validation` / `db_operation` / `cache_op` / `calculation` / `response_build` / `transaction` / `error_handling` |
| `processing[].db_op_detail` | △ | kind=`db_operation` のとき。SQL詳細（v17 db_operations と同形式） |
| `processing[].cache_op` | △ | kind=`cache_op` のとき。`{operation, key_pattern, ttl_sec?, purpose}` |
| `processing[].db_op_detail.sub_no` | △ | §5.2 BEX 内【②DB問合せ】の章内サブ番号。`processing[]` 内の db_operation 出現順に `②-1`, `②-2`, `②-3` を自動採番。`response_spec.items[].source.source_ref` から `②-X 〜データセット` 形式で参照される |
| `response_spec` | ◯ | `{pattern, schema, items[]}`（`response_to_screen_mapping[]` は `front_processes[].steps[]` に移動済、詳細はチェンジログ v49 参照） |
| `response_spec.items[].source` | ◯ | `{type, source_ref, source_column}`。type は `request`（リクエスト透過）/ `db`（DB SELECT 直結）/ `calculated`（DB + バック計算）/ `merged`（複数 SELECT 統合）/ `const`（固定値）。source_ref は `②-X データセット名`、source_column は `テーブル.列名` |
| `response_spec.items[].transform_backend` | ◯ | バック側 Python での加工内容を業務語彙で記述（例: `"date → 'YYYY/MM/DD' 書式変換"`, `"数量 × 単価 × 税率 ÷ 100（小数切り捨て）"`, `"パターン単位で辞書化、商品配列にネスト"`）。「そのまま」「DB の生値をそのまま」も明示する（不可視の加工がないことを示す） |
| `error_patterns[]` | △ | エラー応答パターン一覧 |
| `notes[]` | △ | 業務担当者への注意書き。各要素は `{kind: '注意'|'補足'|'制約', text: string}`。フェーズB施策B3で新設、追加のみで既存フィールドへの影響なし |

### v18 整合性ルール（14_チェックリスト.md / verify_intermediate.py で機械検証）

12. **trigger → front_process 整合**: `trigger_groups[].triggers[].calls[]` ⊆ `front_processes[].process_id`
13. **front_process → backend_process 整合**: `front_processes[].steps[].backend_call.backend_id` ⊆ `backend_processes[].process_id`
14. **backend_process 逆参照整合**: `backend_processes[].called_by_front[]` ⊆ `front_processes[].process_id`、かつ「呼出関係が双方向で一致」（F が B を呼んでいるなら B の called_by_front に F が含まれる）
15. **エンドポイント一意性**: `backend_processes[].endpoint` の `(method, path, action)` 組合せが一意（1 endpoint = 1 process 原則）
16. **process_id 採番規約**: `F001, F002, ...` / `B001, B002, ...` の連番。3桁ゼロ埋め必須、重複なし
17. **validation/calculation/common_logic 参照整合**: `front_processes[].validation_refs[]` ⊆ `validations[].id`、`calculation_refs[]` ⊆ `calculations[].id`、`common_logic_refs[]` ⊆ `common_logic[].id`
18. **画面用語 cross-reference 整合（ERROR）**: 以下の各フィールドに含まれる画面用語の各要素は、`screen_layout.areas[].area_name` ∪ `screen_layout.items[].item_name` ∪ `{meta.feature_name}画面` のいずれかと**厳密一致**すること
    - `events[].location[]` の各要素
    - `trigger_groups[].triggers[].location[]` の各要素
    - `backend_processes[].response_to_screen_mapping[].screen_item_name`
    - `front_processes[].steps[].screen_updates_ref` / `description`（厳密一致は問わないが、出現する画面項目言及は areas/items の語彙に揃える）
19. **items.area_ref 整合（ERROR）**: `screen_layout.items[].area_ref` ⊆ `screen_layout.areas[].area_no`（指定時のみ）
20. **areas[] と items[] の screen_no 整合（WARN）**: 同じ area_ref を持つ items[] は全て同じ screen_no を持つこと（areas[].screen_no と一致）

### events

```json
[
  {
    "no": 1,
    "event_code": "EV00",
    "trigger": "受注入力画面の初期表示時",
    "location": ["受注入力画面"],
    "action": "初期表示",
    "content": "初期処理",
    "classification": "サーバー"
  },
  {
    "no": 4,
    "event_code": "EV03",
    "trigger": "受注入力画面のワークフローステッパー[5]「受注データ確定」ボタンをクリックする。",
    "location": ["受注入力画面", "ワークフローステッパー[5]", "「受注データ確定」ボタン"],
    "action": "クリック",
    "content": "受注データを保存する",
    "classification": "サーバー"
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `no` | ◯ | 1からの通し番号（一覧表の No 列に出力） |
| `event_code` | △ | `EV00`〜の連番。識別用ID。**Phase 1 の出力では必ず付ける**（`EV00` = 初期表示、以降 `EV01`, `EV02`… の連番）。パイプラインの後半（migrate_v49_event_unify.py）が `no` / `legacy_event_code` の形へ移すので、`event_code` を持たない形はパイプライン通過後の最終形である。screen_config の `process_names_by_event_code` と `trigger_dispatch` のキーはこの Phase 1 の `event_code`。docx の §4 トリガーの表には出力しない（表の ID 列は `trigger_groups[].triggers[].trigger_no`）。01_画面レイアウト.md の項目説明テーブル「イベントNo.」列で no への逆引きに使用 |
| `trigger` | ◯ | 旧フィールド。生の自然文（互換性のため維持、location が空のときフォールバック表示） |
| `location` | ◯ | **発生場所の階層配列**。`[画面, 領域, 要素]` の順。convert_events_location_action.py で trigger から自動抽出。docx では §4 トリガーの表に階層表示（└ インデント付き複数行）。表が読むのは `trigger_groups[].triggers[].location` で、画面操作トリガーは「項目」列に3番目以降の要素、自動・外部トリガーは「発生場所」列に全要素を出す |
| `action` | ◯ | **動作種別の短文**。`クリック` / `変更` / `フォーカス外し` / `初期表示` / `押下` 等。docx では §4 トリガーの表の「アクション」列に出力（表が読むのは `trigger_groups[].triggers[].action`） |
| `source_kind` | ◯ | **画面トリガー / 内部処理の分類**。`screen`（業務担当者が直接操作）or `internal`（裏で呼ばれる処理）。convert_events_location_action.py の classify_source_kind で自動判定。`screen` / `internal` の分類を持つキー。docx の §4 トリガーの表は `trigger_groups[]` から作るためこのキーを読まない（`trigger_groups[]` が空のときの代替描画だけが、このキーで分ける） |
| `content` | ◯ | 処理内容の1行サマリ（一覧で俯瞰用） |
| `classification` | ◯ | `"サーバー"` / `"クライアント"`（処理区分、source_kind とは別軸） |

**location/action の生成元:**
- 02_イベント一覧.md のトリガー定型文を `convert_events_location_action.py` がパース
- パターン: `{画面名}の{階層1}[N]{階層2}を{動作}する。` → `location=[画面名, 階層1[N], 階層2], action=動作`
  - `{画面名}` は screen_config の `screen_name` をそのまま書く。`screen_name` は「〜画面」で終わる正式名（例: 受注入力画面）なので、型の中で「{画面名}画面」と重ねて書かない（「受注入力画面画面」になり、パースできず location が外れる）。画面名は trigger 文の先頭に前方一致する条件なので、trigger 文の先頭の語と同じ字面で書く
  - 分割規則: screen_config の `screen_name`（直後に「（…）」が任意で付いてよい）と `areas[].area_name` を trigger 文の先頭から前方一致で取り除き、残りを `[N]` 区切りの階層として扱う。項目名は「の」で割らないので、「の」を含む項目名を書ける
  - `screen_name` が空、前方一致しない、または設定ファイルが読めないときは従来規則（非貪欲に「…画面」までを取り、「の」でも割る）に落ちる
- パース失敗時は `location=[trigger全文], action=""` でフォールバック

### initialization

```json
{
  "pattern": "DB取得",
  "overview": "初期表示処理...",
  "db_operations_ref": ["db_op_init"],
  "items": [
    {
      "no": 1,
      "screen_no": "1",
      "item_name": "顧客コード",
      "active": "活性",
      "visible": "表示",
      "initial_value": "Blank"
    }
  ]
}
```

- `pattern`: `"外部アプリ連携"` / `"DB取得"` / `"基本"`
- `db_operations_ref`: DB取得パターンの場合、`db_operations[].id` の配列
- `items[].active`: `"活性"` / `"非活性"`
- `items[].visible`: `"表示"` / `"非表示"`

### event_processes

```json
[
  {
    "event_code": "EV01",
    "pattern": "§9-A",
    "overview": "顧客を検索する。",
    "details": [
      "・顧客検索パラメータセット",
      "・検証処理",
      "..."
    ],
    "db_operations_ref": ["db_op_search"],
    "parameter_refs": ["param_search"]
  }
]
```

- `pattern`: `"§9-A"` 検索 / `"§9-B"` 登録更新 / `"§9-C"` 画面遷移 / `"§9-D"` ポップアップ表示 / `"§9-E"` ポップアップ閉じる / `"§9-F"` データリセット / `"§9-G"` 値変更 / `"§9-H"` データ削除 / `"§9-I"` 帳票出力
- `details`: 処理詳細の箇条書き行。Md 内の `＜機能説明＞` に相当
- `overview` は業務担当者の語彙で記述（共通の規則）。Phase 4 がこの最初の1文（最初の句点まで）を `front_processes[].overview` に写すので、最初の1文は30字以内（推奨25字）にする

### process_flows

**アーカイブ済み（15A_旧スキーマアーカイブ.md）。** 詳細スキーマ・フィールド定義は移動先を参照。docx 出力対象外、レンダラー（generate_docx.js）から到達不能なデッドコード。新規 Phase 1 Agent はこのキーへの新規書込みをしないこと。

### db_operations

```json
[
  {
    "id": "db_op_search",
    "api_endpoint": {
      "method": "GET",
      "path": "/customers/search",
      "trigger_event": "EV01"
    },
    "operation_type": "SELECT",
    "dataset_name": "顧客検索データセット",
    "dataset": [
      { "no": 1, "logical_column": "顧客コード", "logical_table": "顧客マスタ", "physical_column": "customer_code" }
    ],
    "joins": [
      { "type": "FROM", "logical_table": "顧客マスタ", "condition": "" }
    ],
    "where": [
      { "and_or": "AND", "logical_table": "顧客マスタ", "logical_column": "削除フラグ", "operator": "=", "param_no": "-", "param_name": "0 (0：未削除)" }
    ],
    "order_by": [
      { "priority": 1, "logical_table": "顧客マスタ", "logical_column": "顧客コード", "direction": "昇順", "null_position": "" }
    ],
    "transaction": false
  }
]
```

INSERT/UPDATE/DELETE の場合:

```json
{
  "id": "db_op_save",
  "api_endpoint": { "method": "POST", "path": "/customers", "trigger_event": "EV02" },
  "operation_type": "INSERT",
  "insert_values": [
    { "no": 1, "logical_column": "顧客コード", "value": "[顧客登録パラメータセット].顧客コード" }
  ],
  "transaction": true,
  "error_message_id": "MSE00001"
}
```

- `id`: 内部参照キー。`event_processes[].db_operations_ref` から参照
- `transaction`: INSERT/UPDATE/DELETE は必ず `true`
- `operation_type` ごとに必要なフィールドが変わる（上記の通り）
- `api_endpoint.trigger_event`: この API を呼ぶ Phase 1 の event_code（`events[].event_code` と同じ語彙。例 `EV01`）。1件に1つ
- `api_endpoint.trigger_events`（任意）: event_code の配列。同じ action を複数イベントから呼ぶ多対一に使う。単数 `trigger_event` は従来どおりで併用できる。Phase 1 では event_code で書く（最終形で `events[].no` に置き換わる場合があるが、書き手は event_code のまま書けばよい）。`process_flows[]` の旧キー `trigger_events`（events[].no へ変換される）とは別物で、api_endpoint の中のキー

### parameters

処理間で受け渡す値の一覧。**配列**で持つ（オブジェクトではない）。

```json
[
  {
    "no": 1,
    "name": "受注月",
    "type": "integer",
    "passed_between": ["受注データ取得", "受注データ保存"],
    "description": "受注データを特定する年月"
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `no` | ◯ | 1からの通し番号 |
| `name` | ◯ | パラメータの業務名。`name` と `description` が両方とも空だと verify_intermediate.py が ERROR にする |
| `type` | △ | 値の型（`string` / `integer` / `array` 等） |
| `passed_between[]` | △ | この値を受け渡す処理・API の名前の配列 |
| `description` | △ | 値の意味を業務担当者の語彙で1文 |

- Phase 1 では `_partial_backend.json` の `parameters_backend` に書く（merge_partials.py が `parameters` へ移す）。
- 旧形のオブジェクト（`input_params` / `transition_params` / `post_body_params` の3キー）は廃止。generate_docx.js・verify_intermediate.py のどちらも読まない。

### validations

```json
[
  {
    "no": 1,
    "field": "顧客コード",
    "rule": "入力必須",
    "applied_when": "保存ボタンのクリック時。未入力なら保存を中止する",
    "message_id": "MSE00001"
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `no` | ◯ | 1からの通し番号 |
| `field` | ◯ | 対象の画面項目名（`screen_layout.items[].item_name` の語彙） |
| `rule` | ◯ | チェックの内容（必須 / 形式 / 範囲 / 相関 等を業務担当者の語彙で） |
| `applied_when` | ◯ | 適用する条件と、違反したときの挙動 |
| `message_id` | △ | 違反時に表示するメッセージ。`messages[].message_id` と一致させる |

- docx §5.3 の表の列は「No / 対象項目 / ルール / 適用条件と違反時の挙動 / メッセージID」で、上のキーをこの順に出す。
- `field` / `rule` / `applied_when` が3つとも空の行は verify_intermediate.py が ERROR にする。
- 記述順: 1.必須チェック → 2.フォーマットチェック → 3.その他
- 旧形のキー（対象項目・チェック種別・フォーマット・内容を別々に持つ形）は廃止。generate_docx.js は読まない。

### messages

```json
[
  {
    "message_id": "MSE00001",
    "code": "MSE00001",
    "severity": "error",
    "message": "{項目名}は必須項目です。",
    "emitted_by_api": []
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `message_id` | ◯ | メッセージの識別子。`validations[].message_id` などの参照先。識別子のキー名は `message_id` に統一する（`id` と書かない） |
| `code` | ◯ | メッセージコード。docx §6 の「コード」列に出す |
| `severity` | ◯ | 種別（`error` / `warning` / `info`） |
| `message` | ◯ | メッセージ本文 |
| `emitted_by_api[]` | △ | このメッセージを返す API の配列。画面側だけで出すメッセージは空配列 |

- コードのプレフィックス: `MSE` (エラー) / `MSW` (確認) / `MSI` (通知) / `MSGE` (グローバルエラー)
- 同一文言は同一の `message_id` を再利用
- Phase 1 では `_partial_backend.json` の `messages_backend` に書く（merge_partials.py が `messages` へ移す）。

### calculations

```json
[
  {
    "no": 1,
    "name": "合計金額",
    "formula": "単価 × 数量",
    "inputs": [
      { "name": "単価", "source": "商品マスタ（単価）" },
      { "name": "数量", "source": "画面入力（数量列）" }
    ],
    "outputs": [
      { "name": "合計金額", "destination": "明細行の金額セル" }
    ],
    "remarks": "円未満は切り捨てる"
  },
  {
    "no": 2,
    "name": "請求額",
    "formula": "請求区分が「税抜」なら 合計金額 × 税率、「税込」なら 合計金額、それ以外は 0",
    "inputs": [
      { "name": "合計金額", "source": "合計金額の計算結果" },
      { "name": "請求区分", "source": "画面入力（区分選択）" }
    ],
    "outputs": [
      { "name": "請求額", "destination": "明細行の請求額セル" }
    ],
    "remarks": ""
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `no` | ◯ | 1からの通し番号 |
| `name` | ◯ | 計算結果の名前。docx §5.4 に結果名として出す |
| `formula` | ◯ | 基本式。業務担当者の語彙で書く。条件で式が変わる場合も、この1つの文字列の中に条件ごとの式を書く |
| `inputs[]` | △ | 入力値。各要素は `{name, source}`（`source` = 値の出どころ） |
| `outputs[]` | △ | 出力値。各要素は `{name, destination}`（`destination` = 結果の表示先・保存先） |
| `remarks` | △ | 端数処理・ゼロ除算・NULL の扱いなどの補足 |

- 旧形のキー（結果名・式・条件ごとの式の配列を別々に持つ形）は廃止。generate_docx.js は読まない。

### api_spec

SPA構成時のみ。詳細は 12_API仕様.md 参照。`related_db_operations` / `response_to_screen_mapping` / `related_cache_operations` 各フィールドの追加経緯はチェンジログ参照。**現行有効**（generate_docx.js の `sectionApiSpec` で現役描画中、既存中間JSONに実データあり）。

```json
[
  {
    "no": 1,
    "path": "/customers/search",
    "method": "GET",
    "description": "顧客を検索する",
    "auth": "JWT",
    "request_params": [
      { "name": "keyword", "type": "string", "required": false, "location": "query", "default": "", "validation": "", "description": "検索キーワード" }
    ],
    "response_pattern": "リスト取得",
    "response_schema": {
      "data": "array",
      "total": "number",
      "page": "number",
      "per_page": "number",
      "pages": "number"
    },
    "response_items": [
      { "no": 1, "key": "data[].customer_code", "display_name": "顧客コード", "data_type": "string", "expression": "" }
    ],
    "related_db_operations": ["db_op_search_customers"],
    "related_cache_operations": [],
    "response_to_screen_mapping": [
      {
        "api_field_path": "data[].customer_code",
        "screen_no": "1",
        "screen_item_name": "顧客コード",
        "transform": ""
      },
      {
        "api_field_path": "data[].customer_name",
        "screen_no": "1",
        "screen_item_name": "顧客名",
        "transform": ""
      }
    ]
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `related_db_operations` | △ | このAPIが内部で実行するDB操作のid配列（`db_operations[].id` への参照）。merge_partials.py または手動で `db_operations[].api_endpoint.action == api_spec[].action` の突合により自動推測可能。docx に API 仕様の独立した章は無いが、この値は `migrate_v17_to_v18.py` が `backend_processes[].processing[]` の db_operation step へ移し、docx では 5.2 バックエンド処理の【②DB問合せ】に出る。省くと該当 API の DB 問合せが docx から消えるので、DB 操作がある API には必ず書く |
| `related_cache_operations` | △ | このAPIが操作するキャッシュ系の処理一覧。ロックAPI等、DB操作を伴わずキャッシュ層のみで完結するAPIで使用。各要素は `{ "operation": "キャッシュ SET/GET/DEL", "key_pattern": "lock:{ym}", "ttl_sec": 300, "purpose": "編集ロック取得" }` 形式 |
| `response_to_screen_mapping` | △ | APIレスポンスのフィールドが画面のどの項目に表示されるかのマッピング。`{ api_field_path, screen_no, screen_item_name, transform }` の配列。`api_field_path` は `response_items[].key` と同形式（例: `data[].customer_code`、`session.last_confirmed_at`）。`transform` は画面表示前の変換（例: `"YYYY/MM/DD HH:MM 形式に変換"`、`"カンマ区切り整形"`）。空文字なら無変換。業務担当者が「レスポンスの中身が画面のどこに出るか」を辿るための項目。docx に API 仕様の独立した章は無いが、この値は `migrate_v17_to_v18.py` と `migrate_v48_to_v49.py` が `front_processes[].steps[]` の `response_mapping` step へ移し、docx では 5.1 フロント処理の「レスポンスの画面表示先」の表（No / 画面項目 / API フィールド / JS 変換）に出る。省くとこの表が docx から消えるので、レスポンスを画面に出す API には書く |

### common_logic

```json
[
  {
    "no": 1,
    "name": "請求額計算",
    "expression": "...",
    "used_in": ["EV03 請求計算", "請求一覧画面"],
    "zero_division": "NULL → COALESCE(0)",
    "null_handling": "数値NULL は COALESCE(0)"
  }
]
```

### diagrams（任意、構成図を載せるときのみ）

図は `target/scripts/` の Python スクリプトが作る（diagram-design スキルは任意の代替。無くても作れる）。詳細仕様は 17_構成図生成.md。docx 埋め込みは 16_docx出力ハンドオフ.md「23. 図ガバナンス」を参照。

```json
[
  {
    "id": "process_structure",
    "type": "flowchart",
    "title": "処理構成図",
    "doc_section": "§2.1",
    "spec": {
      "nodes": [
        { "id": "start", "label": "...", "style": "start|process|decision|end" }
      ],
      "edges": [
        { "from": "...", "to": "...", "label": "（任意）" }
      ]
    },
    "html_path": "target/diagrams/{機能名}_{id}.html",
    "svg_path": "target/diagrams/{機能名}_{id}.svg",
    "embed_size": { "width_px": 600, "height_px": 400 },
    "caption": "処理構成図（初期処理〜受注データ確定までの流れ）",
    "reading_note": "上から下へ処理が進む。菱形は分岐、矢印のラベルは分岐条件を示す。"
  }
]
```

| フィールド | 必須 | 説明 |
|:---|:---:|:---|
| `id` | ◯ | 図の内部ID（英小文字 + アンダースコア）。`id` が `derivation_chain_` で始まる場合（例: `derivation_chain_billing_amount`。計算式や共通ロジックの no 等で一意化する。1機能内に複数の導出チェーン図が生成されうるため固定値にしない）は計算導出チェーン図（17_構成図生成.md参照）、`type` は `'flowchart'` を指定する |
| `type` | ◯ | 図の種類（flowchart / architecture / swimlane 等） |
| `title` | ◯ | 設計書に表示する図のタイトル |
| `doc_section` | ◯ | 図を載せる章の覚え書き。実際の配置は図の `id` で決まり（generate_docx.js はこの値を読まない）、`process_structure` = §2.1、`ipo_flowchart` = §2.2、`screen_transition` = §2.3、`screen_structure` = §3.1、`data_flow` = §5.6、`derivation_chain_*` = §5.4 / §5.5。値はこの対応に合わせて書く |
| `spec` | ◯ | 図の定義（nodes / edges）。図を作るスクリプトが読む |
| `html_path` | ◯ | 図を作るスクリプトが出力するHTMLパス |
| `svg_path` | ◯ | SVG抽出後のパス（extract_svg.py で生成） |
| `embed_size` | ◯ | docx 埋め込みピクセルサイズ |
| `caption` | △ | 図の表示キャプション文字列。将来的にA3実装の定型読み方注記（FIGURE_READING_NOTES）をLLM生成文で上書きできるようにする受け皿。フェーズB施策B3で新設、追加のみで既存フィールドへの影響なし |
| `reading_note` | △ | 図の読み方1〜2文。`caption` 同様、定型読み方注記（FIGURE_READING_NOTES）の上書き用受け皿。フェーズB施策B3で新設 |

---

## 整合性ルール（14_チェックリスト.md で機械検証する）

1. **イベントコード整合**: `screen_layout.items[].event_code` ⊆ `events[].event_code`（Phase 1 の形。パイプライン通過後の最終形では `items[].event_ref` ⊆ `events[].no`）
2. **EV番号網羅**: `events[]` の全 EV* に対して `event_processes[]` または `initialization` のいずれかが対応
3. **DB操作参照整合**: `event_processes[].db_operations_ref` ⊆ `db_operations[].id`
4. **パラメータの実体**: `parameters[]` は配列で、各要素の `name` と `description` の少なくとも一方が空でないこと（両方空は ERROR）
5. **論理名整合**: 全ての `logical_column` / `logical_table` が `logical_names[]` に存在
6. **メッセージID整合**: `validations[].message_id` ⊆ `messages[].message_id`
7. **業務語彙チェック**（共通の規則）: `meta.feature_name` / `events[].trigger` / `event_processes[].overview` に旧システムの物理名（project_config の `forbidden_terms_map.terms` に書いた語）が含まれていないこと
8. **日付時刻フォーマット**: 文字列内の日時表現が `YYYY/MM/DD HH:MM` 形式（共通の規則）

（ルール9・10は `process_flows[]` 関連のため 15A_旧スキーマアーカイブ.md へ移動済み。番号は `target/scripts/verify_intermediate.py` のdocstring参照との整合のため詰めていない）

11. **response_to_screen_mapping → 画面要素整合**: `api_spec[].response_to_screen_mapping[].screen_no` ⊆ `screen_layout.items[].screen_no`、かつ `screen_item_name` が当該画面の `items[].item_name` に存在（`api_spec[]` は現行描画で使用中のため本ルールも15_に残置）

## 保存方式

中間JSONは `target/intermediate/{機能名安全化}.json` に保存。機能名安全化規則:

- 全角→半角、空白→アンダースコア、ファイル禁止文字（`/\:*?"<>|`）→アンダースコア
- 例: 機能名「顧客マスタ」→ `顧客マスタ.json`（日本語ファイル名そのままOK、本プロジェクトではUTF-8前提）
- 衝突時はサフィックス `_2`, `_3` ...
