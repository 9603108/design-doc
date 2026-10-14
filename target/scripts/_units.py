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
    "validations_frontend": ["field", "rule", "applied_when", "kind"],  # kind: B5 の処理の種別(verify_vs_source.check_kinds が照合)
    "calculations": ["name", "formula"],
    "messages_backend": ["code", "severity", "message"],
    "parameters_backend": ["name"],
    "common_logic": ["name", "expression"],
    "api_spec": ["method", "path", "description"],
    "db_operations": ["operation_type"],
}

# F2: 合成が backend 下書きの事実(apis[*].db_operations[].ctes・subqueries)で埋める欄。単位(H2)は書かない。
# WHY: 副問い合わせ・CTE を単位に書かせると、ソースの SQL に無い作り話が混じる。単位が書いてしまった値は範囲外の ERROR にせず、
#      check_fragments で数えずに飛ばし、compose が下書きの事実で置き換える(_fill_db_op_facts)。
COMPOSED = {"db_operations[].subqueries", "db_operations[].ctes"}


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
            if generic in COMPOSED:
                # WHY: 合成が下書きの事実で置き換える欄なので、単位が書いても範囲外・重複の問題にしない(WARN は verify_partials が出す)
                continue
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
    """backend 下書き(<画面>_backend_draft.json)から (認証の fact, {文面: メッセージ ID}, 認証の error_patterns, 展開) を取る。
    呼出元: compose。意味合い: 認証の掛かり方とメッセージ ID は機械抽出の下書きが正。auth の error_patterns(ソースに拒否の処理があったものだけ)も返す。
    展開 = {型文: [(ID, 呼出ごとの展開済みの文面, 検査の対象 param)]}。型文は {key} などの穴を持つソースの文面で、展開済みは呼出箇所の実引数で埋めたもの
    (下書きの text_expanded。取れなかった型文は入らない)。下書きが無ければ ("", {}, [], {})。"""
    from _config_loader import INTERMEDIATE_DIR
    fact, msgs, auth_errs, expansions = "", {}, [], {}
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
            t, x = (e.get("message") or "").rstrip("。"), e.get("text_expanded") or ""
            if e.get("id") and x and x != e.get("message") and re.search(r"\{[^{}]+\}", t):
                lst = expansions.setdefault(t, [])
                if e["id"] not in [i for i, _, _ in lst]:  # 同じ展開済みの文は同じ ID(下書きが1件にまとめている)
                    lst.append((e["id"], x, e.get("param") or ""))
    return fact, msgs, auth_errs, expansions


