#!/usr/bin/env python3
# 目的: 意味の判定が要る記述を、1件ずつの判定項目(audit claim)として書き出す。
# 意味合い: design-doc の audit 段(Haiku)が、記述(text)とソースの行(src_excerpt)を並べて OK / NG を判定するための入力。
#           機械で決まる形の検査(verify_* 系)では止められない型（画面項目の備考・FR の手順・CL の説明・VL の rule・計算の備考）を対象にする。
# 接続情報: 入力 = target/intermediate/<機能名>.json / ソース = 各行の src（file・line_start・line_end）
#           出力 = target/intermediate/<機能名>_audit_claims.json
#           キャッシュ = target/intermediate/<機能名>_audit_results.json（前回の判定で ok だった text と src_excerpt の組は書き出さない）
#           担当 = run_units.owner_of（差し戻し先の単位 S1〜H4）。
#           終了コードは常に 0（判定項目の書き出しの失敗で段を止めない）。
import json
import re
import sys
import tempfile
from pathlib import Path

# WHY(G17): python3 -I(分離モード)は実行したスクリプトのフォルダを sys.path に入れないので、同じフォルダの _config_loader・run_units を import できず落ちる。
# 自分のフォルダを先頭に足して、通常実行でも -I でも同じ import が通るようにする(_config_loader・run_units の import より前に置く)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _config_loader import INTERMEDIATE_DIR, load_screen_config
from run_units import load_units, owner_of

# WHY: 判定の文脈として src の前後 2 行を見せる。主抜粋は 40 行で打ち切り、判定の入力を小さく保つ
EXCERPT_CONTEXT = 2
EXCERPT_MAX = 40
# WHY(2026-10-15 run13 試走): 144 件中 54 件が unknown。理由は「呼ぶ関数・state の更新先・[BE1] の定義が抜粋の外」だった。
# src は呼出しの行(onChange・useEffect)を指し、文が述べる処理の本体は別の行にあるため。
# そこで主抜粋の後ろに、文中の語が指す先(関数本体・state の定義・それを使う hook・[BE1] 等の定義元・SQL 定数)を足す。
# 1項目の全体(主抜粋+参照先)は 80 行までにして、判定の入力を肥らせない。1つの参照先は 40 行まで。
EXCERPT_TOTAL = 80
# WHY(G14 run20): unknown の再判定(sonnet)には、状態の宣言・更新処理・定数など参照先をさらに載せた広い抜粋を渡す。
# 通常の判定とキャッシュ照合は従来の EXCERPT_TOTAL の抜粋(src_excerpt)のまま、再判定だけが src_excerpt_wide(この予算)を読む。
EXCERPT_TOTAL_WIDE = EXCERPT_TOTAL * 2
BLOCK_MAX = 40
DEPTH2_BLOCK_MAX = 20  # 参照先のさらに先(2段目)は短く切る
REF_HEADER = "---- 参照先"  # AU_意味の照合.md が text の区切りとして説明する印。変えるなら手順書も直す

# 識別子から外す語(言語の予約語・組込み・どこにでもある語)。参照先を引く対象にしない
_STOP = set("""
if else for while return const let var function new this true false null undefined await async typeof in of def class import
from as with try except finally raise not and or is lambda None True False self cls print len range str int float list dict
set tuple map filter Number String Math Object Array JSON Date Promise Boolean Error useState useEffect useMemo useCallback
export default type interface string number boolean void case switch break continue then catch
""".split())
_IDENT_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]{2,})\b")
_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]{2,})\s*\(")
_TAG_RE = re.compile(r"<([A-Z][A-Za-z0-9_]{2,})\b")
_SETTER_RE = re.compile(r"(?<![\w.])set([A-Z][A-Za-z0-9_]*)\s*\(")
_REF_TOKEN_RE = re.compile(r"\[([A-Z]{1,3}\d+)\]")  # 文中の [BE1] [FR2] などの参照
_HOOK_RE = re.compile(r"\b(?:useCallback|useEffect|useMemo)\s*\(")
_CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".py")


def _src_of(row):
    """行の出典（src）のうち最初の、file と開始行がそろったものを返す。無ければ None。"""
    srcs = row.get("src") or []
    if isinstance(srcs, dict):
        srcs = [srcs]
    for s in srcs:
        if isinstance(s, dict) and s.get("file") and isinstance(s.get("line_start"), int):
            return s
    return None


def _first_src(merged, section, key_field, key_value):
    """merged[section] の中で key_field == key_value の行の、最初の出典。無ければ None。"""
    for d in merged.get(section) or []:
        if isinstance(d, dict) and d.get(key_field) == key_value:
            return _src_of(d)
    return None


def _src_from_api(merged, row):
    """出典の無い検査(API 側の入力検査など)に、その文が触れるリクエスト引数を持つ API の出典を当てる。
    WHY: 出典の無い VL の判定項目は抜粋が空になり、判定が必ず unknown だった(run13 の5件)。
    文(rule・applied_when)の英数字の語が api_spec[].request_params[].name と一致する API の src を使う。引けなければ None。"""
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", f"{row.get('rule') or ''} {row.get('applied_when') or ''}"))
    for api in merged.get("api_spec") or []:
        names = {str(p.get("name")) for p in (api.get("request_params") or []) if isinstance(p, dict)}
        if names & words:
            s = _src_of(api)
            if s:
                return s
    return None


def _src_from_error(merged, row):
    """出典の無い検査(VL)が message_id を持つとき、同じ message_id の error_patterns・messages の出典を当てる。引けなければ None。
    WHY: API 側の入力検査(kind=API検査)は、request_params の名前が文に無くても、エラーの行(message_id で結ぶ)が出典を持つ。"""
    mid = row.get("message_id")
    if not mid:
        return None
    for api in merged.get("api_spec") or []:
        for e in api.get("error_patterns") or []:
            if isinstance(e, dict) and e.get("message_id") == mid and _src_of(e):
                return _src_of(e)
    for key in ("messages_backend", "messages"):
        for m in merged.get(key) or []:
            if isinstance(m, dict) and m.get("message_id") == mid and _src_of(m):
                return _src_of(m)
    return None


def _src_from_screen(merged, row):
    """出典の無い検査(VL)の対象(field)が画面項目の名前なら、その項目の出典を、領域(エリア)の名前なら、領域の項目の出典の範囲(同じファイルの最小〜最大の行)を返す。
    WHY: 最後の手段。検査が元にした画面の場所(対象の項目・エリア)の行を最低限入れ、抜粋を空にしない。引けなければ None。"""
    field = str(row.get("field") or "").strip()
    if not field:
        return None
    sl = merged.get("screen_layout") or {}
    items = [i for i in sl.get("items") or [] if isinstance(i, dict)]
    for it in items:
        if it.get("item_name") == field and _src_of(it):
            return _src_of(it)
    for ar in sl.get("areas") or []:
        if isinstance(ar, dict) and ar.get("area_name") == field:
            srcs = [_src_of(it) for it in items if it.get("area_ref") == ar.get("area_no") and _src_of(it)]
            if srcs:
                same = [s for s in srcs if s["file"] == srcs[0]["file"]]
                return {"file": srcs[0]["file"], "line_start": min(s["line_start"] for s in same),
                        "line_end": max(int(s.get("line_end") or s["line_start"]) for s in same)}
    return None


def _src_from_react(react, section, name):
    """出典の無い行(画面項目の備考・計算の備考)の出典を、react 下書きの同じ名前の行から引く。引けなければ None。
    section: "item"=jsx_elements[].label と display_rows[].labels / "calc"=calc_candidates[].label。名前が同じ行の最初の出典を返す。
    WHY: 古い版の中間 JSON は計算・画面項目に src が無く、抜粋が空 → 判定が必ず unknown だった(run10〜run12 の CA・備考)。下書きは機械抽出で出典が正。"""
    name = str(name or "").strip()
    if not name or not react:
        return None
    if section == "calc":
        rows = [c for c in react.get("calc_candidates") or [] if isinstance(c, dict) and c.get("label") == name]
    else:
        rows = [e for e in react.get("jsx_elements") or [] if isinstance(e, dict) and e.get("label") == name]
        rows += [r for r in react.get("display_rows") or [] if isinstance(r, dict) and name in (r.get("labels") or [])]
    for r in rows:
        if _src_of(r):
            return _src_of(r)
    return None


# validations[].kind → react 下書きの behaviors[].kind。verify_vs_source._KIND_OF と同じ対応
_VL_TO_BEHAVIOR = {"入力検査": "input_check", "表示切替": "display_toggle", "活性切替": "enable_toggle", "絞り込み": "data_filter"}
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _srcs_overlap(a, b):
    """出典 a・b(file・line_start・line_end の dict)が同じファイルで行が重なるか。"""
    return a.get("file") == b.get("file") and not (int(a.get("line_end") or a["line_start"]) < b["line_start"]
                                                   or a["line_start"] > int(b.get("line_end") or b["line_start"]))


