"""API の description が選択肢の一部だけを書いているとき、その選択肢の引数の行に、全部の選択肢と既定値を足す(第19回)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=140(応答の写し 110・写し 120 の後、備考 150 の前)。
WHY(run15・run16): 画面の select(集計単位: レジ別・店舗別)の選択肢のうち、S2 が api_spec[].description に片方(「店舗・レジ別に期間集計する」の
  レジ別)だけを書き、検査 verify_vs_source.check_enum_mentions が [列挙] の ERROR で差し戻した。選択肢の全値と既定値は
  TSX の option 要素と useState の初期値の事実で、下書き(react_draft.jsx_elements[].option_texts・default_text)に出る。
  description は30字以内(verify_partials.check_s2_overview)で、選択肢と既定値を全部は足せない。足す先は、その選択肢を受ける引数の行
  (api_spec[].request_params[] の name が select の bind した state と同じ行)の description とする。引数の行に全選択肢があれば、
  検査(check_enum_mentions)は description が片方だけでも通す。
条件: 検査と同じ。選択肢のうち一部だけが description に出て、既定を表す語(既定・初期・省略…)が無く、全選択肢を書いた引数の行も無いとき。
  全部出ている・1つも出ていない description には触らない(推測で足さない)。引数の行が決まらないとき(name が bind と合わない)は何もしない。
  足す文は「<項目名>の選択肢は<全部>(既定は<既定値>)」。既定値が決まらないとき(下書きに default_text が無い)は既定の括弧を付けない。
"""
import json
import re
import sys
from pathlib import Path

ORDER = 140

# WHY: verify_vs_source._DEFAULT_WORDS と同じ語。この語が description にあれば、検査は既定値を添えたものとして通す
_DEFAULT_WORDS = re.compile(r"既定|初期|省略|指定が無い|指定がない|指定しない")


