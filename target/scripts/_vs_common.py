#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_vs_common.py: verify_vs_source の追加検査(_vs_map・_vs_example・_vs_flow・_vs_db)が共有する読み込みと指摘行の書式(stdlib のみ)

【目的】
中間 JSON と下書き(react・backend)を、検査ごとに読み直さず、1回だけ読んで dict d にして各検査へ渡す。
検査は check(d) -> list[str] の形で、d の下の表のキーから読む。この docstring が、後で検査を足す担当の「どの JSON のどのキーに何があるか」の一覧。

【接続情報】
  - 呼出元: _vs_map / _vs_example / _vs_flow / _vs_db の run(inter_dir, feature)(_vs_checks_ext.run_all 経由で verify_vs_source.main)
  - 入力: inter_dir(INTERMEDIATE_DIR)の <feature>.json と <feature>_*.json
  - 出力: load の dict と warn の1行([WARN] なので verify_vs_source の終了コードは変わらない)

【load の戻り(d のキー)】
  接尾辞をキーにする。ファイルが無ければキーごと入れない(d.get で読む。無いなら検査は何も言わない)。
  "main"           <feature>.json。合成後の最終の中間 JSON。画面項目・FR・API・DB 操作などはこの下(次の節)
  "react_draft"    <feature>_react_draft.json。画面ソース(TSX)の事実(extract_react_draft.py の出力)
  "backend_draft"  <feature>_backend_draft.json。バックエンド(Python)の事実(extract_backend_draft.py の出力)
  "screen_config"  <feature>_screen_config.json(source_files・areas・process_names_by_event_code など。設定)
  "project_config" <feature>_project_config.json(forbidden_terms_map・verify_overrides など。設定)
  "units_prompts"  <feature>_units_prompts.json があれば。単位(S1 など)の担当プロンプト
  WHY: 依頼文は api_spec・screen_items も接尾辞の例に挙げたが、実在のファイルは上の5本。api_spec・screen_layout は別ファイルではなく
       "main" の下のキー(d["main"]["api_spec"] のように読む)。verify_vs_source.main の data が d["main"]、rd が d["react_draft"]、bd が d["backend_draft"] に当たる。

【d["main"](中間 JSON。verify_vs_source.main では data)の主なキー】
  src は全部 [{file, line_start, line_end}](行は 1 始まり。file は絶対パス)。「項目」は画面項目、「FR」は front_processes。
  screen_layout.items[]          画面項目。item_name・area_ref・item_type(Label・Grid・Input・ComboBox・Calendar・Checkbox・RadioButton・Button・TextArea)・format・data_type・length・remarks・example・src・no(IT1…)・screen_no
  screen_layout.areas[]          領域。area_no・area_name・screen_no・description
  initialization.items[]         項目の初期状態。item_name・active・visible・initial_value・no・screen_no
  events[]                       イベント一覧。no・trigger・content・classification・location・action・source_kind・src・legacy_event_code
  event_processes[]              イベントごとの処理。event_ref・pattern・overview・details[](文字列。「…」で画面文言を引く)・db_operations_ref・parameter_refs・src
  front_processes[]              FR(画面側の処理)。process_id(FR1…)・name・area・overview・preconditions・postconditions・called_by_triggers・steps[]・validation_refs・calculation_refs・common_logic_refs・parameter_refs・src
  front_processes[].steps[]      step_no・kind(data_load・validation・state_update・filter など)・description(手順の文)
  api_spec[]                     API 仕様。action・method・path・name・description・auth・response_pattern・response_schema・request_params[]・related_db_operations・src
  api_spec[].response_items[]    API 応答の項目。key(rows[].xxx の形)・display_name・data_type・nullable・type・source{type・source_ref・source_column}・transform_backend・expression(SQL の式か日本語の説明)・description・src
  api_spec[].response_to_screen_mapping[]   写し(API 応答 → 画面項目)。api_field_path・screen_item_name・transform(写し方の文)・screen_no
  api_spec[].error_patterns[]    API のエラー。status_code・condition・error_code・src(error_code は common_errors の code_default と照合される)
  backend_processes[]            BE(バックエンドの処理)。process_id・name・endpoint・request_spec・processing[](step_no・kind・description)・response_spec・error_patterns[](api_spec と同じ形)・called_by_front_processes・src
  db_operations[]                DB 操作。id(DB1…)・api_endpoint{action}・operation_type・dataset_name・remarks・transaction・src
  db_operations[].dataset[]      取得する列。no・logical_table・logical_column・physical_table・physical_column・alias_name(別名の列は physical_column が空で alias_name を持つ)
  db_operations[].joins[]        type・logical_table・condition
  db_operations[].where[]        and_or・logical_table・logical_column・operator・param_no・param_name・value
  db_operations[].group_by[]・order_by[]   logical_table・logical_column(order_by は direction も)
  db_operations[].aggregate[]    集計。function・target・alias・scope(main・cte …)・filter
  db_operations[].ctes[]・subqueries[]   name・definition・conditional_parts・from・filtered
  logical_names[]                論理名の表。no・physical_table・logical_table・physical_column・logical_column(論理表の名前は「(推定)」で終わる)
  parameters[]                   パラメータ。no・name・type・passed_between・description
  validations[]                  VL(画面ルール・入力検査)。no(VL1…)・field・rule・applied_when・kind(入力検査・表示切替・活性切替・絞り込み・API検査)・message_id・src
  messages[]                     メッセージ。message_id(MSE/MSW 番号)・code・severity・message・emitted_by_api・src(messages_backend が別にある版もある)
  calculations[]                 計算。no・name・formula・inputs[](name・source)・outputs[](name・destination)・remarks・src
  common_logic[]                 CL(共通ロジック)。no(CL1…)・name・description・expression・used_in・zero_division・null_handling・runs_on・processing_steps[](no・kind・description)・used_by_processes・src
  trigger_groups[]・diagrams[]・logging_callsites[]・audit_callsites[]・meta{feature_name・backend_files・frontend_files …}   周辺

