# H2 DB 操作と論理名の写し(haiku)

## 入力
- `{機能名}_backend_draft.json` の `apis.<action>.db_operations[]`(id・from・columns)。columns は SELECT 句の全列の骨組みで、
  1列ごとに alias(別名)・expression(元の式)・table / column(元の表と列)・logical_name(論理名の候補)・logical_name_source(候補の出所)を持つ
- バックエンドのソースの SQL、DDL(あれば)、`05_DB操作.md` の構造化表の節、`10_論理名変換.md`

## 列の骨組みは1列も省かない
- 下書きの columns は**全部** dataset に書く(columns が 63 列なら dataset も 63 行)。「ソースに根拠が無い」「画面に出ない」を理由に省かない
- logical_names にも columns の列を全部書く(同じ表・同じ列は1行にまとめてよい)
- dataset の physical_column は columns の column(空なら alias)、physical_table は table。並びは columns の no の順
- 論理名は logical_name の候補があれば使う。出所が guess(列名からの推定)の候補は、論理名の末尾に「(推定)」を付けて書く
  (例: 店舗ID(推定))。ソースや DDL でより確かな名前が分かれば、それに置き換えて「(推定)」を外してよい
- 候補が空の列も省かない。式(expression)と別名から論理名を付け、「(推定)」を付ける
- 列の省略は `verify_partials.py --units` が [列の省略] の ERROR で止め、欠けた列名を挙げて差し戻す

## 書く欄(`_unit_H2.json`。db_operations の行を作る)
- `logical_names[]`: {physical_table, logical_table, physical_column, logical_column}。dataset に出る列を全部
- `db_operations[]`: {id, api_endpoint, operation_type, dataset[], joins[], where[], group_by[], aggregate, order_by[], insert_values[], update_values[], transaction, error_message_id}
  - id は `db_op_<action>`(1つの action に SQL が複数なら `db_op_<action>_<n>`、実行順)
  - api_endpoint は {action}。trigger_events は書かない(apply_react_draft.py が下書きから写す。HTML の画面だけ event_code を書く)
    - 必ず辞書で書く。例: `"api_endpoint": {"action": "list_pos_account_report"}`(文字列 `"list_pos_account_report"` は不可。検査が [形] の ERROR にする)
  - dataset は1列1行: {no, logical_table, logical_column, physical_table, physical_column}。論理名は logical_names と同じ字面
  - joins・where・order_by は SQL を構造化表へ写す(生の SQL 文字列は書かない)。並び順の方向は 昇順 / 降順
  - INSERT / UPDATE / DELETE は transaction を true
- 物理テーブル名は英数字と _ だけ。論理名に角括弧・「集計結果の列」などの仮の表記を残さない
- joins・where・group_by・order_by は英語のキー名で書く(日本語の見出しに置き換えない)
- aliases の別名は、式と組で論理名を付ける。元の列名(会計日 等)に戻さない
- 別名(AS の後ろの名前)は別の列として別名のまま書く。元の列名に戻したりまとめたりしない

## 書かない欄
- dataset_name・remarks(S2)
