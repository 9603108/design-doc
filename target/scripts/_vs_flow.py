#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_vs_flow.py: verify_vs_source の追加検査: 手順・CSV・呼出元・エラー順の照合(stdlib のみ)

【受け持ち】
手順・CSV・呼出元・エラー順。front_processes[].steps・backend_processes[].processing の手順の順序と内容、
CSV 出力(react 下書きの csv_exports)の記述、共通ロジックの呼出元、エラーの出る順(backend 下書きの error_patterns)が、
ソースの事実と合うかを見る。d(_vs_common.load の戻り)の読み方は _vs_common の docstring。

【検査の足し方】
  1. check(d) -> list[str] の関数を書く。指摘が無ければ []。必要な JSON が d に無いときも [] を返す(何も言わない)。
  2. 行は _vs_common.warn(tag, where, msg) で作る(新しい検査は [WARN]。終了コードに影響しない)。
  3. 関数を CHECKS に足す。
  4. 検査の正例・負例を、引数なしで bool を返す selftest 用の関数に書き、SELFTESTS に足す(selftest() が全部呼ぶ)。

【接続情報】
  呼出元: _vs_checks_ext.run_all / selftest(verify_vs_source.main と --selftest から)
"""
import json
import os
import re
from pathlib import Path

import _vs_common

# 検査の本体(check(d) -> list[str])。まだ空。後で1検査ずつ足す
CHECKS: list = []
# 検査の自己テスト(引数なし・bool を返す)。まだ空
SELFTESTS: list = []


_SUBST_RE = re.compile(r"\(推定\)\[([^\]]+)\]")


def check_state_name_substitution(d) -> list[str]:
    """front_processes の steps の description と event_processes の details に出る '(推定)[x]' の x が、
    react_draft の state_initials の state 名と同じなら指摘する。
    WHY: 論理名の置換(論理名に '(推定)' を付けて書く形)が、画面の state 名に誤って当たった誤りを見つけるため。
    呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"] と d["react_draft"] だけ。"""
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    states = {s.get("state") for s in rd.get("state_initials", []) if s.get("state")}
    texts = []
    for fp in main.get("front_processes", []):
        for st in fp.get("steps", []):
            texts.append((fp.get("process_id", ""), st.get("description") or ""))
    for ep in main.get("event_processes", []):
        for t in ep.get("details", []):
            texts.append((ep.get("event_ref", ""), t or ""))
    out = []
    for where, text in texts:
        for x in _SUBST_RE.findall(text):
            if x in states:
                out.append(_vs_common.warn("state名の置換", where, f"(推定)[{x}] の x が state 名に当たっている"))
    return out


def selftest_state_name_substitution() -> bool:
    """当たり1件(state 名 customerList に '(推定)[customerList]')と、外れ1件(state 名が違う)で確かめる。"""
    hit = {
        "main": {"front_processes": [{"process_id": "FR1", "steps": [{"description": "顧客一覧を取得する(推定)[customerList]"}]}],
                 "event_processes": []},
        "react_draft": {"state_initials": [{"state": "customerList"}]},
    }
    miss = {
        "main": {"front_processes": [{"process_id": "FR1", "steps": [{"description": "顧客一覧を取得する(推定)[customerList]"}]}],
                 "event_processes": []},
        "react_draft": {"state_initials": [{"state": "otherState"}]},
    }
    return len(check_state_name_substitution(hit)) == 1 and check_state_name_substitution(miss) == []


CHECKS.append(check_state_name_substitution)
SELFTESTS.append(selftest_state_name_substitution)


def _as_list(v) -> list:
    """state の値(文字列・文字列の配列・無し)を、state 名だけの空でない文字列の配列にそろえる。
    WHY: failure_effects の before_call・on_success・on_failure は形が揺れる。値の '=true' や '=null' の付いた書きは、state 名だけを取る(付いたままだと state_initials に当たらない)。"""
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    names = [x.split("=", 1)[0].strip() for x in v if isinstance(x, str)]
    return [x for x in names if x]


def _span(src):
    """src([{file,line_start,line_end}] か 1件の dict)を (file, 開始行, 終了行) にする。読めなければ None。"""
    if isinstance(src, list):
        src = src[0] if src else None
    if not isinstance(src, dict):
        return None
    s = (src.get("file"), src.get("line_start"), src.get("line_end"))
    return s if s[0] and s[1] is not None and s[2] is not None else None


def _overlap(a, b) -> bool:
    """同じファイルで行の範囲が重なるか。呼出元: check_fr_effects(event の src と FR の src を結ぶ)。"""
    if not a or not b or a[0] != b[0]:
        return False
    return a[1] <= b[2] and b[1] <= a[2]


def check_fr_effects(d) -> list[str]:
    """react_draft の failure_effects を、event の src と FR の src の行の重なりで FR に結ぶ。
    before_call の state を変える手順が、API 呼出(kind が data_load の手順)より前に無い場合と、
    on_success・on_failure の state を変える手順が FR の steps に無い場合を指摘する。
    WHY: 取得の成否で変わる state の手順が FR の手順から抜けると、画面の挙動が仕様から外れるため。
    一致は state 名か state_initials の bound の画面項目名が description に出るかで見る(文章の意味は見ない)。
    呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"] の front_processes と d["react_draft"] の events・state_initials・failure_effects。"""
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    ev_span = {e.get("event_code"): _span(e.get("src")) for e in rd.get("events", [])}
    bound = {}
    for s in rd.get("state_initials", []):
        names = [s.get("state")] + [b.get("label") for b in (s.get("bound") or [])]
        bound[s.get("state")] = [n for n in names if n]
    out = []
    for fe in rd.get("failure_effects", []):
        span = ev_span.get(fe.get("event"))
        if not span:
            continue
        for fp in main.get("front_processes", []):
            if not _overlap(span, _span(fp.get("src"))):
                continue
            steps = fp.get("steps", [])
            where = fp.get("process_id", "")
            # API 呼出の手順は kind が data_load・backend_call のもの。同じ FR に呼出が複数あるときは最後の呼出を基準にする。
            # WHY: 開始時の state 変更は並行する最初の呼出の直後に書かれることがあり、最後の呼出より前にあれば取得の順序として正しいため。
            api_idx = max((i for i, st in enumerate(steps) if st.get("kind") in ("data_load", "backend_call")), default=None)
            for s in _as_list(fe.get("before_call")):
                names = bound.get(s, [s])
                idx = next((i for i, st in enumerate(steps) if any(n in (st.get("description") or "") for n in names)), None)
                # 手順が無い場合は FR の外で変えているとみなし、指摘しない。WHY: 手順の中で API 呼出より後にしか出ない場合だけ、順序の誤りとして見る。
                if idx is not None and api_idx is not None and idx > api_idx:
                    out.append(_vs_common.warn("手順の効果", where, f"before_call の {s} を変える手順が API 呼出より後に出ている"))
            for key in ("on_success", "on_failure"):
                for s in _as_list(fe.get(key)):
                    names = bound.get(s, [s])
                    if not any(any(n in (st.get("description") or "") for n in names) for st in steps):
                        out.append(_vs_common.warn("手順の効果", where, f"{key} の {s} を変える手順が FR の steps に無い"))
    return out