def _behavior_pair_score(merged, v, b):
    """検査 v(validations の行)と振る舞い b(react 下書きの behaviors の行)が同じ処理を指す見込みの点数。0 は無関係。
    (a) b.target(画面に出す文言)が v の文に「…」で引かれている +3 / (b) v の文と b の cond・target に共通の英数字の識別子 +1 ずつ /
    (c) v の対象(field)の画面項目の出典と b の出典(src・control_src)が重なる +2。呼出元: _vl_behavior_srcs。"""
    text = f"{v.get('rule') or ''} {v.get('applied_when') or ''}"
    score = 0
    target = str(b.get("target") or "").strip()
    if target and f"「{target}」" in text:
        score += 3
    score += len(set(_WORD_RE.findall(f"{b.get('cond') or ''} {target}")) & set(_WORD_RE.findall(text)))
    field = v.get("field")
    bsrcs = [s for s in (b.get("src") or []) + (b.get("control_src") or []) if isinstance(s, dict) and isinstance(s.get("line_start"), int)]
    for it in (merged.get("screen_layout") or {}).get("items") or []:
        if isinstance(it, dict) and it.get("item_name") == field:
            if any(_srcs_overlap(s, x) for s in (it.get("src") or []) if isinstance(s, dict) and isinstance(s.get("line_start"), int) for x in bsrcs):
                score += 2
                break
    return score


def _vl_behavior_srcs(merged, react, lacking):
    """出典の無い検査 lacking(validations の添字の一覧)の出典を、react 下書きの behaviors から引いて {添字: src} で返す。
    WHY(2026-10-15 run14): validations は合成が src を付けず(_units._fill_src の対象外)、検査の元になった画面の処理は react 下書きの
    behaviors(表示切替・活性切替・絞り込み・入力検査)にある。run14 は VL5〜VL15 の11件が出典なしで、抜粋が空 → 判定が必ず unknown だった。
    対応づけ: 同じ kind の中で、点数(_behavior_pair_score)が高い組から1対1に決める。残りが同数なら、決めた組の順序が単調(検査の並び=ソースの並び)
    のときだけ並び順で結ぶ(順序が食い違うなら、別の処理の行を渡して誤判定させるより、空のまま次の手段に回す)。"""
    vls = merged.get("validations") or []
    out = {}
    for kind_ja, kind_b in _VL_TO_BEHAVIOR.items():
        mine = [i for i in lacking if isinstance(vls[i], dict) and vls[i].get("kind") == kind_ja]
        bhs = [b for b in (react or {}).get("behaviors") or [] if isinstance(b, dict) and b.get("kind") == kind_b and _src_of(b)]
        if not mine or not bhs:
            continue
        pairs = sorted(((_behavior_pair_score(merged, vls[i], b), -i, -j, i, j) for i in mine for j, b in enumerate(bhs)), reverse=True)
        used_v, used_b, matched = set(), set(), []
        for score, _ni, _nj, i, j in pairs:
            if score > 0 and i not in used_v and j not in used_b:
                used_v.add(i)
                used_b.add(j)
                matched.append((i, j))
        left_v = [i for i in mine if i not in used_v]
        left_b = [j for j in range(len(bhs)) if j not in used_b]
        ordered = [j for _i, j in sorted(matched)]
        if left_v and len(left_v) == len(left_b) and ordered == sorted(ordered):
            matched += list(zip(left_v, left_b))
        for i, j in matched:
            out[i] = _src_of(bhs[j])
    return out


def _candidates(merged, react=None):
    """判定項目の候補 (kind, 欄の道筋, 記述の本文, src, 主語)。空の記述は候補にしない。
    WHY: 種類は「意味の判定が要る記述」に限る。型・件数のような機械で決まる欄は verify_* 系に任せる。
    主語(subject)は、文だけでは何の記述か分からない項目(「点数と金額の2列。左段」)の対象を判定者に伝える。
    react は react 下書き(無ければ None)。出典の無い検査(VL)の出典を behaviors から引くのに使う。"""
    out = []
    vls = merged.get("validations") or []
    # 出典の無い検査: 自分の src → request_params の API → 同じ処理の振る舞い → 同じ message_id のエラー・メッセージの行 → 対象の画面項目・エリア の順に探す
    # WHY: 振る舞い(表示の条件の行)を message_id のメッセージ(文言を出す行だけ)より先にする。VL12 は条件(290 行)でなく文言(292〜294 行)だけが渡っていた
    lacking = [i for i, v in enumerate(vls) if isinstance(v, dict) and str(v.get("rule") or "").strip()
               and not (_src_of(v) or _src_from_api(merged, v))]
    beh = _vl_behavior_srcs(merged, react, lacking)
    for i, it in enumerate((merged.get("screen_layout") or {}).get("items") or []):
        if str(it.get("remarks") or "").strip():
            subj = f"画面項目「{it.get('item_name') or ''}」({it.get('item_type') or ''})の備考"
            out.append(("remarks", f"screen_layout.items[{i}].remarks", it["remarks"],
                        _src_of(it) or _src_from_react(react, "item", it.get("item_name")), subj))
    for i, fp in enumerate(merged.get("front_processes") or []):
        for j, st in enumerate(fp.get("steps") or []):
            if str(st.get("description") or "").strip():
                subj = f"フロント処理 {fp.get('process_id') or ''}「{fp.get('name') or ''}」の手順{st.get('step_no') or j + 1}"
                out.append(("fr_step", f"front_processes[{i}].steps[{j}].description", st["description"],
                            _src_of(st) or _src_of(fp), subj))
    for i, cl in enumerate(merged.get("common_logic") or []):
        if str(cl.get("description") or "").strip():
            out.append(("cl", f"common_logic[{i}].description", cl["description"], _src_of(cl),
                        f"共通ロジック {cl.get('no') or ''}「{cl.get('name') or ''}」の説明"))
    for i, v in enumerate(vls):
        if str(v.get("rule") or "").strip():
            out.append(("vl", f"validations[{i}].rule", v["rule"],
                        _src_of(v) or _src_from_api(merged, v) or beh.get(i) or _src_from_error(merged, v) or _src_from_screen(merged, v),
                        f"検査 {v.get('no') or ''}(対象: {v.get('field') or ''})の rule"))
    for i, c in enumerate(merged.get("calculations") or []):
        if str(c.get("remarks") or "").strip():
            out.append(("calc_remarks", f"calculations[{i}].remarks", c["remarks"],
                        _src_of(c) or _src_from_react(react, "calc", c.get("name")),
                        f"計算 {c.get('no') or ''}「{c.get('name') or ''}」の備考"))
    return out