【d["react_draft"](画面ソースの事実。verify_vs_source.main では rd)の主なキー】
  events[]            event_code(EV00=初期表示)・trigger・action・classification・ui_label・source{file・line・handler}・src・fetch_plan(events[0] だけ。{mode: parallel|serial|single・groups・basis・src})
  event_processes[]   event_code・src・calls[]({action・src})・details・fact_lines
  calc_candidates[]   計算の候補(画面ソースの式)。id(CC01…)・label・expr(JS の式)・operands・operators・funcs(Math.floor など)・literals・guards・operand_labels{オペランド → [画面の名前]}・operand_guard{オペランド → num|none}・in・src
  field_usage[]       API 応答の項目の画面での使われ方。path(rows[].xxx)・how・null_guard・uses[]({expr・element・null_guard・src})
  csv_exports[]       CSV 出力。function・trigger_events(EV07 など)・file_name_template・mime・bom・separator・line_break・quote・header{fixed[]・spread[]}・total_row{when}・joins[]・row_builder{function・row_param・value_separator・columns[]・rows_array・total_call}・formats_numbers(true・false・null。false は数値を整形しない)・src
  client_error_templates[]   クライアント側で作るエラー文面。template(「${r2.status}」などの穴つきの文)・placeholders・kind・src
  state_initials[]    useState の初期値。state・setter・initial(原文)・kind(literal|expression)・readable(読める説明)・bound[]({id・label・item_type_hint}。結ぶ画面項目)・src
  derived_arrays[]    派生配列。name・source・op・fields・lambda_expr・src
  display_rows[]      表示行の定義。array・labels[]・specs[]({label・csv・kin・cnt・unit・nth・dead・note・src})・cell_format{cnt・kin・single。各 {suffix・grouping・null_as_zero …}}・src
  jsx_elements[]      JSX の要素。id(JX01…)・tag・type・label・text・bind・on・disabled・visible_if・visible_chain・disabled_chain・item_type_hint・options[]({value・text}。select のみ)・map・src
  behaviors[]         振る舞い。id(BH01…)・kind(input_check|display_toggle|enable_toggle|data_filter)・cond・target・src・control_src
  failure_effects[]   取得の成否で変わる state。event・call・before_call・on_success・on_failure・in_finally・untouched_on_failure・failure_text・failure_state・on_success_from・src
  initial_fetches[]   初期表示の取得。call・effect・deps・src
  ui_strings[]        画面の文言。text・kind(jsx_text など)・visible_if・src
  continuous_operation[]   連続操作。event・button・disabled_while_loading・guard・cancel・prevention・fact・outcomes[]
  db_operations[]     api_endpoint だけ(画面から見た呼出)。unresolved[]

