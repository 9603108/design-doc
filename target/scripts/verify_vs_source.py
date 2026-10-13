#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_vs_source.py — 中間 JSON の記述を、ソースの実物と機械で突き合わせる(stdlib のみ)

【目的】
LLM が書いた文言・例示値が、ソース(Python / TSX)の文字列に実在するかを照合し、作り話を止める。
今あるのは B4(メッセージ文言)・D1 後半(S1 の example)・B1(式)・B2(画面項目)・B5(処理の種別。ERROR で終了コード 1)・B3(応答項目の型と null 可。null 可の違いは ERROR)。F の照合は同じファイルへ足す(改良案 0.2)。

【意味合い】
- 戻りは verify_partials と同じ「[種別] 場所: 内容 → 差し戻し先 <単位>」。先頭に [WARN] を付けた行は終了コードに影響しない。
  新しい検査はまず WARN で入れ、2回の実走で誤検出が無ければ ERROR へ上げる(改良案 0.2)
- 文字列の抽出は「ある」ことの確認用なので、取りすぎる方(比較の < > まで JSX の文言として拾う)は許す。
  取りすぎは WARN を減らす向き(見逃し)にしか働かず、作り話の誤検出は増やさない

【接続情報】
  - 呼出元: run_units.AFTER(verify_intermediate の直前) / verify_partials.main_units(S1 の example を collect で照合)
  - 入力: target/intermediate/<機能名>.json(合成後の中間 JSON)と screen_config の source_files、
          中間 JSON の src[].file とその同じフォルダの .py(補助モジュール)
  例: python3 target/scripts/verify_vs_source.py <機能名>
