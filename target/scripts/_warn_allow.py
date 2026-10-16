#!/usr/bin/env python3
"""WARN の許可リスト(target/intermediate/{機能名}_warn_allow.json)の読込と照合。

WHY: 検査の WARN は「直さなくてよい理由がある」ものと「見落とし」が混ざる。理由つきで登録した WARN だけを許し、
     登録の無い WARN が残れば不合格にできるようにする(納品の条件「WARN にはすべて説明がつく」)。
     run23 の実測で、run22 の登録 10 件のうち 5 件しか当たらなかった。キーに手順の番号(step_no=4)や処理の名前
     (POS店舗一覧取得)のような回し直しで変わる値を入れていたため。回し直しで変わりにくい識別子で引く形を足した。
WHERE: verify_intermediate.py・verify_vs_source.py の --warn-allow-strict と、run_units.py の gate が使う共通部品(stdlib のみ)。
       WARN の種類(KINDS)・種類ごとの対象の欄・識別子の引き方は、このファイルの1か所に置く。

登録の形は2つ。どちらも reason(空でない説明)と date(YYYY-MM-DD)が要る。理由の無い登録は受けない。
 新形式(種類で書く。回し直しに強い):
   {"kind":"period", "targets":["steps"], "owner":"CSV出力:クリック", "step_kind":"state_update", "text":"データ行:",
    "reason":"…", "date":"2026-10-16"}
   kind     WARN の種類(KINDS のキー)。必須
   targets  対象の欄(steps・remarks・rule など。steps は steps・processing・processing_steps をまとめて指す)。target(文字列)でも可
   owner    処理・項目を指す元の識別子。データから引く(owner_idents): バックエンド=endpoint の action(例 list_pos_shops)、
            画面処理=トリガーの「場所の末尾:action」(例 CSV出力:クリック)か legacy_id、検証=field、計算・共通=name
   step_kind 手順の種別(steps の要素の kind)。手順の番号は使わない
   text     WARN の本文の抜粋、または手順・欄の全文に含まれる文字列(例: 失敗したとき・データ行:)
   section  front_processes などの節名
   kind だけの登録は受けない(targets・owner・step_kind・text・section のどれかで絞る)。owner・step_kind を使う照合にはデータが要る
 旧形式(移行用): {"key":"WARN行に部分一致する文字列", "reason":"…", "date":"…"}。番号・名前が入るので回し直しで外れやすい
"""
import json
import re
import sys
from pathlib import Path

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# WARN の種類。説明は検査の出力と selftest 用
KINDS = {
    "period": "1 文 1 概念ルール違反疑い(句点が2個以上)",
    "too_long": "概要長すぎ疑い(句点なしで長い)",
    "params_empty": "request_spec.params が空",
    "threshold": "閾値の言い換え",
}

# 対象の欄の別名(WARN の欄の名前の集まり)
FIELD_GROUPS = {"steps": {"steps", "processing", "processing_steps"}}
STEP_KEYS = ("steps", "processing", "processing_steps")

_SEC = r"(?P<section>[a-z_]+)\[(?P<id>[^\]]+)\]"
# (種類, 正規表現, 欄を固定する場合の欄名)。名前つきグループ: section・id・field・step_no・excerpt
_PARSERS = [
    ("period", re.compile(r"概念ルール違反疑い \(step\): " + _SEC + r"\.(?P<field>\w+)\[step_no=(?P<step_no>[^\]]+)\]\.description に句点 \d+ 個 \((?P<excerpt>.*)\)\s*$"), None),
    ("period", re.compile(r"概念ルール違反疑い \(error_pattern\): " + _SEC + r"\.(?P<field>error_patterns)\[\]\.business_reason に句点 \d+ 個 \((?P<excerpt>.*)\)\s*$"), None),
    ("period", re.compile(r"概念ルール違反疑い: " + _SEC + r"\.(?P<field>\w+) に句点 \d+ 個 \((?P<excerpt>.*)\)\s*$"), None),
    ("too_long", re.compile(r"概要長すぎ疑い: " + _SEC + r"\.(?P<field>\w+) が \d+ 文字 \+ 句点なし \((?P<excerpt>.*)\)\s*$"), None),
    ("params_empty", re.compile(_SEC + r"[^:]*: request_spec\.params が空"), "params"),
    ("threshold", re.compile(r"\[閾値の言い換え\] (?P<id>[A-Za-z]+\d+):"), "threshold"),
]
_ID_KEYS = ("process_id", "validation_id", "calc_id", "logic_id", "op_id", "no", "id")
_NEW_FIELDS = {"kind", "targets", "target", "owner", "step_kind", "text", "section", "reason", "date"}


