# H2 DB 操作と論理名の写し(haiku)

## 入力
- `{機能名}_backend_draft.json` の `apis.<action>.db_operations[]`(id・from・columns・ctes・subqueries・aggregate_units)。columns は SELECT 句の全列の骨組みで、
  1列ごとに alias(別名)・expression(元の式)・table / column(元の表と列)・logical_name(論理名の候補)・logical_name_source(候補の出所)・kind(column か alias_only)・real_column・alias_name・source_columns を持つ
- バックエンドのソースの SQL、DDL(あれば)、スキル直下の `05_DB操作.md` の構造化表の節、スキル直下の `10_論理名変換.md`(絶対パスは依頼文の【パス】に書いてある)

## 列の骨組みは1列も省かない
- 下書きの columns は**全部** dataset に書く(columns が 63 列なら dataset も 63 行)。「ソースに根拠が無い」「画面に出ない」を理由に省かない
- 下書きの expanded:true の行(column が 'f_*' の形。実行時に展開される列)は、dataset に1行で写す(physical_column は 'f_*'、expanded:true、rule・type も写す)。展開後の列に分けて書かない
- logical_names にも columns の列を全部書く(同じ表・同じ列は1行にまとめてよい)
- dataset の physical_column は columns[].real_column の列の部分(表.列 の列)を写す。kind が alias_only の列は real_column が空で、下書きの physical_column に入っている値は別名なので、physical_column に書かない(空にし、別名は logical_column と alias_name で示す)。**logical_names にも同じ別名の行を書き、physical_column は空・alias_name に別名を入れる(dataset の行と同じ書き方。logical_names でも alias_name を落とさない)**。physical_table は columns[].real_column の表の部分(空なら table)。logical_names は (physical_table, physical_column) ごとに1行。並びは columns の no の順
- 論理名は logical_name の候補があれば使う。出所が guess(列名からの推定)・ui_label_guess(画面の語からの推定)の候補は、論理名の末尾に「(推定)」を付けて書く
- logical_name_source が docstring・comment・csv_header・ui_label の候補は、下書きの語をそのまま使う(合成が事実の名前へ寄せるので、書き換えない)
  (例: 店舗ID(推定))。ソースや DDL でより確かな名前が分かれば、それに置き換えて「(推定)」を外してよい
- 候補が空の列も省かない。式(expression)と別名から論理名を付け、「(推定)」を付ける
  - 集計の別名(min・max など。aggregate の alias)の論理名は「最初の会計日(推定)」のように、集計の意味(最初の・最後の・件数など)を足した名に「(推定)」を付ける。元の列の語が docstring・コメントにあっても外さない(検査 check_docstring_terms は集計の別名の行を指摘しない)
- 列の省略は `verify_partials.py --units` が [列の省略] の ERROR で止め、欠けた列名を挙げて差し戻す
- 物理列の空欄(下書きの元の表の列が1つに決まるもの)は合成が補う。空のまま書いてよい
- 論理名は別名1つにつき1つ。「A と B」「A および B」のように2つを括って1つの名前にしない
- 論理表名は合成が決め直す(根拠の無い表は物理表名+(推定)、project_config の table_logical_names にある表はその語)。書き分けなくてよい。担当が論理表名を工夫しなくてよい。where・order_by の when も合成が付け直す

## 書く欄(`_unit_H2.json`。db_operations の行を作る)
- `logical_names[]`: {physical_table, logical_table, physical_column, logical_column}。dataset に出る列を全部。kind が alias_only の列の行は alias_name(別名)も持ち、physical_column は空にする(実列の行は alias_name を省いてよい)
  - 別名を physical_column に書いた・logical_names で alias_name を落とした行は、検査(verify_partials)と合成が下書きの事実から機械で直す(推測では直さない)。それでも別名の行が1行も無い列は [列の省略] で差し戻す
- `db_operations[]`: {id, api_endpoint, operation_type, dataset[], joins[], where[], group_by[], aggregate, order_by[], insert_values[], update_values[], transaction, error_message_id}
  - id は `db_op_<action>`(1つの action に SQL が複数なら `db_op_<action>_<n>`、実行順)
  - api_endpoint は {action}。trigger_events は書かない(apply_react_draft.py が下書きから写す。HTML の画面だけ event_code を書く)
    - 必ず辞書で書く。例: `"api_endpoint": {"action": "list_orders"}`(文字列 `"list_orders"` は不可。検査が [形] の ERROR にする)
  - dataset は1列1行: {no, logical_table, logical_column, physical_table, physical_column}。論理名は logical_names と同じ字面
  - joins・where・order_by は SQL を構造化表へ写す(生の SQL 文字列は書かない)。並び順の方向は 昇順 / 降順
  - INSERT / UPDATE / DELETE は transaction を true
- 物理テーブル名は英数字と _ だけ。論理名に角括弧・「集計結果の列」などの仮の表記を残さない
- where の行は value に右辺の値を書く(下書きの where[].value をそのまま写す。BETWEEN は「下限 AND 上限」、プレースホルダは名前)。param_name には値を書かない(値は value に入れる)
- group_by・order_by は下書きの apis[].group_by / order_by をそのまま写す(when があればそのまま残す)。X や空にしない
- aggregate は {function, target, filter, over, alias, scope} の配列。下書きの features.aggregates と features.windows を1件ずつそのまま写す(over はウィンドウだけ。alias は省かない。SUM が数十件あっても全部書く)
- joins・where・group_by・order_by は英語のキー名で書く(日本語の見出しに置き換えない)
- aliases の別名は、式と組で論理名を付ける。論理名を元の列名のもの(会計日 等)に戻さない
- 別名(AS の後ろの名前)ごとに dataset の行を分ける。同じ元列に別名が複数ある(first_date・last_date がどちらも kai_date)ときも、別名の数だけ行を書き、1行にまとめない。
  論理名は別名ごとに別の字面にする(物理列は columns[].real_column のとおり)

## 書かない欄
- dataset_name・remarks(S2)
- `db_operations[].subqueries`・`db_operations[].ctes`。合成が下書きの `apis.<action>.db_operations[]` から入れる(書いても合成が事実で置き換える)
- 副問い合わせ(サブクエリ)の中の集計(SUM・MAX 等)は `aggregate` に入れない。副問い合わせは合成が `subqueries` に入れる