def selftest_fr_effects() -> bool:
    """当たり1件(on_failure の error を変える手順が無い)と、外れ0件(error を変える手順が有る)で確かめる。"""
    def make(extra_step: bool) -> dict:
        steps = [
            {"step_no": 1, "kind": "state_update", "description": "loading を true にする"},
            {"step_no": 2, "kind": "data_load", "description": "API を呼ぶ"},
            {"step_no": 3, "kind": "state_update", "description": "rows に結果を入れる"},
        ]
        if extra_step:
            steps.append({"step_no": 4, "kind": "state_update", "description": "error に失敗文を入れる"})
        src = {"file": "/x/screen.tsx", "line_start": 5, "line_end": 30}
        return {
            "main": {"front_processes": [{"process_id": "FR1", "src": src, "steps": steps}]},
            "react_draft": {
                "events": [{"event_code": "EV01", "src": {"file": "/x/screen.tsx", "line_start": 10, "line_end": 20}}],
                "failure_effects": [{"event": "EV01", "call": "fetchX", "before_call": ["loading"],
                                     "on_success": ["rows"], "on_failure": ["error"]}],
                "state_initials": [{"state": "loading", "bound": []}, {"state": "rows", "bound": []},
                                   {"state": "error", "bound": []}],
            },
        }
    return len(check_fr_effects(make(False))) == 1 and check_fr_effects(make(True)) == []


CHECKS.append(check_fr_effects)
SELFTESTS.append(selftest_fr_effects)


def _csv_required(ce) -> list:
    """csv_exports の1件から、ソースに有る事実を (見出し, 候補の文字列の並び) で返す。候補のどれかが手順の文に出れば満たす。
    WHY: ソースに無い事実は求めない。事実の無い書式(BOM 無しなど)は必要な記述も無いため、フラグの真の行だけを要件にする。"""
    req = []
    if ce.get("bom"):
        req.append(("BOM", ("BOM", "文字コードの印")))
    if "CRLF" in str(ce.get("line_break") or "").upper():
        req.append(("CRLF改行", ("CRLF", "改行")))
    if ce.get("quote"):
        req.append(("引用符の二重化", ("二重", "エスケープ")))
    for h in (ce.get("header") or {}).get("fixed") or []:
        if isinstance(h, str) and h:
            req.append((f"見出し「{h}」", (h,)))
    if ce.get("total_row"):
        req.append(("合計行", ("合計",)))
    for c in (ce.get("row_builder") or {}).get("columns") or []:
        suf = c.get("suffix") if isinstance(c, dict) else None
        if isinstance(suf, str) and suf:
            req.append((f"列の接尾辞「{suf}」", (suf,)))
    tpl = re.sub(r"\$?\{[^}]*\}", "", ce.get("file_name_template") or "")
    for w in re.split(r"[^0-9A-Za-z぀-ヿ一-鿿]+", tpl):
        if w and not w.isascii():
            req.append((f"ファイル名の語「{w}」", (w,)))
    return req


def check_csv_facts(d) -> list[str]:
    """react_draft の csv_exports の各行について、src の行が重なる FR の steps の文に、ソースに有る CSV の事実が書かれているかを見る。
    欠けは1行ずつ指摘する。呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"] の front_processes と d["react_draft"] の csv_exports。"""
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    ev_span = {e.get("event_code"): _span(e.get("src")) for e in rd.get("events", [])}
    out = []
    for ce in rd.get("csv_exports", []):
        # FR への結び方は2通り: csv 自体の src が FR と重なる、または csv の trigger_events(EV07 など)の event の src が FR と重なる。
        # WHY: CSV の出力は画面の event から呼ばれ、csv の定義の行は FR の行と離れていることが多いため。
        spans = [_span(ce.get("src"))] + [ev_span.get(code) for code in ce.get("trigger_events") or []]
        spans = [s for s in spans if s]
        if not spans:
            continue
        for fp in main.get("front_processes", []):
            if not any(_overlap(s, _span(fp.get("src"))) for s in spans):
                continue
            text = " ".join((st.get("description") or "") for st in fp.get("steps", []))
            where = fp.get("process_id", "")
            for label, cands in _csv_required(ce):
                if not any(c in text for c in cands):
                    out.append(_vs_common.warn("CSVの事実", where, f"{ce.get('function') or 'CSV'} の {label} が FR の手順に無い"))
    return out


def selftest_csv_facts() -> bool:
    """事実を全部持つ csv_exports に対し、手順の文が事実を欠くと指摘が出て、全部書けば0件になることで確かめる。"""
    ce = {
        "function": "exportCsv", "bom": True, "line_break": "CRLF", "quote": '"',
        "header": {"fixed": ["店舗名", "金額"]}, "total_row": {"when": "always"},
        "row_builder": {"columns": [{"suffix": "円"}]}, "file_name_template": "会計レポート_${yyyymm}.csv",
        "src": {"file": "/x/screen.tsx", "line_start": 40, "line_end": 60},
    }
    def make(text: str) -> dict:
        return {
            "main": {"front_processes": [{"process_id": "FR1", "src": {"file": "/x/screen.tsx", "line_start": 30, "line_end": 70},
                                          "steps": [{"step_no": 1, "kind": "screen_update", "description": text}]}]},
            "react_draft": {"csv_exports": [ce]},
        }
    bare = make("会計データを取得する")
    full = make("BOM付きUTF-8、CRLF改行、引用符は二重化、見出しは店舗名と金額、合計行を付け、円の単位で、会計レポートの名前で保存する")
    return len(check_csv_facts(bare)) >= 1 and check_csv_facts(full) == []


CHECKS.append(check_csv_facts)
SELFTESTS.append(selftest_csv_facts)