def _read_lines(path, cache):
    """ファイルの行の一覧(キャッシュ付き)。読めなければ None。"""
    if path not in cache:
        try:
            cache[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[path] = None
    return cache[path]


def _resolve(src_file, project_root):
    path = Path(src_file)
    if not path.is_absolute():
        path = Path(project_root or ".") / path
    return path


def _block_end(lines, i, cap):
    """0 始まりの i 行から始まる定義(関数・hook・定数・SQL 文字列)の最後の行(0 始まり)を返す。cap 行で打ち切る。
    WHY: 言語ごとのパーサを持たず、(1) 三連引用符・バッククォートの閉じ (2) Python の def/class のインデント (3) 括弧の釣り合い の順で終端を決める。"""
    last = min(len(lines) - 1, i + cap - 1)
    first = lines[i]
    stripped = first.strip()
    for q in ('"""', "'''", "`"):
        if first.count(q) % 2 == 1:
            for j in range(i + 1, last + 1):
                if q in lines[j]:
                    return j
            return last
    if re.match(r"(?:async\s+)?(?:def|class)\s", stripped):
        indent = len(first) - len(first.lstrip())
        k = i
        while k < last and not lines[k].rstrip().endswith(":"):
            k += 1
        end = k
        for j in range(k + 1, last + 1):
            s = lines[j]
            if s.strip() and len(s) - len(s.lstrip()) <= indent:
                break
            end = j
        return end
    bal, seen = 0, False
    for j in range(i, last + 1):
        for ch in lines[j]:
            if ch in "{([":
                bal += 1
                seen = True
            elif ch in "})]":
                bal -= 1
        # WHY: `const f = (a) =>` の次の行に本体が続く形・`.filter(...)` のメソッドチェーンは、括弧が釣り合っても文が終わっていない
        nxt = lines[j + 1].lstrip() if j + 1 < len(lines) else ""
        if seen and bal <= 0 and not lines[j].rstrip().endswith(("=>", "=", ",", "&&", "||", "?", ":", "+")) \
                and not nxt.startswith("."):
            return j
        if not seen and lines[j].rstrip().endswith(";"):
            return j
    return last


def _def_re(name):
    n = re.escape(name)
    return re.compile(
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?\s+{n}\b|(?:const|let|var)\s+{n}\b"
        rf"|def\s+{n}\b|class\s+{n}\b|(?:type|interface|enum)\s+{n}\b)"
        rf"|^\s*(?:const|let|var)\s*\[[^\]]*\b{n}\b[^\]]*\]\s*="  # useState のタプル([x, setX] の両方を引ける)
        rf"|^{n}\s*=[^=]"  # Python のモジュール直下の定数(SQL 文字列など)
    )


def _find_def(lines, name):
    rx = _def_re(name)
    for i, line in enumerate(lines):
        if rx.match(line):
            return i
    return None


def _imports(lines, base_dir):
    """import / from-import の名前 → ソースファイルの辞書。相対パス(./ ../ ・Python の同階層・相対)だけ引く。
    WHY: 呼ぶ関数が別ファイル(api.ts の fetchXxx など)にあるとき、その本体まで届かせる。外部パッケージは引かない。"""
    text = "\n".join(lines)
    out = {}
    pairs = []
    for m in re.finditer(r"import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+[\"']([^\"']+)[\"']", text):
        pairs.append(([x.split(" as ")[-1].strip() for x in m.group(1).split(",") if x.strip()], m.group(2)))
    for m in re.finditer(r"import\s+(\w+)\s+from\s+[\"']([^\"']+)[\"']", text):
        pairs.append(([m.group(1)], m.group(2)))
    for m in re.finditer(r"^from\s+(\.*[\w.]*)\s+import\s+([\w, ]+)$", text, re.M):
        mod = m.group(1)
        rel = mod if mod.startswith(".") else "./" + mod.replace(".", "/")
        pairs.append(([x.strip() for x in m.group(2).split(",") if x.strip()], rel))
    for names, mod in pairs:
        if not mod.startswith("."):
            continue
        stem = (base_dir / mod).resolve()
        for cand in [stem.with_name(stem.name + e) for e in _CODE_EXT] + [stem / ("index" + e) for e in _CODE_EXT] \
                + ([stem] if stem.suffix in _CODE_EXT else []):
            if cand.is_file():
                for n in names:
                    out[n] = cand
                break
    return out


def _hook_blocks(lines):
    """useCallback / useEffect / useMemo の呼出しごとに (開始, 終了, 名前, 依存配列の語の集合) を返す(0 始まり)。
    WHY: state を更新する行(onChange の setX)が引き起こす処理は、その state を依存配列に持つ hook にある。"""
    out = []
    for i, line in enumerate(lines):
        if not _HOOK_RE.search(line):
            continue
        end = _block_end(lines, i, BLOCK_MAX)
        m = re.search(r"\[([^\[\]]*)\]\s*\)\s*;?\s*$", lines[end])
        deps = {w for w in re.findall(r"[A-Za-z_]\w*", m.group(1))} if m else set()
        mn = re.match(r"\s*(?:const|let|var)\s+(\w+)\s*=", line)
        out.append((i, end, mn.group(1) if mn else None, deps))
    return out


def _state_var(lines, setter):
    """setter 名(setRows)から state の変数名(rows)を [x, setX] = useState の行で引く。"""
    rx = re.compile(rf"\[\s*(\w+)\s*,\s*{re.escape(setter)}\s*\]\s*=")
    for line in lines:
        m = rx.search(line)
        if m:
            return m.group(1)
    return None


class _Excerpt:
    """1項目の抜粋の組み立て。主抜粋と参照先の断片を、全体 budget 行(既定 EXCERPT_TOTAL)の予算で並べる。"""

    def __init__(self, cache, budget=None):
        self.cache = cache
        self.budget = EXCERPT_TOTAL if budget is None else budget  # 全体の行数の予算(再判定用の広い抜粋は EXCERPT_TOTAL_WIDE)
        self.segs = []  # (path, 開始行(1始まり), 終了行, ラベル or None, 行の文字列リスト)
        self.total = 0

    def _overlaps(self, path, s, e):
        return any(p == path and not (e < s2 or s > e2) for p, s2, e2, _l, _t in self.segs)

    def add(self, path, i0, i1, label, cap):
        """0 始まり i0..i1 の行を断片として足す。重複・予算切れなら足さず False。予算を超えるぶんは切る。"""
        lines = _read_lines(path, self.cache)
        if lines is None:
            return False
        # WHY: 参照先の定義が主抜粋と部分的に重なることが多い(hook の末尾が主抜粋に入っている等)。重なる行は足さず、残りだけを足す。
        # 完全に既存の断片の中なら足さない
        while self._overlaps(path, i0 + 1, i1 + 1):
            hit = next((s2, e2) for p, s2, e2, _l, _t in self.segs if p == path and not (i1 + 1 < s2 or i0 + 1 > e2))
            if i0 + 1 < hit[0]:
                i1 = hit[0] - 2
            elif i1 + 1 > hit[1]:
                i0 = hit[1]
            else:
                return False
            if i1 < i0:
                return False
        room = self.budget - self.total - (1 if label else 0)
        if room < 3:
            return False
        n = min(i1 - i0 + 1, cap, room)
        body = lines[i0:i0 + n]
        if n < i1 - i0 + 1 and room > n:
            body = body + ["...(以下略)"]  # 切ったことを判定者に伝える(切れた先に答えがあれば unknown にしてよい)
        self.segs.append((path, i0 + 1, i0 + n, label, body))
        self.total += len(body) + (1 if label else 0)
        return True

    def text(self):
        parts = []
        for path, s, e, label, body in self.segs:
            if label:
                parts.append(f"{REF_HEADER}: {label} ({path.name}:{s}-{e}) ----")
            parts.extend(body)
        return "\n".join(parts)


def _idents(text_lines):
    """行の一覧から (呼ぶ関数と JSX タグ, 更新関数 setX, その他の識別子) を、出現順・重複なしで返す。"""
    text = "\n".join(text_lines)
    calls = list(dict.fromkeys([m.group(1) for m in _CALL_RE.finditer(text)] + _TAG_RE.findall(text)))
    setters = list(dict.fromkeys("set" + m.group(1) for m in _SETTER_RE.finditer(text)))
    rest = list(dict.fromkeys(m.group(1) for m in _IDENT_RE.finditer(text)))
    calls = [c for c in calls if c not in _STOP and not (c.startswith("set") and c in setters)]
    rest = [r for r in rest if r not in _STOP and r not in calls and r not in setters]
    return calls, setters, rest


def _add_statement_defs(ex, path0, lines0, imports, statement):
    """文(statement)が述べる識別子の定義(定数・関数)を、ラベル=識別子名の断片として足す。呼出元 = _expand。
    WHY: _expand が参照先を引くのは主抜粋の本文の識別子だけで、文だけが挙げる名前(CSV_SPECS・_DATE_RE・onSubmit など)は
    src の行に現れないと抜粋に入らず、判定が根拠不足の unknown になった。引けない名前は黙って飛ばす(ただの語のことが多い)。"""
    for name in dict.fromkeys(m.group(1) for m in _IDENT_RE.finditer(statement)):
        if name in _STOP:
            continue
        target = imports.get(name)
        if target:
            tlines = _read_lines(target, ex.cache) or []
            i = _find_def(tlines, name)
            if i is not None:
                ex.add(target, i, _block_end(tlines, i, DEPTH2_BLOCK_MAX), name, DEPTH2_BLOCK_MAX)
            continue
        i = _find_def(lines0, name)
        if i is not None:
            ex.add(path0, i, _block_end(lines0, i, DEPTH2_BLOCK_MAX), name, DEPTH2_BLOCK_MAX)


_COND_LINE_RE = re.compile(r"&&|\s\?\s|\bif\s*\(|v-if")


def _add_display_conditions(ex, path0, lines0, statement):
    """主抜粋の要素を表示するかを決める条件の行(`{x && (` `{x ? (` `if (x)` `v-if`)を、1行ずつ「表示条件」の断片として足す。呼出元 = _expand。
    WHY: 「〜のときだけ表示する」型の文は、src が表示される要素の行を指し、条件の行は数行上にあって抜粋の外になっていた。
    (a) 主抜粋の直前15行のうち、主抜粋の先頭行よりインデントが浅い条件行を近い順に最大2行、
    (b) 文と主抜粋が使う識別子を含む条件行を、ファイル全体から最大2行足す。予算は ex.add が管理する。"""
    _p, s0, _e0, _l, body0 = ex.segs[0]
    first = lines0[s0 - 1] if 0 < s0 <= len(lines0) else ""
    indent0 = len(first) - len(first.lstrip())
    taken = 0
    for i in range(s0 - 2, max(s0 - 2 - 15, -1), -1):
        ln = lines0[i]
        if taken >= 2:
            break
        if _COND_LINE_RE.search(ln) and len(ln) - len(ln.lstrip()) < indent0:
            taken += ex.add(path0, i, i, "表示条件", 1)
    idents = {m.group(1) for m in _IDENT_RE.finditer(statement + "\n" + "\n".join(body0))} - set(_STOP)
    taken = 0
    for i, ln in enumerate(lines0):
        if taken >= 2:
            break
        if _COND_LINE_RE.search(ln) and any(re.search(rf"\b{re.escape(n)}\b", ln) for n in idents):
            taken += ex.add(path0, i, i, "表示条件", 1)


def _add_state_decls(ex, path0, lines0, statement):
    """主抜粋と文に出る識別子のうち、同じファイルで `[x, setX] = useState(...)` と宣言されている state の宣言行を、初期値まで足す。呼出元 = _expand。
    WHY(G14 run20): 「初期は〜が真(false)」のような文の根拠は state の初期値にあるが、src は使用箇所を指し、宣言は抜粋の外で unknown になった(13件)。
    複数行にまたがる初期値は _block_end で閉じる。state 名は決め打ちせず、識別子の一致だけで選ぶ。予算は ex.add が管理する。"""
    _p, _s0, _e0, _l, body0 = ex.segs[0]
    words = {m.group(1) for m in _IDENT_RE.finditer(statement + "\n" + "\n".join(body0))} - set(_STOP)
    decl = re.compile(r"^\s*(?:const|let|var)\s*\[\s*(\w+)\s*,\s*set\w+\s*\]\s*=\s*useState")
    for i, ln in enumerate(lines0):
        m = decl.match(ln)
        if m and m.group(1) in words:
            ex.add(path0, i, _block_end(lines0, i, DEPTH2_BLOCK_MAX), f"{m.group(1)} の宣言と初期値", DEPTH2_BLOCK_MAX)


_FUNC_START_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+\w+|(?:const|let|var)\s+\w+\s*=\s*(?:async\s*)?(?:\(|\w+\s*=>|function))")


def _add_setter_updates(ex, path0, lines0, statement):
    """主抜粋と文が参照する state の更新関数 setX を呼んでいる箇所のうち、hook の外のイベントハンドラ(`const onApply = () => {...}` 等)を足す。呼出元 = _expand。
    WHY(G14 run20): ボタン押下で applied を確定する処理(setApplied を呼ぶ関数)は、hook ではなく通常のハンドラにあり、(3)(4) が拾えず unknown になった(6件)。
    setX の呼出し行を含む関数ブロックを _block_end で閉じて足す(最大3件)。関数の形が見つからない行(JSX 内の onClick={() => setX(..)} など)はその1行だけ足す。"""
    _p, _s0, _e0, _l, body0 = ex.segs[0]
    words = {m.group(1) for m in _IDENT_RE.finditer(statement + "\n" + "\n".join(body0))} - set(_STOP)
    decls = []
    for ln in lines0:
        m = re.match(r"^\s*(?:const|let|var)\s*\[\s*(\w+)\s*,\s*(set\w+)\s*\]\s*=\s*useState", ln)
        if m:
            decls.append((m.group(1), m.group(2)))
    setters = [st for var, st in decls if var in words or st in words]
    # WHY(G14 run20 の取り残し): 「「表示」を押した時点の値で集計する」の根拠は、入力の state(from)を別の state(applied)へ写す setApplied({ from, to }) にある。
    # 文にも主抜粋にも applied は出ないので、参照する state の値を引数に渡している setter の呼出しも、更新する処理の候補に足す(後ろに回す)
    refvars = {var for var, st in decls if var in words or st in words}
    for var, st in decls:
        if st in setters:
            continue
        if any(re.search(rf"\b{re.escape(st)}\s*\(", ln) and any(re.search(rf"\b{re.escape(r)}\b", ln.split(st, 1)[1]) for r in refvars) for ln in lines0):
            setters.append(st)
    hooks = _hook_blocks(lines0)
    taken = 0
    for st in setters:
        call = re.compile(rf"\b{re.escape(st)}\s*\(")
        for i, ln in enumerate(lines0):
            if taken >= 3:
                return
            if not call.search(ln):
                continue
            # WHY(G14 run20 の取り残し): 読込中・エラーのように、JSX は state を読むだけで更新は useCallback / useEffect の中(load など)にある文は、
            # hook の中の setter 呼出しを含む hook ごと足す(以前は hook の中を飛ばし、更新の根拠が抜粋に入らず unknown が残った)
            hk = next((h for h in hooks if h[0] <= i <= h[1]), None)
            if hk is not None:
                taken += ex.add(path0, hk[0], hk[1], f"{st} を更新する処理(hook{(' ' + hk[2]) if hk[2] else ''})", BLOCK_MAX)
                continue
            indent = len(ln) - len(ln.lstrip())
            start = next((k for k in range(i, max(i - BLOCK_MAX, -1), -1)
                          if _FUNC_START_RE.match(lines0[k]) and len(lines0[k]) - len(lines0[k].lstrip()) < indent or (k == i and _FUNC_START_RE.match(lines0[k]))), None)
            if start is None:
                taken += ex.add(path0, i, i, f"{st} を更新する処理", 1)
            else:
                taken += ex.add(path0, start, _block_end(lines0, start, BLOCK_MAX), f"{st} を更新する処理", BLOCK_MAX)


def _add_promise_chain(ex, path0, lines0, statement):
    """主抜粋と足し済みの断片にある `.then(` の呼出し文と try/catch/finally を、連鎖の末尾まで足す。呼出元 = _expand。
    WHY(G14 run20): 「失敗したら〜を表示し、最後に読込中を解除する」型の文は .then/.catch/.finally(setRows 等を含む)が根拠だが、
    src の行はその一部で、連鎖の残りが抜粋の外になり unknown になった(5件)。連鎖の途中の行(`.catch(` で始まる行)から先頭の呼出し行へ戻り、
    _block_end(メソッドチェーンの続きを文の途中とみなす)で閉じる。本文はそのまま含めるので setRows 等も根拠に残る。上限は BLOCK_MAX、最大3件。"""
    taken = 0
    for path, s, _e, _label, body in list(ex.segs):
        if path != path0:
            continue
        for k, ln in enumerate(body):
            if taken >= 3:
                return
            if not (re.search(r"\.then\s*\(", ln) or re.match(r"\s*try\s*\{", ln)):
                continue
            j = s - 1 + k
            while j > 0 and lines0[j].lstrip().startswith("."):
                j -= 1
            end = _block_end(lines0, j, BLOCK_MAX)
            taken += ex.add(path0, j, end, "非同期処理(then/catch/finally)", BLOCK_MAX)
            # ex.add は既存の断片と重なる範囲の後ろ側を落とすので、断片の末尾より後ろに続く連鎖(.catch/.finally)は別に足す
            tail = max(s - 1 + len(body), j)
            if tail <= end:
                ex.add(path0, tail, end, "非同期処理(then/catch/finally)", BLOCK_MAX)


def _add_server_defs(ex, merged, project_root, statement):
    """文または主抜粋が名指しする API(action か path が本文に出るもの)の、サーバのソースの該当関数を足す。呼出元 = _expand。
    WHY(G14 run20): サーバ側の処理を述べる文(4件)は、[BE1] のような ID が文に無いと (1) が引けず、画面のソースしか抜粋に無かった。
    merged の api_spec と backend_processes の出典(_src_of)を、action・path の一致で選ぶ。action 名は決め打ちしない。出典の行から上へ def を探して関数の先頭にし、
    _block_end で閉じる(上限 BLOCK_MAX)。同じ範囲は ex.add の重複排除に任せる。最大2件。"""
    _p, _s0, _e0, _l, body0 = ex.segs[0]
    text = statement + "\n" + "\n".join(body0)
    taken = 0
    for sec in ("api_spec", "backend_processes"):
        for d in merged.get(sec) or []:
            if taken >= 2:
                return
            if not isinstance(d, dict):
                continue
            ep = d.get("endpoint") if isinstance(d.get("endpoint"), dict) else d
            action, path = str(ep.get("action") or ""), str(ep.get("path") or "")
            named = (action and re.search(rf"\b{re.escape(action)}\b", text)) or (path and path in text)
            s = _src_of(d)
            if not (named and s):
                continue
            p = _resolve(s["file"], project_root)
            plines = _read_lines(p, ex.cache) or []
            i0 = s["line_start"] - 1
            start = next((k for k in range(min(i0, len(plines) - 1), max(i0 - BLOCK_MAX, -1), -1)
                          if re.match(r"\s*(?:async\s+)?def\s", plines[k])), i0)
            if 0 <= start < len(plines):
                taken += ex.add(p, start, _block_end(plines, start, BLOCK_MAX), f"サーバ処理: {action or path}", BLOCK_MAX)


def _expand(ex, merged, project_root, statement, src):
    """主抜粋(ex の先頭の断片)の後ろに、判定に要る参照先を足す。呼出元 = _excerpt。
    足す順(優先度の高い順): (1) 文中の [BE1] 等の定義元 (2) 主抜粋が呼ぶ関数・使う定数の本体 (3) 主抜粋が呼ぶ state の更新関数の定義行
    (4) その state を依存配列に持つ hook と、その hook を起動する useEffect (5) 別ファイルから import した関数の本体(参照先の中の呼出しも)
    (1c) 主抜粋の要素の表示条件の行 (1c2) 参照する state の宣言と初期値 (1c3) その state を更新するイベントハンドラ
    (1c4) 非同期処理の then/catch/finally (1c5) 文が名指しする API のサーバ側の該当関数 (1d) 文中の識別子の定義(予算を (2)〜(5) が使い切る前に確保する)
    WHY: 参照先を決めるのは識別子の一致だけで、案件固有の名前・語は使わない。"""
    path0, s0, e0, _l, body0 = ex.segs[0]
    lines0 = _read_lines(path0, ex.cache) or []
    # (1) [BE1] 等: 文は設計書の ID で処理を指すが、ID の定義は設計書の側にしか無い。名前と呼び先(action)を見出しにし、元のソースの範囲を足す
    for tok in dict.fromkeys(_REF_TOKEN_RE.findall(statement)):
        for sec in ("backend_processes", "front_processes", "common_logic", "api_spec"):
            for d in merged.get(sec) or []:
                if not isinstance(d, dict) or tok not in (d.get("process_id"), d.get("no")):
                    continue
                ep = d.get("endpoint") if isinstance(d.get("endpoint"), dict) else {}
                head = " ".join(x for x in [f"[{tok}] =", str(d.get("name") or ""),
                                            f"({ep.get('method', '')} {ep.get('path', '')} action={ep.get('action', '')})" if ep else ""] if x)
                s = _src_of(d)
                if s:
                    p = _resolve(s["file"], project_root)
                    i0 = s["line_start"] - 1
                    ex.add(p, i0, min(int(s.get("line_end") or s["line_start"]) - 1, i0 + 24), head, 25)
                break
    # (1b) 文中の「…」で引いた画面の文言(4字以上)が同じファイルにあれば、その行の前後1行を足す。
    # WHY: 「0件のときは該当なしの旨を表示する(画面の文言: 「…」)」のような文は、src が操作の行を指し、文言の出る箇所は別の行にある
    for lit in dict.fromkeys(re.findall(r"「([^」]{4,60})」", statement)):
        lit = lit.rstrip("。")
        hit = next((i for i, ln in enumerate(lines0) if lit in ln), None)
        if hit is not None:
            ex.add(path0, max(hit - 1, 0), min(hit + 1, len(lines0) - 1), "文言の出現箇所", 3)
    # (1c) 表示条件の行
    _add_display_conditions(ex, path0, lines0, statement)
    # (1c2)〜(1c5) 状態の初期値・確定処理・then/catch/finally・サーバ処理。WHY: 照合の unknown の主因で、(2)〜(5) が予算を使い切る前に確保する
    _add_state_decls(ex, path0, lines0, statement)
    _add_setter_updates(ex, path0, lines0, statement)
    _add_promise_chain(ex, path0, lines0, statement)
    _add_server_defs(ex, merged, project_root, statement)
    calls, setters, rest = _idents(body0)
    base = path0.parent
    imports = _imports(lines0, base)
    # (1d) 文が名指しする識別子の定義
    _add_statement_defs(ex, path0, lines0, imports, statement)
    # WHY: 呼出しでなく値として渡す関数(onClick={exportCsv}・useEffect(load, ...))も、同じファイルに関数の定義があれば引く。
    # 定義の行が関数の形(=> / function / def / useCallback)の名前だけを足し、ただの変数名(rows など)は引かない
    def _fn_like(name):
        i = _find_def(lines0, name)
        return i is not None and bool(re.search(r"=>|function|\bdef\b|useCallback", lines0[i]))
    queue = [(0, k, n) for k, n in enumerate(calls + [r for r in rest if r.isupper() or r[0].isupper() or _fn_like(r)])]
    seen = set()

    def take_def(path, lines, name, cap):
        i = _find_def(lines, name)
        if i is None:
            return False
        return ex.add(path, i, _block_end(lines, i, cap), name, cap)

    # (2) 同じファイルの定義(呼ぶ関数・使う定数・JSX の部品)
    for _d, _k, name in queue:
        if name in seen:
            continue
        seen.add(name)
        if name in imports:
            continue
        take_def(path0, lines0, name, BLOCK_MAX)
    # (3)(4) state の更新関数: 定義行と、その state を依存配列に持つ hook(と、hook を起動する useEffect)
    hooks = _hook_blocks(lines0)
    for st in setters:
        var = _state_var(lines0, st)
        i = _find_def(lines0, st)
        if i is not None:
            ex.add(path0, i, i, f"{st} の定義", 1)
        picked = [h for h in hooks if (var and var in h[3]) or any(re.search(rf"\b{st}\s*\(", lines0[k]) for k in range(h[0], h[1] + 1))]
        for h in sorted(picked, key=lambda h: h[2] is None)[:3]:
            ex.add(path0, h[0], h[1], h[2] or "hook", BLOCK_MAX)  # (2) で足し済みなら何もしない
            if h[2]:
                for h2 in hooks:
                    if h2[2] is None and (h[2] in h2[3] or re.search(rf"\buse\w+\(\s*{re.escape(h[2])}\b", lines0[h2[0]])):
                        ex.add(path0, h2[0], h2[1], f"{h[2]} を起動する hook", 3)
    # (5) 別ファイルの関数: 主抜粋と、足した参照先の中で呼ばれる import 済みの名前。参照先の中の同じファイルの定数・関数は短く足す
    for path, _s, _e, label, body in list(ex.segs):
        plines = _read_lines(path, ex.cache) or []
        pimports = imports if path == path0 else _imports(plines, path.parent)
        pcalls, _ps, prest = _idents(body)
        for name in pcalls + [r for r in prest if r.isupper()]:
            target = pimports.get(name)
            if target:
                take_def(target, _read_lines(target, ex.cache) or [], name, DEPTH2_BLOCK_MAX if label else BLOCK_MAX)
            elif label is not None:
                # 参照先の中の同じファイルの関数・定数(SQL 定数など)。主抜粋の分は (2) で足し済み
                take_def(path, plines, name, DEPTH2_BLOCK_MAX)


def _excerpt(src, project_root, cache, merged=None, statement="", budget=None, files_out=None, segs_out=None):
    """src の行の前後 EXCERPT_CONTEXT 行(主抜粋)と、merged があれば文が指す参照先を足した抜粋を返す。読めなければ空文字。
    WHY: 出典の file は絶対パスと project_root 基準の相対が混ざるため、両方に対応する。cache はファイルごとの行の一覧。
    budget は全体の行数の予算(None なら EXCERPT_TOTAL)。build_claims が再判定用に EXCERPT_TOTAL_WIDE で呼び直す。
    files_out が list なら、主抜粋と参照先の断片が読んだファイルの絶対パス(str)を重複なく出現順に足す。
    WHY: 照合の依頼文(run_units.py)が、抜粋の代わりに各ファイルの全文を載せられるようにするため(G18)。
    segs_out が list なら、断片ごとに {file(絶対パス), line_start, line_end, label, lines} を出現順に足す。
    WHY: 大きすぎて全文を載せられないファイルだけ、依頼文がそのファイルぶんの断片を抜粋の節に出すため。他のファイルは全文で載せる。"""
    path = _resolve(src["file"], project_root)
    lines = _read_lines(path, cache)
    if lines is None:
        return ""
    ls = max(src["line_start"] - EXCERPT_CONTEXT, 1)
    le = min(int(src.get("line_end") or src["line_start"]) + EXCERPT_CONTEXT, len(lines))
    le = min(le, ls + EXCERPT_MAX - 1)
    ex = _Excerpt(cache, budget)
    ex.segs.append((path, ls, le, None, lines[ls - 1:le]))
    ex.total = le - ls + 1
    if merged is not None:
        try:
            _expand(ex, merged, project_root, statement, src)
        except (OSError, ValueError, RecursionError):
            pass  # 参照先の追加に失敗しても、主抜粋だけは返す
    if files_out is not None:
        for seg in ex.segs:
            if str(seg[0]) not in files_out:
                files_out.append(str(seg[0]))
    if segs_out is not None:
        for p, s, e, label, body in ex.segs:
            segs_out.append({"file": str(p), "line_start": s, "line_end": e, "label": label, "lines": list(body)})
    return ex.text()


# 設計書の番号の種類ごとの (一覧の欄, 番号の欄, 名前の欄の候補)。接頭辞は id_scheme.json の categories と同じ
_ID_SOURCES = {
    "BE": ("backend_processes", "process_id", ("name",)),
    "FR": ("front_processes", "process_id", ("name",)),
    "AP": ("api_spec", "no", ("name", "path")),
    "DB": ("db_operations", "id", ("dataset_name",)),
    "VL": ("validations", "no", ("field",)),
    "CA": ("calculations", "no", ("name",)),
    "CL": ("common_logic", "no", ("name",)),
    "MS": ("messages", "message_id", ("message",)),
    "EV": ("events", "no", ("name", "event_name")),
    "LG": ("common_logging_processes", "process_id", ("name",)),
    "IT": ("screen_layout.items", "no", ("item_name",)),
    "AR": ("screen_layout.areas", "area_no", ("area_name", "name")),
}
_ID_REF = re.compile(r"\[([A-Z]{2})(\d+)\]")


def _id_labels(merged):
    """設計書の番号(\"BE2\" など) -> 名前つきの説明 の辞書。バックエンド処理・API は action 名も添える。
    WHY: 照合の担当はソースしか見ないので、[BE2] だけでは何の処理か分からず unknown になった(run22 の AC004・AC011)。"""
    labels = {}
    for prefix, (path, idf, namef) in _ID_SOURCES.items():
        rows = merged
        for part in path.split("."):
            rows = rows.get(part) if isinstance(rows, dict) else None
        for r in rows or []:
            if not isinstance(r, dict) or not r.get(idf):
                continue
            name = next((str(r[k]).strip() for k in namef if r.get(k)), "")
            action = ""
            if prefix == "BE":
                action = str((r.get("endpoint") or {}).get("action") or "")
            elif prefix == "AP":
                action = str(r.get("action") or "")
            elif prefix == "DB":
                action = str((r.get("api_endpoint") or {}).get("action") or "")
            if prefix == "DB" and not name:
                name = str(r.get("operation_type") or "")
            bits = name + (f" = action {action}" if action and name else (f"action {action}" if action else ""))
            if bits:
                labels[str(r[idf])] = bits
    return labels


def _expand_ids(text, labels, road=""):
    """文の中の [BE2] などを [BE2](名前 = action 名) に置き換える。引けない番号はそのまま残して警告する。"""
    def sub(m):
        key = m.group(1) + m.group(2)
        if m.group(1) not in _ID_SOURCES:
            return m.group(0)
        if key in labels:
            return f"[{key}]({labels[key]})"
        print(f"[WARN] 設計書の番号 [{key}] の名前を中間 JSON から引けない: {road}", file=sys.stderr)
        return m.group(0)
    return _ID_REF.sub(sub, text)


def build_claims(merged, project_root, prior_ok, units, react=None):
    """判定項目の一覧を作る。prior_ok は前回 ok だった (text, src_excerpt) の集合で、そのまま除く（キャッシュ）。
    呼出元 = main() と _selftest()。owner は run_units.owner_of で欄の道筋から決める（引けなければ None）。
    react は react 下書き(出典の無い検査の出典を behaviors から引く。無ければ None)。
    抜粋が空の項目は作らない(判定が必ず unknown になるため。除いた項目は stderr に出す)。"""
    cache = {}
    claims = []
    labels = _id_labels(merged)
    for kind, road, text, src, subject in _candidates(merged, react):
        excerpt = _excerpt(src, project_root, cache, merged, text) if src else ""  # 抜粋は元の文(番号つき)で引く
        if not excerpt:
            print(f"[警告] 抜粋が空のため判定項目から除いた: {road}", file=sys.stderr)
            continue
        text_orig = text
        text = _expand_ids(text, labels, road)
        if (text, excerpt) in prior_ok:
            continue
        # 再判定(unknown の見直し)用に、予算を広げた抜粋も添える。キャッシュ照合は上の excerpt(従来の予算)で行うので内容を変えない
        src_files = []  # 抜粋が読んだファイル(主+参照先)。再判定の抜粋と同じ範囲で集める
        src_segs = []  # 断片ごとの出典(ファイル・行範囲・ラベル・行)。全文を載せられないファイルの抜粋の節に使う
        excerpt_wide = _excerpt(src, project_root, cache, merged, text_orig, EXCERPT_TOTAL_WIDE, src_files, src_segs)
        claims.append({
            "id": f"AC{len(claims) + 1:03d}",
            "kind": kind,
            "owner": owner_of(f"{road} {text}", units),
            "subject": subject,
            "text": text,
            "src_excerpt": excerpt,
            "src_excerpt_wide": excerpt_wide,
            "src_files": src_files,
            "src_segs": src_segs,
            "src_ref": ({"file": src["file"], "line_start": src["line_start"], "line_end": src.get("line_end")} if src else None),
        })
    return claims


def load_prior_ok(feature):
    """前回の判定結果（_audit_results.json）から ok の (text, src_excerpt) の集合を返す。無ければ空集合。
    WHY: 前回 ok のものを書き直すと、判定の Haiku が同じ項目を毎回読み直すため。結果が壊れていれば警告して空にする。"""
    p = INTERMEDIATE_DIR / f"{feature}_audit_results.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[警告] 前回の判定結果を読めないので、キャッシュは使わない: {p} ({e})", file=sys.stderr)
        return set()
    rows = data if isinstance(data, list) else (data.get("items") or [])
    return {(str(r.get("text") or ""), str(r.get("src_excerpt") or ""))
            for r in rows if isinstance(r, dict) and (r.get("ok") is True or r.get("verdict") == "ok")}