def parse_warn(line):
    """WARN 行を {kind, section, id, field, step_no, excerpt, line} にする。種類が分からない行は None(旧形式の key だけで照合する)。"""
    for kind, rx, fixed_field in _PARSERS:
        m = rx.search(line)
        if not m:
            continue
        g = m.groupdict()
        ex = g.get("excerpt")
        if ex is not None and ex.endswith("..."):
            ex = ex[:-3]
        return {"kind": kind, "section": g.get("section"), "id": g.get("id"), "field": fixed_field or g.get("field"),
                "step_no": g.get("step_no"), "excerpt": ex, "line": line}
    return None


def _targets(e):
    t = e.get("targets")
    if t is None and e.get("target") is not None:
        t = [e["target"]]
    return t or []


def _expand(targets):
    out = set()
    for t in targets:
        out |= FIELD_GROUPS.get(t, {t})
    return out


def load_allow(path):
    """許可リストを読んで登録の一覧を返す。ファイルが無ければ [] 。
    旧形式は key・reason・date、新形式は kind と絞り込みの欄・reason・date が要る。
    空の欄・date が YYYY-MM-DD でない・知らない欄や種類・kind だけで絞りが無い登録は ValueError(理由の無い登録は受けない)。"""
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    entries = data.get("allow") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{p}: 'allow' の配列が無い")

    def need(e, i, f):
        v = e.get(f)
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"{p}: allow[{i}].{f} が空(理由の無い登録は受けない)")

    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise ValueError(f"{p}: allow[{i}] が object でない")
        if "key" in e or "kind" not in e:
            for f in ("key", "reason", "date"):
                need(e, i, f)
        else:
            unknown = sorted(set(e) - _NEW_FIELDS)
            if unknown:
                raise ValueError(f"{p}: allow[{i}] に知らない欄 {unknown}(使える欄: {sorted(_NEW_FIELDS)})")
            if e["kind"] not in KINDS:
                raise ValueError(f"{p}: allow[{i}].kind が知らない種類: {e['kind']}(使える種類: {sorted(KINDS)})")
            for f in ("reason", "date"):
                need(e, i, f)
            for f in ("owner", "step_kind", "text", "section", "target"):
                if f in e:
                    need(e, i, f)
            if "targets" in e and (not isinstance(e["targets"], list) or not e["targets"]
                                   or any(not isinstance(t, str) or not t.strip() for t in e["targets"])):
                raise ValueError(f"{p}: allow[{i}].targets は空でない文字列の配列にする")
            if not (_targets(e) or any(e.get(f) for f in ("owner", "step_kind", "text", "section"))):
                raise ValueError(f"{p}: allow[{i}] は kind だけ(targets・owner・step_kind・text・section のどれかで絞る)")
        if not DATE_RE.match(e["date"]):
            raise ValueError(f"{p}: allow[{i}].date が YYYY-MM-DD でない: {e['date']}")
    return entries


