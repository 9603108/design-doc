#!/usr/bin/env python3
"""design-doc の単位方式(委譲の分担)を機械化する。

WHY: メインが手で組んでいた「単位ごとの依頼文」「検査後の後段コマンド列」「差し戻しの再依頼」を
毎回同じ形で出すため。分担の正本は prompts/units.json で、ここはそれを読むだけ(分担を二重に持たない)。

サブコマンド:
  prompts <機能名> --out <json> [--src <path>]...  各単位の Agent に渡す依頼文を書く
  after <機能名>                                    検査→後段を順に流し、ERROR で止めて差し戻しを JSON で出す
  retry <機能名> <単位> --issues <json>             差し戻しの文面を足した依頼文を出す

接続: prompts の出力は呼出元(メイン)が Agent の prompt に渡す。after は単位の書込みが
終わった後にメインが呼ぶ。retry は after が出した sendbacks を受けて、その単位だけ呼び直す。
標準ライブラリのみ。
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# WHY: スクリプトの場所(target/scripts)から2つ上を設計書スキルの根とみなす。絶対パスの埋め込みを避ける。
SKILL = Path(__file__).resolve().parents[2]
INTER = SKILL / "target" / "intermediate"
UNITS_JSON = SKILL / "prompts" / "units.json"
COMMON = SKILL / "prompts" / "_共通.md"

# 検査が差し戻し先を示す行の形: "... → 差し戻し先 S1"(複数は , 、 / で並ぶ)
SENDBACK_RE = re.compile(r"→ 差し戻し先 (\S+)")
DIAGRAM_MARK = "(構成図)"

# 後段コマンド列。順序は SKILL.md「実行順序」と units_prompts.json の after に従う。
# {feat} は機能名に置き換える。構成図の行は DIAGRAM_MARK で展開する(ipo_flowchart 以外の id を回す)。
AFTER = [
    "python3 target/scripts/verify_partials.py --units",
    "FEATURE_NAME={feat} python3 target/scripts/merge_partials.py {feat} --compose-units",
    "python3 target/scripts/apply_react_draft.py {feat} --stage partial --backend-draft "
    "target/intermediate/{feat}_backend_draft.json",
    "python3 target/scripts/merge_partials.py {feat}",
    "python3 target/scripts/apply_react_draft.py {feat} --stage merge",
    "python3 target/scripts/convert_events_location_action.py {feat}",
    "python3 target/scripts/add_screen_areas.py {feat}",
    "python3 target/scripts/extract_source_files.py {feat}",
    "python3 target/scripts/migrate_v17_to_v18.py {feat}",
    "python3 target/scripts/normalize_screen_terminology.py {feat}",
    "python3 target/scripts/apply_trigger_dispatch.py {feat}",
    "python3 target/scripts/_fill_event_processes.py {feat}",
    "python3 target/scripts/_fix_derived_dispatch.py {feat}",
    "python3 target/scripts/add_diagrams_to_intermediate.py {feat}",
    "python3 target/scripts/build_ipo_flowchart_svg.py {feat}",
    DIAGRAM_MARK,
    "python3 target/scripts/apply_logical_name_substitution.py {feat}",
    "python3 target/scripts/build_call_graph.py {feat}",
    # WHY: logging.observation_points が無い案件ではスクリプト自身が「スキップ」で正常終了する。
    "python3 target/scripts/apply_logging_steps.py {feat}",
    "python3 target/scripts/apply_id_scheme.py {feat}",
    "python3 target/scripts/migrate_v49_event_unify.py {feat}",
    "python3 target/scripts/migrate_v48_to_v49.py {feat}",
    # WHY: IDs(FR・BE)が確定した後に、共通ロジックの呼出元の名前を ID へ結ぶ(used_by_processes_ids)。
    "python3 target/scripts/fill_common_logic_callers.py {feat}",
    "python3 target/scripts/verify_vs_source.py {feat}",
    "python3 target/scripts/verify_intermediate.py {feat}",
    "python3 target/scripts/verify_call_graph.py {feat}",
    "INPUT_JSON=target/intermediate/{feat}.json DOCX_OUTPUT_PATH=target/output/詳細設計書_{feat}.docx "
    "NODE_PATH=$(npm root -g) node target/scripts/generate_docx.js",
    "python3 target/scripts/verify_docx_layout.py target/output/詳細設計書_{feat}.docx",
]


def load_units():
    # WHY: 分担表(units.json)が正本。ここに単位の一覧を書き写さない。
    with open(UNITS_JSON, encoding="utf-8") as f:
        return json.load(f)["units"]


def build_prompt(units, feat, u, srcs=(), sendbacks=()):
    """単位 u の依頼文を組む。sendbacks があれば差し戻しを足して既存の _unit_<u>.json を直させる。"""
    spec = units[u]
    owns = "、".join(spec["owns"])
    inter = str(INTER)
    skill = str(SKILL)
    lines = [
        f"design-doc スキルの委譲の単位 {u}({spec['title']})を行う。機能名(FEATURE_NAME): {feat}。",
        "",
        "【あなたの道具】cbm-serena(Serena と Bash を持ち、Read/Write は無い)。"
        "最初に mcp__codebase-memory-mcp__list_projects を1回呼ぶこと。"
        "ファイルの読み書きは Serena(mcp__serena__read_file / create_text_file / replace_content 等)で、絶対パスで行う。"
        "Bash は検査の python3 実行だけに使い、grep/cat/sed でソースを読まない。",
        "",
        "【パス(全て絶対パス)】",
        f"- スキルのフォルダ: {skill}(コマンドはここへ cd して流す)",
        f"- 共通の決まり: {COMMON}(最初に読む)",
        f"- あなたの手順書: {SKILL / spec['prompt']}(読んで従う)",
        f"- 分担表: {UNITS_JSON}",
        # WHY: H2 の手順書は 05_DB操作.md と 10_論理名変換.md を裸の名前で挙げ、依頼文に場所が無かったため H2 が見つけられなかった。
        #      スキル直下の実在パスを絶対パスで渡す(実在は selftest で確かめる)
        f"- スキル直下の文書(手順書が名前で挙げるもの): {SKILL / '05_DB操作.md'} と {SKILL / '10_論理名変換.md'}",
        f"- 中間データの置き場: {inter}/",
        f"- 下書き: {inter}/{feat}_react_draft.json と {inter}/{feat}_backend_draft.json",
        f"- 設定: {inter}/{feat}_screen_config.json と {inter}/{feat}_project_config.json",
    ]
    if srcs:
        lines.append("- 入力のソース: " + " と ".join(str(Path(s).resolve()) for s in srcs))
    lines += [
        "",
        "【やること】",
        "- 読むのは _共通.md、手順書、手順書が挙げた入力だけ。SKILL.md は読まない。",
        f"- 書くのは {inter}/_unit_{u}.json の1ファイルだけ。他のファイル(partial・中間 JSON・他の単位の _unit_*.json・screen_config)は書かない。"
        f"書いてよい欄は units.json の {u} の owns だけ: {owns}",
        "- 前回の設計書や過去の partial の中身は参照しない(ソースと下書きから書く)。",
        "- 【出典】下書きにある行の src は書かない(合成が付ける)。下書きに無い行(ソースを読んで足した行)だけ、読んだ範囲 {file,line_start,line_end} の配列を src に書く。",
        f"- 終わったら `cd {skill} && python3 target/scripts/verify_partials.py --units --unit {u}` を流し、"
        f"行末が「→ 差し戻し先 {u}」の問題を直してから、3行以内で報告する(書いた行数・残った問題・判断できなかった所)。",
    ]
    # WHY: S1 と S2 は並行に書くため、S2 が画面項目名を推測して S1 と食い違い差し戻しになる(run3 で52件)。
    # S2 にだけ、下書き(extract_react_draft の出力)にある画面の文言を渡し、screen_item_name をそれに揃えさせる。
    # 出所: events[].ui_label(操作の表示名)・display_rows[].labels(表示行の名前)・jsx_elements[].label(入力欄・選択欄の名前)。
    # jsx_elements を入れないと、イベントに表示名の無い選択欄(店舗の選択欄など。ui_label が空)が一覧から落ち、
    # 選択肢に使う API(店舗一覧)の response_to_screen_mapping が空になる(2026-10-13 POS会計レポート)。下書きが無ければ何も足さない。
    if u == "S2":
        draft = INTER / f"{feat}_react_draft.json"
        if draft.exists():
            d = json.loads(draft.read_text(encoding="utf-8"))
            labels = [e["ui_label"] for e in d.get("events", []) if e.get("ui_label")]
            labels += [j["label"] for j in d.get("jsx_elements", []) if j.get("label")]
            labels += [x for r in d.get("display_rows", []) for x in r.get("labels", [])]
            labels = list(dict.fromkeys(labels))
            if labels:
                lines += ["", "【画面項目名の一覧(下書きの画面の文言。screen_item_name はこの名前をそのまま使う。推測で作らない)】"]
                lines += [f"- {x}" for x in labels]
    if sendbacks:
        lines += [
            "",
            "【差し戻し】前回の検査が次を指した。既存の _unit_" + u + ".json を直す(新規に書き直さない)。",
        ]
        lines += [f"- {s}" for s in sendbacks]
    return "\n".join(lines) + "\n"


ERROR_LINE_RE = re.compile(r"\[ERROR\]|^\s*ERROR\b")
KEY_PATH_RE = re.compile(r"[a-z_]+(?:\[[^\]]*\])?(?:\.[a-z_]+(?:\[[^\]]*\])?)*")
# WHY: 合成後の中間 JSON の欄は、単位の owns と名前が違うものがある。合成・後段が写した元の欄へ読み替える。
# backend_processes[].overview は api_spec[].description の写し(POS会計レポートの中間 JSON で一致を確認)。
KEY_ALIAS = {
    "backend_processes.overview": "api_spec.description",
    "validations": "validations_frontend",
    "messages": "messages_backend",
    # front_processes[].steps[] は migrate_v17_to_v18.py が event_processes[].details から作る
    "front_processes.steps.description": "event_processes.details",
    # 画面操作の trigger(trigger_groups[].triggers[])は events[].trigger から migrate_v17_to_v18.py が作る。location の語彙違反は H1 の trigger の書き方
    # (検査の文中の「screen_layout の権威語彙」に引かれて S1 へ誤って振られていた)
    "trigger_groups.triggers": "events",
}
# WHY: 行が指す欄と直す欄が違う検査。「400 のエラーが validations に無い」は error_patterns(S2)はソースどおりで、
# 欠けているのは validations の行なので、validations を書く単位(H4)へ戻す。
MSG_TO_KEY = [(re.compile(r"validations に無い"), "validations_frontend"),
              # 画面への写しの欠けは S2 の欄(行の道筋が api_spec[0](action) の形で欄名を持たない古い文面も同じ単位へ)
              (re.compile(r"response_to_screen_mapping が空"), "api_spec.response_to_screen_mapping")]


def owner_of(line, units):
    """ERROR 行の対象の欄を units の owns と照らし、書いた単位を返す。引けなければ None。"""
    owns = [(o.replace("[]", ""), u) for u, spec in units.items() for o in spec["owns"]]
    keys = [k for rx, k in MSG_TO_KEY if rx.search(line)]
    keys += [re.sub(r"\[[^\]]*\]", "", m) for m in KEY_PATH_RE.findall(line)]
    for key in keys:
        for src, dst in KEY_ALIAS.items():
            if key == src or key.startswith(src + "."):
                key = dst + key[len(src):]
        hits = [(len(o), u) for o, u in owns if key == o or key.startswith(o + ".")]
        if hits:
            return max(hits)[1]
    return None


def parse_sendbacks(text, units=None):
    """検査の出力から「→ 差し戻し先 <単位>」の行を単位ごとに拾う。

    units を渡すと、差し戻し先の無い ERROR 行も owner_of で単位を引き、引けなければ "unknown" に入れる。
    units を渡したとき、units 表に無い名前(S0 など並行の単位の外・誤記)は retry できないので "unknown" に入れる。
    """
    out = {}
    for line in text.splitlines():
        m = SENDBACK_RE.search(line)
        if m:
            targets = [u for u in re.split(r"[,、/]", m.group(1)) if u]
            if units is not None:
                targets = [u if u in units else "unknown" for u in targets]
                targets = list(dict.fromkeys(targets))
        elif units is not None and ERROR_LINE_RE.search(line):
            targets = [owner_of(line, units) or "unknown"]
        else:
            continue
        for unit in targets:
            out.setdefault(unit, []).append(line.strip())
    return out


def diagram_commands(feat):
    # WHY: 構成図の id は設定から得る(id を書き並べない)。ipo_flowchart は別経路なので除く。
    probe = (
        "import sys; sys.path.insert(0, 'target/scripts'); import _config_loader as c; "
        f"print('\\n'.join(c.get_all_diagram_ids('{feat}')))"
    )
    p = subprocess.run([sys.executable, "-c", probe], cwd=SKILL, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("get_all_diagram_ids failed: " + p.stderr[-400:])
    return diagram_steps(feat, p.stdout.split())


def diagram_steps(feat, ids):
    # WHY: SKILL.md Phase 5 の2つのループと同じ形。build は ipo_flowchart 以外(IPO は AFTER の build_ipo_flowchart_svg)、
    # extract_svg / svg_to_png は ipo_flowchart を含む全 id を target/diagrams/{feat}_{id}.html/.svg/.png で回す。
    cmds = [f"python3 target/scripts/build_generic_diagram_svg.py {feat} {did}" for did in ids if did != "ipo_flowchart"]
    for did in ids:
        base = f"target/diagrams/{feat}_{did}"
        cmds.append(f"python3 target/scripts/extract_svg.py {base}.html {base}.svg")
        cmds.append(f"python3 target/scripts/svg_to_png.py {base}.svg {base}.png 1400")
    return cmds


def run_after(feat):
    steps = []
    for tmpl in AFTER:
        if tmpl == DIAGRAM_MARK:
            steps += diagram_commands(feat)
        else:
            steps.append(tmpl.replace("{feat}", feat))
    for i, cmd in enumerate(steps, 1):
        print(f"[{i}/{len(steps)}] {cmd[:80]}", file=sys.stderr)
        p = subprocess.run(cmd, shell=True, cwd=SKILL, capture_output=True, text=True)
        out = p.stdout + p.stderr
        # WHY: 成功時にも「ERROR 0 件」と出力する検査があるため、出力の文字では判定せず終了コードだけで判定する。
        # 各検査は ERROR があれば exit 1 を返す(verify_call_graph.py・verify_docx_layout.py で確認)。
        if p.returncode != 0:
            # WHY: 差し戻し先を拾えないまま止まると、メインが次に何をするか決まらない。最低でも unknown に残す。
            sendbacks = parse_sendbacks(out, load_units()) or {"unknown": (out.strip().splitlines() or [cmd])[-1:]}
            result = {
                "stopped_at": cmd,
                "sendbacks": sendbacks,
                "tail": out[-800:],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
    print(json.dumps({"ok": True, "steps": len(steps)}, ensure_ascii=False))
    return 0


def selftest():
    s = parse_sendbacks("x ERROR foo → 差し戻し先 S1\nok\n  bar → 差し戻し先 H2,H3\n")
    assert sorted(s) == ["H2", "H3", "S1"], s
    assert s["S1"] == ["x ERROR foo → 差し戻し先 S1"], s
    assert parse_sendbacks("ERROR 差し戻しの無い行") == {}
    units = load_units()
    assert len(units) == 7, sorted(units)
    # 差し戻し先の無い合成後の ERROR(2026-10-13 POS会計レポートの verify_intermediate)
    s = parse_sendbacks(
        "=== 検証結果: ERROR 3 件 ===\n--- ERROR（docx生成前に要修正） ---\n"
        "  [ERROR] api_spec[a].error_patterns: 400 のエラー MSE00001 が validations に無い（validations に1件書き）\n"
        "  [ERROR] v55.1 §10 補遺 概要本文に物理名残存: backend_processes[BE1].overview 検出=['UPPER:POS']\n"
        "  [ERROR] db_operations[DB1].dataset: 列「a・b」が括り表記\n"
        "  [ERROR] api_spec[a].response_items[k].display_name が空\n"
        "  [ERROR] 何の欄か分からない\n"
        "  [WARN] backend_processes[BE1].overview 長い\n", units)
    assert {u: len(v) for u, v in s.items()} == {"H4": 1, "S2": 1, "H2": 1, "H3": 1, "unknown": 1}, s
    # 2026-10-13: 画面への写しの欠けは S2。画面操作 trigger の location の語彙違反は H1(文中の「screen_layout の権威語彙」で S1 に誤振りしない)
    s = parse_sendbacks(
        "  [ERROR] api_spec[0].response_to_screen_mapping(list_pos_shops): response_items があるのに response_to_screen_mapping が空（x）\n"
        "  [ERROR] api_spec[0](list_pos_shops): response_items があるのに response_to_screen_mapping が空（古い文面）\n"
        "  [ERROR] events[EV7].location[2]='空欄項目も表示チェックボックス' が screen_layout の権威語彙にない\n"
        "  [ERROR] trigger_groups[kind=screen_operation].triggers[no=TR6].location[2]='空欄項目も表示チェックボックス' が screen_layout の権威語彙にない\n"
        "  [ERROR] screen_layout.items[2].example: 何か\n", units)
    assert {u: len(v) for u, v in s.items()} == {"S2": 2, "H1": 2, "S1": 1}, s
    assert parse_sendbacks("ERROR foo → 差し戻し先 S1", units) == {"S1": ["ERROR foo → 差し戻し先 S1"]}
    # S2 の依頼文の画面項目名の一覧に、イベントに表示名の無い選択欄(jsx_elements の label)も入る
    import tempfile
    global INTER
    with tempfile.TemporaryDirectory() as td:
        Path(td, "テスト機能_react_draft.json").write_text(json.dumps({
            "events": [{"event_code": "EV02", "ui_label": ""}, {"event_code": "EV01", "ui_label": "表示"}],
            "jsx_elements": [{"tag": "select", "label": "店舗"}, {"tag": "span", "label": ""}],
            "display_rows": [{"labels": ["総売上"]}]}, ensure_ascii=False), encoding="utf-8")
        old, INTER = INTER, Path(td)
        try:
            p = build_prompt(units, "テスト機能", "S2")
        finally:
            INTER = old
    assert "- 店舗\n" in p and "- 表示\n" in p and "- 総売上\n" in p, p
    # units 表に無い差し戻し先(S0 など)は unknown に入れる。units を渡さなければ従来どおり名前のまま
    assert parse_sendbacks("[WARN] x → 差し戻し先 S0", units) == {"unknown": ["[WARN] x → 差し戻し先 S0"]}
    assert sorted(parse_sendbacks("x → 差し戻し先 S0,S1,H9", units)) == ["S1", "unknown"]
    assert list(parse_sendbacks("x → 差し戻し先 S0")) == ["S0"]
    # retry は units 表に無い単位で KeyError にせず、分かる文言で終了コード 2
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["retry", "テスト機能", "S0", "--issues", "/dev/null"]) == 2
    assert "units 表に無い" in err.getvalue(), err.getvalue()
    text = build_prompt(units, "テスト機能", "S1", sendbacks=["ERROR 欄 → 差し戻し先 S1"])
    assert "_unit_S1.json" in text and "ERROR 欄 → 差し戻し先 S1" in text, text
    assert "【差し戻し】" not in build_prompt(units, "テスト機能", "S1")
    assert "【出典】" in build_prompt(units, "テスト機能", "S1"), "A3: 指示文に出典の1行"
    # A3: .src の欄は _merge で和集合(同じ要素は1つ)。.src でない list は従来どおり上書き
    from _units import _merge
    a, b, c = {"file": "a", "line_start": 1, "line_end": 2}, {"file": "b", "line_start": 3, "line_end": 4}, {"file": "a", "line_start": 1, "line_end": 2}
    assert _merge({"src": [a]}, {"src": [b, c], "t": [1]}, "x") == {"src": [a, b], "t": [1]}
    assert _merge({"t": [1]}, {"t": [2]}, "x") == {"t": [2]}
    # 手順書が名前で挙げる文書(05・10)は、依頼文に絶対パスで出る・実在する(パスのずれの再発を止める)
    for doc in ("05_DB操作.md", "10_論理名変換.md"):
        assert (SKILL / doc).exists(), f"スキル直下に無い: {doc}"
        assert str(SKILL / doc) in build_prompt(units, "テスト機能", "H2"), f"依頼文に {doc} の絶対パスが無い"
    check_step_args("テスト機能")


def check_step_args(feat):
    """各段のスクリプトが在り、`len(sys.argv) < N` で求める位置引数の数を満たすかを見る。

    ponytail: スクリプト本文の `len(sys.argv) < N` だけを見る。argparse の必須引数は見ない(要れば --help を流す)。
    """
    steps = [s for t in AFTER if t != DIAGRAM_MARK for s in [t.replace("{feat}", feat)]]
    steps += diagram_steps(feat, ["ipo_flowchart", "process_structure"])
    for cmd in steps:
        toks = cmd.split()
        i = next(k for k, t in enumerate(toks) if t.endswith((".py", ".js")))
        script = SKILL / toks[i]
        assert script.exists(), cmd
        argc = 1 + len([t for t in toks[i + 1:] if not t.startswith("-")])
        need = [int(n) for n in re.findall(r"len\(sys\.argv\)\s*<\s*(\d+)", script.read_text(encoding="utf-8"))]
        assert all(argc >= n for n in need), f"引数が足りない: {cmd} (要 {max(need)}、有 {argc})"


def main(argv=None):
    ap = argparse.ArgumentParser(description="design-doc の単位方式を機械化する")
    ap.add_argument("--selftest", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p1 = sub.add_parser("prompts", help="各単位の依頼文を書く")
    p1.add_argument("feature")
    p1.add_argument("--out", required=True)
    p1.add_argument("--src", action="append", default=[], help="入力のソース(絶対パス)。複数可")
    p2 = sub.add_parser("after", help="検査と後段を順に流す")
    p2.add_argument("feature")
    p3 = sub.add_parser("retry", help="差し戻しを足した依頼文を書く")
    p3.add_argument("feature")
    p3.add_argument("unit")
    p3.add_argument("--issues", required=True, help="after の出力 JSON(sendbacks を読む)")
    a = ap.parse_args(argv)

    if a.selftest:
        selftest()
        print("selftest OK")
        return 0
    if a.cmd == "prompts":
        units = load_units()
        data = {"units": [
            {"unit": u, "model": units[u]["model"], "prompt": build_prompt(units, a.feature, u, a.src)}
            for u in units
        ]}
        Path(a.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"wrote {len(data['units'])} units to {a.out}")
        return 0
    if a.cmd == "after":
        return run_after(a.feature)
    if a.cmd == "retry":
        units = load_units()
        # WHY: units 表に無い名前(S0・unknown・誤記)は単位ではない。KeyError の追跡でなく、何を直すかの文言で止める。
        if a.unit not in units:
            print(f"単位 {a.unit} は units 表に無いので retry できない(単位: {'・'.join(units)})。"
                  "unknown や S0 などの差し戻しは、after が出した行の内容を読んで、スクリプトの不具合か、"
                  "S0・R1 のような並行の単位の外の担当(units.json の outside_fragments)かを判断して直す", file=sys.stderr)
            return 2
        with open(a.issues, encoding="utf-8") as f:
            issues = json.load(f)
        # WHY: after の出力(sendbacks の辞書)か、差し戻し文面の一覧の両方を受ける。
        lines = issues.get("sendbacks", {}).get(a.unit, []) if isinstance(issues, dict) else issues
        if not lines:
            print(f"差し戻しの文面が無い: {a.unit}", file=sys.stderr)
            return 1
        print(json.dumps({"unit": a.unit, "model": units[a.unit]["model"],
                          "prompt": build_prompt(units, a.feature, a.unit, sendbacks=lines)},
                         ensure_ascii=False, indent=1))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
