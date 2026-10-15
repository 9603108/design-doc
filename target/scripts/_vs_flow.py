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
