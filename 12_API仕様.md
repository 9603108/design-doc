# API仕様抽出サブスキル

## 入力
- HTMLファイルパス（fetch/axios呼出し箇所の特定用）
- バックエンドファイルパス（存在する場合）

## 適用条件
SPA構成の場合のみ作成する。従来型のサーバーレンダリングの場合はスキップ。

## 実行手順

### 1. エンドポイントの収集

フロントエンドソースから全API呼出しを抽出し、エンドポイント一覧を作成:

| No | パス | メソッド | 説明 | 認証 |
|----|------|---------|------|------|

### 2. 各エンドポイントのリクエストパラメータ

| パラメータ名 | 型 | 必須 | デフォルト値 | バリデーション | 説明 |
|------------|-----|------|-----------|-------------|------|

パラメータ送信方法の表記:
- クエリパラメータ: `?year`
- パスパラメータ: `{id}`
- リクエストボディ: Body

### 3. レスポンススキーマ

エンドポイントごとにレスポンスのJSON構造を定義:

成功レスポンスパターン:
| パターン | 形式 |
|---------|------|
| リスト取得 | `{ "data": [...], "total": N, "page": N, "per_page": N, "pages": N }` |
| 単一レコード | `{ "data": { ... } }` |
| 登録・更新成功 | `{ "data": { ... }, "message": "..." }` |
| 削除成功 | `{ "message": "..." }` |

### 4. レスポンス項目定義

| No | キー名 | 表示名 | データ型 | 計算式 |
|----|--------|--------|---------|--------|

列数が20以上の場合、接頭辞×指標のマトリクス形式で簡略化可能。

### 5. セキュリティ対策

| 対策項目 | 実装方式 | 対象範囲 |
|---------|---------|---------|

標準対策: SQLインジェクション、XSS、CSRF、認証、入力バリデーション

### 6. 実行するDB操作の紐付け（v15 で確定）

各APIエンドポイントは内部で **複数のDB操作（SQL）を順次実行** する。§11 API仕様の各エントリには「実行するDB操作」列を追加し、`db_operations[].id` への参照リストを表示する。

中間JSON 上の表現:

```json
{
  "no": 1,
  "path": "/api/orders",
  "method": "POST",
  "action": "get_session_data",
  "description": "受注入力セッションデータを取得",
  "related_db_operations": ["db_op_get_session_data_main", "db_op_get_session_data_last_confirmed"],
  ...
}
```

`related_db_operations` は `db_operations[].api_endpoint.action == api_spec[].action` の突合で自動推測可能。

過去経緯:
- v14 まで §11 API仕様 と §12 DB操作 が別セクションで並んでいたが、両者の関係性が見えなかった
- v15 でレビュー指摘「11→12のマッピングが見えない」→ `related_db_operations` で明示的に紐付け、§11 表に「実行するDB操作」列を追加

---

## v49 改修要点（要約）— `response_spec.items[]` 拡張

### `source` / `transform_backend` フィールドを必ず付与

v49 で ③ JSONレスポンス表に「データ起源」「バック側の加工（Python）」列が追加された。Phase 1 Agent は `backend_processes[].response_spec.items[]` の各エントリに以下を抽出して付与する:

```json
{
  "no": 10,
  "key": "data.items[].invoiceAmount",
  "display_name": "請求額",
  "data_type": "integer",
  "source": {
    "type": "calculated",
    "source_ref": "②-1 受注明細データセット",
    "source_column": "order_items.quantity / order_items.unit_price / invoices.tax_rate"
  },
  "transform_backend": "数量 × 単価 × 税率 ÷ 100（小数切り捨て）"
}
```

### `source.type` の値（5 種）

| type | 意味 | 例 |
|:---|:---|:---|
| `request` | リクエストパラメータ透過 | `data.header.orderMonth` ← `order_month` |
| `db` | DB SELECT 直結 | `data.items[].productCode` ← `②-1 / order_items.product_code` |
| `calculated` | DB + Python 計算 | `data.items[].invoiceAmount` ← `②-1 + バック計算` |
| `merged` | 複数 SELECT 統合 | `data.header.lastConfirmedAt` ← `②-2 / MAX(confirmed_at)` |
| `const` | バック側固定値 | `data.message` = "受注データを確定しました" |

### `transform_backend` は業務語彙で

- ✅ OK: `date → 'YYYY/MM/DD' 書式変換`、`数量 × 単価 × 税率 ÷ 100（小数切り捨て）`、`受注単位でまとめ、明細配列にネスト`
- ❌ NG: `Python の関数 abc() を呼ぶ`、`pandas で groupby`、`内部実装の詳細`

### `response_to_screen_mapping[]` は v49 で廃止

旧 `backend_processes[].response_spec.response_to_screen_mapping[]` は v49 で `front_processes[].steps[]` の `kind='response_mapping'` step に移動。詳細は `04_機能別処理.md` v49 改修要点を参照。

