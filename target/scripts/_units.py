#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_units.py — 委譲の単位(サブエージェント1体)ごとの断片 _unit_<単位>.json を検査し、partial 4本へ合成する共通部品

【目的】
Phase 1 を「欄ごとに担当(haiku / sonnet)を分けたサブエージェント」へ並行に委譲する。各単位は自分の欄だけを
target/intermediate/_unit_<単位>.json に書き、互いの出力を待たない。合成はここで機械的に行う。
  - 分担表 prompts/units.json が「どの単位がどの欄を書くか(owns)」「識別子つきの一覧の行を誰が作るか(creators)」を持つ
  - 検査(check_*): 単位どうしの書く範囲が重ならないこと、担当外の欄を書いていないこと、
    行の識別子が作り手の断片と食い違っていないこと、合成後の参照(action・event_code・message_id 等)が実在すること
  - 合成(compose): 識別子(IDENTITY)で行を突き合わせて欄を重ね、partial 4本の形(PARTIAL_OF)に分ける

【意味合い】
- 行の並びは作り手(creators)の断片の並びを正とする(api_spec の並びが B001… の番号になるため)
- 問題は1行1件「[種別] 場所: 内容 → 差し戻し先 <単位>」。差し戻し先は分担表の owns から引く
- 断片が1つも無いときは何もしない(HTML の画面を1体の Agent で作る従来の流れは変わらない)

【接続情報】
  - 呼出元: verify_partials.py --units(検査) / merge_partials.py --compose-units(合成して partial 4本を書く)
  - 分担表: <スキルのフォルダ>/prompts/units.json。単位の手順書は同じフォルダの <単位>_*.md
  - 合成後の partial は従来どおり apply_react_draft.py --stage partial → merge_partials.py が読む