"""
import ast
import json
import re
import sys
import tempfile
from pathlib import Path

# TSX の文字列: 引用符の文字列 / テンプレート(${…} は定数部だけ残す) / JSX の文言(タグの間の字面)
_QUOTED = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')
_TEMPLATE = re.compile(r"`([^`]*)`")
_JSX_TEXT = re.compile(r">([^<>{}]+)<")
_HOLE = re.compile(r"\{[^{}\s]+\}")


def norm(s):
    """空白をまとめ、末尾の「。」と前後の空白を外す。呼出元: collect_source_strings・check_*(両辺を同じ形で比べる)。"""
    return re.sub(r"\s+", " ", s).strip().rstrip("。").strip()


def collect_source_strings(files):
    """files(Path の列)の文字列を正規化して返す(set)。読めない・構文が壊れたファイルは飛ばす。
    Python は ast の Constant(str)と JoinedStr の定数部、TSX/TS は引用符・テンプレートの定数部・JSX の文言。"""
    out = set()
    for f in files:
        try:
            text = Path(f).read_text(encoding="utf-8")
        except OSError:
            continue
        if str(f).endswith(".py"):
            try:
                for n in ast.walk(ast.parse(text)):
                    if isinstance(n, ast.Constant) and isinstance(n.value, str):
                        out.add(norm(n.value))
            except SyntaxError:
                continue
        else:
            for m in _QUOTED.finditer(text):
                out.add(norm(m.group(1) or m.group(2) or ""))
            for m in _TEMPLATE.finditer(text):
                out.update(norm(p) for p in re.split(r"\$\{[^}]*\}", m.group(1)))
            out.update(norm(m.group(1)) for m in _JSX_TEXT.finditer(text))
    out.discard("")
    return out


def source_files(feat):
    """feat の照合に使うソースのパス(重複なし)。screen_config の source_files、中間 JSON の src[].file、
    backend の src の同じフォルダの .py(auth.py など補助モジュール)。呼出元: source_strings。"""
    from _config_loader import INTERMEDIATE_DIR, get_source_files
    files = [p for p in get_source_files(feat)]
    jp = Path(INTERMEDIATE_DIR) / f"{feat}.json"
    if jp.exists():
        def walk(n):
            if isinstance(n, dict):
                for k, v in n.items():
                    if k == "src" and isinstance(v, list):
                        yield from (Path(s["file"]) for s in v if isinstance(s, dict) and s.get("file"))
                    else:
                        yield from walk(v)
            elif isinstance(n, list):
                for v in n:
                    yield from walk(v)
        files += list(walk(json.loads(jp.read_text(encoding="utf-8"))))
    files += [q for p in list(files) if p.suffix == ".py" and p.parent.is_dir() for q in p.parent.glob("*.py")]
    return list(dict.fromkeys(files))


def source_strings(feat):
    """source_files の文字列の集合。呼出元: main・verify_partials.main_units。"""
    return collect_source_strings(source_files(feat))


def _in_source(text, strings):
    """text(正規化済み)がソースのどれかの文字列に含まれるか。{key} などの穴は、穴の間の定数部がどれかの文字列に
    全部含まれるかで見る(穴の中身は実行時に決まるため)。"""
    chunks = [c.strip() for c in _HOLE.split(text) if len(c.strip()) >= 2]
    return not chunks or any(all(c in s for c in chunks) for s in strings)


def check_messages(data, strings):
    """messages[]/messages_backend[] の message と、event_processes[].details の「…」の画面文言が、ソースに実在するか。"""
    problems = []
    for key in ("messages", "messages_backend"):
        for m in data.get(key) or []:
            t = norm(m.get("message") or "") if isinstance(m, dict) else ""
            if t and not _in_source(t, strings):
                problems.append(f"[WARN] [文言] {key}[{m.get('message_id')}]: 「{t}」がソースの文字列に無い → 差し戻し先 H4")
    for ep in data.get("event_processes") or []:
        for line in ep.get("details") or []:
            for q in re.findall(r"「([^」]+)」", line) if isinstance(line, str) else []:
                if "…" not in q and not _in_source(norm(q), strings):
                    problems.append(f"[WARN] [文言] event_processes[{ep.get('event_code') or ep.get('event_ref')}].details: 「{q}」がソースの文字列に無い → 差し戻し先 S3")
    return problems


def check_examples(items, strings):
    """S1 の screen_layout.items[].example が、ソースの文字列に実在するか(D1 後半)。"""
    return [f"[WARN] [例示値] screen_layout.items[{i.get('item_name')}]: example「{i['example']}」がソースの文字列に無い → 差し戻し先 S1"
            for i in items if isinstance(i, dict) and i.get("example") and not _in_source(norm(str(i["example"])), strings)]


def _name_variants(names):
    """画面での名前(label・csv)の字面の揺れ。括弧を全角に、番号の数字を除き、括弧の中身も候補にする(例 小計3(税込) → 小計（税込）・税込)。"""
    out = set()
    for n in names:
        w = n.replace("(", "（").replace(")", "）")
        out |= {n, w, re.sub(r"\d+", "", w)}
        out.update(re.findall(r"（([^）]+)）", w))
    return {v for v in out if len(v) >= 2}


def check_calcs(calcs, candidates):
    """H4 の calculations[] を react 下書きの calc_candidates と突き合わせる(B1。WARN)。
    対: 計算の name が候補の label と同じ。対の無い候補は [式の漏れ]。対の式は、項目の名前・切り捨ての語・数字を見る。"""
    problems, paired = [], set()
    for c in calcs or []:
        cand = next((k for k in candidates or [] if k.get("label") and k["label"] == c.get("name")), None)
        if cand is None:
            continue
        paired.add(cand["id"])
        text = (c.get("formula") or "") + " " + " ".join(i.get("name", "") for i in c.get("inputs") or [])
        where = f"calculations[{c.get('name')}]"
        for o in cand.get("operands") or []:
            vs = _name_variants((cand.get("operand_labels") or {}).get(o) or [])
            if vs and not any(v in text for v in vs):
                problems.append(f"[WARN] [式] {where}: ソースの式 {cand['expr']} の {o}({'・'.join(sorted(vs)[:2])})が式に無い → 差し戻し先 H4")
        words = (c.get("formula") or "") + (c.get("remarks") or "")
        if "Math.floor" in cand.get("funcs", []) and not re.search(r"切り捨|切捨|FLOOR|floor", words):
            problems.append(f"[WARN] [式] {where}: ソースは Math.floor(切り捨て)だが式に切り捨ての語が無い → 差し戻し先 H4")
        for w in ("四捨五入", "切り上げ", "切上"):
            if w in words and not {"Math.round", "Math.ceil"} & set(cand.get("funcs", [])):
                problems.append(f"[WARN] [式] {where}: 「{w}」はソースの式に無い → 差し戻し先 H4")
        for n in re.findall(r"\d+(?:\.\d+)?", re.sub(r"[（(][^）)]*[）)]", "", c.get("formula") or "")):
            if n not in (cand.get("literals") or []):
                problems.append(f"[WARN] [式] {where}: 式の数字 {n} がソースの定数 {cand.get('literals')} に無い → 差し戻し先 H4")
    problems += [f"[WARN] [式の漏れ] calc_candidates[{k['id']}]「{k.get('label')}」: ソースの式 {k['expr']} に対の calculations が無い → 差し戻し先 H4"
                 for k in candidates or [] if k["id"] not in paired]
    return problems


_OPERATION_TYPES = {"ComboBox", "Calendar", "Checkbox", "Input", "Button", "TextArea"}


def check_items(items, jsx):
    """S1 の screen_layout.items[] を react 下書きの jsx_elements と突き合わせる(B2。WARN)。
    (1) ラベルを持つ JSX 要素に同じ字面の item_name が無い [画面項目] 欠け / (2) 入力・操作系の item に対の JSX 要素が無い
    [画面項目] ソースに無い項目 / (3) item_type と item_type_hint が違う [種別]。"""
    by_name = {i.get("item_name"): i for i in items if isinstance(i, dict)}
    by_label = {norm(j["label"]): j for j in jsx or [] if j.get("label")}
    problems = [f"[WARN] [画面項目] screen_layout.items: ソースの {j['tag']}「{lb}」({j['item_type_hint']})に対応する item_name が無い(欠け) → 差し戻し先 S1"
                for lb, j in by_label.items() if lb not in by_name]
    for name, it in by_name.items():
        if it.get("item_type") not in _OPERATION_TYPES:
            continue
        j = by_label.get(norm(name or ""))
        if j is None:
            problems.append(f"[WARN] [画面項目] screen_layout.items[{name}]: ソースに対応する入力・操作系の要素が無い項目 → 差し戻し先 S1")
        elif j["item_type_hint"] != it["item_type"]:
            problems.append(f"[WARN] [種別] screen_layout.items[{name}]: item_type={it['item_type']} だがソースの要素は {j['tag']}(目安 {j['item_type_hint']}) → 差し戻し先 S1")
    return problems


_KIND_OF = {"入力検査": "input_check", "表示切替": "display_toggle", "活性切替": "enable_toggle", "絞り込み": "data_filter"}


def _overlap(a, b):
    """src の配列 a・b のどれかが同じファイルで行範囲が重なるか。"""
    return any(x.get("file") == y.get("file") and x["line_start"] <= y["line_end"] and y["line_start"] <= x["line_end"]
               for x in a or [] for y in b or [])


def check_kinds(validations, front_processes, behaviors):
    """処理の種別の書き間違いを react 下書きの behaviors と突き合わせる(B5。ERROR)。

    (1) validations(H4 の validations_frontend / 最終の validations)の kind が、字面で対になる behavior の kind と違う [種別]。
        kind=入力検査 なのに画面側に input_check が1件も無い [種別]。
    (2) front_processes[].steps[] の kind=validation が、src で対になる behavior(表示切替・活性切替・絞り込み)で、
        同じ処理に input_check が無い [種別](表示切替を入力検査と書く誤り)。
    呼出元: main。behaviors が空(下書きが古い)なら何も言わない。"""
    if not behaviors:
        return []
    problems, has_input = [], any(b["kind"] == "input_check" for b in behaviors)
    for v in validations or []:
        want = _KIND_OF.get(v.get("kind"))
        if want is None:
            continue
        text = " ".join(str(v.get(k) or "") for k in ("field", "rule", "applied_when"))
        got = {b["kind"] for b in behaviors if (len(b.get("target") or "") >= 2 and b["target"] in text) or (b.get("cond") and b["cond"] in text)}
        where = f"validations[{v.get('no') or v.get('field')}]"
        if want == "input_check" and not has_input:
            problems.append(f"[種別] {where}: kind=入力検査 だが画面のソースに入力検査(setError・throw・required 等)が無い → 差し戻し先 H4")
        elif got and want not in got:
            problems.append(f"[種別] {where}: kind={v['kind']} だがソースの書き方は {'・'.join(sorted(got))} → 差し戻し先 H4")
    for fp in front_processes or []:
        pair = [b for b in behaviors if _overlap(fp.get("src"), b["src"] + b.get("control_src", []))]
        if not pair or any(b["kind"] == "input_check" for b in pair):
            continue
        for s in fp.get("steps") or []:
            if s.get("kind") == "validation":
                kinds = "・".join(sorted({b["kind"] for b in pair}))
                problems.append(f"[種別] front_processes[{fp.get('process_id')}].steps[{s.get('step_no')}]: kind=validation(入力検査)だがソースの書き方は {kinds}(入力検査ではない) → 差し戻し先 S3")
    return problems


def _cls(t):
    """型の語(TS の型・H3 の data_type のどちらも)を string / number / boolean / array / object に分類する。分からなければ None。"""
    s = re.sub(r"\s*\|\s*(null|undefined)\b", "", str(t or "")).strip().lower()
    if s.endswith("[]") or s.startswith(("array", "配列")):
        return "array"
    for k, words in (("string", ("string", "date", "文字列", "日付")), ("number", ("number", "int", "numeric", "float", "数値", "整数")),
                     ("boolean", ("boolean", "bool", "真偽")), ("object", ("object", "オブジェクト"))):
        if s.startswith(words):
            return k
    return None


def check_types(api_spec, apis):
    """H3 の response_items の型と null 可を、backend 下書きの apis[action].response_fields(api.ts の型)と突き合わせる(B3)。
    [型]: 型の分類が違う(WARN)。[null可]: nullable が違う(ERROR)。nullable が無い行は data_type の「| null」で見る。
    呼出元: main。response_fields が無い API・下書きに無い key は見ない。"""
    problems = []
    for a in api_spec or []:
        fields = {f["path"]: f for f in ((apis or {}).get(a.get("action")) or {}).get("response_fields") or []}
        for r in a.get("response_items") or []:
            f = fields.get(r.get("key"))
            if f is None:
                continue
            where = f"api_spec[{a.get('action')}].response_items[{r['key']}]"
            if _cls(r.get("data_type")) and _cls(f["ts_base"]) and _cls(r.get("data_type")) != _cls(f["ts_base"]):
                problems.append(f"[WARN] [型] {where}: data_type={r.get('data_type')} だが api.ts の型は {f['ts_type']} → 差し戻し先 H3")
            got = r["nullable"] if "nullable" in r else "null" in str(r.get("data_type")).lower()
            if bool(got) != f["nullable"]:
                problems.append(f"[null可] {where}: nullable={bool(got)} だが api.ts の型は {f['ts_type']}(null 可={f['nullable']}) → 差し戻し先 H3")
    return problems


def selftest():
    # B3: reji_no は string | null。nullable を反転すると [null可] ERROR、型の分類違いは WARN
    apis = {"act": {"response_fields": [{"path": "rows[].reji_no", "ts_type": "string | null", "ts_base": "string", "nullable": True},
                                         {"path": "rows[].days", "ts_type": "number", "ts_base": "number", "nullable": False},
                                         {"path": "dead_cols", "ts_type": "string[]", "ts_base": "string[]", "nullable": False}]}}
    spec = lambda *rs: [{"action": "act", "response_items": [dict(zip(("key", "data_type", "nullable"), r)) for r in rs]}]
    assert check_types(spec(("rows[].reji_no", "string", True), ("rows[].days", "integer", False), ("dead_cols", "string[]", False)), apis) == []
    assert check_types([{"action": "act", "response_items": [{"key": "rows[].reji_no", "data_type": "string | null"}]}], apis) == [], "nullable 無しは data_type の | null で見る"
    got = check_types(spec(("rows[].reji_no", "string", False), ("rows[].days", "string", False), ("x.unknown", "string", True)), apis)
    assert [g.split(" ")[0] for g in got] == ["[null可]", "[WARN]"] and "rows[].reji_no" in got[0], got
    sp = lambda a, b=None: [{"file": "P.tsx", "line_start": a, "line_end": b or a}]
    beh = [{"id": "BH01", "kind": "data_filter", "cond": "showDead || !s.dead", "target": "", "src": sp(124), "control_src": sp(276)},
           {"id": "BH02", "kind": "display_toggle", "cond": "rows.length > 1", "target": "合計", "src": sp(305), "control_src": []}]
    fp = [{"process_id": "FR7", "src": sp(276), "steps": [{"step_no": 1, "kind": "validation"}, {"step_no": 2, "kind": "state_update"}]}]
    got = check_kinds([], fp, beh)
    assert len(got) == 1 and "FR7" in got[0] and not got[0].startswith("[WARN]"), got
    fp[0]["steps"][0]["kind"] = "filter"
    assert check_kinds([], fp, beh) == []
    vs = [{"no": "VL1", "field": "合計カード", "rule": "合計の表示", "kind": "入力検査"}, {"no": "VL2", "field": "合計", "kind": "表示切替"}]
    got = check_kinds(vs, [], beh)
    assert len(got) == 1 and "VL1" in got[0] and "入力検査" in got[0], got
    assert check_kinds([{"no": "VL3", "field": "合計", "kind": "絞り込み"}], [], beh) and not check_kinds(vs, [], [])
    jsx = [{"tag": "select", "label": "店舗", "item_type_hint": "ComboBox"}, {"tag": "input", "label": "開始", "item_type_hint": "Calendar"}]
    assert check_items([{"item_name": "店舗", "item_type": "ComboBox"}, {"item_name": "開始", "item_type": "Calendar"}], jsx) == []
    got = check_items([{"item_name": "店舗", "item_type": "ComboBox"}, {"item_name": "開始", "item_type": "Input"}, {"item_name": "幻", "item_type": "Button"}], jsx)
    assert [g.split(" ")[1] for g in got] == ["[種別]", "[画面項目]"], got
    assert [g for g in check_items([{"item_name": "店舗", "item_type": "ComboBox"}], jsx) if "欠け" in g]
    cands = [{"id": "CC01", "label": "差", "expr": "r.s3 - r.n", "operands": ["s3", "n"], "funcs": [], "literals": [],
              "operand_labels": {"s3": ["小計", "小計3(税込)"], "n": ["点数"]}},
             {"id": "CC02", "label": "平均", "expr": "r.n ? Math.floor(r.s3 / r.n) : 0", "operands": ["n", "s3"],
              "funcs": ["Math.floor"], "literals": ["0"], "operand_labels": {"s3": ["小計3(税込)"], "n": ["点数"]}}]
    ok = [{"name": "差", "formula": "差 = 小計（税込） − 点数", "inputs": []},
          {"name": "平均", "formula": "平均 = FLOOR(小計（税込） ÷ 点数)", "inputs": []}]
    assert check_calcs(ok, cands) == [], check_calcs(ok, cands)
    # 四捨五入に変える・点数を落とす・対を消すと WARN
    bad = [{"name": "差", "formula": "差 = 小計（税込）", "inputs": []},
           {"name": "平均", "formula": "平均 = 四捨五入(小計（税込） ÷ 点数)", "inputs": []}]
    got = check_calcs(bad, cands)
    assert any("点数" in g and "[式]" in g for g in got) and any("四捨五入" in g for g in got) and any("切り捨て" in g for g in got), got
    assert [g for g in check_calcs(ok[:1], cands) if "[式の漏れ]" in g] and not check_calcs(ok[:1], cands[:1])
    with tempfile.TemporaryDirectory() as d:
        py, ts = Path(d, "q.py"), Path(d, "P.tsx")
        py.write_text('def f(k):\n    raise ValueError("from は YYYY-MM-DD 形式で指定してください")\n    raise ValueError(f"{k} は正の整数で指定してください")\n', encoding="utf-8")
        ts.write_text('const a = <option value="">- 全店 -</option>;\nconst b = "読込中…";\n', encoding="utf-8")
        s = collect_source_strings([py, ts])
        assert "from は YYYY-MM-DD 形式で指定してください" in s and "- 全店 -" in s and "は正の整数で指定してください" in s, s
        ok = {"messages": [{"message_id": "M1", "message": "from は YYYY-MM-DD 形式で指定してください"},
                           {"message_id": "M2", "message": "{key} は正の整数で指定してください。"}]}
        assert check_messages(ok, s) == [], check_messages(ok, s)
        # 受け入れ条件: 「from」を「集計開始日」に書き換えた文言は WARN
        bad = {"messages": [{"message_id": "M1", "message": "集計開始日 は YYYY-MM-DD 形式で指定してください"}]}
        assert len(check_messages(bad, s)) == 1 and check_messages(bad, s)[0].startswith("[WARN] [文言]")
        det = {"event_processes": [{"event_code": "EV1", "details": ["画面に「- 全店 -」と出す", "「作り話」と出す"]}]}
        assert len(check_messages(det, s)) == 1
        assert check_examples([{"item_name": "店舗", "example": "- 全店 -"}, {"item_name": "日付", "example": "2026-10-01"}], s) == [
            "[WARN] [例示値] screen_layout.items[日付]: example「2026-10-01」がソースの文字列に無い → 差し戻し先 S1"]
    print("selftest OK")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()
    if not argv:
        print("使い方: verify_vs_source.py <機能名> | --selftest", file=sys.stderr)
        return 2
    from _config_loader import INTERMEDIATE_DIR
    feat = argv[0]
    jp = Path(INTERMEDIATE_DIR) / f"{feat}.json"
    try:
        data = json.loads(jp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[入力不正] {jp}: {e}", file=sys.stderr)
        return 2
    strings = source_strings(feat)
    if not strings:
        print("[WARN] [文言] ソースの文字列が1つも取れない(source_files と src の file を確認)")
        return 0
    problems = check_messages(data, strings) + check_examples((data.get("screen_layout") or {}).get("items") or [], strings)
    cands, jsx, beh = [], [], []
    type_problems = []
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_backend_draft.json")):
        type_problems = check_types(data.get("api_spec"), json.loads(p.read_text(encoding="utf-8")).get("apis"))
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_react_draft.json")):
        rd = json.loads(p.read_text(encoding="utf-8"))
        cands, jsx, beh = rd.get("calc_candidates") or [], rd.get("jsx_elements") or [], rd.get("behaviors") or []
    items = (data.get("screen_layout") or {}).get("items") or []
    item_problems = check_items(items, jsx) if jsx else []
    problems += item_problems
    ops = [i for i in items if i.get("item_type") in _OPERATION_TYPES]
    print(f"[INFO] 画面項目の照合: ソースの入力・操作系 {len(jsx)} 件・S1 の入力・操作系 {len(ops)} 件・WARN {len(item_problems)} 件")
    calc_problems = check_calcs(data.get("calculations") or [], cands) if cands else []
    problems += calc_problems
    kind_problems = check_kinds(data.get("validations") or data.get("validations_frontend"), data.get("front_processes"), beh)
    for p in problems + kind_problems + type_problems:
        print(p)
    null_problems = [p for p in type_problems if not p.startswith("[WARN]")]
    print(f"[INFO] 応答項目の型の照合: ERROR {len(null_problems)} 件・WARN {len(type_problems) - len(null_problems)} 件")
    n = len(data.get("calculations") or [])
    print(f"[INFO] 計算式の照合: ソースの式 {len(cands)} 件・calculations {n} 件・WARN {len(calc_problems)} 件")
    print(f"[INFO] 処理の種別の照合: ソースの behaviors {len(beh)} 件・ERROR {len(kind_problems)} 件")
    print(f"[INFO] 文言の照合: ソースの文字列 {len(strings)} 件に対し WARN {len(problems)} 件")
    return 1 if kind_problems or null_problems else 0


if __name__ == "__main__":
    sys.exit(main())
