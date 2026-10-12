# H2 DB 操作と論理名の写し(haiku)

## 入力
- `{機能名}_backend_draft.json` の `apis.<action>.dataset`(物理テーブル・物理列)
- バックエンドのソースの SQL、DDL(あれば)、`05_DB操作.md` の構造化表の節、`10_論理名変換.md`

## 書く欄(`_unit_H2.json`。db_operations の行を作る)
- `logical_names[]`: {physical_table, logical_table, physical_column, logical_column}。dataset に出る列を全部
- `db_operations[]`: {id, api_endpoint, operation_type, dataset[], joins[], where[], group_by[], aggregate, order_by[], insert_values[], update_values[], transaction, error_message_id}
  - id は `db_op_<action>`(1つの action に SQL が複数なら `db_op_<action>_<n>`、実行順)
  - api_endpoint は {action}。trigger_events は書かない(apply_react_draft.py が下書きから写す。HTML の画面だけ event_code を書く)
  - dataset は1列1行: {no, logical_table, logical_column, physical_table, physical_column}。論理名は logical_names と同じ字面
  - joins・where・order_by は SQL を構造化表へ写す(生の SQL 文字列は書かない)。並び順の方向は 昇順 / 降順
  - INSERT / UPDATE / DELETE は transaction を true
- 物理テーブル名は英数字と _ だけ。論理名に角括弧・「集計結果の列」などの仮の表記を残さない

## 書かない欄
- dataset_name・remarks(S2)