def _abs_srcs(srcs, root) -> list:
    """出典の file が相対なら project_root につなぐ。WHY: 出典の file は絶対パスの想定だが、相対で書かれた版も読めるようにする。"""
    out = []
    for s in srcs or []:
        f = s.get("file") or ""
        if f and root and not os.path.isabs(f):
            s = dict(s, file=os.path.join(root, f))
        out.append(s)
    return out


def check_cl_backend_callers(d) -> list[str]:
    """サーバー・DB で動く common_logic の used_by_processes_ids に挙がる BE ごとに、その BE の関数から CL の定義へ参照で辿れるか、
    または CL の手掛かり(下書き候補の name・text)が BE の本文に字面で出るかを見る。どちらも無い BE を指摘する。
    判定できない(CL の定義が無い・python でない等)BE は指摘しない。読み方は fill_common_logic_callers の _module_reach・_be_fits・_keys_of_common を使う(書き換えない)。
    呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"] の common_logic・backend_processes、d["backend_draft"] の apis・logic_candidates、d["screen_config"] の project_root。"""
    from fill_common_logic_callers import _be_fits, _keys_of_common, _module_reach
    main = d.get("main")
    if not main:
        return []
    bd = d.get("backend_draft") or {}
    root = (d.get("screen_config") or {}).get("project_root") or ""
    action_of = {bp.get("process_id"): (bp.get("endpoint") or {}).get("action") for bp in main.get("backend_processes", [])}
    idx = {"be_action": action_of}
    apis = {a: dict(v, src=_abs_srcs(v.get("src"), root)) for a, v in (bd.get("apis") or {}).items()}
    cands = [dict(c, src=_abs_srcs(c.get("src"), root)) for c in bd.get("logic_candidates") or []]
    mod_cache, cache = {}, {}
    out = []
    for cl in main.get("common_logic", []):
        ro = cl.get("runs_on") or ""
        if not ("サーバー" in ro or "データベース" in ro):
            continue
        cl2 = dict(cl, src=_abs_srcs(cl.get("src"), root))
        keys = _keys_of_common(cl2, cands)
        for bid in cl.get("used_by_processes_ids") or []:
            if bid not in action_of or _be_fits(bid, keys, idx, apis, cache):
                continue
            # WHY: None(判定できない)は指摘しない。False(辿れず、手掛かりも無い)だけを指摘する。
            if _module_reach(cl2, bid, idx, apis, mod_cache) is False:
                out.append(_vs_common.warn("呼出元BE", bid, f"共通ロジック「{cl.get('name')}」の定義へ、BE の関数から参照で辿れず、手掛かりも本文に無い"))
    return out


def selftest_cl_backend_callers() -> bool:
    """同じ python ファイルに CL の定義(LIMIT)と、BE1(LIMIT を参照)・BE2(参照しない)を置き、BE2 だけ指摘されることで確かめる。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        f = os.path.join(tmp, "mod.py")
        Path(f).write_text("LIMIT = 5\ndef list_a(p):\n    return LIMIT\ndef list_b(p):\n    return 1\n", encoding="utf-8")
        d = {
            "main": {
                "backend_processes": [{"process_id": "BE1", "endpoint": {"action": "list_a"}},
                                      {"process_id": "BE2", "endpoint": {"action": "list_b"}}],
                "common_logic": [{"name": "CL_L", "runs_on": "データベース",
                                  "src": [{"file": f, "line_start": 1, "line_end": 1}],
                                  "used_by_processes_ids": ["BE1", "BE2"]}],
            },
            "backend_draft": {"apis": {"list_a": {"src": [{"file": f, "line_start": 2, "line_end": 3}]},
                                       "list_b": {"src": [{"file": f, "line_start": 4, "line_end": 5}]}}},
        }
        got = check_cl_backend_callers(d)
        return len(got) == 1 and "BE2" in got[0]


CHECKS.append(check_cl_backend_callers)
SELFTESTS.append(selftest_cl_backend_callers)


def _err_key(r) -> str:
    """エラーの行の照合キー。出典の src(先頭の file と line_start)を 'file:line' にして返す。
    WHY: main の error_code は validation_error 等の種別、draft の id は MSE 番号で値の空間が違い一致しないため、
    両方に付く同じ出典(src)で結ぶ。src が無い行だけ従来どおり error_code か id を使う(照合しない空は呼出元で除く)。
    呼出元: check_error_order。"""
    srcs = r.get("src") or []
    if isinstance(srcs, dict):
        srcs = [srcs]
    for s in srcs:
        if isinstance(s, dict) and s.get("file") and s.get("line_start") is not None:
            return f"{s['file']}:{s['line_start']}"
    return str(r.get("error_code") or r.get("id") or "").strip()


def check_error_order(d) -> list[str]:
    """action ごとに、d["main"]["api_spec"] の error_patterns と d["backend_draft"]["apis"][action]["error_patterns"] の
    両方にあるエラーの相対順を比べ、違えば1行で指摘する。並び(ソースの順)は下書きの順を正とする。
    WHY: エラーの出る順が仕様とソースで違うと、同じ条件でも画面に出るメッセージが変わるため。
    呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"]["api_spec"] と d["backend_draft"]["apis"]。"""
    main = d.get("main")
    if not main:
        return []
    apis = (d.get("backend_draft") or {}).get("apis") or {}
    out = []
    for api in main.get("api_spec", []) or []:
        action = api.get("action")
        draft_rows = (apis.get(action) or {}).get("error_patterns") or []
        if not action or not draft_rows:
            continue
        main_keys = [k for k in (_err_key(r) for r in api.get("error_patterns") or []) if k]
        draft_keys = [k for k in (_err_key(r) for r in draft_rows) if k]
        in_draft, in_main = set(draft_keys), set(main_keys)
        cm = [k for k in dict.fromkeys(main_keys) if k in in_draft]
        cd = [k for k in dict.fromkeys(draft_keys) if k in in_main]
        if cm != cd:
            out.append(_vs_common.warn("エラーの順", action, f"両方にあるエラーの並びが、ソースの順と違う(仕様 {' > '.join(cm)} / ソース {' > '.join(cd)})"))
    return out


