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


def compose(spec, frags):
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
    return out


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
    for op in composed.get("db_operations") or []:
        api = op.get("api_endpoint") or {}
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
    refs = check_refs(spec, compose(spec, frags))
    if only:
        refs = [p for p in refs if p.endswith(f" {only}")]
    return problems + refs
