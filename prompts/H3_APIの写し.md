# H3 API の写し(haiku)

## 入力
- `{機能名}_backend_draft.json` の `apis`(action ごとの response_items・error_patterns)
- バックエンドのソース(action の分岐、リクエストからパラメータを取り出す箇所、return する JSON)

## 書く欄(`_unit_H3.json`。api_spec・response_items・request_params の行を作る)
- `api_spec[]`: {action, no, method, path, response_pattern, response_schema, request_params[], response_items[]}
  - 並び順が B001・B002… の番号になる。初期表示で呼ぶ順を先にする(screen_config の initial_processes の backend_id と合わせる)
  - no は AP1・AP2… 。path は実際の URL のパス(`/api/sales`)
  - request_params は全件: {name, type, required, location(body / query / path / header), default, validation, description}
  - response_items は return する JSON の全 key を1キー1行: {key, no, display_name, data_type}
    - key は `rows[].shop_id` の形。「各列」「等」や（）でまとめない。display_name は業務の名前。ただし合成(_compose_response.py)が画面への写し・同じ別名の列の論理名・request の説明から先に補うので、補えない行だけ業務の名前を書く(空欄のまま残すと検査が H3 に戻す)
    - 応答の型を api.ts の型と照合する: 下書きの `apis[action].response_fields`(path・ts_base・nullable)に同じ path があれば、
      data_type は ts_base の語(string / number / boolean / `string[]` など。日付は date(YYYY-MM-DD) でもよい)に合わせ、
      `nullable`(true / false)は下書きの値をそのまま書く。型に `| null` がある項目は必ず `nullable: true`。下書きに無い key は nullable を書かない
    - data_type・nullable は事実がある列では合成(_compose_response.py)が決める。TS の index signature(`number | string | string[] | null`)を写さない
- action・key・name の字面は S2 も使う。ソースどおりに書く

## 書かない欄
- name・description・auth・related_*・source・transform_backend・response_to_screen_mapping・error_patterns(S2)