def selftest_error_order() -> bool:
    """同じ2件の並びを、仕様とソースで入れ替えた版で1件の指摘が出ることと、同じ並びで0件になることで確かめる。"""
    spec = {"action": "A", "error_patterns": [{"error_code": "MSE1"}, {"error_code": "MSE2"}, {"error_code": "MSE3"}]}
    swapped = {"main": {"api_spec": [spec]}, "backend_draft": {"apis": {"A": {"error_patterns": [{"id": "MSE2"}, {"id": "MSE1"}, {"id": "MSE3"}]}}}}
    same = {"main": {"api_spec": [spec]}, "backend_draft": {"apis": {"A": {"error_patterns": [{"id": "MSE1"}, {"id": "MSE2"}, {"id": "MSE3"}]}}}}
    return len(check_error_order(swapped)) == 1 and check_error_order(same) == []


CHECKS.append(check_error_order)
SELFTESTS.append(selftest_error_order)


def _src_list(src) -> list:
    """src(dict か dict の配列)を (file, 開始行, 終了行) の配列にする。読めない要素は捨てる。呼出元: check_event_verb。"""
    if isinstance(src, dict):
        src = [src]
    out = []
    for s in src or []:
        sp = _span(s)
        if sp:
            out.append(sp)
    return out


# クリック系の語。WHY: 押す操作の語を、押さない要素(onChange だけの input・select)に使った食い違いを見るため
_CLICK_WORDS = ("クリック", "押下")
# 要素の種類ごとに手順書(02_イベント一覧.md)の型が求める語。WHY: チェックボックスの change は「チェック」、ラジオは「選択」で書く型のため
_TYPE_VERB = {"checkbox": ("チェック", "チェックボックス"), "radio": ("選択",)}


def _trigger_verdict(typ, want, text):
    """trigger の文 text が、要素の種類 typ の型で書かれているかを見る。外れていれば理由の文、合っていれば None。
    型: checkbox は「{項目名}をチェック、または未チェック。」(手順書 02_イベント一覧.md の change(チェックボックス) の行)。
    radio は「選択」を含む。それ以外の onChange だけの要素は「クリック」「押下」を含まない。
    WHY: action の語だけ直っても、trigger の文が「…をチェックする。」のままだと、外す操作でも走ることが読めない(run15 の EV7・TR6)。呼出元: check_event_verb。"""
    if not text:
        return None
    if typ == "checkbox":
        if "チェック、または未チェック" not in text:
            return f"checkbox の onChange なのに trigger が「{text}」(型は「{{項目名}}をチェック、または未チェック。」)"
    elif want:
        if not any(w in text for w in want):
            return f"{typ} の onChange なのに trigger が「{text}」(型の語は「{want[0]}」)"
    elif any(w in text for w in _CLICK_WORDS):
        return f"onChange だけの要素なのに trigger が「{text}」(型の語は「変更」「選択」など)"
    return None


def check_event_verb(d) -> list[str]:
    """main の events[] を、src が重なる react_draft の jsx_elements(要素の種類 type・tag と、イベント属性 on)と照らし、
    アクション語と trigger の文が要素の種類と食い違う行を指摘する。同じ location の trigger_groups の trigger(action と description)も同じ語なので、EV と TR を1行にまとめる。
    食い違い: (1) checkbox・radio が手順書の型の語(チェック・選択)で無い。(2) onChange だけの要素(select・input)が「クリック」「押下」。
    (3) trigger の文が型どおりでない(checkbox は「{項目名}をチェック、または未チェック。」。_trigger_verdict)。(1)(2) は action、(3) は trigger の文を見る。
    WHY: 要素の種類と属性は画面ソースで決まる事実で、アクション語と trigger の文はその型で書く約束のため。onClick を持つ要素(button・label 等)は対象外。
    action が「チェック」に直っても trigger の文が「…をチェックする。」のままだった run15 の見逃しを、trigger の文も見て止める。
    呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"] の events・trigger_groups と d["react_draft"] の jsx_elements。"""
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    jsx = [(j, _src_list(j.get("src"))) for j in rd.get("jsx_elements", [])]
    out = []
    for ev in main.get("events", []):
        act = ev.get("action") or ""
        spans = _src_list(ev.get("src"))
        verdict = None
        found = None
        for j, js in jsx:
            if not any(_overlap(a, b) for a in spans for b in js):
                continue
            ons = j.get("on") or []
            if "onClick" in ons or "onChange" not in ons:
                continue
            found = found or j
            typ = (j.get("type") or "").lower()
            want = _TYPE_VERB.get(typ)
            if want and not any(w in act for w in want):
                verdict = f"{typ} の onChange なのにアクション語が「{act}」(型の語は「{want[0]}」)"
            elif not want and any(w in act for w in _CLICK_WORDS):
                verdict = f"{j.get('tag') or typ} の onChange だけの要素なのにアクション語が「{act}」(型の語は「変更」「選択」など)"
            if verdict:
                break
        if not found:
            continue
        loc = ev.get("location")
        trs = []
        for g in main.get("trigger_groups", []):
            for t in g.get("triggers", []):
                if loc and t.get("location") == loc and (t.get("action") or "") == act:
                    trs.append((t.get("trigger_no") or "", t.get("description") or ""))
        if verdict:
            where = "/".join([ev.get("no") or ""] + [n for n, _ in trs if n])
            out.append(_vs_common.warn("イベントの語", where, f"{verdict} → 差し戻し先 H1"))
        # WHY: trigger の文は EV の trigger と TR の description の2か所にある。型を外した側だけを where に出す(1行にまとめる)
        typ = (found.get("type") or "").lower()
        checks = [(ev.get("no") or "", ev.get("trigger") or "")] + trs
        bad = [(n, v) for n, v in ((n, _trigger_verdict(typ, _TYPE_VERB.get(typ), text)) for n, text in checks) if v]
        if bad:
            out.append(_vs_common.warn("イベントの語", "/".join(n for n, _ in bad if n), f"{bad[0][1]} → 差し戻し先 H1"))
    return out