def _selftest():
    """判定項目の形（抜粋・owner・主語・キャッシュの除外）と、参照先の追加（関数本体・state・hook・[BE1]・予算）を一時ファイルで確かめる。"""
    with tempfile.TemporaryDirectory() as tmp:
        src_file = Path(tmp) / "a.tsx"
        src_file.write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n", encoding="utf-8")
        s = {"file": str(src_file), "line_start": 10, "line_end": 11}
        merged = {
            "screen_layout": {"items": [{"item_name": "保存", "remarks": "編集モード時のみ表示", "src": [s]}, {"remarks": "", "src": [s]}]},
            "front_processes": [{"steps": [{"description": "保存する", "src": [s]}]}],
            "common_logic": [],
            "validations": [{"rule": "必須", "src": [s]}],
            "calculations": [],
        }
        units = load_units()
        claims = build_claims(merged, "", set(), units)
        if len(claims) != 3:
            print(f"[selftest NG] 判定項目が 3 件でない（空の備考は除く）: {len(claims)}")
            return 0
        if claims[0]["src_excerpt"].splitlines() != [f"line{i}" for i in range(8, 14)]:
            print(f"[selftest NG] 抜粋の行が前後2行の範囲でない: {claims[0]['src_excerpt']!r}")
            return 0
        if not all("owner" in c for c in claims):
            print("[selftest NG] owner の欄が無い項目がある")
            return 0
        if "保存" not in claims[0]["subject"]:
            print(f"[selftest NG] 主語に画面項目名が入っていない: {claims[0]['subject']!r}")
            return 0
        prior = {(c["text"], c["src_excerpt"]) for c in claims if c["text"] == "保存する"}
        if len(build_claims(merged, "", prior, units)) != 2:
            print("[selftest NG] 前回 ok の項目が書き出しから除かれていない")
            return 0
        # 参照先: onChange の行(setShop)の抜粋に、state の定義・依存配列に持つ hook・hook を起動する effect・
        # 呼ぶ関数の本体・import 先の関数・[BE1] の定義元の範囲が入る。全体は 80 行以内
        api = Path(tmp) / "api.ts"
        api.write_text("export async function fetchRows(p) {\n  return call('list_rows', p);\n}\n", encoding="utf-8")
        be = Path(tmp) / "b.py"
        be.write_text('LIVE = "x IS NULL"\n\ndef handler(req):\n    sql = LIVE\n    return sql\n', encoding="utf-8")
        ui = Path(tmp) / "ui.tsx"
        ui.write_text("\n".join([
            'import { fetchRows } from "./api";',
            "function helper(v) {",
            "  return v + 1;",
            "}",
            "export default function Page() {",
            "  const [shop, setShop] = useState(0);",
            "  const load = useCallback(() => {",
            "    fetchRows({ shop }).then((d) => setRows(d));",
            "  }, [shop]);",
            "  useEffect(load, [load]);",
            "  // pad1",
            "  // pad2",
            "  // pad3",
            "  // pad4",
            "  return <select onChange={(e) => setShop(helper(e.target.value))} />;",
            "}",
        ]) + "\n", encoding="utf-8")
        merged2 = {"backend_processes": [{"process_id": "BE1", "name": "行の取得", "endpoint": {"method": "POST", "path": "/api/x", "action": "list_rows"},
                                           "src": [{"file": str(be), "line_start": 3, "line_end": 5}]}]}
        ex = _excerpt({"file": str(ui), "line_start": 15, "line_end": 15}, "", {}, merged2, "[BE1] のレスポンスを画面に写す")
        for need in ("setShop の定義", "load", "を起動する hook", "helper", "fetchRows", "[BE1] = 行の取得", "def handler", "LIVE ="):
            if need not in ex:
                print(f"[selftest NG] 参照先に {need!r} が入っていない:\n{ex}")
                return 0
        ex2 = _excerpt({"file": str(ui), "line_start": 2, "line_end": 2}, "", {}, merged2, "0件のときは「useCallback」を表示する")
        if "文言の出現箇所" not in ex2:
            print(f"[selftest NG] 文中の「…」の文言の出現箇所が足されていない:\n{ex2}")
            return 0
        if len(ex.splitlines()) > EXCERPT_TOTAL:
            print(f"[selftest NG] 抜粋が {EXCERPT_TOTAL} 行を超えた: {len(ex.splitlines())}")
            return 0
        # 出典の無い VL は、文の英数字の語と request_params の name が一致する API の src を使う
        m3 = {"validations": [{"no": "VL1", "field": "x", "rule": "形式", "applied_when": "API が from を受け取ったとき"}],
              "api_spec": [{"request_params": [{"name": "from"}], "src": [{"file": str(be), "line_start": 3, "line_end": 5}]}]}
        c3 = build_claims(m3, "", set(), units)
        if "def handler" not in c3[0]["src_excerpt"]:
            print(f"[selftest NG] 出典の無い VL に API の出典が当たっていない: {c3[0]['src_excerpt']!r}")
            return 0
        # 画面の処理の検査(表示切替・活性切替)で出典が無いものは、react 下書きの behaviors から引く(run14 の VL5〜VL15)。
        # VL1(「新」の印・文言で結ぶ)・VL2(エラー・変数で結ぶ)・VL3(対象項目の出典で結ぶ)・VL4(点数が付かない2件を並び順で結ぶ)・VL5(対象がエリアで behaviors が無い)
        vui = Path(tmp) / "v.tsx"
        vui.write_text("\n".join(f"v{i}" for i in range(1, 31)) + "\n", encoding="utf-8")

        def sp(a, b=None):
            return [{"file": str(vui), "line_start": a, "line_end": b or a}]
        m4 = {"screen_layout": {"items": [{"item_name": "CSV出力", "item_type": "Button", "src": sp(20)},
                                          {"item_name": "左の項目", "item_type": "Label", "area_ref": "AR1", "src": sp(3, 4)},
                                          {"item_name": "右の項目", "item_type": "Label", "area_ref": "AR1", "src": sp(8, 9)}],
                                "areas": [{"area_no": "AR1", "area_name": "表エリア"}]},
              "validations": [
                  {"no": "VL1", "field": "表エリア", "kind": "表示切替", "rule": "行の見出しに「新」の印を付ける", "applied_when": "追加の行のとき"},
                  {"no": "VL2", "field": "表エリア", "kind": "表示切替", "rule": "取得に失敗したとき、エラー内容を画面に出す", "applied_when": "error に値が入ったとき"},
                  {"no": "VL3", "field": "CSV出力", "kind": "活性切替", "rule": "行が0件のとき押せない", "applied_when": "行の件数だけで決まる"},
                  {"no": "VL4", "field": "表エリア", "kind": "表示切替", "rule": "1つ目の何か", "applied_when": "a"},
                  {"no": "VL5", "field": "表エリア", "kind": "入力検査", "rule": "何かを調べる", "applied_when": "b"}],
              "front_processes": [], "common_logic": [], "calculations": []}
        react = {"behaviors": [
            {"id": "BH1", "kind": "display_toggle", "cond": "s.added", "target": "新", "src": sp(11)},
            {"id": "BH2", "kind": "display_toggle", "cond": "error", "target": "div", "src": sp(12)},
            {"id": "BH3", "kind": "display_toggle", "cond": "x", "target": "y", "src": sp(13)},
            {"id": "BH4", "kind": "enable_toggle", "cond": "!rows.length", "target": "CSV出力", "src": sp(20)}]}
        c4 = {c["subject"].split("(")[0]: c for c in build_claims(m4, "", set(), units, react)}
        want = {"検査 VL1": "v11", "検査 VL2": "v12", "検査 VL3": "v20", "検査 VL4": "v13"}
        for k, line in want.items():
            if line not in c4[k]["src_excerpt"].splitlines():
                print(f"[selftest NG] 出典の無い VL {k} に behaviors の出典 {line} が当たっていない: {c4[k]['src_excerpt']!r}")
                return 0
        # VL5 は同じ kind(入力検査)の behaviors が無い → 対象(表エリア)の項目の範囲(3〜9 行)
        if "v3" not in c4["検査 VL5"]["src_excerpt"].splitlines() or "v9" not in c4["検査 VL5"]["src_excerpt"].splitlines():
            print(f"[selftest NG] 出典の無い VL に対象エリアの出典が当たっていない: {c4['検査 VL5']['src_excerpt']!r}")
            return 0
        # behaviors が無い(react=None)でも、対象の画面項目・エリアの出典で抜粋は空にならない
        if any(not c["src_excerpt"] for c in build_claims(m4, "", set(), units)):
            print("[selftest NG] react が無いときに抜粋が空の VL が残った")
            return 0
        # 計算の備考・画面項目の備考で src が無いものは、react 下書きの同じ名前(calc_candidates・jsx_elements の label)の出典を使う
        m6 = {"screen_layout": {"items": [{"item_name": "合計", "item_type": "Label", "remarks": "全行の合算"}]},
              "calculations": [{"no": "CA1", "name": "差引", "remarks": "小計から返品を引く"}]}
        r6 = {"calc_candidates": [{"label": "差引", "src": sp(5)}], "jsx_elements": [{"label": "合計", "src": sp(15)}]}
        c6 = {c["kind"]: c for c in build_claims(m6, "", set(), units, r6)}
        if "v5" not in c6["calc_remarks"]["src_excerpt"].splitlines() or "v15" not in c6["remarks"]["src_excerpt"].splitlines():
            print(f"[selftest NG] 出典の無い計算・画面項目の備考に react 下書きの出典が当たっていない: {c6}")
            return 0
        # 並び順の結びは、点数で決めた組の順序が食い違うときは使わない(別の処理の行を渡さない)
        m5 = dict(m4, validations=[{"no": "VL1", "field": "表エリア", "kind": "表示切替", "rule": "「y」を出す", "applied_when": "a"},
                                   {"no": "VL2", "field": "表エリア", "kind": "表示切替", "rule": "「新」を出す", "applied_when": "b"},
                                   {"no": "VL3", "field": "表エリア", "kind": "表示切替", "rule": "何か", "applied_when": "c"}])
        got = _vl_behavior_srcs(m5, react, [0, 1, 2])
        if got.get(0, {}).get("line_start") != 13 or got.get(1, {}).get("line_start") != 11 or 2 in got:
            print(f"[selftest NG] 点数で決めた組(VL1→行13・VL2→行11)の順序が食い違うのに、並び順で残りを結んだ: {got}")
            return 0
        # 文が名指しする定数・関数の定義(CSV_SPECS・_DATE_RE・onSubmit)と、表示条件の行({error && ()が、src が表示要素の行だけを指していても抜粋に入る
        gx = Path(tmp) / "gx.tsx"
        gx.write_text("\n".join([
            "const CSV_SPECS = [", "  { key: 'a' },", "  { key: 'b' },", "];",
            "const _DATE_RE = /^[0-9]+$/;",
            "const onSubmit = (e) => {", "  e.preventDefault();", "};",
            "function Page() {", "  return (", "    <div>", "      {error && (",
            "        <section>", "          <h1>a</h1>", "          <h2>b</h2>", "          <h3>c</h3>",
            "          <p>shown</p>", "        </section>", "      )}", "    </div>"]) + "\n", encoding="utf-8")
        gx_ex = _excerpt({"file": str(gx), "line_start": 16, "line_end": 16}, "", {}, {},
                         "CSV_SPECS・_DATE_RE・onSubmit・error のとき表示する")
        for need in ("const CSV_SPECS = [", "const _DATE_RE", "const onSubmit", "{error && ("):
            if need not in gx_ex:
                print(f"[selftest NG] 文が名指しする定義・表示条件の行 {need!r} が抜粋に入らない: {gx_ex!r}")
                return 0
        # 出典を引けない項目(対象の画面項目・領域・react 下書きのどれにも当たらない VL)は、抜粋が空になるので判定項目にしない
        m7 = {"screen_layout": {"items": []}, "front_processes": [], "common_logic": [], "calculations": [],
              "validations": [{"no": "VL1", "field": "どこにも無い項目", "kind": "入力検査", "rule": "何かを調べる", "applied_when": "b"}]}
        if build_claims(m7, "", set(), units):
            print(f"[selftest NG] 出典を引けない項目が判定項目に残った: {build_claims(m7, '', set(), units)}")
            return 0
        # G14: 文が参照する state の宣言と初期値・押下ハンドラ(setApplied)・then/catch/finally の末尾・サーバ関数が、src の行(連鎖の途中)だけを指していても抜粋に入る。
        # src_excerpt_wide は src_excerpt 以上の長さで、src_excerpt の内容(キャッシュ照合の鍵)は広げた予算の影響を受けない
        ap = Path(tmp) / "ap.tsx"
        ap.write_text("\n".join([
            "const [showDead, setShowDead] = useState(false);", "const [applied, setApplied] = useState(0);", "const [rows, setRows] = useState([]);",
            "const onApply = () => {", "  setApplied(5);", "};",
            "// pad7", "// pad8", "// pad9", "// pad10", "// pad11",
            "function reload() {", "  fetch(\"/api/z\")", "    .then((d) => setRows(d))", "    .catch(() => setRows([]))", "    .finally(() => doneMarker());", "}"]) + "\n", encoding="utf-8")
        sv = Path(tmp) / "sv.py"
        sv.write_text("def run_z(req):\n    x = 1\n    return x\n", encoding="utf-8")
        stmt_g14 = "showDead が初期は偽で、applied を確定し、失敗したら rows を空にする"
        merged_g14 = {"backend_processes": [{"process_id": "BE9", "name": "z", "endpoint": {"method": "POST", "path": "/api/z", "action": "run_z"},
                                             "src": [{"file": str(sv), "line_start": 2, "line_end": 2}]}]}
        g14_ex = _excerpt({"file": str(ap), "line_start": 13, "line_end": 13}, "", {}, merged_g14, stmt_g14)
        for need in ("showDead の宣言と初期値", "useState(false)", "setApplied を更新する処理", "setApplied(5)", "doneMarker", "サーバ処理: run_z", "def run_z"):
            if need not in g14_ex:
                print(f"[selftest NG] G14 の追加断片 {need!r} が抜粋に入らない: {g14_ex!r}")
                return 0
        g14_claims = build_claims({"front_processes": [{"steps": [{"description": stmt_g14, "src": [{"file": str(ap), "line_start": 13, "line_end": 13}]}]}],
                                   "backend_processes": merged_g14["backend_processes"]}, "", set(), units)
        g14_c = next((c for c in g14_claims if c["text"] == stmt_g14), None)
        if g14_c is None or len(g14_c["src_excerpt_wide"]) < len(g14_c["src_excerpt"]) or "showDead の宣言と初期値" not in g14_c["src_excerpt_wide"]:
            print(f"[selftest NG] src_excerpt_wide が src_excerpt 以上の長さで抜粋の中身を含んでいない: {g14_c}")
            return 0
        if g14_c["src_excerpt"] != _excerpt({"file": str(ap), "line_start": 13, "line_end": 13}, "", {}, merged_g14, stmt_g14):
            print("[selftest NG] src_excerpt が従来の予算の抜粋(_excerpt の既定)と変わった")
            return 0
        # G18: src_files は主抜粋のファイルと参照先(サーバ処理)のファイルを重複なく出現順に持ち、抜粋の中身は変えない
        g14_names = [Path(f).name for f in g14_c["src_files"]]
        if g14_names != [ap.name, sv.name]:
            print(f"[selftest NG] src_files が主ファイルと参照先ファイルを重複なく持たない: {g14_c['src_files']}")
            return 0
        # G19: src_segs は断片ごとの出典(ファイル絶対パス・行範囲・ラベル・行)を出現順に持ち、主抜粋はラベル無し、参照先(サーバ処理)はラベルと行を持つ
        # (大きすぎて全文を載せられないファイルだけ、依頼文がこの断片を抜粋の節にする)
        g19_segs = g14_c.get("src_segs") or []
        g19_sv = [s for s in g19_segs if s["file"] == str(sv)]
        if (not g19_segs or g19_segs[0]["file"] != str(ap) or g19_segs[0]["label"] is not None or {s["file"] for s in g19_segs} != set(g14_c["src_files"])
                or not g19_sv or not g19_sv[0]["label"] or not any("def run_z" in ln for s in g19_sv for ln in s["lines"])
                or any(s["line_end"] < s["line_start"] or not s["lines"] for s in g19_segs)):
            print(f"[selftest NG] src_segs が断片ごとの出典(ファイル・行範囲・ラベル・行)を持たない: {g19_segs}")
            return 0
        g19_s = []
        _excerpt({"file": str(ap), "line_start": 13, "line_end": 13}, "", {}, merged_g14, stmt_g14, EXCERPT_TOTAL_WIDE, None, g19_s)
        if g19_s != g19_segs:
            print("[selftest NG] segs_out の断片が、build_claims の src_segs と合わない")
            return 0
        # G14(取り残し): (a) 読込中の表示は state を読むだけで、更新(setLoading)は useCallback の hook の中にある
        # (b) 入力の state(from)を別の state(applied)へ写す setApplied({ from }) は、文にも主抜粋にも applied が出なくても抜粋に入る
        hk_src = [
            "const [loading, setLoading] = useState(false);", "const [from, setFrom] = useState('a');", "const [applied, setApplied] = useState({ from: 'a' });",
            "const load = useCallback(() => {", "  setLoading(true);", "  fetchX().finally(() => setLoading(false));", "}, [applied]);",
            "// pad1", "// pad2", "// pad3", "// pad4", "// pad5", "// pad6", "// pad7",
            "const view = (", "  <div>", "    {loading && <span>wait</span>}", "    <input value={from} onChange={(e) => setFrom(e.target.value)} />", "  </div>", ");",
            "const onSubmit = (e) => {", "  e.preventDefault();", "  setApplied({ from });", "};"]
        hk = Path(tmp) / "hk.tsx"
        hk.write_text("\n".join(hk_src) + "\n", encoding="utf-8")
        ex_a = _excerpt({"file": str(hk), "line_start": hk_src.index("    {loading && <span>wait</span>}") + 1, "line_end": hk_src.index("    {loading && <span>wait</span>}") + 1}, "", {}, {}, "取得が終わるまでの間だけ表示する")
        for need in ("setLoading を更新する処理(hook load)", "setLoading(true)", ".finally(() => setLoading(false))"):
            if need not in ex_a:
                print(f"[selftest NG] G14 hook の中の state 更新 {need!r} が抜粋に入らない: {ex_a!r}")
                return 1
        ex_b = _excerpt({"file": str(hk), "line_start": hk_src.index("    <input value={from} onChange={(e) => setFrom(e.target.value)} />") + 1,
                         "line_end": hk_src.index("    <input value={from} onChange={(e) => setFrom(e.target.value)} />") + 1}, "", {}, {}, "押した時点の値で集計する")
        for need in ("setApplied を更新する処理", "setApplied({ from })"):
            if need not in ex_b:
                print(f"[selftest NG] G14 別の state へ写す更新 {need!r} が抜粋に入らない: {ex_b!r}")
                return 1
    # 設計書の番号の名前つき置き換え: 引ける番号は名前と action を添え、引けない番号はそのまま残す
    lab = _id_labels({"backend_processes": [{"process_id": "BE2", "name": "POS会計レポートの取得",
                                           "endpoint": {"action": "list_pos_account_report"}}],
                      "calculations": [{"no": "CA1", "name": "差引"}]})
    got = _expand_ids("[BE2] のレスポンスを [CA1] に写す。[BE9] も使う", lab)
    want = "[BE2](POS会計レポートの取得 = action list_pos_account_report) のレスポンスを [CA1](差引) に写す。[BE9] も使う"
    if got != want:
        print(f"[selftest NG] 設計書の番号の置き換えが違う: {got!r}")
        return 1
    print("[OK] selftest: 判定項目の形（抜粋・owner・主語・キャッシュの除外）と参照先の追加・設計書の番号の名前つき置き換え")
    return 0