def load_data(path):
    """識別子の引き当てに使う中間 JSON を読む。読めなければ None(owner・step_kind の登録は当たらないだけで、他は動く)。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def split_warns(lines):
    """検査の出力の行から WARN 行(印 [WARN] を含む行)だけを返す。呼出元: 各検査の --warn-allow-strict、gate。"""
    return [ln for ln in lines if "[WARN]" in ln]


def _find_item(data, section, id_):
    if not isinstance(data, dict):
        return section, None
    secs = [section] if section else [k for k, v in data.items() if isinstance(v, list)]
    for s in secs:
        for it in data.get(s) or []:
            if isinstance(it, dict) and any(it.get(k) == id_ for k in _ID_KEYS):
                return s, it
    return section, None


def owner_idents(data, section, item):
    """処理・項目を指す、回し直しで変わりにくい識別子の集合。process_id(番号)・name(処理の言い回し)は含めない。"""
    out = set()
    if not isinstance(item, dict):
        return out
    for k in ("legacy_id", "action", "event_code", "legacy_event_code", "field"):
        v = item.get(k)
        if isinstance(v, str) and v:
            out.add(v)
    ep = item.get("endpoint")
    if isinstance(ep, dict) and isinstance(ep.get("action"), str):
        out.add(ep["action"])
    if section in ("calculations", "common_logic") and isinstance(item.get("name"), str):
        out.add(item["name"])
    if section == "front_processes" and isinstance(data, dict):
        pid = item.get("process_id")
        for g in data.get("trigger_groups") or []:
            for t in (g.get("triggers") or []) if isinstance(g, dict) else []:
                if isinstance(t, dict) and pid in (t.get("calls") or []):
                    loc = t.get("location")
                    if isinstance(loc, list) and loc and t.get("action"):
                        out.add(f"{loc[-1]}:{t['action']}")
    return out


def _find_step(item, rec):
    if not isinstance(item, dict) or rec.get("step_no") is None:
        return None
    for s in item.get(rec["field"]) or []:
        if isinstance(s, dict) and str(s.get("step_no") or s.get("no")) == str(rec["step_no"]):
            return s
    return None


def _match_new(e, rec, data):
    if rec is None or e["kind"] != rec["kind"]:
        return False
    tg = _targets(e)
    if tg and rec["field"] not in _expand(tg):
        return False
    section, item = _find_item(data, rec["section"], rec["id"])
    if e.get("section") and e["section"] != section:
        return False
    if e.get("owner") and e["owner"] not in owner_idents(data, section, item):
        return False
    step = _find_step(item, rec)
    if e.get("step_kind") and not (step and step.get("kind") == e["step_kind"]):
        return False
    if e.get("text"):
        full = ""
        if step and isinstance(step.get("description"), str):
            full = step["description"]
        elif isinstance(item, dict) and isinstance(item.get(rec["field"]), str):
            full = item[rec["field"]]
        hay = f"{rec['excerpt']}\n{full}" if rec["excerpt"] is not None else f"{rec['line']}\n{full}"
        if e["text"] not in hay:
            return False
    return True


def matches(e, line, rec, data):
    """登録 e が WARN 行に当たるか。旧形式は key の部分一致、新形式は種類・欄・識別子・特徴で照合する。"""
    if "key" in e:
        return e["key"] in line
    return _match_new(e, rec, data)


def unallowed(warn_lines, allow, data=None):
    """許可に当たらない WARN 行を返す。data は識別子(owner・step_kind)の引き当てに使う中間 JSON(省略可)。"""
    out = []
    for w in warn_lines:
        rec = parse_warn(w)
        if not any(matches(e, w, rec, data) for e in allow):
            out.append(w)
    return out


def unmatched(warn_lines, allow, data=None):
    """どの WARN にも当たらなかった登録を返す(回し直しで外れた登録・直って要らなくなった登録の洗い出し用)。"""
    recs = [(w, parse_warn(w)) for w in warn_lines]
    return [e for e in allow if not any(matches(e, w, rec, data) for w, rec in recs)]


def describe_entry(e):
    """当たらない登録を gate の出力に出す短い表記。"""
    if "key" in e:
        return f"key={e['key']}"
    parts = [f"kind={e['kind']}"] + [f"{f}={e[f]}" for f in ("targets", "target", "section", "owner", "step_kind", "text") if f in e]
    return " ".join(parts)


def selftest():
    import tempfile
    lines = ["[ERROR] x", "  [WARN] api_spec[a].description 長い", "[WARN] db_operations[DB1] 空", "[INFO] i"]
    warns = split_warns(lines)
    assert len(warns) == 2, warns
    allow = [{"key": "api_spec[a].description", "reason": "原文どおり", "date": "2026-10-16"}]
    assert unallowed(warns, allow) == ["[WARN] db_operations[DB1] 空"], unallowed(warns, allow)
    assert unallowed(warns, []) == warns
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x_warn_allow.json"
        assert load_allow(p) == [], "ファイルが無ければ []"
        p.write_text(json.dumps({"allow": allow}), encoding="utf-8")
        assert load_allow(p) == allow
        assert load_data(p) == {"allow": allow} and load_data(Path(d) / "none.json") is None
        base = {"reason": "r", "date": "2026-10-16"}
        bads = (
            {"key": "", "reason": "r", "date": "2026-10-16"},
            {"key": "k", "reason": " ", "date": "2026-10-16"},
            {"key": "k", "reason": "r", "date": ""},
            {"key": "k", "reason": "r", "date": "10/16"},
            {"kind": "period", "targets": ["steps"], "reason": "", "date": "2026-10-16"},   # 理由なし
            {"kind": "period", "targets": ["steps"], "reason": "r", "date": "10/16"},
            {"kind": "period", **base},                                                       # kind だけ
            {"kind": "nothing", "targets": ["steps"], **base},                                # 知らない種類
            {"kind": "period", "targets": [], **base},
            {"kind": "period", "targets": ["steps"], "ownr": "x", **base},                    # 欄名の打ち間違い
            {"kind": "period", "targets": ["steps"], "text": " ", **base},
            {"reason": "r", "date": "2026-10-16"},                                            # key も kind も無い
        )
        for bad in bads:
            p.write_text(json.dumps({"allow": [bad]}), encoding="utf-8")
            try:
                load_allow(p)
            except ValueError:
                continue
            raise AssertionError(f"拒否されない: {bad}")
        good = [{"kind": "period", "targets": ["steps", "remarks", "rule"], **base},
                {"kind": "period", "target": "steps", "owner": "x", "step_kind": "state_update", "text": "t", "section": "front_processes", **base}]
        p.write_text(json.dumps({"allow": good}), encoding="utf-8")
        assert load_allow(p) == good

    # 実際の WARN の形(run23)から、種類・欄・手順の番号を取り出す
    step_w = "[WARN] v51 §19 1 文 1 概念ルール違反疑い (step): front_processes[FR8].steps[step_no=5].description に句点 4 個 (データ行: 表示中の行ごとに開始日(確定済みの集計開始日の値)、終了日(確定済み...)"
    r = parse_warn(step_w)
    assert (r["kind"], r["section"], r["id"], r["field"], r["step_no"]) == ("period", "front_processes", "FR8", "steps", "5"), r
    assert r["excerpt"].startswith("データ行:") and not r["excerpt"].endswith("..."), r
    rule_w = "[WARN] v51 §19 1 文 1 概念ルール違反疑い: validations[VL12].rule に句点 2 個 (書式 YYYY-MM-DD だけを見る（^\\d{4}-\\d{2}-\\d{2}$）。文字列でない値も拒否する。2026-0...)"
    r = parse_warn(rule_w)
    assert (r["kind"], r["id"], r["field"]) == ("period", "VL12", "rule"), r
    rem_w = "[WARN] v51 §19 1 文 1 概念ルール違反疑い: calculations[CA4].remarks に句点 2 個 (客数が 0 のときは 0。現行と同じ...)"
    assert parse_warn(rem_w)["field"] == "remarks"
    ep_w = "[WARN] v51 §19 1 文 1 概念ルール違反疑い (error_pattern): backend_processes[BE2].error_patterns[].business_reason に句点 2 個 (あ。い。...)"
    assert parse_warn(ep_w)["field"] == "error_patterns"
    long_w = "[WARN] v51 §19 概要長すぎ疑い: front_processes[FR1].overview が 90 文字 + 句点なし (あいう...)"
    assert parse_warn(long_w)["kind"] == "too_long"
    par_w = "[WARN] backend_processes[BE1] POS会計対象店舗一覧取得: request_spec.params が空（リクエストに何も受け取らない API？）"
    r = parse_warn(par_w)
    assert (r["kind"], r["id"], r["field"]) == ("params_empty", "BE1", "params"), r
    th_w = "[WARN] [閾値の言い換え] VL7: 「2を超えて」はソースの比較(最小 [1, 2] 件)と1件ずれる"
    assert parse_warn(th_w)["kind"] == "threshold" and parse_warn(th_w)["id"] == "VL7"
    assert parse_warn("[WARN] 知らない種類") is None

    # 回し直しで手順の番号・処理の名前が変わっても、種類・識別子・特徴で当たる
    def data(step_no, bname, desc="データ行: 表示中の行ごとに…。引用符で囲む。"):
        return {
            "front_processes": [{"process_id": "FR8", "legacy_id": "F008", "name": "CSV出力",
                                 "steps": [{"step_no": step_no, "kind": "process", "description": desc}]}],
            "trigger_groups": [{"triggers": [{"action": "クリック", "location": ["画面", "検索条件エリア", "CSV出力"], "calls": ["FR8"]}]}],
            "backend_processes": [{"process_id": "BE1", "name": bname, "endpoint": {"action": "list_pos_shops"}}],
            "validations": [{"no": "VL7", "field": "空欄項目も表示"}],
        }
    e_step = {"kind": "period", "targets": ["steps"], "owner": "CSV出力:クリック", "step_kind": "process", "text": "データ行:", **base}
    e_params = {"kind": "params_empty", "owner": "list_pos_shops", **base}
    e_kindwide = {"kind": "period", "targets": ["steps", "remarks", "rule"], **base}
    for step_no, bname in (("5", "POS会計対象店舗一覧取得"), ("4", "POS店舗一覧取得")):
        dd = data(int(step_no), bname)
        w = step_w.replace("step_no=5", f"step_no={step_no}")
        pw = par_w.replace("POS会計対象店舗一覧取得", bname)
        assert unallowed([w, pw], [e_step, e_params], dd) == [], step_no
    dd = data(5, "x")
    # 絞りが外れれば当たらない(特徴・種別・識別子・種類・欄)
    assert unallowed([step_w], [{**e_step, "text": "合計行:"}], dd) == [step_w]
    assert unallowed([step_w], [{**e_step, "step_kind": "state_update"}], dd) == [step_w]
    assert unallowed([step_w], [{**e_step, "owner": "F009"}], dd) == [step_w]
    assert unallowed([step_w], [{**e_step, "owner": "F008"}], dd) == []
    assert unallowed([step_w], [{**e_step, "kind": "too_long"}], dd) == [step_w]
    assert unallowed([step_w], [{**e_step, "targets": ["remarks"]}], dd) == [step_w]
    assert unallowed([step_w], [e_step], None) == [step_w], "データが無ければ owner の登録は当たらない"
    # 全文の特徴(抜粋の60字より後ろ)も引ける
    assert unallowed([step_w], [{**e_step, "text": "引用符で囲む"}], data(5, "x")) == []
    assert unallowed([step_w], [{**e_step, "text": "引用符で囲む"}], data(5, "x", desc="データ行: あ。い。")) == [step_w]
    # 種類ごとの許容: 欄で書ける。steps は processing・processing_steps も指す
    assert unallowed([step_w, rule_w, rem_w, ep_w, long_w], [e_kindwide]) == [ep_w, long_w]
    assert unallowed([step_w.replace(".steps[", ".processing[")], [{"kind": "period", "target": "steps", **base}]) == []
    # 旧形式と新形式の混在。しきい値は owner(検証の field)で引く
    old = {"key": "front_processes[FR8].steps[step_no=4].description に句点", **base}
    assert unallowed([step_w, th_w], [old, {"kind": "threshold", "owner": "空欄項目も表示", **base}], dd) == [step_w]
    # 当たらない登録の洗い出し
    miss = unmatched([step_w, rule_w], [old, e_step, e_params, e_kindwide], dd)
    assert miss == [old, e_params], miss
    assert describe_entry(old).startswith("key=") and "owner=list_pos_shops" in describe_entry(e_params)
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    print("usage: _warn_allow.py --selftest", file=sys.stderr)
    sys.exit(2)