def selftest_event_verb() -> bool:
    """checkbox の onChange が「クリック」(当たり)・「チェック」(外れ)と、button の onClick が「クリック」(外れ)で確かめる。"""
    def make(action: str, typ: str, on: list) -> dict:
        src = [{"file": "/x/s.tsx", "line_start": 7, "line_end": 7}]
        return {
            "main": {"events": [{"no": "EV1", "action": action, "src": src, "location": ["画面", "A"]}],
                     "trigger_groups": [{"triggers": [{"trigger_no": "TR1", "action": action, "location": ["画面", "A"]}]}]},
            "react_draft": {"jsx_elements": [{"id": "JX1", "tag": "input", "type": typ, "on": on, "src": src}]},
        }
    hit = check_event_verb(make("クリック", "checkbox", ["onChange"]))

    def with_trigger(typ: str, action: str, text: str) -> list:
        """trigger の文(EV の trigger と TR の description)を text にして、同じ d で見る。"""
        d = make(action, typ, ["onChange"])
        d["main"]["events"][0]["trigger"] = text
        d["main"]["trigger_groups"][0]["triggers"][0]["description"] = text
        return check_event_verb(d)
    # WHY(run15 の EV7・TR6): action が「チェック」でも、trigger の文が「…をチェックする。」のままなら型(チェック、または未チェック。)を外れている。
    #      EV と TR は1行にまとまり、型どおりなら0件。radio は「選択」、onChange だけの text の trigger に「クリック」は当たり
    trig = with_trigger("checkbox", "チェック", "POS会計レポート画面の検索条件エリアの空欄項目も表示をチェックする。")
    trig_ok = with_trigger("checkbox", "チェック", "POS会計レポート画面の検索条件エリアの空欄項目も表示をチェック、または未チェック。")
    return (len(hit) == 1 and "TR1" in hit[0]
            and check_event_verb(make("チェック", "checkbox", ["onChange"])) == []
            and check_event_verb(make("クリック", "button", ["onClick"])) == []
            and len(check_event_verb(make("クリック", "text", ["onChange"]))) == 1
            and len(trig) == 1 and "EV1/TR1" in trig[0] and trig[0].endswith("→ 差し戻し先 H1") and trig_ok == []
            and len(with_trigger("radio", "選択", "画面のAの項目をクリックする。")) == 1
            and with_trigger("radio", "選択", "画面のAの項目を選択する。") == []
            and len(with_trigger("text", "変更", "画面のAの項目をクリックする。")) == 1)


CHECKS.append(check_event_verb)
SELFTESTS.append(selftest_event_verb)


def _fn_name_at(text_lines, line_no):
    """ソースの line_no(1始まり)の行に定義される関数・定数の名前を返す。無ければ None。呼出元: check_cl_rerender。"""
    if not (1 <= line_no <= len(text_lines)):
        return None
    m = re.search(r"(?:\bconst|\blet|\bfunction)\s+([A-Za-z_$][\w$]*)", text_lines[line_no - 1])
    return m.group(1) if m else None


_FN_DEF_RE = re.compile(r"\s*(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>|\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")


def _in_event_handler(lines, line_no):
    """line_no(1始まり)の行が、イベントの handler として渡される名前つきの関数(onClick={exportCsv} の exportCsv)の本体の中なら True。
    WHY(2026-10-15 run15): CSV出力の関数 exportCsv の中で呼ぶ q() を「applied の変化による再描画でも走る」と WARN した誤検出。
    exportCsv は onClick でクリックされたときだけ走り、state の変化による再描画では走らない。再描画で走るのは描画の本体
    (JSX を返す関数・render・useMemo の中)だけなので、handler の中の呼出は再描画の根拠に数えない。
    見方: 呼出行から外側へ、インデントが浅くなる行ごとに const X = (...) => か function X( を拾い、X が on*={X} などで handler に
    渡されていれば True。インライン handler(onClick={() => ...})は見ない(名前が無い)。呼出元: check_cl_rerender。"""
    if not (1 <= line_no <= len(lines)):
        return False
    text = "\n".join(lines)

    def indent(s):
        return len(s) - len(s.lstrip())
    cur = indent(lines[line_no - 1])
    for k in range(line_no - 2, -1, -1):
        ln = lines[k]
        if not ln.strip() or indent(ln) >= cur:
            continue
        cur = indent(ln)
        m = _FN_DEF_RE.match(ln)
        name = (m.group(1) or m.group(2)) if m else None
        if name and re.search(r"\bon[A-Z]\w*\s*=\s*\{\s*(?:\([^)]*\)\s*=>\s*)?" + re.escape(name) + r"\b", text):
            return True
        if cur == 0:
            break
    return False


# 画面のコンポーネント関数の定義行(先頭の桁にある function Card( か const Card = )。呼出元: _enclosing_component
_TOP_COMPONENT_RE = re.compile(r"(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Z][\w$]*)|(?:export\s+)?const\s+([A-Z][\w$]*)\s*=")
# 先頭の桁で始まる宣言(コンポーネントでないもの)。ここを越えて上へ探さない。呼出元: _enclosing_component
_TOP_DECL_RE = re.compile(r"(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|const|let|class|type|interface)\b")


def _enclosing_component(lines, line_no):
    """line_no(1始まり)の行を囲むコンポーネント関数(先頭の桁の function Card( か const Card =)の名前を返す。囲まれていなければ None。
    WHY: CL の関数が Card の描画の中で呼ばれるとき、Card に渡す値(rows から作る agg)が変われば、その呼出も走り直す。
    その「呼出を囲むコンポーネント」を、呼出行から上へ先頭の桁の宣言を探して決める。
    コンポーネントでない宣言(function toAgg など)に先に当たったとき・関数を閉じる行(}・};)に当たったときは、囲まれていないとして None を返す
    (越えて探すと、無関係なコンポーネントに結んでしまう)。引数の型の続き(}: { や }) {)は宣言の途中なので越える。呼出元: check_cl_rerender。"""
    for k in range(min(line_no, len(lines)) - 2, -1, -1):
        ln = lines[k]
        if not ln.strip() or ln[:1].isspace():
            continue
        m = _TOP_COMPONENT_RE.match(ln)
        if m:
            return m.group(1) or m.group(2)
        if _TOP_DECL_RE.match(ln) or ln.rstrip() in ("}", "};", ")", ");"):
            return None
    return None


def _word_rx(names):
    """names のどれかを、別の語の一部(d.rows の rows・setRows の rows)に当たらない形で探す正規表現。names が空なら何にも当たらない。"""
    if not names:
        return re.compile(r"(?!)")
    return re.compile(r"(?<![\w$.])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w$])")


def _derived_names(lines, seeds) -> set:
    """seeds(state 名)から、const X = ... の右辺に seeds か派生済みの名前が出る X を、増えなくなるまで足して返す(rows → aggs → total など)。
    WHY: 描画で使う値(Card に渡す agg)は state そのものでなく、state から useMemo などで作った値なので、名前を辿って「state の変化で変わる値」を集める。
    右辺が関数の本体の始まり(行末が {・(・=>・[)の宣言は、値でなく関数の定義なので見ない。複数行の右辺は、次の ; までの最大5行を足す。呼出元: check_cl_rerender。"""
    names = set(seeds)
    decls = []
    for i, ln in enumerate(lines):
        m = re.match(r"\s*(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=(.*)$", ln)
        if not m or re.search(r"(?:\{|\(|=>|\[)\s*$", m.group(2)):
            continue
        rhs = m.group(2)
        k = i
        while not rhs.rstrip().endswith(";") and k + 1 < len(lines) and k - i < 5:
            k += 1
            rhs += " " + lines[k].strip()
        decls.append((m.group(1), rhs))
    changed = True
    while changed:
        changed = False
        rx = _word_rx(names)
        for x, rhs in decls:
            if x not in names and rx.search(rhs):
                names.add(x)
                changed = True
    return names