【d["backend_draft"](バックエンドの事実。verify_vs_source.main では bd。apis を bapis と呼ぶ)の主なキー】
  apis{action → {…}}  action ごと。キー: dataset・src・response_items[]({key・src})・error_patterns・boundaries・joins・where・group_by・order_by・aliases[]({alias・expression})・features・db_operations・response_fields
    error_patterns[]  status_code・condition・message(型文。{p} などの穴つき)・text_expanded(穴を展開した文)・param・check・id(MSE00001…)・src
    boundaries[]      境界値。param・check・message・compare[]・pattern・pattern_name・impossible_dates_accepted[]・present(false は検査が無いこと)・src
    response_fields[] api.ts の応答の型。path・ts_type・ts_base・nullable・optional・ts_src(SQL の別名の列は index_signature・sql_hint・db_type・db_nullable も持つ)
    db_operations[]   DB 操作の事実。id・from{table・name・alias}・columns[]・features{aggregates・windows}・ctes[]・subqueries[]・table_aliases{別名 → 実表}・aggregate_units[]・src
    db_operations[].columns[]     alias・alias_name・expression・table・column・physical_column・real_column・source_columns[]・is_expression・kind(column|alias_only)・alias_expression・logical_name・logical_name_source(docstring など)・no
    db_operations[].aggregate_units[]   func・arg・alias・scope(main|cte x)・group_by[]・unit・when
  common_errors[]     共通の例外処理。exception・errors[]({code・status_code(整数か式の文字列)・message・code_default・code_from・message_from})・via・assigns・src
  auth[]              認証。function・applies_to・fact・error_patterns[]・src
  logic_candidates[]  バックエンドの計算・条件の候補。name・kind(sql_condition など)・text・runs_on・src
  unresolved[]        下書きが解決できなかったもの

【warn の書式】
  '[WARN] [<tag>] <where>: <msg>'。新しい検査は [WARN] で入れる(verify_vs_source の方針。ERROR は2回の実走で誤検出が無いと確かめてから)。
  差し戻し先を書くときは msg の末尾に「 → 差し戻し先 S1」の形で付ける(verify_partials と同じ)。
"""
import glob
import json
from pathlib import Path

# WHY: 中間 JSON の本体だけは <feature>.json で接尾辞が無い。_vs_ の各検査は d["main"] と読むので、接尾辞の無いこの1本にキー名を与える
_MAIN_KEY = "main"


def load(inter_dir: Path, feature: str) -> dict:
    """inter_dir の <feature>.json("main")と <feature>_*.json(接尾辞がキー)を読んで dict にする。無いファイル・読めない JSON はキーごと入れない。
    呼出元: _vs_map / _vs_example / _vs_flow / _vs_db の run。"""
    base = Path(inter_dir)
    out = {}
    # WHY: feature に [ や * があっても glob の記号として読まれないよう、feature の部分だけ escape する
    pats = {_MAIN_KEY: base / f"{feature}.json"}
    for p in sorted(glob.glob(str(base / f"{glob.escape(feature)}_*.json"))):
        pats[Path(p).name[len(feature) + 1:-len(".json")]] = Path(p)
    for key, path in pats.items():
        try:
            out[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def warn(tag: str, where: str, msg: str) -> str:
    """検査の指摘の1行。[WARN] 付きなので verify_vs_source の終了コードに影響しない。呼出元: 各 _vs_* の検査。"""
    return f"[WARN] [{tag}] {where}: {msg}"


def lines(a, b=None) -> str:
    """行の範囲の表記。開始行と終了行が同じ(終了行が無い)なら「288」、違えば「285-291」。
    WHY(2026-10-16): 1行の出典を「288-288」と書くと、範囲なのか1行なのかを読み手が確かめ直す。表記をここに集め、指摘・ログの全部でそろえる。
    呼出元: span、_vs_map・_vs_flow の検査、_compose_src_line・verify_partials のログ。"""
    return f"{a}" if b is None or b == a else f"{a}-{b}"


def span(s) -> str:
    """出典(src の1要素)を「ファイル名:行の範囲」にする。行の範囲は lines の表記。呼出元: lines と同じ。"""
    s = s if isinstance(s, dict) else {}
    return f"{Path(str(s.get('file') or '')).name}:{lines(s.get('line_start'), s.get('line_end'))}"