"""
import copy
import json
import re
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
UNITS_SPEC_PATH = SKILL_DIR / "prompts" / "units.json"
UNIT_PREFIX = "_unit_"

# 識別子つきの一覧(汎用パス → 行の識別キー)。単位どうしは、この識別子で同じ行を指す
IDENTITY = {
    "screen_layout.items": "item_name",
    "events": "event_code",
    "events_peripheral": "event_code",
    "event_processes": "event_code",
    "db_operations": "id",
    "api_spec": "action",
    "api_spec[].response_items": "key",
    "api_spec[].request_params": "name",
    "messages_backend": "message_id",
}

# 断片の最上位キー → partial の名前(merge_partials.PARTIALS のキー)。
# event_processes は partial の従来のキーに無い。merge_partials が details をここから event_processes へ写す
PARTIAL_OF = {
    "screen_layout": "screen_events", "events": "screen_events", "event_processes": "screen_events",
    "calculations": "calc_state", "validations_frontend": "calc_state", "common_logic": "calc_state",
    "logical_names": "backend", "db_operations": "backend", "api_spec": "backend",
    "messages_backend": "backend", "parameters_backend": "backend",
    "events_peripheral": "peripheral", "common_logic_peripheral": "peripheral",
}
PARTIAL_NAMES = ["screen_events", "calc_state", "backend", "peripheral"]

# 1からの通し番号 no を持つ一覧。no は並び順で機械的に決まるので、単位には書かせず合成で振る(書いてあっても振り直す)
NUMBERED = ["screen_layout.items", "validations_frontend", "calculations", "parameters_backend", "common_logic"]

# 合成後に必須のキー(15_中間JSONスキーマ.md の「必須 ◯」のうち、単位が書くもの)。一覧の汎用パス → キー。
# 識別子(IDENTITY)・合成で振る no・後段の script が付けるキー(events の no・location・action・source_kind 等)は含めない。
# 欠けの差し戻し先は分担表の owns から引く。担当の単位の断片がまだ無いとき(並行の途中)は照合しない
REQUIRED = {
    "screen_layout.items": ["area_ref", "item_type"],  # screen_no は add_screen_areas.py が area_ref から写す
    "events": ["trigger", "content", "classification"],
    "validations_frontend": ["field", "rule", "applied_when"],
    "calculations": ["name", "formula"],
    "messages_backend": ["code", "severity", "message"],
    "parameters_backend": ["name"],
    "common_logic": ["name", "expression"],
    "api_spec": ["method", "path", "description"],
    "db_operations": ["operation_type"],
}


def _rows(composed, generic):
    node = composed
    for k in generic.split("."):
        node = node.get(k) if isinstance(node, dict) else None
    return [e for e in node or [] if isinstance(e, dict)]


def load_spec(path=None):
    return json.loads(Path(path or UNITS_SPEC_PATH).read_text(encoding="utf-8"))


def load_fragments(directory):
    """directory の _unit_<単位>.json を読む。戻り: ({単位: dict}, 問題の list)"""
    frags, problems = {}, []
    for p in sorted(Path(directory).glob(f"{UNIT_PREFIX}*.json")):
        unit = p.stem[len(UNIT_PREFIX):]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"[入力不正] {p.name}: {e} → 差し戻し先 {unit}")
            continue
        if not isinstance(data, dict):
            problems.append(f"[入力不正] {p.name}: 最上位がオブジェクトでない → 差し戻し先 {unit}")
            continue
        frags[unit] = data
    return frags, problems


def owns(pattern, generic):
    """pattern(分担表の欄)が generic(汎用パス)を含むか。'a.b' は 'a.b'・'a.b.c'・'a.b[].c' を含む"""
    return generic == pattern or generic.startswith(pattern + ".") or generic.startswith(pattern + "[]")


def owner_of(spec, generic):
    for unit, u in (spec.get("units") or {}).items():
        if any(owns(p, generic) for p in u.get("owns") or []):
            return unit
    return None


def _walk(node, generic, concrete, leaves, entries):
    """断片を葉(書いた欄)と行(識別子つき一覧の行)に分解する。
    leaves: (汎用パス, 具体パス) / entries: (一覧の汎用パス, 行の具体パス)"""
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, f"{generic}.{k}" if generic else k, f"{concrete}.{k}" if concrete else k, leaves, entries)
        return
    if isinstance(node, list) and generic in IDENTITY:
        idk = IDENTITY[generic]
        for i, el in enumerate(node):
            if not isinstance(el, dict) or el.get(idk) in (None, ""):
                leaves.append((generic + "[]", f"{concrete}[#{i}]<{idk} が無い>"))
                continue
            row = f"{concrete}[{el[idk]}]"
            entries.append((generic, row))
            for k, v in el.items():
                if k != idk:
                    _walk(v, f"{generic}[].{k}", f"{row}.{k}", leaves, entries)
        return
    leaves.append((generic, concrete))


def decompose(frag):
    leaves, entries = [], []
    _walk(frag, "", "", leaves, entries)
    return leaves, entries


def check_spec(spec):
    """分担表そのものの検査: 単位どうしの欄が重ならない・識別子つき一覧には作り手が1つある"""
    problems = []
    units = spec.get("units") or {}
    names = list(units)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for pa in units[a].get("owns") or []:
                for pb in units[b].get("owns") or []:
                    if owns(pa, pb) or owns(pb, pa):
                        problems.append(f"[分担表] {a} の {pa} と {b} の {pb} が重なる")
    creators = spec.get("creators") or {}
    for lst in IDENTITY:
        c = creators.get(lst)
        if c is None:
            problems.append(f"[分担表] {lst} の行の作り手(creators)が無い")
        elif c not in units:
            problems.append(f"[分担表] {lst} の作り手 {c} が units に無い")
        elif not any(owns(p, lst) or owns(lst, p) for p in units[c].get("owns") or []):
            problems.append(f"[分担表] {lst} の作り手 {c} がその一覧の欄を持たない")
    return problems


def check_fragments(spec, frags, only=None):
    """断片の検査。[範囲外] 担当外の欄 / [重複] 同じ欄を2単位が書いた / [参照] 行の識別子が作り手の断片に無い"""
    problems = []
    units = spec.get("units") or {}
    creators = spec.get("creators") or {}
    parsed = {u: decompose(f) for u, f in frags.items()}
    rows = {u: {e[1] for e in parsed[u][1]} for u in parsed}
    written = {}
    for unit, (leaves, entries) in parsed.items():
        if unit not in units:
            problems.append(f"[範囲外] _unit_{unit}.json: 分担表(prompts/units.json)に無い単位 → 差し戻し先 {unit}")
            continue
        mine = units[unit].get("owns") or []
        for generic, concrete in leaves:
            written.setdefault(concrete, []).append(unit)
            if not any(owns(p, generic) for p in mine):
                who = owner_of(spec, generic)
                problems.append(f"[範囲外] {unit}: {concrete} は {who or '分担表のどの単位でもない欄'} の担当 → 差し戻し先 {unit}")
        for lst, row in entries:
            c = creators.get(lst)
            # 作り手の断片がまだ無いとき(並行に走っている途中)は照合しない。全単位が揃った後の検査で拾う
            if c and c != unit and c in rows and row not in rows[c]:
                problems.append(f"[参照] {unit}: 行 {row} が作り手 {c} の断片に無い(識別子の食い違いか {c} の書き漏れ) → 差し戻し先 {unit} か {c}")
    for concrete, us in written.items():
        if len(us) > 1:
            problems.append(f"[重複] {concrete}: {', '.join(us)} が同じ欄を書いた → 差し戻し先 担当外の側")
    if only:
        problems = [p for p in problems if f" {only}" in p or f"_unit_{only}." in p]
    return problems


def _backend_draft_facts():
    """backend 下書き(<画面>_backend_draft.json)から (認証の fact, {文面: メッセージ ID}, 認証の error_patterns) を取る。
    呼出元: compose。意味合い: 認証の掛かり方とメッセージ ID は機械抽出の下書きが正。auth の error_patterns(ソースに拒否の処理があったものだけ)も返す。下書きが無ければ ("", {}, [])。"""
    from _config_loader import INTERMEDIATE_DIR
    fact, msgs, auth_errs = "", {}, []
    for p in sorted(Path(INTERMEDIATE_DIR).glob("*_backend_draft.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        auths = d.get("auth") or []
        auth_errs += [e for a in auths for e in a.get("error_patterns") or []]
        fact = fact or next((a["fact"] for a in auths if a.get("fact")), "")
        pats = [e for a in (d.get("apis") or {}).values() for e in a.get("error_patterns") or []]
        pats += [e for a in auths for e in a.get("error_patterns") or []]
        for e in pats:
            for key in ("message", "text_expanded"):  # H4 は型文のままか展開済みの文面のどちらかで書く
                if e.get("id") and e.get(key):
                    msgs[e[key]] = e["id"]
    return fact, msgs, auth_errs


def _align_message_ids(out, draft_msgs):
    """H4 の messages_backend が下書きと同じ文面で別の ID を書いていたら、下書きの ID へ寄せる。
    参照している error_patterns・validations_frontend の message_id も同じ置換にする(参照切れを作らない)。"""
    rename = {}
    for m in out.get("messages_backend") or []:
        want = draft_msgs.get(m.get("message"))
        if want and m.get("message_id") != want:
            rename[m["message_id"]] = want
            m["message_id"] = m["code"] = want
    if not rename:
        return
    refs = [e for a in out.get("api_spec") or [] for e in a.get("error_patterns") or []]
    refs += [v for v in out.get("validations_frontend") or []]
    for r in refs:
        if isinstance(r, dict) and r.get("message_id") in rename:
            r["message_id"] = rename[r["message_id"]]


def _expand_message_holes(out, draft_msgs):
    """messages_backend の型文の {key} 等を、画面項目名で展開する(verify_intermediate は未展開の穴を ERROR にするため)。
    意味合い: 下書きは同じ ID の文面を「型文」と「展開済み(例 shop_id は…)」の両方で持つ。展開済みから {key} の実際の値(要求の項目名)を
    取り、その項目の説明(request_params / parameters_backend の description)が画面項目名で始まるなら、その画面項目名で置き換える。
    対応が取れない穴が1つでもあれば、その文面は触らず穴として残す(検査が差し戻す)。
    ponytail: 対応は「説明が画面項目名で始まる」だけで取る(最長の項目名を優先)。取れなければ下書きの明示の対応が要る。"""
    items = sorted({i["item_name"] for i in (out.get("screen_layout") or {}).get("items") or []
                    if isinstance(i, dict) and i.get("item_name")}, key=len, reverse=True)
    rows = [r for a in out.get("api_spec") or [] for r in a.get("request_params") or []] + list(out.get("parameters_backend") or [])
    item_of = {}
    for r in rows:
        if isinstance(r, dict) and r.get("name"):
            desc = r.get("description") or ""
            item_of.setdefault(r["name"], next((n for n in items if desc.startswith(n)), None))
    by_id = {}
    for text, mid in draft_msgs.items():
        by_id.setdefault(mid, []).append(text)
    for m in out.get("messages_backend") or []:
        parts = re.split(r"(\{[^{}\s]+\})", m.get("message") or "")
        if len(parts) < 3:
            continue
        pat = "".join("(.+?)" if i % 2 else re.escape(p.rstrip("。") if i == len(parts) - 1 else p) for i, p in enumerate(parts))
        for text in by_id.get(m.get("message_id"), []):
            g = re.fullmatch(pat, text.rstrip("。"))
            if g and all(item_of.get(v) for v in g.groups()):
                names = iter(item_of[v] for v in g.groups())
                m["message"] = "".join(next(names) if i % 2 else p for i, p in enumerate(parts))
                break


def missing_logical_names(composed):
    """dataset に出る (物理列, 論理列) のうち logical_names に論理列が無いもの。戻り: [(logical_table, logical_column, physical_table, physical_column)]
    呼出元: _fill_logical_names(合成)・verify_partials.check_h2_logical_names(H2 単位の早期 WARN)。"""
    known = {n.get("logical_column") for n in composed.get("logical_names") or [] if isinstance(n, dict)}
    out = []
    for op in composed.get("db_operations") or []:
        for r in op.get("dataset") or []:
            lc = r.get("logical_column") if isinstance(r, dict) else None
            if lc and lc not in known:
                known.add(lc)
                out.append((r.get("logical_table", ""), lc, r.get("physical_table", ""), r.get("physical_column", "")))
    return out


def _fill_logical_names(out):
    """dataset の論理列が logical_names に無ければ、物理名と対で足す(値の欠けを埋めるだけの機械的な補い)。"""
    miss = missing_logical_names(out)
    if not miss:
        return
    names = out.setdefault("logical_names", [])
    for lt, lc, pt, pc in miss:
        names.append({"no": len(names) + 1, "logical_table": lt, "logical_column": lc,
                      "physical_table": pt, "physical_column": pc})


def compose(spec, frags, project_config=None):
    """断片を識別子で突き合わせて重ねる。戻り: 最上位キーの dict。行の並びは作り手の断片を先に取る"""
    creators = spec.get("creators") or {}
    out = {}
    keys = []
    for frag in frags.values():
        for k in frag:
            if k not in keys:
                keys.append(k)
    for k in keys:
        first = creators.get(k)
        order = ([first] if first in frags else []) + [u for u in frags if u != first]
        for u in order:
            if k in frags[u]:
                if k not in out:
                    out[k] = copy.deepcopy(frags[u][k])
                else:
                    out[k] = _merge(out[k], frags[u][k], k)
    # project_config の common_errors(全 API 共通のエラー)と auth_description(認証の説明)を各 api_spec へ足す。
    # 意味合い: 共通のエラーを単位ごとに書かせると食い違うため、設定から機械的に入れる。
    # 同じ status_code が既にあれば足さず、認証の欄が埋まっていれば触らない。設定が無ければ何もしない。
    # 接続情報: 呼出元 = merge_partials.py --compose-units が _config_loader.load_project_config() の値を渡す。
    # 足した message_id は messages_backend にも入れ、check_refs の参照検査で実在を確かめる。
    common = list((project_config or {}).get("common_errors") or [])
    auth_desc = (project_config or {}).get("auth_description") or ""
    draft_msgs = {}
    if project_config is not None:
        # backend 下書きの auth.fact(認証の掛かり方の事実)と、メッセージの ID・文面を正として使う
        auth_fact, draft_msgs, auth_errs = _backend_draft_facts()
        if not auth_desc and auth_fact:
            auth_desc = auth_fact
        # 401/403 などの認証の失敗は、ソースが拒否の処理を持つときだけ足す(下書きの auth.error_patterns)。固定の文では足さない(D2)
        for e in auth_errs:
            sc = e.get("status_code")
            if sc and not any(c.get("status_code") == sc for c in common):
                common.append({"status_code": sc, "condition": e.get("text_expanded") or e.get("message") or ""})
    _align_message_ids(out, draft_msgs)
    _expand_message_holes(out, draft_msgs)
    if common or auth_desc:
        for api in out.get("api_spec") or []:
            if auth_desc and not api.get("auth"):
                api["auth"] = auth_desc
            errs = api.setdefault("error_patterns", [])
            for ce in common:
                if not any(e.get("status_code") == ce.get("status_code") for e in errs):
                    errs.append(copy.deepcopy(ce))
        if common and "api_spec" in out:
            msgs = out.setdefault("messages_backend", [])
            have = {m.get("message_id") for m in msgs if isinstance(m, dict)}
            for ce in common:
                mid = ce.get("message_id")
                if mid and mid not in have:
                    msgs.append({**{k: ce[k] for k in ("code", "severity", "message") if k in ce}, "message_id": mid})
                    have.add(mid)
    _fill_logical_names(out)
    for generic in NUMBERED:
        for i, row in enumerate(_rows(out, generic), start=1):
            row["no"] = i
    return out


def check_required(spec, frags, composed):
    """合成後の必須キーの欠け。[必須] 場所: キーが無い → 差し戻し先 <そのキーを持つ単位>"""
    problems = []
    for generic, keys in REQUIRED.items():
        idk = IDENTITY.get(generic)
        for i, row in enumerate(_rows(composed, generic)):
            for k in keys:
                owner = owner_of(spec, f"{generic}[].{k}")
                if k not in row and owner in frags:
                    problems.append(f"[必須] {generic}[{row.get(idk) or '#' + str(i)}]: {k} が無い → 差し戻し先 {owner}")
    return problems


def _merge(dst, src, generic):
    if isinstance(dst, dict) and isinstance(src, dict):
        for k, v in src.items():
            g = f"{generic}.{k}"
            dst[k] = _merge(dst[k], v, g) if k in dst else copy.deepcopy(v)
        return dst
    if isinstance(dst, list) and isinstance(src, list) and generic in IDENTITY:
        idk = IDENTITY[generic]
        index = {el.get(idk): el for el in dst if isinstance(el, dict)}
        for el in src:
            if isinstance(el, dict) and el.get(idk) in index:
                tgt = index[el.get(idk)]
                for k, v in el.items():
                    g = f"{generic}[].{k}"
                    tgt[k] = _merge(tgt[k], v, g) if k in tgt else copy.deepcopy(v)
            else:
                dst.append(copy.deepcopy(el))
        return dst
    return copy.deepcopy(src)


def split_partials(composed):
    """合成した最上位キーを partial 4本に分ける。PARTIAL_OF に無いキーは unknown に名前を返す(検査の [範囲外] で止まる)"""
    parts = {n: {} for n in PARTIAL_NAMES}
    unknown = []
    for k, v in composed.items():
        if k in PARTIAL_OF:
            parts[PARTIAL_OF[k]][k] = v
        else:
            unknown.append(k)
    return parts, unknown


def check_refs(spec, composed):
    """合成後の参照の検査。並行に書いた単位どうしの食い違いを、ここで拾う"""
    problems = []

    def ids(key, idk):
        return {e.get(idk) for e in composed.get(key) or [] if isinstance(e, dict)}

    def back(generic):
        return owner_of(spec, generic) or "?"

    events = ids("events", "event_code") | ids("events_peripheral", "event_code")
    actions = ids("api_spec", "action")
    op_ids = ids("db_operations", "id")
    msgs = ids("messages_backend", "message_id")
    items = {e.get("item_name") for e in (composed.get("screen_layout") or {}).get("items") or []}
    if "events" in composed:
        for ep in composed.get("event_processes") or []:
            if ep.get("event_code") not in events:
                problems.append(f"[参照] event_processes[{ep.get('event_code')}]: events に無い event_code → 差し戻し先 {back('event_processes')}")
        for it in (composed.get("screen_layout") or {}).get("items") or []:
            c = it.get("event_code")
            if c and c not in events:
                problems.append(f"[参照] screen_layout.items[{it.get('item_name')}].event_code={c}: events に無い → 差し戻し先 {back('screen_layout.items[].event_code')}")
        # trigger の項目名が S1 の item_name と食い違うと、後段の events[].location が権威語彙に無い ERROR になる。
        # その event_code を持つ項目の名前が、どれも trigger に字面で入っていなければ H1 へ戻す
        by_code = {}
        for it in (composed.get("screen_layout") or {}).get("items") or []:
            if it.get("event_code") and it.get("item_name"):
                by_code.setdefault(it["event_code"], []).append(it["item_name"])
        for ev in composed.get("events") or []:
            names = by_code.get(ev.get("event_code"))
            if names and not any(n in (ev.get("trigger") or "") for n in names):
                problems.append(f"[項目名] events[{ev.get('event_code')}].trigger「{ev.get('trigger')}」に画面項目名「{'」か「'.join(names)}」が無い(screen_layout.items の item_name と字面を揃える) → 差し戻し先 {back('events[].trigger')}")
    for op in composed.get("db_operations") or []:
        if not isinstance(op, dict):
            problems.append(f"[形] db_operations の要素が辞書でない({type(op).__name__}): {{id, api_endpoint: {{action, trigger_events}}, ...}} の辞書で書く → 差し戻し先 {back('db_operations[].api_endpoint')}")
            continue
        api = op.get("api_endpoint") or {}
        if not isinstance(api, dict):
            problems.append(f"[形] db_operations[{op.get('id')}].api_endpoint が辞書でない({type(api).__name__}「{api}」): {{\"action\": \"<api_spec の action>\", \"trigger_events\": [\"<event_code>\"]}} の辞書で書く → 差し戻し先 {back('db_operations[].api_endpoint')}")
            continue
        if not isinstance(api.get("trigger_events") or [], list):
            problems.append(f"[形] db_operations[{op.get('id')}].api_endpoint.trigger_events がリストでない: [\"<event_code>\"] で書く → 差し戻し先 {back('db_operations[].api_endpoint')}")
            continue
        if "api_spec" in composed and api.get("action") and api.get("action") not in actions:
            problems.append(f"[参照] db_operations[{op.get('id')}].api_endpoint.action={api.get('action')}: api_spec に無い → 差し戻し先 {back('db_operations[].api_endpoint')}")
        if "events" in composed:
            for c in (api.get("trigger_events") or []) + ([api["trigger_event"]] if api.get("trigger_event") else []):
                if c not in events:
                    problems.append(f"[参照] db_operations[{op.get('id')}].api_endpoint: {c} が events に無い → 差し戻し先 {back('db_operations[].api_endpoint')}")
    for api in composed.get("api_spec") or []:
        a = api.get("action")
        if "db_operations" in composed:
            for r in api.get("related_db_operations") or []:
                if r not in op_ids:
                    problems.append(f"[参照] api_spec[{a}].related_db_operations: {r} が db_operations に無い → 差し戻し先 {back('api_spec[].related_db_operations')}")
        keys = {r.get("key") for r in api.get("response_items") or []}
        for m in api.get("response_to_screen_mapping") or []:
            if items and m.get("screen_item_name") not in items:
                problems.append(f"[参照] api_spec[{a}].response_to_screen_mapping: 画面項目 {m.get('screen_item_name')} が screen_layout.items に無い → 差し戻し先 {back('api_spec[].response_to_screen_mapping')}")
            if keys and m.get("api_field_path") not in keys:
                problems.append(f"[参照] api_spec[{a}].response_to_screen_mapping: {m.get('api_field_path')} が response_items の key に無い → 差し戻し先 {back('api_spec[].response_to_screen_mapping')}")
        if "messages_backend" in composed:
            for e in api.get("error_patterns") or []:
                if e.get("message_id") and e.get("message_id") not in msgs:
                    problems.append(f"[参照] api_spec[{a}].error_patterns: {e.get('message_id')} が messages_backend に無い → 差し戻し先 {back('api_spec[].error_patterns')}")
    if "messages_backend" in composed:
        for v in composed.get("validations_frontend") or []:
            if v.get("message_id") and v.get("message_id") not in msgs:
                problems.append(f"[参照] validations_frontend[{v.get('no')}]: {v.get('message_id')} が messages_backend に無い → 差し戻し先 {back('validations_frontend')}")
    return problems


def check_all(spec, frags, only=None):
    """分担表・断片・合成後の参照をまとめて検査する。only を与えるとその単位に関わる行だけ返す"""
    problems = check_spec(spec) + check_fragments(spec, frags, only)
    composed = compose(spec, frags)
    refs = check_refs(spec, composed) + check_required(spec, frags, composed)
    if only:
        refs = [p for p in refs if p.endswith(f" {only}")]
    return problems + refs