def _fed_components(lines, names) -> set:
    """names のどれかを渡している JSX の要素(<Card ... />)のコンポーネント名の集合。要素は、<Name の行から最初の /> までの最大15行を見る。
    WHY: Card の描画の中の呼出は、Card に渡す値が変わると走り直すので、「どのコンポーネントが state の変化で描き直されるか」を要素の渡し方から決める。呼出元: check_cl_rerender。"""
    rx = _word_rx(names)
    out = set()
    for i, ln in enumerate(lines):
        for m in re.finditer(r"<([A-Z][\w$]*)\b", ln):
            block = " ".join(lines[i:i + 15])
            end = block.find("/>")
            if rx.search(block if end < 0 else block[:end]):
                out.add(m.group(1))
    return out


def check_cl_rerender(d) -> list[str]:
    """画面側(frontend)の common_logic の関数が、state の変化による再描画で走るのに、その state を変える FR が used_by_processes_ids に無いものを指摘する。
    state を変える FR: (a) react_draft の events の source.handler に setter(setShowDead(...) 等)があり、event の src が FR の src と重なるもの。
    (b) react_draft の failure_effects の on_success の state(取得結果 rows など)を持つ event の src が FR の src と重なるもの(取得し直して rows を置き換える FR)。
    走る条件(どちらか): (1) CL の関数の呼出行が、その state の参照行(宣言と setter を除く)の直後 8 行以内にある(フィルタ条件の state の下で関数を呼ぶ形)。
    (2) 呼出を囲むコンポーネント(Card の描画の中の num()・fmt())が、その state から作った値(rows → aggs → total)を渡されて描かれる。
    または呼出行が、その state から作った値を直接使う(sumAggs(aggs))。ソースのテキストを読む近似なので WARN のみ。
    WHY: 表示の切替だけの FR でも、切替で行が増えれば CL の関数が初めて走る。取得し直しの FR(店舗の絞り込み・集計単位の切替)も、rows が置き換わるたびに
    Card の描画で CL の関数が行ごとに走る。呼出元の列に挙げないと、CL の使われ方が仕様から抜ける(run15: CL3・CL4 に FR3・FR6 が無かった)。
    呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"] の common_logic・front_processes と d["react_draft"] の events・state_initials・failure_effects。"""
    main = d.get("main")
    rd = d.get("react_draft")
    if not main or not rd:
        return []
    inits = rd.get("state_initials", [])
    setter_of = {s.get("setter"): s.get("state") for s in inits if s.get("setter") and s.get("state")}
    decl_lines = {(sp[0], sp[1]) for s in inits for sp in _src_list(s.get("src"))}
    ev_src = {e.get("event_code"): _src_list(e.get("src")) for e in rd.get("events", [])}
    # FR ごとに変える state(event の handler に setter が出るもの)。fetched は、そのうち取得結果の置き換え((b))で変えるもの
    fr_states = {}
    fetched = {}

    def add_states(spans, states, from_fetch):
        for fp in main.get("front_processes", []):
            if any(_overlap(a, b) for a in spans for b in _src_list(fp.get("src"))):
                fr_states.setdefault(fp.get("process_id"), set()).update(states)
                if from_fetch:
                    fetched.setdefault(fp.get("process_id"), set()).update(states)

    for e in rd.get("events", []):
        h = (e.get("source") or {}).get("handler") or ""
        states = {st for setter, st in setter_of.items() if re.search(r"\b" + re.escape(setter) + r"\s*\(", h)}
        if states:
            add_states(_src_list(e.get("src")), states, False)
    # WHY: 取得し直す FR は、handler では setShopId・setGroup を呼ぶだけで、rows を置き換えるのは useCallback の依存で走る取得。
    #      その rows は failure_effects の on_success に出るので、event の src が重なる FR の「変える state」に足す
    declared = {s.get("state") for s in inits if s.get("state")}
    for fe in rd.get("failure_effects", []):
        states = {s for s in _as_list(fe.get("on_success")) if s in declared}
        if states and ev_src.get(fe.get("event")):
            add_states(ev_src[fe.get("event")], states, True)
    if not fr_states:
        return []
    cache = {}
    def lines_of(f):
        if f not in cache:
            try:
                cache[f] = Path(f).read_text(encoding="utf-8").splitlines()
            except OSError:
                cache[f] = None
        return cache[f]
    out = []
    for cl in main.get("common_logic", []):
        used = cl.get("used_by_processes_ids") or []
        sp = _span(cl.get("src"))
        if not used or not sp or not sp[0].endswith((".tsx", ".ts", ".jsx", ".js")):
            continue
        lines = lines_of(sp[0])
        name = _fn_name_at(lines, sp[1]) if lines else None
        if not name:
            continue
        # WHY: handler(クリックで走る関数)の中の呼出は、state の変化による再描画では走らない。除く(_in_event_handler の WHY)
        calls = [i + 1 for i, ln in enumerate(lines) if re.search(r"(?<![\w$.])" + re.escape(name) + r"\s*\(", ln) and (i + 1) != sp[1]
                 and not _in_event_handler(lines, i + 1)]
        if not calls:
            continue
        derived_cache = {}
        for pid, states in sorted(fr_states.items()):
            if pid in used:
                continue
            # WHY: 取得結果の state を先に見る。同じ FR が group と rows の両方で当たるとき、行を置き換える rows を理由に書く
            for st in sorted(states, key=lambda s: (s not in fetched.get(pid, set()), s)):
                setter = next((k for k, v in setter_of.items() if v == st), "")
                refs = [i + 1 for i, ln in enumerate(lines)
                        if re.search(r"(?<![\w$.])" + re.escape(st) + r"(?![\w$])", ln)
                        and (sp[0], i + 1) not in decl_lines
                        and not (setter and re.search(r"\b" + re.escape(setter) + r"\b", ln))]
                hit = any(r <= c <= r + 8 for r in refs for c in calls)
                if not hit:
                    if st not in derived_cache:
                        names = _derived_names(lines, {st})
                        # WHY: 呼出行の直接の使用には、state 自身を数えない。state 自身(宣言行 useState(todayJst()) など)は上の「参照行の直後」の見方で
                        #      宣言と setter を除いて見ており、ここで数えると宣言の行の呼出(CL1 の todayJst())を取り違える(run15 で実測した誤検出)
                        derived_cache[st] = (_word_rx(names - {st}), _fed_components(lines, names))
                    rx, fed = derived_cache[st]
                    hit = any(_enclosing_component(lines, c) in fed or rx.search(" ".join(lines[c - 1:c + 2])) for c in calls)
                if hit:
                    kind = "取得結果の state " if st in fetched.get(pid, set()) else "state "
                    out.append(_vs_common.warn("CL呼出元の再描画", cl.get("no") or "", f"「{cl.get('name')}」の関数 {name}() は {kind}{st} の変化による再描画でも走るが、{st} を変える {pid} が呼出元に無い → 差し戻し先 S1"))
                    # WHY: 同じ FR が別の state(shopId と rows など)でも当たるときは、1つの FR につき1行にする
                    break
    return out


