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


def _candidates(merged):
    """判定項目の候補 (kind, 欄の道筋, 記述の本文, src, 主語)。空の記述は候補にしない。
    WHY: 種類は「意味の判定が要る記述」に限る。型・件数のような機械で決まる欄は verify_* 系に任せる。
    主語(subject)は、文だけでは何の記述か分からない項目(「点数と金額の2列。左段」)の対象を判定者に伝える。"""
    out = []
    for i, it in enumerate((merged.get("screen_layout") or {}).get("items") or []):
        if str(it.get("remarks") or "").strip():
            subj = f"画面項目「{it.get('item_name') or ''}」({it.get('item_type') or ''})の備考"
            out.append(("remarks", f"screen_layout.items[{i}].remarks", it["remarks"], _src_of(it), subj))
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
    for i, v in enumerate(merged.get("validations") or []):
        if str(v.get("rule") or "").strip():
            out.append(("vl", f"validations[{i}].rule", v["rule"], _src_of(v) or _src_from_api(merged, v),
                        f"検査 {v.get('no') or ''}(対象: {v.get('field') or ''})の rule"))
    for i, c in enumerate(merged.get("calculations") or []):
        if str(c.get("remarks") or "").strip():
            out.append(("calc_remarks", f"calculations[{i}].remarks", c["remarks"], _src_of(c),
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
    """1項目の抜粋の組み立て。主抜粋と参照先の断片を、全体 EXCERPT_TOTAL 行の予算で並べる。"""

    def __init__(self, cache):
        self.cache = cache
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
        room = EXCERPT_TOTAL - self.total - (1 if label else 0)
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


def _expand(ex, merged, project_root, statement, src):
    """主抜粋(ex の先頭の断片)の後ろに、判定に要る参照先を足す。呼出元 = _excerpt。
    足す順(優先度の高い順): (1) 文中の [BE1] 等の定義元 (2) 主抜粋が呼ぶ関数・使う定数の本体 (3) 主抜粋が呼ぶ state の更新関数の定義行
    (4) その state を依存配列に持つ hook と、その hook を起動する useEffect (5) 別ファイルから import した関数の本体(参照先の中の呼出しも)
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
    calls, setters, rest = _idents(body0)
    base = path0.parent
    imports = _imports(lines0, base)
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


def _excerpt(src, project_root, cache, merged=None, statement=""):
    """src の行の前後 EXCERPT_CONTEXT 行(主抜粋)と、merged があれば文が指す参照先を足した抜粋を返す。読めなければ空文字。
    WHY: 出典の file は絶対パスと project_root 基準の相対が混ざるため、両方に対応する。cache はファイルごとの行の一覧。"""
    path = _resolve(src["file"], project_root)
    lines = _read_lines(path, cache)
    if lines is None:
        return ""
    ls = max(src["line_start"] - EXCERPT_CONTEXT, 1)
    le = min(int(src.get("line_end") or src["line_start"]) + EXCERPT_CONTEXT, len(lines))
    le = min(le, ls + EXCERPT_MAX - 1)
    ex = _Excerpt(cache)
    ex.segs.append((path, ls, le, None, lines[ls - 1:le]))
    ex.total = le - ls + 1
    if merged is not None:
        try:
            _expand(ex, merged, project_root, statement, src)
        except (OSError, ValueError, RecursionError):
            pass  # 参照先の追加に失敗しても、主抜粋だけは返す
    return ex.text()


def build_claims(merged, project_root, prior_ok, units):
    """判定項目の一覧を作る。prior_ok は前回 ok だった (text, src_excerpt) の集合で、そのまま除く（キャッシュ）。
    呼出元 = main() と _selftest()。owner は run_units.owner_of で欄の道筋から決める（引けなければ None）。"""
    cache = {}
    claims = []
    for kind, road, text, src, subject in _candidates(merged):
        excerpt = _excerpt(src, project_root, cache, merged, text) if src else ""
        if (text, excerpt) in prior_ok:
            continue
        claims.append({
            "id": f"AC{len(claims) + 1:03d}",
            "kind": kind,
            "owner": owner_of(f"{road} {text}", units),
            "subject": subject,
            "text": text,
            "src_excerpt": excerpt,
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
    print("[OK] selftest: 判定項目の形（抜粋・owner・主語・キャッシュの除外）と参照先の追加")
    return 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
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
    claims = build_claims(merged, sc.get("project_root") or "", load_prior_ok(feature), load_units())
    out = INTERMEDIATE_DIR / f"{feature}_audit_claims.json"
    out.write_text(json.dumps(claims, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"判定項目 {len(claims)} 件を {out} に書いた（owner が None の件数 {sum(1 for c in claims if c['owner'] is None)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
