#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_vs_source.py — 中間 JSON の記述を、ソースの実物と機械で突き合わせる(stdlib のみ)

【目的】
LLM が書いた文言・例示値が、ソース(Python / TSX)の文字列に実在するかを照合し、作り話を止める。
今あるのは B4(メッセージ文言)・D1 後半(S1 の example)・B1(式)・B2(画面項目)・B5(処理の種別。ERROR で終了コード 1)・B3(応答項目の型と null 可。null 可の違いは ERROR)・F2(境界値と DB 例外。WARN)・F3(初期取得の順。WARN)・B1 の事実(実列と別名・CTE・副問合せ・集計の単位・例外の code。ERROR、集計の単位だけ WARN)。F の照合は同じファイルへ足す(改良案 0.2)。

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
# WHY: JSX の文の途中の式({shops.length} など)は実行時の値なので、文の中の {…} は取った文言の中に残す(穴として照合する)
_JSX_TEXT = re.compile(r">((?:[^<>{}]|\{[^{}]*\})+)<")
# WHY: 強調・コードなどのインライン要素は文の途中に入るが、文は1つなので、照合用にはタグを外す
_INLINE_TAG = re.compile(r"</?(?:strong|em|b|i|code|span|small|a|kbd)\b[^>]*>")
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
                    elif isinstance(n, ast.JoinedStr):
                        # f-string は穴を {_} にした型文も入れる(呼出の実引数で展開した文言が、この型文に当てはまるかを _in_source が見る)
                        out.add(norm("".join(v.value if isinstance(v, ast.Constant) else "{_}" for v in n.values)))
            except SyntaxError:
                continue
        else:
            for m in _QUOTED.finditer(text):
                out.add(norm(m.group(1) or m.group(2) or ""))
            for m in _TEMPLATE.finditer(text):
                out.update(norm(p) for p in re.split(r"\$\{[^}]*\}", m.group(1)))
            out.update(norm(m.group(1)) for m in _JSX_TEXT.finditer(text))
            # WHY: <strong>や<code>の境界で文が切れている(「<strong>取込欠損の疑い</strong>：…」)ので、インライン要素のタグを外した版からも文を取る
            out.update(norm(m.group(1)) for m in _JSX_TEXT.finditer(_INLINE_TAG.sub("", text)))
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
    if not chunks or any(all(c in s for c in chunks) for s in strings):
        return True
    # 展開済みの文言(例 shop_id は正の整数で指定してください)は、穴({_})を持つソースの f-string の型文に当てはまれば実在とみなす
    return any("{_}" in s and len("".join(s.split("{_}")).strip()) >= 2 and re.search("(?:.+?)".join(re.escape(p) for p in s.split("{_}")), text) for s in strings)


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


_EX_NUMBER = re.compile(r"[0-9０-９][0-9０-９,.，．]*")


def _example_in_source(text, strings):
    """S1 の例示値が、ソースの文字列に実在するか。数字の並び(金額・件数・日付)は実行時の値なので穴 {_} にしてから _in_source で見る。
    単位・記号・固有の語は字面のまま照合する。
    WHY: 例示値は実データの一例で、書式に合う数字まで字面で照合すると、正しい書式の例示が WARN になる(POS 会計レポートの S1 で発生)。
    呼出元: check_examples(verify_partials.check_examples も同じ関数を呼ぶので、両検査の基準は揃う)。"""
    # WHY: 「3 点 / 4,500 円」のように区切った例示は、区切りごとに別々に照合する。画面では点と円が別のセルに出るので、ソースも別々の文字列を持つ
    return all(_in_source(_EX_NUMBER.sub("{_}", part), strings) for part in norm(text).split(" / "))


def check_examples(items, strings):
    """S1 の screen_layout.items[].example が、ソースの文字列に実在するか(D1 後半)。"""
    return [f"[WARN] [例示値] screen_layout.items[{i.get('item_name')}]: example「{i['example']}」がソースの文字列に無い → 差し戻し先 S1"
            for i in items if isinstance(i, dict) and i.get("example") and not _example_in_source(norm(str(i["example"])), strings)]


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
        # WHY: 入力の名前(例 合計2(支払科目計))の数字は列の名前で、式の定数ではない。名前を外してから数字を見る
        body = c.get("formula") or ""
        for i in c.get("inputs") or []:
            if i.get("name"):
                body = body.replace(i["name"], "")
        for n in re.findall(r"\d+(?:\.\d+)?", re.sub(r"[（(][^）)]*[）)]", "", body)):
            if n not in (cand.get("literals") or []):
                problems.append(f"[WARN] [式] {where}: 式の数字 {n} がソースの定数 {cand.get('literals')} に無い → 差し戻し先 H4")
    problems += [f"[WARN] [式の漏れ] calc_candidates[{k['id']}]「{k.get('label')}」: ソースの式 {k['expr']} に対の calculations が無い → 差し戻し先 H4"
                 for k in candidates or [] if k["id"] not in paired]
    return problems


_OPERATION_TYPES = {"ComboBox", "Calendar", "Checkbox", "Input", "Button", "TextArea"}
# jsx_elements のうち表示専用の要素(_react_facts._DISPLAY_HINT の tag と同じ)。check_items の (1)〜(3) の対象外
_DISPLAY_TAGS = {"span", "strong", "h1", "h2", "a", "table"}