def _drafts(directory, suffix):
    """<機能名>_<suffix>.json を全部読む。読めないファイルは無いものとして飛ばす(_compose_error_example と同じ読み方)。"""
    out = []
    for p in sorted(Path(directory).glob(f"*_{suffix}.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def sentence(el):
    """select 要素(下書きの jsx_elements の1件)から、足す文を作る。選択肢が2つ未満なら None。"""
    texts = [t for t in el.get("option_texts") or [] if isinstance(t, str) and t]
    if len(texts) < 2:
        return None
    label = str(el.get("label") or "").strip() or "選択項目"
    default = el.get("default_text")
    tail = f"(既定は{default})" if isinstance(default, str) and default else ""
    return f"{label}の選択肢は{'・'.join(texts)}{tail}"


def _norm(name):
    """引数名と state 名の突き合わせ用の形。大文字小文字と _ を無視する(show_dead と showDead)。"""
    return re.sub(r"[_\-]", "", str(name or "")).lower()


def _param_text(p):
    """検査 check_enum_mentions が見る、引数の行の文字列(name・description・validation・default)。"""
    return " ".join(str(p.get(k) or "") for k in ("name", "description", "validation", "default"))


def _covers(p, texts, values):
    """引数の行 p が、全選択肢(表示名の語、または value の語)を書いているか。check_enum_mentions と同じ条件。"""
    pt = _param_text(p)
    return all(t in pt for t in texts) or bool(values and all(len(v) >= 3 and v in pt for v in values))


def fill(out, log):
    """一部の選択肢だけを書いた description の API で、選択肢を受ける引数の行の description に全選択肢と既定値の文を足す(足すのは1回だけ。足した後は引数の行が全部を書くので冪等)。"""
    from _config_loader import INTERMEDIATE_DIR
    apis = [a for a in out.get("api_spec") or [] if isinstance(a, dict)]
    if not apis:
        return
    for react in _drafts(INTERMEDIATE_DIR, "react_draft"):
        for el in react.get("jsx_elements") or []:
            texts = [t for t in el.get("option_texts") or [] if isinstance(t, str) and t]
            add = sentence(el)
            if add is None:
                continue
            values = [str(o.get("value")) for o in el.get("options") or [] if isinstance(o, dict) and o.get("value") not in ("", None)]
            for a in apis:
                desc = str(a.get("description") or "")
                found = [t for t in texts if t in desc]
                params = [p for p in a.get("request_params") or [] if isinstance(p, dict)]
                if not found or len(found) == len(texts) or _DEFAULT_WORDS.search(desc) or any(_covers(p, texts, values) for p in params):
                    continue
                target = [p for p in params if el.get("bind") and _norm(p.get("name")) == _norm(el["bind"])]
                if len(target) != 1:
                    continue
                p = target[0]
                old = str(p.get("description") or "")
                new = (old.rstrip("。 ") + "。" if old else "") + add
                log.append(f"[列挙] api_spec[{a.get('action')}].request_params[{p.get('name')}].description に選択肢の全部と既定値を足す: {old!r} -> {new!r}")
                p["description"] = new


def _selftest():
    import tempfile
    import _config_loader

    sel = {"id": "JX10", "tag": "select", "label": "集計単位", "bind": "group",
           "options": [{"value": "register", "text": "レジ別（現行と同じ）"}, {"value": "shop", "text": "店舗別（レジ合算）"}],
           "option_texts": ["レジ別", "店舗別"], "default_value": "register", "default_text": "レジ別"}
    react = {"jsx_elements": [sel, {"id": "JX07", "tag": "select", "label": "店舗", "bind": "shopId"}]}

    def mk(desc_a="POS会計の締め金額を店舗・レジ別に期間集計する", pdesc="集計単位", pvalid="文字列", name="group"):
        return {"api_spec": [
            {"action": "list_report", "description": desc_a, "request_params": [
                {"name": "from", "description": "集計の開始日"}, {"name": name, "description": pdesc, "validation": pvalid}]},
            {"action": "list_shops", "description": "POS会計データのある店舗の一覧を返す", "request_params": []}]}

    def run(react_d, out):
        old = _config_loader.INTERMEDIATE_DIR
        with tempfile.TemporaryDirectory() as td:
            Path(td, "x_react_draft.json").write_text(json.dumps(react_d, ensure_ascii=False), encoding="utf-8")
            _config_loader.INTERMEDIATE_DIR = Path(td)
            try:
                log = []
                fill(out, log)
                return log
            finally:
                _config_loader.INTERMEDIATE_DIR = old

    # description が片方(レジ別)だけで、引数の行(group)も全部を書いていない → 引数の行に全選択肢と既定値が入る。description(30字以内)・他の API は触らない
    out = mk()
    log = run(react, out)
    p = out["api_spec"][0]["request_params"][1]
    want = "集計単位。集計単位の選択肢はレジ別・店舗別(既定はレジ別)"
    assert p["description"] == want, p
    assert out["api_spec"][0]["description"] == "POS会計の締め金額を店舗・レジ別に期間集計する" and out["api_spec"][0]["request_params"][0] == {"name": "from", "description": "集計の開始日"}, out
    assert len(log) == 1 and log[0].startswith("[列挙] api_spec[list_report].request_params[group]"), log
    assert run(react, out) == [] and p["description"] == want, "冪等でない"
    # 検査 check_enum_mentions の通る形になる(全選択肢が引数の行にある)
    assert all(t in _param_text(p) for t in sel["option_texts"])
    # state 名と引数名の表記ゆれ(大文字小文字・_)は同じとみなす
    out = mk(name="Group_")
    run(react, out)
    assert "レジ別・店舗別" in out["api_spec"][0]["request_params"][1]["description"], out
    # 引数の行が既に全部を書いている(value の語でも可)・description が全部を書く・既定の語がある・1つも書かない → 触らない
    for kw in ({"pdesc": "集計単位。レジ別・店舗別"}, {"pdesc": "集計単位", "pvalid": "register または shop"}, {"desc_a": "レジ別と店舗別を期間集計する"},
               {"desc_a": "店舗・レジ別に期間集計する(既定はレジ別)"}, {"desc_a": "期間集計する"}):
        out = mk(**kw)
        assert run(react, out) == [] and out == mk(**kw), kw
    # 引数の行が決まらない(bind と合う name が無い)ときは何もしない。既定値が決まらない下書きは既定の括弧を付けない
    out = mk(name="unit")
    assert run(react, out) == [] and out == mk(name="unit")
    no_default = {"jsx_elements": [{k: v for k, v in sel.items() if not k.startswith("default")}]}
    out = mk()
    run(no_default, out)
    assert out["api_spec"][0]["request_params"][1]["description"].endswith("集計単位の選択肢はレジ別・店舗別"), out
    # 選択肢の語が無い下書き(事実が無い)は何もしない
    out = mk()
    assert run({"jsx_elements": [{"id": "JX1", "tag": "select", "label": "x"}]}, out) == [] and out == mk()
    print("[selftest OK] _compose_enum_description (一部の選択肢だけの API の、選択肢を受ける引数の行に全選択肢と既定値を足す。description は触らない・全部出ている/既定の語/事実が無い/引数が決まらないときは触らない・冪等)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_enum_description.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