def selftest_cl_rerender() -> bool:
    """showDead の下で conv() を呼ぶ小さな tsx で、FR2(setShowDead を持つ)が CL の呼出元に無いと1件、有ると0件になることで確かめる。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        f = os.path.join(tmp, "s.tsx")
        Path(f).write_text(
            "const conv = (v: unknown) => Number(v);\n"
            "const [showDead, setShowDead] = useState(false);\n"
            "const rows = list.filter((s) => showDead || !s.dead)\n"
            "  .map((s) => conv(s.v));\n"
            "<input onChange={(e) => setShowDead(e.target.checked)} />\n", encoding="utf-8")
        def make(used: list) -> dict:
            return {
                "main": {"front_processes": [{"process_id": "FR1", "src": [{"file": f, "line_start": 3, "line_end": 4}]},
                                             {"process_id": "FR2", "src": [{"file": f, "line_start": 5, "line_end": 5}]}],
                         "common_logic": [{"no": "CL1", "name": "変換", "used_by_processes_ids": used,
                                           "src": [{"file": f, "line_start": 1, "line_end": 1}]}]},
                "react_draft": {"state_initials": [{"state": "showDead", "setter": "setShowDead",
                                                    "src": [{"file": f, "line_start": 2, "line_end": 2}]}],
                                "events": [{"event_code": "EV1", "source": {"file": f, "line": 5, "handler": "(e) => setShowDead(e.target.checked)"},
                                            "src": [{"file": f, "line_start": 5, "line_end": 5}]}]},
            }
        ok = len(check_cl_rerender(make(["FR1"]))) == 1 and check_cl_rerender(make(["FR1", "FR2"])) == []
        # WHY(run15 の誤検出の再現): CL の関数 q() を、onClick でクリックされる exportCsv の中だけで呼ぶとき、
        # applied の参照行の直後 8 行以内でも再描画では走らないので指摘なし。JSX を返す render の中で呼ぶ num() は従来どおり1件
        g = os.path.join(tmp, "h.tsx")
        Path(g).write_text(
            "const q = (v: unknown) => `\"${v}\"`;\n"
            "const [applied, setApplied] = useState(0);\n"
            "const exportCsv = () => {\n"
            "  const line = (r: Row | null) =>\n"
            "    [\n"
            "      applied.from,\n"
            "      q(r),\n"
            "    ].join(\",\");\n"
            "};\n"
            "<button onClick={exportCsv} />\n"
            "<form onSubmit={() => setApplied(1)} />\n", encoding="utf-8")
        h = {
            "main": {"front_processes": [{"process_id": "FR2", "src": [{"file": g, "line_start": 11, "line_end": 11}]}],
                     "common_logic": [{"no": "CL5", "name": "引用符囲み", "used_by_processes_ids": ["FR9"],
                                       "src": [{"file": g, "line_start": 1, "line_end": 1}]}]},
            "react_draft": {"state_initials": [{"state": "applied", "setter": "setApplied", "src": [{"file": g, "line_start": 2, "line_end": 2}]}],
                            "events": [{"event_code": "EV2", "source": {"file": g, "line": 11, "handler": "() => setApplied(1)"},
                                        "src": [{"file": g, "line_start": 11, "line_end": 11}]}]},
        }
        in_handler = _in_event_handler(Path(g).read_text(encoding="utf-8").splitlines(), 7)
        in_render = _in_event_handler(Path(f).read_text(encoding="utf-8").splitlines(), 4)
        # WHY(run15 の見逃しの再現): 店舗の絞り込み(FR2)は handler で setShopId を呼ぶだけで、rows を置き換えるのは useCallback の取得。
        # CL の関数 num() は Card の描画の中で行ごとに呼ばれ、Card は rows から作った aggs を渡されて描かれる。
        # FR2 が呼出元に無ければ「取得結果の state rows」の再描画として1件、有れば0件。Card を介さない非コンポーネント(toAgg)の呼出は数えない
        p = os.path.join(tmp, "p.tsx")
        Path(p).write_text(
            "const num = (v: unknown) => Number(v);\n"
            "function toAgg(r: Row): Agg {\n"
            "  return { k: num(r.k) };\n"
            "}\n"
            "function Card({ agg }: { agg: Agg }) {\n"
            "  const kin = num(agg.k);\n"
            "  return <td>{kin}</td>;\n"
            "}\n"
            "export default function Page() {\n"
            "  const [rows, setRows] = useState<Row[]>([]);\n"
            "  const [shopId, setShopId] = useState(0);\n"
            "  const aggs = useMemo(() => rows.map(toAgg), [rows]);\n"
            "  const load = useCallback(() => {\n"
            "    fetchRows(shopId).then((d) => setRows(d.rows));\n"
            "  }, [shopId]);\n"
            "  return (\n"
            "    <main>\n"
            "      <select onChange={(e) => setShopId(Number(e.target.value))} />\n"
            "      {rows.map((r, i) => (\n"
            "        <Card key={i}\n"
            "          agg={aggs[i]}\n"
            "        />\n"
            "      ))}\n"
            "    </main>\n"
            "  );\n"
            "}\n", encoding="utf-8")

        def fetch_make(used: list) -> dict:
            return {
                "main": {"front_processes": [{"process_id": "FR2", "src": [{"file": p, "line_start": 18, "line_end": 18}]}],
                         "common_logic": [{"no": "CL3", "name": "数値化", "used_by_processes_ids": used,
                                           "src": [{"file": p, "line_start": 1, "line_end": 1}]}]},
                "react_draft": {"state_initials": [{"state": "rows", "setter": "setRows", "src": [{"file": p, "line_start": 10, "line_end": 10}]},
                                                   {"state": "shopId", "setter": "setShopId", "src": [{"file": p, "line_start": 11, "line_end": 11}]}],
                                "events": [{"event_code": "EV2", "source": {"file": p, "line": 18, "handler": "(e) => setShopId(Number(e.target.value))"},
                                            "src": [{"file": p, "line_start": 18, "line_end": 18}]}],
                                "failure_effects": [{"event": "EV2", "call": "fetchRows", "on_success": ["rows"], "on_failure": ["error"]}]},
            }
        fetched = check_cl_rerender(fetch_make(["FR1"]))
        p_lines = Path(p).read_text(encoding="utf-8").splitlines()
        fetch_ok = (len(fetched) == 1 and "取得結果の state rows" in fetched[0] and "FR2" in fetched[0] and fetched[0].endswith("→ 差し戻し先 S1")
                    and check_cl_rerender(fetch_make(["FR1", "FR2"])) == []
                    and _enclosing_component(p_lines, 3) is None and _enclosing_component(p_lines, 6) == "Card")
        return ok and check_cl_rerender(h) == [] and in_handler and not in_render and fetch_ok


CHECKS.append(check_cl_rerender)
SELFTESTS.append(selftest_cl_rerender)


_GT_RE = re.compile(r"(\d+)\s*(?:行|件|店|レジ|枚|個|つ|人|回|日|列)?\s*(?:を超え|超え|より多)")
_GE_RE = re.compile(r"(\d+)\s*(?:行|件|店|レジ|枚|個|つ|人|回|日|列)?\s*以上")
_SRC_LEN_RE = re.compile(r"\.length\s*(>=|>)\s*(\d+)")


def _min_counts(text: str) -> list:
    """文から「N を超えて」(最小は N+1)と「N 以上」(最小は N)の閾値を (最小の件数, 語) の配列で返す。呼出元: check_threshold_wording。"""
    out = [(int(n) + 1, f"{n}を超えて") for n in _GT_RE.findall(text)]
    out += [(int(n), f"{n}以上") for n in _GE_RE.findall(text)]
    return out


def check_threshold_wording(d) -> list[str]:
    """画面項目の備考・検証の文・FR の手順などの文の、件数の閾値の言い換えを指摘する(WARN)。
    (1) 1文の中に「Nを超えて」と「N以上」が混在して最小の件数が食い違う(「2行を超えて(2行以上)」)。
    (2) 文の最小の件数が、ソースの比較(react_draft の behaviors・jsx_elements・ui_strings の `.length > N` `.length >= N`)の最小に
        1 だけずれる(超える/以上の取り違え)。ソースの最小の集合に入る閾値は通す。
    WHY: 表示条件の閾値は超える/以上の1語で意味が1件変わるのに、言い換えのときに取り違えやすい。ソースの比較との突合は、無関係な比較に当たる誤検出を避けるため、1件ずれる場合だけ指摘する。
    呼出元: run(_vs_checks_ext.run_all 経由)。読むのは d["main"] と d["react_draft"]。"""
    main = d.get("main")
    rd = d.get("react_draft")
    if not main:
        return []
    texts = []
    for it in (main.get("screen_layout") or {}).get("items", []):
        texts.append((it.get("no") or "", it.get("remarks")))
    for v in main.get("validations", []):
        texts.append((v.get("no") or "", v.get("rule")))
        texts.append((v.get("no") or "", v.get("applied_when")))
    for fp in main.get("front_processes", []):
        for st in fp.get("steps", []):
            texts.append((fp.get("process_id") or "", st.get("description")))
    for ep in main.get("event_processes", []):
        for t in ep.get("details", []):
            texts.append((ep.get("event_ref") or "", t))
    for c in main.get("calculations", []):
        texts.append((c.get("no") or "", c.get("remarks")))
    src_min = set()
    if rd:
        blob = json.dumps([rd.get(k) for k in ("behaviors", "jsx_elements", "ui_strings")], ensure_ascii=False)
        for op, n in _SRC_LEN_RE.findall(blob):
            src_min.add(int(n) + 1 if op == ">" else int(n))
    out = []
    for where, text in texts:
        if not isinstance(text, str) or not text:
            continue
        found = _min_counts(text)
        if not found:
            continue
        if len({m for m, _ in found}) > 1:
            out.append(_vs_common.warn("閾値の言い換え", where, f"1文の中で閾値の言い方が食い違う({' / '.join(w for _, w in found)})"))
            continue
        m, w = found[0]
        if src_min and m not in src_min and any(abs(m - s) == 1 for s in src_min):
            out.append(_vs_common.warn("閾値の言い換え", where, f"「{w}」はソースの比較(最小 {sorted(src_min)} 件)と1件ずれる"))
    return out


def selftest_threshold_wording() -> bool:
    """「2行を超えて(2行以上)」の混在で1件、ソースが length > 1 のとき「2行を超えて」だけの文で1件、「2行以上」で0件になることで確かめる。"""
    def make(remarks: str) -> dict:
        return {"main": {"screen_layout": {"items": [{"no": "IT1", "remarks": remarks}]}},
                "react_draft": {"behaviors": [{"cond": "rows.length > 1"}]}}
    return (len(check_threshold_wording(make("会計レポートが2行を超えて(2行以上)あるときだけ表示する"))) == 1
            and len(check_threshold_wording(make("2行を超えてあるときだけ表示する"))) == 1
            and check_threshold_wording(make("2行以上あるときだけ表示する")) == [])


CHECKS.append(check_threshold_wording)
SELFTESTS.append(selftest_threshold_wording)


def run(inter_dir, feature) -> list:
    """d = _vs_common.load(...) を各 check に渡し、指摘の行を連結して返す。呼出元: _vs_checks_ext.run_all。"""
    d = _vs_common.load(Path(inter_dir), feature)
    out = []
    for check in CHECKS:
        out += check(d)
    return out


def selftest() -> bool:
    """SELFTESTS の各関数を全部呼び、全部 True なら True。WHY: 1つ落ちても残りを呼び、どれが落ちたかを1回の実走で全部出せるようにする。"""
    results = [bool(t()) for t in SELFTESTS]
    return all(results)