def check_items(items, jsx, display_rows=None):
    """S1 の screen_layout.items[] を react 下書きの jsx_elements と突き合わせる(B2。WARN)。
    (1) ラベルを持つ JSX 要素に同じ字面の item_name が無い [画面項目] 欠け / (2) 入力・操作系の item に対の JSX 要素が無い
    [画面項目] ソースに無い項目 / (3) item_type と item_type_hint が違う [種別]。
    (4) display_rows(定数配列の行名)が item_name に全部ある(無ければ ERROR。WARN の接頭辞を付けない)。
    表示専用の要素(Label・Grid・Link。label は空)は (1)〜(3) の対象外。jsx が None なら (4) だけ見る。
    呼出元: main(最終)・verify_partials.check_unit_extras(S1 の単位)。"""
    by_name = {i.get("item_name"): i for i in items if isinstance(i, dict)}
    lack = [lb for r in display_rows or [] for lb in r.get("labels") or [] if lb not in by_name]
    disp = ([f"[画面項目] screen_layout.items: react 下書きの display_rows の行名 {', '.join(lack)} が無い → 差し戻し先 S1"]
            if lack else [])
    if jsx is None:
        return disp
    by_label = {norm(j["label"]): j for j in jsx or [] if j.get("label")}
    problems = disp + [f"[WARN] [画面項目] screen_layout.items: ソースの {j['tag']}「{lb}」({j['item_type_hint']})に対応する item_name が無い(欠け) → 差し戻し先 S1"
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
        # WHY: 同じ行範囲で対になる behavior(src が重なる)を先に使う。字面(cond の一部の語)で拾うと、同じ語を含む別の表示用の行を誤って対にする(POS の VL5 は「s.dead」で表示用の行に当たった)
        by_src = {b["kind"] for b in behaviors if _overlap(v.get("src"), b["src"] + b.get("control_src", []))}
        got = by_src or {b["kind"] for b in behaviors if (len(b.get("target") or "") >= 2 and b["target"] in text) or (b.get("cond") and b["cond"] in text)}
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
    # SQL の型名(bigint・varchar など。db_type)も同じ分類に入れる
    for k, words in (("string", ("string", "date", "文字列", "日付", "varchar", "text", "char", "timestamp", "time", "uuid")),
                     ("number", ("number", "int", "bigint", "smallint", "numeric", "decimal", "float", "double", "real", "数値", "整数")),
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
            # index signature の列は api.ts の型が共用体で当てにならないので、SQL の型(db_type)と null 可(db_nullable。不明なら見ない)で見る
            sql = f.get("index_signature") and f.get("db_type")
            want, exp = (f["db_type"], f.get("db_nullable")) if sql else (f["ts_base"], f["nullable"])
            if _cls(r.get("data_type")) and _cls(want) and _cls(r.get("data_type")) != _cls(want):
                problems.append(f"[WARN] [型] {where}: data_type={r.get('data_type')} だが {'SQL' if sql else 'api.ts'} の型は {want if sql else f['ts_type']} → 差し戻し先 H3")
            got = r["nullable"] if "nullable" in r else "null" in str(r.get("data_type")).lower()
            if exp is not None and bool(got) != exp:
                problems.append(f"[null可] {where}: nullable={bool(got)} だが {'SQL' if sql else 'api.ts'} の型は {want if sql else f['ts_type']}(null 可={exp}) → 差し戻し先 H3")
    return problems


def check_fetch_plan(plan, front_processes):
    """初期表示(front_processes の名前に「初期」を含む処理)の steps が、react 下書き events[0].fetch_plan.mode と同じ取得順を書いているかを見る(F3)。
    parallel なら「並行|同時」、serial なら「待って」の語が要る。逆の語だけがあるのも食い違い。呼出元: main。
    返す行は [WARN] [取得順]。差し戻し先は付けない: この語は migrate_v17_to_v18.py(_react_facts.add_fetch_order_step)が
    初期処理の steps に機械で入れるので、無ければモデルの書き直しでなくスクリプトの不具合(units 表に無い S0 へ戻さない)。
    plan が無い・single は見ない。"""
    mode = (plan or {}).get("mode")
    if mode not in ("parallel", "serial"):
        return []
    out = []
    for p in front_processes or []:
        if "初期" not in str(p.get("name")):
            continue
        t = " ".join(str(s.get("description")) for s in p.get("steps") or [])
        par, wait = bool(re.search(r"並行|同時", t)), "待って" in t
        if (mode == "parallel" and not par) or (mode == "serial" and (par or not wait)):
            want = "「並行」か「同時」" if mode == "parallel" else "「…の応答を待って…を取得する」"
            out.append(f"[WARN] [取得順] {p.get('process_id')}.steps: 下書きの fetch_plan.mode={mode}({plan.get('basis', '')})だが、steps に{want}の書き方が無い → スクリプトの不具合(migrate_v17_to_v18.py が初期処理の steps に入れるはずの行。差し戻しでなく報告する)")
    return out


def _boundary_words(b):
    """boundaries の1件から、H4 の rule に要る語の候補(どれか1つ)を返す。候補が決められない検査(集合に無い値など)は None。"""
    if b.get("present") is False:
        return {"order": ("大小", "前後", "逆転"), "span_limit": ("上限", "期間の長さ")}.get(b.get("check"))
    if b.get("impossible_dates_accepted"):
        return ("暦", "実在", "存在しない")
    if re.search(r"<=\s*0\b|<\s*1\b", " ".join(b.get("compare") or [])):
        return ("正の整数", "0以下", "0 以下", "1以上", "1 以上")
    if "\\d{4}-\\d{2}-\\d{2}" in str(b.get("pattern")):
        return ("YYYY-MM-DD", "形式", "書式")
    return None


def check_boundaries(validations, api_spec, apis, common_errors):
    """境界値と DB 例外を、backend 下書きの boundaries・common_errors と突き合わせる(F2。WARN)。
    [境界値]: 下書きの境界値(正の整数・日付の書式・暦に無い日付を通すこと・大小や期間上限の検査が無いこと)の語が、
              H4 の validations の rule・field・applied_when のどこにも無い(差し戻し先 H4)。
    [DB例外]: common_errors で status が取れた共通エラー(数値のもの)が api_spec の error_patterns に無い、または
              api_spec の 5xx がソースの common_errors・その API の error_patterns に無い(差し戻し先 S2)。
    呼出元: main。下書きの boundaries が空なら境界値は見ない。status が式のもの(getattr(…) or 400)・via の中で決まるものは見ない。"""
    problems = []
    text = " ".join(str(v.get(k) or "") for v in validations or [] if isinstance(v, dict) for k in ("field", "rule", "applied_when"))
    for act, a in (apis or {}).items():
        for b in a.get("boundaries") or []:
            words = _boundary_words(b)
            if words and not any(w in text for w in words):
                what = b.get("fact") or f"{b.get('param')} の検査 {b.get('check')}"
                problems.append(f"[WARN] [境界値] validations: 下書きの boundaries({act})「{what}」の語({'・'.join(words[:3])})が rule に無い → 差し戻し先 H4")
    shared = {}
    for c in common_errors or []:
        for e in c.get("errors") or []:
            if isinstance(e.get("status_code"), int):
                shared.setdefault(e["status_code"], []).append(c.get("exception"))
    for a in api_spec or []:
        got = {str(e.get("status_code")) for e in a.get("error_patterns") or [] if isinstance(e, dict)}
        own = {str(e.get("status_code")) for e in ((apis or {}).get(a.get("action")) or {}).get("error_patterns") or []}
        where = f"api_spec[{a.get('action')}].error_patterns"
        problems += [f"[WARN] [DB例外] {where}: ソースの共通エラー {sc}({'・'.join(map(str, ex))})が無い → 差し戻し先 S2"
                     for sc, ex in shared.items() if str(sc) not in got]
        problems += [f"[WARN] [DB例外] {where}: status {s} はソースの common_errors にも下書きの error_patterns にも無い → 差し戻し先 S2"
                     for s in sorted(got) if s.startswith("5") and int(s) not in shared and s not in own]
    return problems


# WHY: SQL の関数の形(name( ))。日本語の説明の式と、SQL の式を区別するために使う
_SQL_CALL = re.compile(r"\b[A-Za-z_]+\s*\(")


def _sq(s):
    """SQL の式の字面を比べる形にする(小文字・::型・表の接頭辞・空白を除く)。呼出元: check_backend_facts。"""
    s = re.sub(r"::\w+", "", str(s or "").lower())
    return re.sub(r"\s+", "", re.sub(r"\b[a-z_]\w*\.(?=[a-z_*])", "", s))


def _sq_head(s):
    """_sq の形から FILTER (…) 以降を落とす。FILTER の条件は定数名で書かれる(_WARN_COND 等)ことがあり、字面で比べられないため。"""
    return re.sub(r"filter\(.*", "", _sq(s))


def check_backend_facts(data, apis, common_errors):
    """設計書(data)を backend 下書きの事実(apis[action].db_operations の columns・ctes・subqueries・aggregate_units と common_errors)と突き合わせる(ERROR)。
    [別名]: 物理列として書いた名前が SQL の別名(実列に無い) / 集計の別名が null なのにソースは AS を持つ。差し戻し先 H2
    [式]: api_spec の response_items[].expression が実 SQL の式と食い違う(SQL の関数の形を持つ式だけ比べる。日本語の説明は言い換えを許すので見ない)。差し戻し先 S2
    [CTE]: ソースに CTE が無いのに CTE と書く / 定義が空の CTE を参照 / 絞り込みのある CTE を空・条件なしと書く / 本体の集計と書いた別名が CTE の集計。差し戻し先 S2・H2
    [副問合せ]: 別名の無い集計が副問合せの中の集計なのに本体の集計(aggregate)として書いてある。差し戻し先 H2
    [集計単位]: count の説明の「日数」が、数える単位(GROUP BY)に日付の列が無い・日付以外の列も含むのと合わない(WARN)。差し戻し先 S2
    [エラーcode]: api_spec の error_patterns の condition に書いた code=<値> が、ソースの例外処理の code_default のどれでもない(差し戻し先 S2)。messages の code は見ない(MSE 番号が正。形は verify_intermediate.py)
    呼出元: main(下書きがあるとき)。apis が空なら何も言わない。差し戻し先は units.json の owns に合わせた(dataset・aggregate は H2、expression・説明・error_patterns は S2)。"""
    ops = {act: a.get("db_operations") or [] for act, a in (apis or {}).items()}
    problems, alias_only, real = [], {}, set()
    for olist in ops.values():
        for op in olist:
            for c in op.get("columns") or []:
                if c.get("kind") == "alias_only" and c.get("alias_name"):
                    alias_only.setdefault(c["alias_name"], c.get("alias_expression") or c.get("expression"))
                real.update(s.rsplit(".", 1)[-1] for s in [c.get("real_column")] + list(c.get("source_columns") or []) if s)
    # (1) 物理列として書かれた名前が、実列に無く別名だけにある
    rows = [("logical_names", r) for r in data.get("logical_names") or []] + \
           [(f"db_operations[{op.get('id')}].dataset", r) for op in data.get("db_operations") or [] for r in op.get("dataset") or []]
    seen = set()
    for where, r in rows:
        n = r.get("physical_column") if isinstance(r, dict) else None
        if n in alias_only and n not in real and (where, n) not in seen:
            seen.add((where, n))
            problems.append(f"[別名] {where}: physical_column「{n}」は SQL の別名(式 {alias_only[n]})で実列ではない → 差し戻し先 H2")
    texts = {}  # action → [(場所, 文, 差し戻し先)]。CTE・集計単位の記述を見る
    for op in data.get("db_operations") or []:
        act = (op.get("api_endpoint") or {}).get("action")
        texts.setdefault(act, []).extend((f"db_operations[{op.get('id')}].{k}", op.get(k), "S2") for k in ("remarks", "dataset_name") if op.get(k))
    for a in data.get("api_spec") or []:
        t = texts.setdefault(a.get("action"), [])
        t.append((f"api_spec[{a.get('action')}].description", a.get("description"), "S2"))
        t.extend((f"api_spec[{a.get('action')}].response_items[{r.get('key')}].{k}", r.get(k), "S2") for r in a.get("response_items") or [] for k in ("description", "transform_backend") if r.get(k))
    # (2) 集計: 別名 null・副問合せの集計・CTE の集計
    for op in data.get("db_operations") or []:
        dops = ops.get((op.get("api_endpoint") or {}).get("action")) or []
        units = [u for d in dops for u in d.get("aggregate_units") or []]
        subs = [_sq(s.get("definition")) for d in dops for s in d.get("subqueries") or []]
        doc_alias = {a.get("alias") for a in op.get("aggregate") or [] if a.get("alias")}
        for a in op.get("aggregate") or []:
            fn, where = str(a.get("function")).lower(), f"db_operations[{op.get('id')}].aggregate[{a.get('alias') or a.get('function')}]"
            if not a.get("alias"):
                if any(_sq(f"{fn}({a.get('target')})") in s for s in subs):
                    problems.append(f"[副問合せ] {where}: {fn}({a.get('target')}) は副問合せの中の集計で、本体の集計(aggregate)ではない → 差し戻し先 H2")
                    continue
                miss = [u for u in units if str(u.get("func")).lower() == fn and u.get("alias") and u["alias"] not in doc_alias]
                exact = [u for u in miss if _sq(u.get("arg")) == _sq(a.get("target"))]
                if exact or len(miss) == 1:
                    problems.append(f"[別名] {where}: alias が null だがソースは {(exact or miss)[0]['alias']} という別名を付けている → 差し戻し先 H2")
            elif (a.get("scope") or "main") == "main":
                same = [u for u in units if u.get("alias") == a["alias"] and str(u.get("func")).lower() == fn]
                if same and all(u.get("scope") != "main" for u in same):
                    problems.append(f"[CTE] {where}: 別名 {a['alias']} の集計は {same[0].get('scope')} の中にあり、本体の集計ではない → 差し戻し先 H2")
    # (2) 式: response_items[].expression が実 SQL の式と同じか(FILTER の中の条件は定数名のことがあるので見ない)
    for a in data.get("api_spec") or []:
        cols = {c.get("alias"): c for d in ops.get(a.get("action")) or [] for c in d.get("columns") or [] if c.get("is_expression")}
        for r in a.get("response_items") or []:
            c = cols.get(str(r.get("key")).rsplit(".", 1)[-1])
            # WHY: 日本語の説明(例 期間内の総売上を合計して整数にする)は言い換えを許すので字面では比べない。SQL の関数の形を持つ式だけ比べる
            if c and r.get("expression") and _SQL_CALL.search(str(r["expression"])):
                x, y = _sq_head(r["expression"]), _sq_head(c["expression"])
                fx, fy = set(re.findall(r"[a-z_]+(?=\()", x)), set(re.findall(r"[a-z_]+(?=\()", y))
                # 関数の名前が全部実 SQL にあれば、引数の細部(CASE の展開・外側の array_remove 等)は字面で比べない
                if x != y and x not in y and y not in x and not (fx and fx <= fy):
                    problems.append(f"[式] api_spec[{a.get('action')}].response_items[{r['key']}].expression: 「{r['expression']}」が実 SQL の式「{c['expression']}」と違う → 差し戻し先 S2")
    # (2) CTE の記述
    for act, dops in ops.items():
        ctes = [c for d in dops for c in d.get("ctes") or []]
        for where, t, unit in texts.get(act, []):
            t = str(t)
            if not ctes and re.search(r"CTE|共通表式", t):
                problems.append(f"[CTE] {where}: CTE と書いてあるがソースの SQL に WITH(CTE)が無い → 差し戻し先 {unit}")
            for c in ctes:
                m = re.search(rf"(?:CTE|共通表式)[「『(（\s]*{re.escape(c['name'])}\b|\b{re.escape(c['name'])}\b[」』)）\s]*(?:という|の)?\s*(?:CTE|共通表式)", t)
                if not m:
                    continue
                sent = next((s for s in t.split("。") if m.group(0) in s), t)
                if not str(c.get("definition")).strip():
                    problems.append(f"[CTE] {where}: CTE {c['name']} はソースで定義が空なのに参照している → 差し戻し先 {unit}")
                elif c.get("filtered") and re.search(r"空|絞り込み(?:は)?(?:無|な)し|条件(?:は)?(?:無|な)し|全件", sent):
                    problems.append(f"[CTE] {where}: CTE {c['name']} はソースで WHERE により絞り込むのに「{sent}」と書いている → 差し戻し先 {unit}")
    # (3) count の説明と数える単位(同じ別名の単位が複数の DB 操作にあるときは1件にまとめる)
    unit_seen = set()
    for a in data.get("api_spec") or []:
        for r in a.get("response_items") or []:
            leaf = str(r.get("key")).rsplit(".", 1)[-1]
            for d in ops.get(a.get("action")) or []:
                for u in d.get("aggregate_units") or []:
                    text = " ".join(str(r.get(k) or "") for k in ("display_name", "description", "transform_backend"))
                    if u.get("alias") == leaf and str(u.get("func")).lower() == "count" and u.get("scope") == "main" and re.search(r"日数|日の数", text) and (a.get("action"), r["key"]) not in unit_seen:
                        # 行数が日数になるのは、数える単位が日付の列だけのとき。日付が無い・日付以外(店舗・レジなど)も含むときは WARN(両方向を捕まえる)
                        gb = u.get("group_by") or []
                        dates = [g for g in gb if re.search(r"date|_dt|day|日", g, re.IGNORECASE)]
                        others = [g for g in gb if g not in dates]
                        if dates and not others:
                            continue
                        unit_seen.add((a.get("action"), r["key"]))
                        why = "日付の列が無い" if not dates else f"日付の列以外({'・'.join(others)})も数える単位に含まれ、行数が日数と合わない"
                        problems.append(f"[WARN] [集計単位] api_spec[{a.get('action')}].response_items[{r['key']}]: 「日数」と書くが count の単位は {u.get('unit')} で{why} → 差し戻し先 S2")
    # (4) エラー code: API のエラー code は api_spec の error_patterns の condition に code=<値> と書く(スキーマに専用の欄は無い。最も近い既存の欄)。
    # WHY: messages・messages_backend の code は message_id と同じ MSE 番号で、API の code ではない(形は verify_intermediate.py が見る)。
    #      ここでは condition の code= だけを common_errors の code_default と照合する
    known = {e.get("code_default") for c in common_errors or [] for e in c.get("errors") or [] if e.get("code_default")}
    for a in data.get("api_spec") or []:
        for ep in a.get("error_patterns") or []:
            codes = re.findall(r"code\s*[=＝]\s*([A-Za-z_]\w*)", str(ep.get("condition")))
            problems += [f"[エラーcode] api_spec[{a.get('action')}].error_patterns: code「{c}」はソースの例外処理の code({'・'.join(sorted(known))})に無い → 差し戻し先 S2"
                         for c in codes if known and c not in known]
    return problems


def selftest():
    # 事実(別名・集計・CTE・副問合せ・例外の code)の照合
    col = lambda al, kind, real_c="", src=(), expr="x": {"alias": al, "alias_name": al, "kind": kind, "real_column": real_c, "source_columns": list(src),
                                                         "expression": expr, "is_expression": kind == "alias_only"}
    dop = {"id": "db_op_a", "columns": [col("shop_id", "column", "t.mise_no", ["t.mise_no"]), col("days", "alias_only", expr="count(*)"),
                                         col("first_date", "alias_only", "", ["t.kai_date"], "min(t.kai_date)"), col("amount", "alias_only", "", ["t.amount"], "SUM(t.amount)")],
           "ctes": [{"name": "d", "definition": "SELECT * FROM t WHERE x = 1", "from": "t", "filtered": True}, {"name": "e", "definition": "", "from": "", "filtered": False}],
           "subqueries": [{"location": "WHERE", "definition": "SELECT max(kai_date) FROM t", "filtered": False}],
           "aggregate_units": [{"func": "count", "arg": "*", "alias": "days", "scope": "main", "group_by": ["t.mise_no"], "unit": "t.mise_no"},
                               {"func": "min", "arg": "t.kai_date", "alias": "first_date", "scope": "main", "group_by": ["t.mise_no"], "unit": "t.mise_no"},
                               {"func": "sum", "arg": "t.n", "alias": "tot", "scope": "cte d", "group_by": ["t.kai_date"], "unit": "t.kai_date"}]}
    bapis = {"act": {"db_operations": [dop], "error_patterns": [{"id": "MSE00001", "status_code": 400}]}}
    ces = [{"exception": "VE", "errors": [{"code_default": "validation_error", "status_code": "getattr(e, 'status', 400) or 400"}]},
           {"exception": "Exception", "errors": [{"code_default": "internal_error", "status_code": 500}]}]
    good = {"logical_names": [{"physical_column": "mise_no"}, {"physical_column": "amount"}],
            "db_operations": [{"id": "DB1", "api_endpoint": {"action": "act"}, "dataset": [{"no": 1, "physical_column": "kai_date"}],
                               "aggregate": [{"function": "min", "target": "t.kai_date", "alias": "first_date", "scope": "main"}], "remarks": "期間で絞る"}],
            "api_spec": [{"action": "act", "description": "d", "response_items": [{"key": "rows[].first_date", "expression": "min(kai_date)", "description": "最初"},
                                                                                   {"key": "rows[].days", "expression": "count(*)", "description": "店舗の行数"}],
                          "error_patterns": [{"condition": "不正 (code=validation_error)"}]}],
            "messages_backend": [{"message_id": "MSE00001", "code": "validation_error"}]}
    assert check_backend_facts(good, bapis, ces) == [], check_backend_facts(good, bapis, ces)
    assert check_backend_facts({}, None, None) == []
    # 別名を物理列にした(days は式の別名で実列に無い。amount は実列でもあるので許す)→ ERROR
    bad = json.loads(json.dumps(good))
    bad["logical_names"].append({"physical_column": "days"})
    bad["db_operations"][0]["dataset"].append({"no": 2, "physical_column": "first_date"})
    got = check_backend_facts(bad, bapis, ces)
    assert [g.split(" ")[0] for g in got] == ["[別名]"] * 2 and "days" in got[0] and "first_date" in got[1] and all("差し戻し先 H2" in g for g in got), got
    # 集計の別名が null / 副問合せの集計を本体として書いた / CTE の集計を本体として書いた
    bad = json.loads(json.dumps(good))
    bad["db_operations"][0]["aggregate"] = [{"function": "min", "target": "kai_date", "alias": None, "scope": "main"}, {"function": "max", "target": "kai_date", "alias": None, "scope": "main"},
                                            {"function": "sum", "target": "t.n", "alias": "tot", "scope": "main"}]
    got = check_backend_facts(bad, bapis, ces)
    assert [g.split(" ")[0] for g in got] == ["[別名]", "[副問合せ]", "[CTE]"] and "first_date" in got[0], got
    # 式の食い違い(FILTER の中の定数名は見ない)
    bad = json.loads(json.dumps(good))
    bad["api_spec"][0]["response_items"][0]["expression"] = "max(kai_date)"
    got = check_backend_facts(bad, bapis, ces)
    assert len(got) == 1 and got[0].startswith("[式]") and "差し戻し先 S2" in got[0], got
    # CTE: 定義が空の e を参照 / 絞り込みのある d を条件なしと書く / ソースに CTE が無い API
    bad = json.loads(json.dumps(good))
    bad["db_operations"][0]["remarks"] = "CTE e を使う。CTE d は条件なしで全件を取る"
    got = check_backend_facts(bad, bapis, ces)
    assert [g.split(" ")[0] for g in got] == ["[CTE]"] * 2 and "e" in got[0] and "d" in got[1], got
    assert check_backend_facts(bad, {"act": {"db_operations": [{"columns": [], "ctes": []}]}}, ces)[0].startswith("[CTE]")
    # count の単位に日付が無いのに「日数」→ WARN
    bad = json.loads(json.dumps(good))
    bad["api_spec"][0]["response_items"][1]["description"] = "期間内の会計日数"
    got = check_backend_facts(bad, bapis, ces)
    assert len(got) == 1 and got[0].startswith("[WARN] [集計単位]"), got
    # 数える単位に日付以外(店舗)も含むのに「日数」→ WARN。日付だけの単位なら WARN にしない
    bapis2 = json.loads(json.dumps(bapis))
    bapis2["act"]["db_operations"][0]["aggregate_units"][0]["group_by"] = ["t.mise_no", "t.kai_date"]
    got = check_backend_facts(bad, bapis2, ces)
    assert len(got) == 1 and got[0].startswith("[WARN] [集計単位]") and "t.mise_no" in got[0], got
    bapis2["act"]["db_operations"][0]["aggregate_units"][0]["group_by"] = ["t.kai_date"]
    assert check_backend_facts(bad, bapis2, ces) == [], check_backend_facts(bad, bapis2, ces)
    # エラー code: condition の code= は common_errors の code_default と照合する。messages の code は照合しない(MSE 番号が正)
    bad = json.loads(json.dumps(good))
    bad["messages_backend"][0]["code"] = "MSE00001"
    bad["messages"] = [{"message_id": "MSE00001", "code": "MSE00001"}, {"message_id": "MSE00001", "message": "code 欄なし"}]
    bad["api_spec"][0]["error_patterns"] = [{"condition": "不正 (code=validation_error)"}, {"condition": "不正 (code=MSE00001)"}]
    got = check_backend_facts(bad, bapis, ces)
    assert len(got) == 1 and got[0].startswith("[エラーcode] api_spec[act].error_patterns") and "MSE00001" in got[0] and "差し戻し先 S2" in got[0], got
    # messages の code が validation_error でも MSE 番号でも、この検査は見ない(形は verify_intermediate.py の検査)
    bad["messages_backend"][0]["code"] = "validation_error"
    bad["api_spec"][0]["error_patterns"] = [{"condition": "不正 (code=validation_error)"}]
    assert check_backend_facts(bad, bapis, ces) == [], check_backend_facts(bad, bapis, ces)
    # F2: 境界値の語が rule に無い・共通エラーの status が api_spec に無いと WARN
    bs = [{"param": "shop_id", "check": "v <= 0", "compare": ["v <= 0"]},
          {"param": "from", "check": "x", "compare": [], "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "impossible_dates_accepted": ["2026-02-30"]},
          {"param": "from,to", "present": False, "check": "order", "fact": "大小を比べる式が無い"},
          {"param": "group", "check": "group not in _G", "compare": ["group not in _G"]}]
    fa = {"act": {"boundaries": bs, "error_patterns": [{"status_code": 400}]}}
    ce = [{"exception": "psycopg2.Error", "errors": [{"status_code": 500}]}, {"exception": "ValidationError", "errors": [{"status_code": "getattr(e) or 400"}]}]
    vl = [{"field": "店舗", "rule": "正の整数", "applied_when": ""}, {"field": "日付", "rule": "YYYY-MM-DD。暦に無い日付は DB 例外になる。開始と終了の大小は検査しない"}]
    ap = [{"action": "act", "error_patterns": [{"status_code": 400}, {"status_code": 500}]}]
    assert check_boundaries(vl, ap, fa, ce) == [], check_boundaries(vl, ap, fa, ce)
    got = check_boundaries([{"field": "店舗", "rule": "必須"}], [{"action": "act", "error_patterns": [{"status_code": 400}, {"status_code": 503}]}], fa, ce)
    assert [g.split(" ")[1] for g in got] == ["[境界値]"] * 3 + ["[DB例外]"] * 2 and all(g.startswith("[WARN]") for g in got), got
    assert not check_boundaries([], [{"action": "act"}], {}, []) and not check_boundaries([], [], None, None)
    # B3: reji_no は string | null。nullable を反転すると [null可] ERROR、型の分類違いは WARN
    apis = {"act": {"response_fields": [{"path": "rows[].reji_no", "ts_type": "string | null", "ts_base": "string", "nullable": True},
                                         {"path": "rows[].days", "ts_type": "number", "ts_base": "number", "nullable": False},
                                         {"path": "dead_cols", "ts_type": "string[]", "ts_base": "string[]", "nullable": False}]}}
    spec = lambda *rs: [{"action": "act", "response_items": [dict(zip(("key", "data_type", "nullable"), r)) for r in rs]}]
    assert check_types(spec(("rows[].reji_no", "string", True), ("rows[].days", "integer", False), ("dead_cols", "string[]", False)), apis) == []
    assert check_types([{"action": "act", "response_items": [{"key": "rows[].reji_no", "data_type": "string | null"}]}], apis) == [], "nullable 無しは data_type の | null で見る"
    got = check_types(spec(("rows[].reji_no", "string", False), ("rows[].days", "string", False), ("x.unknown", "string", True)), apis)
    assert [g.split(" ")[0] for g in got] == ["[null可]", "[WARN]"] and "rows[].reji_no" in got[0], got
    # B3 SQL の型: index signature の列は db_type(bigint → number)と db_nullable で見る。api.ts の共用体型は見ない
    ix = {"act": {"response_fields": [{"path": "rows[].s", "ts_type": "number | string | null", "ts_base": "number | string", "nullable": True,
                                       "index_signature": True, "sql_hint": "SUM(x)::bigint", "db_type": "bigint", "db_nullable": False}]}}
    assert check_types(spec(("rows[].s", "integer", False)), ix) == []
    got = check_types(spec(("rows[].s", "string", True)), ix)
    assert [g.split(" ")[0] for g in got] == ["[WARN]", "[null可]"] and "SQL" in got[0], got
    # F3: 初期表示の取得順。parallel は「並行」、serial は「待って」の記述が要る。語が無い・食い違うと WARN
    fr = lambda *d: [{"process_id": "FR1", "name": "初期表示", "steps": [{"description": x} for x in d]}]
    par, ser = {"mode": "parallel", "basis": "別々の useEffect"}, {"mode": "serial", "basis": "then"}
    assert check_fetch_plan(par, fr("店舗の一覧と会計レポートの取得は並行して進める")) == []
    assert check_fetch_plan(ser, fr("店舗の一覧の応答を待って会計レポートを取得する")) == []
    got = check_fetch_plan(par, fr("店舗の一覧を取得する", "会計レポートを取得する"))
    assert len(got) == 1 and got[0].startswith("[WARN] [取得順]") and "差し戻し先" not in got[0] and "スクリプトの不具合" in got[0], got
    assert check_fetch_plan(ser, fr("取得は並行して進める")) and not check_fetch_plan({"mode": "single"}, fr("x")) and not check_fetch_plan(None, fr("x"))
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
    # kind=API検査(バック側の入力検査。H4 手順書)は、画面に input_check が無くても [種別] にしない(画面の検査だけを見る照合の対象外)
    assert check_kinds([{"no": "VL9", "field": "集計開始日", "rule": "YYYY-MM-DD の形式", "kind": "API検査"}], [], beh) == []
    jsx = [{"tag": "select", "label": "店舗", "item_type_hint": "ComboBox"}, {"tag": "input", "label": "開始", "item_type_hint": "Calendar"}]
    assert check_items([{"item_name": "店舗", "item_type": "ComboBox"}, {"item_name": "開始", "item_type": "Calendar"}], jsx) == []
    got = check_items([{"item_name": "店舗", "item_type": "ComboBox"}, {"item_name": "開始", "item_type": "Input"}, {"item_name": "幻", "item_type": "Button"}], jsx)
    assert [g.split(" ")[1] for g in got] == ["[種別]", "[画面項目]"], got
    assert [g for g in check_items([{"item_name": "店舗", "item_type": "ComboBox"}], jsx) if "欠け" in g]
    # B2 残り: 表示専用要素(label 無し)は (1)〜(3) の対象外。display_rows の欠けは ERROR(接頭辞 [WARN] 無し)
    disp = [{"tag": "span", "label": "", "text": "読込中…", "item_type_hint": "Label"}]
    assert check_items([], disp) == []
    got = check_items([{"item_name": "総売上"}], None, [{"labels": ["総売上", "返品"]}])
    assert len(got) == 1 and "返品" in got[0] and not got[0].startswith("[WARN]"), got
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
        # 型文({key})を呼出の実引数で展開した文言は、ソースの f-string の型文に当てはまれば実在。当てはまらない文言は WARN
        ex = {"messages_backend": [{"message_id": "M3", "message": "shop_id は正の整数で指定してください"},
                                   {"message_id": "M4", "message": "shop_id は日付で指定してください"}]}
        got = check_messages(ex, s)
        assert len(got) == 1 and "M4" in got[0], got
        # 受け入れ条件: 「from」を「集計開始日」に書き換えた文言は WARN
        bad = {"messages": [{"message_id": "M1", "message": "集計開始日 は YYYY-MM-DD 形式で指定してください"}]}
        assert len(check_messages(bad, s)) == 1 and check_messages(bad, s)[0].startswith("[WARN] [文言]")
        det = {"event_processes": [{"event_code": "EV1", "details": ["画面に「- 全店 -」と出す", "「作り話」と出す"]}]}
        assert len(check_messages(det, s)) == 1
        assert check_examples([{"item_name": "店舗", "example": "- 全店 -"}, {"item_name": "日付", "example": "2026-10-01"}], s) == [], "日付の書式の実値は通す"
        assert check_examples([{"item_name": "店名", "example": "作り話の店 3 件"}], s) == [
            "[WARN] [例示値] screen_layout.items[店名]: example「作り話の店 3 件」がソースの文字列に無い → 差し戻し先 S1"]
    # S1 の例示値: 数字の並び(日付・金額)は実行時の値として穴扱い。字面の語(店名など)がソースに無ければ WARN
    assert check_examples([{"item_name": "日付", "example": "2026-10-14"}], s) == []
    assert check_examples([{"item_name": "店舗", "example": "- 全店 - 2 件"}], s) == []
    got = check_examples([{"item_name": "店舗", "example": "作り話の店 2 件"}], s)
    assert len(got) == 1 and got[0].startswith("[WARN] [例示値]"), got
    # S2 の式: 日本語の説明は字面で比べない(SQL の関数の形の式は従来どおり比べる)
    prose = {"api_spec": [{"action": "act", "response_items": [{"key": "rows[].first_date", "expression": "期間内の最小の会計日"}]}]}
    assert check_backend_facts(prose, bapis, ces) == [], check_backend_facts(prose, bapis, ces)
    # H4 の式: 列の名前(合計2(支払科目計))の数字は定数ではない。式の中の定数 3 は WARN
    cc = [{"id": "CC09", "label": "支払科目に無い額", "expr": "r.s - r.t", "operands": ["s", "t"], "funcs": [], "literals": [],
           "operand_labels": {"s": ["小計（税込売上金額）"], "t": ["合計2(支払科目計)"]}}]
    dd = [{"name": "支払科目に無い額", "formula": "支払科目に無い額 = 小計（税込売上金額） − 合計2(支払科目計)",
           "inputs": [{"name": "小計（税込売上金額）"}, {"name": "合計2(支払科目計)"}]}]
    assert check_calcs(dd, cc) == [], check_calcs(dd, cc)
    cc2 = [{"id": "CC09", "label": "支払科目に無い額", "expr": "r.s * 3", "operands": ["s"], "funcs": [], "literals": [],
            "operand_labels": {"s": ["小計（税込売上金額）"]}}]
    got = check_calcs([{"name": "支払科目に無い額", "formula": "支払科目に無い額 = 小計（税込売上金額） × 3", "inputs": [{"name": "小計（税込売上金額）"}]}], cc2)
    assert len(got) == 1 and "式の数字 3" in got[0], got
    # VL5: 同じ行範囲の対(124 の data_filter)を先に使う。src が無いときは字面で対を探す(従来どおり表示用の行に当たる)
    bh = [{"id": "BH01", "kind": "display_toggle", "cond": "s.dead", "target": "空", "src": sp(132), "control_src": []},
          {"id": "BH10", "kind": "data_filter", "cond": "showDead || !s.dead", "target": "", "src": sp(124), "control_src": sp(276)}]
    vl = {"no": "VL9", "field": "空欄項目も表示", "rule": "表示の絞り込み: 空欄項目(s.dead)を表示対象から外す", "kind": "絞り込み"}
    assert check_kinds([dict(vl, src=sp(123, 124) + sp(276))], [], bh) == []
    assert len(check_kinds([vl], [], bh)) == 1
    # S1 の例示: 区切り(" / ")ごとに照合する。インライン要素(strong)と JSX の式({…})で切れた文も、タグを外せば照合できる
    assert _example_in_source("3 点 / 4,500 円", s)
    with tempfile.TemporaryDirectory() as d:
        tsx = Path(d, "N.tsx")
        tsx.write_text("<p>\n  <strong>取込欠損の疑い</strong>：この期間に、上書きされた疑いのある日があります。\n</p>\n"
                       "<p>\n  POSがあるのは\n  {shops.length}店だけなので\n</p>\n", encoding="utf-8")
        ss = collect_source_strings([tsx])
        assert _example_in_source("取込欠損の疑い：この期間に、上書きされた疑いのある日があります。", ss), ss
        assert _example_in_source("POSがあるのは10店だけなので", ss), ss
        assert not _example_in_source("作り話の文は10店だけなので", ss)
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
    # 目的: react 下書きの ui_strings(画面の文言。B4)も照合の文字列に足す。{…} で途切れた文言や、source_files の外の画面を拾う
    n_ui = 0
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_react_draft.json")):
        ui = json.loads(p.read_text(encoding="utf-8")).get("ui_strings") or []
        n_ui += len(ui)
        strings |= {norm(u["text"]) for u in ui if u.get("text")}
    if not strings:
        print("[WARN] [文言] ソースの文字列が1つも取れない(source_files と src の file を確認)")
        return 0
    problems = check_messages(data, strings) + check_examples((data.get("screen_layout") or {}).get("items") or [], strings)
    cands, jsx, beh, disp_rows = [], [], [], []
    type_problems, n_sql, plan, bound_problems, n_bound, fact_problems = [], 0, None, [], 0, []
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_backend_draft.json")):
        bd = json.loads(p.read_text(encoding="utf-8"))
        apis = bd.get("apis") or {}
        type_problems = check_types(data.get("api_spec"), apis)
        # F2: 境界値と共通エラー(DB 例外)の照合。validations は最終の validations か、断片の validations_frontend
        bound_problems = check_boundaries(data.get("validations") or data.get("validations_frontend"), data.get("api_spec"), apis, bd.get("common_errors"))
        # B1 の事実(別名・CTE・副問合せ・集計の単位・例外の code)の照合。[WARN] 付きは終了コードに影響しない
        fact_problems = check_backend_facts(data, apis, bd.get("common_errors"))
        n_bound = sum(len(a.get("boundaries") or []) for a in apis.values())
        n_sql = sum(1 for a in apis.values() for f in a.get("response_fields") or [] if f.get("db_type"))
    for p in sorted(Path(INTERMEDIATE_DIR).glob(f"{feat}_react_draft.json")):
        rd = json.loads(p.read_text(encoding="utf-8"))
        cands, jsx, beh = rd.get("calc_candidates") or [], rd.get("jsx_elements") or [], rd.get("behaviors") or []
        disp_rows = rd.get("display_rows") or []
        plan = ((rd.get("events") or [{}])[0]).get("fetch_plan")
    items = (data.get("screen_layout") or {}).get("items") or []
    item_problems = check_items(items, jsx, disp_rows) if jsx else []
    problems += item_problems
    ops = [i for i in items if i.get("item_type") in _OPERATION_TYPES]
    shown = [j for j in jsx if j.get("tag") in _DISPLAY_TAGS]
    names = {norm(i.get("item_name") or "") for i in items}
    print(f"[INFO] 画面項目の照合: ソースの入力・操作系 {len(jsx) - len(shown)} 件・S1 の入力・操作系 {len(ops)} 件・WARN {len(item_problems)} 件")
    print(f"[INFO] 表示専用要素の照合: ソースの表示専用要素 {len(shown)} 件(visible_if あり {sum(1 for j in shown if j.get('visible_if'))} 件)・S1 に同じ字面の項目あり {sum(1 for j in shown if norm(j.get('text') or '') in names)} 件")
    calc_problems = check_calcs(data.get("calculations") or [], cands) if cands else []
    problems += calc_problems
    kind_problems = check_kinds(data.get("validations") or data.get("validations_frontend"), data.get("front_processes"), beh)
    fetch_problems = check_fetch_plan(plan, data.get("front_processes"))
    for p in problems + kind_problems + type_problems + fetch_problems + bound_problems + fact_problems:
        print(p)
    fact_errors = [p for p in fact_problems if not p.startswith("[WARN]")]
    print(f"[INFO] 下書きの事実(別名・CTE・副問合せ・集計の単位・エラー code)の照合: ERROR {len(fact_errors)} 件・WARN {len(fact_problems) - len(fact_errors)} 件")
    n_code = sum(1 for a in data.get("api_spec") or [] for e in a.get("error_patterns") or [] if isinstance(e, dict) and "code=" in str(e.get("condition")))
    print(f"[INFO] エラー code の照合: api_spec の condition に code= と書いた行 {n_code} 件(common_errors の code_default と照合。0 件なら照合は効いていない)")
    print(f"[INFO] 境界値・DB 例外の照合: 下書きの boundaries {n_bound} 件・WARN {len(bound_problems)} 件")
    null_problems = [p for p in type_problems if not p.startswith("[WARN]")]
    print(f"[INFO] 応答項目の型の照合: ERROR {len(null_problems)} 件・WARN {len(type_problems) - len(null_problems)} 件・SQL の型の手掛かりつきの欄 {n_sql} 件")
    n = len(data.get("calculations") or [])
    print(f"[INFO] 初期取得の順の照合: fetch_plan {(plan or {}).get('mode', 'なし')}・WARN {len(fetch_problems)} 件")
    print(f"[INFO] 計算式の照合: ソースの式 {len(cands)} 件・calculations {n} 件・WARN {len(calc_problems)} 件")
    print(f"[INFO] 処理の種別の照合: ソースの behaviors {len(beh)} 件・ERROR {len(kind_problems)} 件")
    print(f"[INFO] 文言の照合: ソースの文字列 {len(strings)} 件(うち下書きの ui_strings {n_ui} 件)に対し WARN {len(problems)} 件")
    return 1 if kind_problems or null_problems or fact_errors else 0


if __name__ == "__main__":
    sys.exit(main())