def _draft_common_errors():
    """backend 下書きの最上位 common_errors(入口の except 節)のうち、status が数値で取れたものを共通エラーの行にして返す(F2)。
    呼出元: compose。意味合い: 入口で受ける DB 例外などの 500 系は、API ごとに書かせると抜けるため、ソースで status が決まったものだけ機械で足す。
    status が式(getattr(e, 'status', 400) or 400)のもの・via の補助関数の中で決まるもの・同じ status の2件目以降は足さない(推測しない)。
    condition は例外名と code だけの事実(業務の言い換えは S2 が書く)。下書きが無ければ []。"""
    from _config_loader import INTERMEDIATE_DIR
    by_status = {}
    for p in sorted(Path(INTERMEDIATE_DIR).glob("*_backend_draft.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for c in d.get("common_errors") or []:
            for e in c.get("errors") or []:
                if isinstance(e.get("status_code"), int):
                    by_status.setdefault(e["status_code"], []).append((c.get("exception"), e.get("code"), c.get("src") or []))
    out = []
    for sc, hits in by_status.items():
        excs = "・".join(dict.fromkeys(x for x, _, _ in hits if x))
        codes = [c for _, c, _ in hits if isinstance(c, str) and re.fullmatch(r"\w+", c)]
        src = [s for _, _, ss in hits for s in ss]
        out.append({"status_code": sc, "condition": f"想定外の例外が起きたとき({excs}" + (f"。code={codes[0]}" if codes else "") + ")",
                    **({"src": src} if src else {})})
    return out


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


def _expand_messages(out, expansions):
    """messages_backend の型文({key} など)を、下書きが持つ呼出ごとの実際の文面(from・to・shop_id の実引数で展開したもの)へ置き換える。
    呼出元: compose(_align_message_ids の前)。意味合い: 画面に出る文言はソースの実行時の文字列なので、型文のままだと
    verify_intermediate が未展開の穴を ERROR にする。画面項目名への言い換えはせず、呼出ごとに1件のメッセージ(ID は下書きのもの)にする。
    型文が下書きに無い・展開が取れなかったものは触らず穴のまま残す(検査が差し戻す)。
    参照(api_spec.error_patterns・validations_frontend の message_id、request_params.validation の括弧内の ID)は、元の型文の ID から
    展開した ID へ付け替える。展開が複数のときは、その行の字面に request_params の説明の先頭の語(例 集計開始日)があれば、その項目の呼出の ID。
    決まらなければ先頭の展開の ID。
    ponytail: 付け替えは説明の先頭の語の一致だけで取る(外れても参照は実在する ID を指す)。"""
    rows = [m for m in out.get("messages_backend") or []]
    fixed = [m for m in rows if not (isinstance(m, dict) and expansions.get((m.get("message") or "").rstrip("。")))]
    have = {m.get("message_id") for m in fixed if isinstance(m, dict)}
    remap, new = {}, []
    for m in rows:
        ex = expansions.get((m.get("message") or "").rstrip("。")) if isinstance(m, dict) else None
        if not ex:
            new.append(m)
            continue
        remap[m.get("message_id")] = [(i, p) for i, _, p in ex]
        for i, text, _ in ex:
            if i not in have:
                have.add(i)
                new.append({**{k: v for k, v in m.items() if k != "src"}, "message_id": i, "code": i, "message": text})
    if not remap:
        return
    out["messages_backend"] = new
    params = [q for a in out.get("api_spec") or [] for q in a.get("request_params") or [] if isinstance(q, dict) and q.get("name")]
    head = {q["name"]: re.split(r"[。、（(]", q.get("description") or "")[0] for q in params}
    refs = [e for a in out.get("api_spec") or [] for e in a.get("error_patterns") or []] + list(out.get("validations_frontend") or [])
    for r in refs:
        if isinstance(r, dict) and r.get("message_id") in remap:
            text = json.dumps(r, ensure_ascii=False)
            r["message_id"] = next((i for i, p in remap[r["message_id"]] if head.get(p) and head[p] in text), remap[r["message_id"]][0][0])
    for q in params:
        def to_id(g, q=q):
            return next((i for i, p in remap.get(g.group(0), []) if p == q["name"]), g.group(0))
        if q.get("validation"):
            q["validation"] = re.sub(r"MSE\d+", to_id, q["validation"])


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


def _fill_src(out):
    """下書きの src(ソースの出典 {file,line_start,line_end} の配列)を、合成した行へ機械で付ける。断片に src がある行は触らない。
    呼出元: compose(_fill_logical_names の前)。意味合い: src は機械抽出の下書きが正で、単位に書かせると作り話の行番号が混じるため。
    照合: events/event_processes←react の event_code、api_spec←backend.apis の action、response_items←apis[action] の key、
    error_patterns←下書きの id(message_id)、messages_backend←同じ id、db_operations←apis[*].db_operations の id。下書きが無ければ何もしない。"""
    from _config_loader import INTERMEDIATE_DIR
    ev, ep, api, ri, err, dbo = {}, {}, {}, {}, {}, {}
    d = Path(INTERMEDIATE_DIR)
    for p in sorted(d.glob("*_react_draft.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ev.update({e["event_code"]: e["src"] for e in r.get("events") or [] if e.get("src") and e.get("event_code")})
        ep.update({e["event_code"]: e["src"] for e in r.get("event_processes") or [] if e.get("src") and e.get("event_code")})
    for p in sorted(d.glob("*_backend_draft.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for act, a in (b.get("apis") or {}).items():
            if a.get("src"):
                api[act] = a["src"]
            ri.update({(act, x["key"]): x["src"] for x in a.get("response_items") or [] if x.get("src") and x.get("key")})
            dbo.update({o["id"]: o["src"] for o in a.get("db_operations") or [] if o.get("src") and o.get("id")})
            err.update({e["id"]: e["src"] for e in a.get("error_patterns") or [] if e.get("src") and e.get("id")})
        err.update({e["id"]: e["src"] for au in b.get("auth") or [] for e in au.get("error_patterns") or [] if e.get("src") and e.get("id")})

    def put(row, src):
        if src and not row.get("src"):
            row["src"] = copy.deepcopy(src)

    for r in _rows(out, "events"):
        put(r, ev.get(r.get("event_code")))
    for r in _rows(out, "event_processes"):
        put(r, ep.get(r.get("event_code")))
    for r in _rows(out, "db_operations"):
        put(r, dbo.get(r.get("id")))
    for r in _rows(out, "messages_backend"):
        put(r, err.get(r.get("message_id")))
    for a in _rows(out, "api_spec"):
        put(a, api.get(a.get("action")))
        for x in a.get("response_items") or []:
            if isinstance(x, dict):
                # rows[].x のように下書きの return キー(rows)の下の項目は、キー自身が無ければ先頭のキー(rows)の出典を使う(下書きにある範囲)
                put(x, ri.get((a.get("action"), x.get("key"))) or ri.get((a.get("action"), re.split(r"[\[.]", str(x.get("key")))[0])))
        for x in a.get("error_patterns") or []:
            if isinstance(x, dict):
                put(x, err.get(x.get("message_id")))


def _initial_text(si):
    """state_initials の1件(kind・initial・readable)を、画面項目の「初期値」欄の文にする。
    リテラルは原文のまま(空文字と空配列だけ言葉にする)、日付関数を含む式は readable、それ以外の式は原文を「式:」付きで書く。"""
    kind, raw = si.get("kind"), si.get("initial") or ""
    if kind == "none":
        return "未設定(undefined)"
    if kind == "literal":
        return {'""': "空文字", "''": "空文字", "[]": "空の一覧"}.get(raw, raw)
    return si.get("readable") or f"式: {raw}"


def _fill_db_op_facts(out):
    """db_operations[].ctes・subqueries を、backend 下書きの apis[*].db_operations[] の事実(同じ id の行)で置き換える(F2)。
    呼出元: compose(_fill_src の後)。意味合い: 副問い合わせ・CTE はソースの SQL を機械が分解した事実なので、単位(H2)の書き写しは使わない。
    単位が書いた値は捨てる(差し戻さない。check_fragments が COMPOSED の欄を飛ばす)。下書きに同じ id の行が無いときは事実が無いので欄を外す。
    WHY: 欄を外すのは、単位が書いた値が下書きに無い SQL の作り話を残さないため。事実で埋めるのを下書きの有無に任せると、無い下書きで作り話が残る。"""
    from _config_loader import INTERMEDIATE_DIR
    facts = {}
    for p in sorted(Path(INTERMEDIATE_DIR).glob("*_backend_draft.json")):
        try:
            b = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for a in (b.get("apis") or {}).values():
            for o in a.get("db_operations") or []:
                if isinstance(o, dict) and o.get("id"):
                    facts[o["id"]] = o
    _set_db_op_facts(_rows(out, "db_operations"), facts)


def _set_db_op_facts(rows, facts):
    """rows の各行の ctes・subqueries を、facts(id → 下書きの db_operations の行)の値で置き換える。facts に無い id の欄は外す。"""
    for r in rows:
        f = facts.get(r.get("id"))
        if f is None:
            r.pop("ctes", None)
            r.pop("subqueries", None)
            continue
        r["ctes"] = copy.deepcopy(f.get("ctes") or [])
        r["subqueries"] = copy.deepcopy(f.get("subqueries") or [])


def _fill_initial_values(out):
    """画面項目(screen_layout.items)の initial_value 欄を、react 下書きの state_initials(useState の初期値の事実)から埋める。
    呼出元: compose(_fill_src の後)。意味合い: 初期表示の表を「全項目 Blank」と書く誤りを止めるため、モデルに書かせず事実で入れる。
    照合: state_initials[].bound[].label と item_name の一致。結ぶ state が無い項目(ボタン・固定の文言など)は欄を作らない。
    initial_value が既にある項目は触らない。下書きに state_initials が無ければ何もしない。
    接続: 欄を読んで初期処理の表へ写す側は merge_partials.build_initialization(少なくとも、そこが初期値を Blank 一律にしている)。"""
    from _config_loader import INTERMEDIATE_DIR
    by_label = {}
    for p in sorted(Path(INTERMEDIATE_DIR).glob("*_react_draft.json")):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for si in r.get("state_initials") or []:
            for b in si.get("bound") or []:
                if b.get("label"):
                    by_label.setdefault(str(b["label"]).strip(), si)
    for it in _rows(out, "screen_layout.items"):
        si = by_label.get(str(it.get("item_name") or "").strip())
        if si and not it.get("initial_value"):
            it["initial_value"] = _initial_text(si)


def _react_draft_facts():
    """react 下書きから (初期表示のイベントの code の集合, 初期表示で呼ぶ API の action の集合) を取る。
    呼出元: _fill_initial_db_triggers。意味合い: 初期表示の行は events の action が「初期表示」のもの(画面名は使わない)。
    API は、その行の event_processes の calls と、db_operations の trigger_events にその行の code を持つものの action。
    initial_fetches の関数名(fetchXxx)は API の action と綴りが違うため、照合には使わない(推測で結ばない)。
    下書きが無ければ (set(), set())。"""
    from _config_loader import INTERMEDIATE_DIR
    drafts = []
    for p in sorted(Path(INTERMEDIATE_DIR).glob("*_react_draft.json")):
        try:
            drafts.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    init = {e["event_code"] for d in drafts for e in d.get("events") or []
            if isinstance(e, dict) and e.get("action") == "初期表示" and e.get("event_code")}
    actions = set()
    for d in drafts:
        for ep in d.get("event_processes") or []:
            if isinstance(ep, dict) and ep.get("event_code") in init:
                actions |= {c["action"] for c in ep.get("calls") or [] if isinstance(c, dict) and c.get("action")}
        for op in d.get("db_operations") or []:
            if not isinstance(op, dict):
                continue
            api = op.get("api_endpoint")
            if isinstance(api, dict) and api.get("action") and init & set(api.get("trigger_events") or []):
                actions.add(api["action"])
    return init, actions


def _origin_error_codes():
    """backend 下書きの common_errors(_error_origins の出どころ)から、式の code → 既定の code(code_default)の対応を取る。
    呼出元: _fix_message_codes。意味合い: 例外の code は、ソースの式 getattr(e, 'code', None) or 'validation_error' のまま
    メッセージに書かれやすい。式の中の既定の文字列(validation_error)が実際の code なので、そこへ寄せる。下書きが無ければ {}。"""
    from _config_loader import INTERMEDIATE_DIR
    out = {}
    for p in sorted(Path(INTERMEDIATE_DIR).glob("*_backend_draft.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for c in d.get("common_errors") or []:
            for e in c.get("errors") or []:
                if isinstance(e, dict) and e.get("code") and e.get("code_default"):
                    out.setdefault(str(e["code"]), e["code_default"])
    return out


def _event_code_map(events):
    """events の行から (旧コード → 現行の番号の対応, 現行の番号の集合) を取る。
    呼出元: _normalize_event_refs・_fill_initial_db_triggers。意味合い: 現行の番号は、行の no が「EV<数字>」の形ならその no
    (migrate_v49_event_unify の後の形)、無ければ event_code(Phase 1 の形)。旧コードは行の legacy_event_code と event_code。
    両方とも現行の番号へ写す。これで、移行の前後どちらの形の events にも同じ写像が当たる。"""
    alias, current = {}, set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        no = ev.get("no")
        cur = no if isinstance(no, str) and re.fullmatch(r"EV\d+", no) else ev.get("event_code")
        if not cur:
            continue
        current.add(cur)
        for old in (ev.get("legacy_event_code"), ev.get("event_code")):
            if old and old != cur:
                alias.setdefault(old, cur)
    return alias, current


def _normalize_event_refs(out, log=None):
    """共通ロジックと DB 操作・画面項目の参照に残る旧イベントコード(EV00 等)を、events の現行の番号へ写す。
    呼出元: compose(_fill_initial_values の後)。意味合い: 旧コードのまま残ると、存在しない発火イベントとして後段で出る。
    events を正として参照の側を合わせる。写せない(events に無い)コードは残さず削り、log に書く(事実を優先し、置き換えを記録する)。
    events が無いときは何もしない(events を持たない断片の合成で、参照を全部消さないため)。"""
    alias, current = _event_code_map(_rows(out, "events"))
    if not current:
        return

    def fix(code, where):
        if not isinstance(code, str) or not code or code in current:
            return code
        new = alias.get(code)
        if log is not None:
            log.append(f"[正規化] {where}: {code} → {new or '削除(events に無い)'}")
        return new

    def fix_list(lst, where):
        return [x for x in (fix(c, where) for c in lst) if x]

    for cl in _rows(out, "common_logic"):
        for k in ("used_in", "used_by_processes"):
            if isinstance(cl.get(k), list):
                cl[k] = fix_list(cl[k], f"common_logic[{cl.get('name')}].{k}")
    for op in _rows(out, "db_operations"):
        api = op.get("api_endpoint")
        if not isinstance(api, dict):
            continue
        if isinstance(api.get("trigger_events"), list):
            api["trigger_events"] = fix_list(api["trigger_events"], f"db_operations[{op.get('id')}].trigger_events")
        if api.get("trigger_event"):
            new = fix(api["trigger_event"], f"db_operations[{op.get('id')}].trigger_event")
            if new:
                api["trigger_event"] = new
            else:
                api.pop("trigger_event")
    for it in _rows(out, "screen_layout.items"):
        if it.get("event_code"):
            new = fix(it["event_code"], f"screen_layout.items[{it.get('item_name')}].event_code")
            if new:
                it["event_code"] = new
            else:
                it.pop("event_code")


def _fill_initial_db_triggers(out, log=None):
    """初期表示が呼ぶ API の DB 操作に、初期表示のイベントを発火イベントとして足す。発火イベント欄が無い DB 操作には欄を作る。
    呼出元: compose(_normalize_event_refs の後)。意味合い: 初期表示のときに引く DB の参照が発火イベントの欄に現れないと、
    機能別処理の表で紐づかない。初期表示の API は _react_draft_facts で決める(画面の名前や番号は決め打ちしない)。
    下書きが無い・初期表示の行が events に無いときは何もしない。"""
    init_codes, actions = _react_draft_facts()
    alias, current = _event_code_map(_rows(out, "events"))
    targets = sorted({alias.get(c, c) for c in init_codes} & current)
    if not targets or not actions:
        return
    for op in _rows(out, "db_operations"):
        api = op.get("api_endpoint")
        if not isinstance(api, dict) or api.get("action") not in actions:
            continue
        tev = api.get("trigger_events")
        if not isinstance(tev, list):
            tev = api["trigger_events"] = []
        for c in targets:
            if c not in tev:
                tev.append(c)
                if log is not None:
                    log.append(f"[初期表示] db_operations[{op.get('id')}].trigger_events に {c} を足す({api.get('action')})")


def _fix_message_codes(out, log=None):
    """messages_backend の code が式のまま(getattr(e, 'code', None) or 'validation_error' など)のとき、
    下書きの _error_origins(common_errors の code_default)の実際の code へ置き換える。
    呼出元: compose(_fill_initial_db_triggers の後)。意味合い: 単位が式を code に写すと、検査は通っても実際の code と食い違う。
    事実(下書きの code_default)を正とし、置き換えた旨を log に残す。対応が無い code は触らない(推測しない)。"""
    origins = _origin_error_codes()
    if not origins:
        return
    for m in _rows(out, "messages_backend"):
        c = m.get("code")
        if not isinstance(c, str) or re.fullmatch(r"\w+", c) or c not in origins:
            continue
        m["code"] = origins[c]
        if log is not None:
            log.append(f"[正規化] messages_backend[{m.get('message_id')}].code: {c} → {origins[c]}")


def compose(spec, frags, project_config=None, log=None):
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
    draft_msgs, expansions = {}, {}
    if project_config is not None:
        # backend 下書きの auth.fact(認証の掛かり方の事実)と、メッセージの ID・文面を正として使う
        auth_fact, draft_msgs, auth_errs, expansions = _backend_draft_facts()
        if not auth_desc and auth_fact:
            auth_desc = auth_fact
        # 401/403 などの認証の失敗は、ソースが拒否の処理を持つときだけ足す(下書きの auth.error_patterns)。固定の文では足さない(D2)
        for e in auth_errs:
            sc = e.get("status_code")
            if sc and not any(c.get("status_code") == sc for c in common):
                common.append({"status_code": sc, "condition": e.get("text_expanded") or e.get("message") or ""})
        # F2: 入口の except 節で status が数値で決まる共通エラー(500 系)。同じ status が既にあれば足さない
        for ce in _draft_common_errors():
            if not any(c.get("status_code") == ce["status_code"] for c in common):
                common.append(ce)
    # F1: message はソースの文字列のまま。型文の {key} などは、呼出ごとの実引数で展開した実際の文言(下書きの text_expanded)に
    # 1呼出1件で置き換える。画面項目名への言い換えはしない。展開できない穴は残す(verify_intermediate が ERROR にする)
    _expand_messages(out, expansions)
    _align_message_ids(out, draft_msgs)
    if common or auth_desc:
        for api in out.get("api_spec") or []:
            if auth_desc and not api.get("auth"):
                api["auth"] = auth_desc
            errs = api.setdefault("error_patterns", [])
            for ce in common:
                same = [e for e in errs if e.get("status_code") == ce.get("status_code")]
                if not same:
                    errs.append(copy.deepcopy(ce))
                elif ce.get("src"):  # 単位が書いた同じ status の行にも、入口の except 節の出典を足す(A3)
                    for e in same:
                        if not e.get("src"):
                            e["src"] = copy.deepcopy(ce["src"])
        if common and "api_spec" in out:
            msgs = out.setdefault("messages_backend", [])
            have = {m.get("message_id") for m in msgs if isinstance(m, dict)}
            for ce in common:
                mid = ce.get("message_id")
                if mid and mid not in have:
                    msgs.append({**{k: ce[k] for k in ("code", "severity", "message") if k in ce}, "message_id": mid})
                    have.add(mid)
    _fill_src(out)
    _fill_db_op_facts(out)
    _fill_initial_values(out)
    # 参照の正規化は events を正として行う。順序: 旧コードを現行へ写す → 初期表示の発火を足す → code を下書きの実際の値へ寄せる
    _normalize_event_refs(out, log)
    _fill_initial_db_triggers(out, log)
    _fix_message_codes(out, log)
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
    # A3: 汎用パスが .src で終わる欄(出典の配列)は、上書きせず和集合にする(同じ要素は1つ)。複数の単位が同じ行の出典を書いても消し合わない
    if isinstance(dst, list) and isinstance(src, list) and generic.endswith(".src"):
        dst += [copy.deepcopy(x) for x in src if x not in dst]
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


def _selftest_initial_values():
    """_fill_initial_values を一時の下書きで確かめる(リテラル・日付式・式・未設定・結ぶ state が無い項目・既存値の維持)。"""
    import tempfile
    import _config_loader
    si = [
        {"state": "a", "kind": "literal", "initial": "true", "readable": None, "bound": [{"label": "空欄項目も表示"}]},
        {"state": "b", "kind": "expression", "initial": "monthStart()", "readable": "当月1日(JST基準)", "bound": [{"label": "集計開始日"}]},
        {"state": "c", "kind": "expression", "initial": "f(x)", "readable": None, "bound": [{"label": "式項目"}]},
        {"state": "d", "kind": "none", "initial": "", "readable": None, "bound": [{"label": "未設定項目"}]},
        {"state": "e", "kind": "literal", "initial": "\"\"", "readable": None, "bound": [{"label": "空項目"}]},
    ]
    old = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "x_react_draft.json").write_text(json.dumps({"state_initials": si}, ensure_ascii=False), encoding="utf-8")
        _config_loader.INTERMEDIATE_DIR = td
        try:
            out = {"screen_layout": {"items": [{"item_name": n} for n in ("空欄項目も表示", "集計開始日", "式項目", "未設定項目", "空項目", "ボタン")]
                                     + [{"item_name": "集計開始日2", "initial_value": "手書き"}]}}
            _fill_initial_values(out)
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    got = {i["item_name"]: i.get("initial_value") for i in out["screen_layout"]["items"]}
    assert got == {"空欄項目も表示": "true", "集計開始日": "当月1日(JST基準)", "式項目": "式: f(x)", "未設定項目": "未設定(undefined)",
                   "空項目": "空文字", "ボタン": None, "集計開始日2": "手書き"}, got
    print("[selftest OK] _units._fill_initial_values")


def _selftest_compose_facts():
    """参照の正規化(旧イベントコード・初期表示の発火・code の式)を一時の下書きと合成の欄で確かめる。
    移行の前(event_code だけ)と後(no と legacy_event_code を持つ)の両方の形の events に当てる。"""
    import tempfile
    import _config_loader
    react = {"events": [{"event_code": "EV00", "action": "初期表示"}, {"event_code": "EV01", "action": "クリック"}],
             "event_processes": [{"event_code": "EV00", "calls": [{"action": "list_a"}]}],
             "db_operations": [{"api_endpoint": {"action": "list_a", "trigger_events": ["EV00"]}}]}
    backend = {"common_errors": [{"exception": "queries.ValidationError", "errors": [
        {"code": "getattr(e, 'code', None) or 'validation_error'", "code_default": "validation_error", "message": "str(e)"}]}]}
    out = {"events": [{"no": "EV1", "legacy_event_code": "EV00", "action": "初期表示"},
                      {"no": "EV2", "legacy_event_code": "EV01", "action": "クリック"}],
           "common_logic": [{"name": "判定", "used_in": ["EV00", "EV01", "EV09"]}],
           "db_operations": [{"id": "DB1", "api_endpoint": {"action": "list_a", "trigger_events": ["EV2"]}},
                             {"id": "DB2", "api_endpoint": {"action": "list_a"}},
                             {"id": "DB3", "api_endpoint": {"action": "list_b"}}],
           "screen_layout": {"items": [{"item_name": "店舗", "event_code": "EV01"}]},
           "messages_backend": [{"message_id": "MSE1", "code": "getattr(e, 'code', None) or 'validation_error'", "message": "str(e)"}]}
    log = []
    old = _config_loader.INTERMEDIATE_DIR
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "x_react_draft.json").write_text(json.dumps(react, ensure_ascii=False), encoding="utf-8")
        (Path(td) / "x_backend_draft.json").write_text(json.dumps(backend, ensure_ascii=False), encoding="utf-8")
        _config_loader.INTERMEDIATE_DIR = td
        try:
            _normalize_event_refs(out, log)
            _fill_initial_db_triggers(out, log)
            _fix_message_codes(out, log)
        finally:
            _config_loader.INTERMEDIATE_DIR = old
    assert out["common_logic"][0]["used_in"] == ["EV1", "EV2"], out["common_logic"]
    assert out["screen_layout"]["items"][0]["event_code"] == "EV2", out["screen_layout"]
    got = [op["api_endpoint"].get("trigger_events") for op in out["db_operations"]]
    assert got == [["EV2", "EV1"], ["EV1"], None], got
    assert out["messages_backend"][0]["code"] == "validation_error", out["messages_backend"]
    assert any("EV09" in x and "削除" in x for x in log), log
    assert any("EV00 → EV1" in x or "EV00" in x for x in log), log
    print("[selftest OK] _units._normalize_event_refs / _fill_initial_db_triggers / _fix_message_codes")


if __name__ == "__main__":
    _selftest_initial_values()
    _selftest_compose_facts()