def main(argv):
    if "--selftest" in argv:
        # WHY: _selftest は NG を表示しても return 0 する枝が多く、そのままでは失敗が exit 0 に埋もれる。[selftest NG] が出たら exit 1 にする
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _selftest()
        sys.stdout.write(buf.getvalue())
        return 1 if rc or "[selftest NG]" in buf.getvalue() else 0
    if len(argv) < 2:
        print("使い方: build_audit_claims.py <機能名> | --selftest", file=sys.stderr)
        return 0
    feature = argv[1]
    src_json = INTERMEDIATE_DIR / f"{feature}.json"
    if not src_json.exists():
        print(f"[警告] 中間JSON {src_json} が無いので、判定項目は書かない", file=sys.stderr)
        return 0
    merged = json.loads(src_json.read_text(encoding="utf-8"))
    try:
        sc = load_screen_config(feature)
    except FileNotFoundError:
        sc = {}
    # react 下書き(無ければ None)。出典の無い検査(VL)の出典を behaviors から引く
    try:
        react = json.loads((INTERMEDIATE_DIR / f"{feature}_react_draft.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        react = None
    claims = build_claims(merged, sc.get("project_root") or "", load_prior_ok(feature), load_units(), react)
    out = INTERMEDIATE_DIR / f"{feature}_audit_claims.json"
    out.write_text(json.dumps(claims, ensure_ascii=False, indent=1), encoding="utf-8")
    # WHY: 抜粋が空の項目は build_claims が除く(判定が必ず unknown になるため)ので、ここでは件数と owner 未定の件数だけを出す
    print(f"判定項目 {len(claims)} 件を {out} に書いた（owner が None の件数 {sum(1 for c in claims if c['owner'] is None)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
