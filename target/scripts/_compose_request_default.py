"""request_params[].default の「既定値なし」の書き方を、空文字 "" の1つにそろえる(G32。契約 i6.k1)。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=145(列挙の説明 140 の後、備考 150 の前)。
  判定 is_no_default は verify_partials.check_request_defaults(H3 の断片の検査)も import して使う(判定語を1か所に持つ)。
WHY: 既定値が無い引数の default を、担当が「なし」「なし(全店)」「」と思い思いに書き、回ごとに揺れた。手順書(H3)で「なし」を禁じ、
  検査で ERROR にしたうえで、残った分をここで機械的に空にそろえる。
  「なし(全店)」のように括弧に省略時の意味があるときは、その意味を description に「未指定のときは全店」の形で足す(同じ文が既にあれば足さない)。
  既定が実際の値(0・register・2026-10-01 など)・文字列でない default・default の欄が無い行は触らない(推測で足さない)。2回流して同じ(冪等)。
"""
import re
import sys
import unicodedata
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す

ORDER = 145

# WHY: 「既定値なし」と見なす語。NFKC で全角半角をそろえ(（）→()・－→-)、前後の空白と大文字小文字を無視した形に当てる。
#   なし・無し・未指定・N/A・-・—(・―)を、そのまま、または (…) で包んだ形で認め、後ろに (意味) が付いてもよい(なし(全店))。
_NO_DEFAULT = re.compile(r"^\(?\s*(?:なし|無し|未指定|n/a|-|—|―)\s*\)?\s*(?:\((?P<meaning>[^()]*)\))?$")


def _match(value):
    if not isinstance(value, str):
        return None
    return _NO_DEFAULT.match(unicodedata.normalize("NFKC", value).strip().lower())


def is_no_default(value):
    """default の文字列が「既定値なし」の語に当たるか。空文字は当たらない(すでにそろっている)。呼出元: fill, verify_partials.check_request_defaults。"""
    return bool(isinstance(value, str) and value.strip() and _match(value))


def _meaning(value):
    """「なし(全店)」の括弧の中身(省略時の意味)。無ければ ''。"""
    m = _match(value)
    return (m.group("meaning") or "").strip() if m else ""


def fill(out, log):
    """api_spec[*].request_params[*].default が「既定値なし」の語なら '' にそろえ、括弧に意味があれば description に「未指定のときは<意味>」を足す。"""
    for a in out.get("api_spec") or []:
        if not isinstance(a, dict):
            continue
        for p in a.get("request_params") or []:
            if not isinstance(p, dict) or not is_no_default(p.get("default")):
                continue
            old, meaning = p["default"], _meaning(p["default"])
            p["default"] = ""
            if meaning:
                add = f"未指定のときは{meaning}"
                desc = str(p.get("description") or "")
                if add not in desc:
                    p["description"] = (desc.rstrip("。 ") + "。" if desc else "") + add
            log.append(f"[既定値] api_spec[{a.get('action')}].request_params[{p.get('name')}].default「{old}」を空欄にそろえる")


def _selftest():
    def mk():
        return {"api_spec": [{"action": "list", "request_params": [
            {"name": "a", "default": "なし", "description": "A"},
            {"name": "shop", "default": "なし(全店)", "description": "店舗"},
            {"name": "c", "default": ""},
            {"name": "d", "default": "0"},
            {"name": "e", "default": "register"}]}]}

    out, log = mk(), []
    fill(out, log)
    ps = out["api_spec"][0]["request_params"]
    assert ps[0]["default"] == "" and ps[1]["default"] == "", ps
    assert ps[1]["description"] == "店舗。未指定のときは全店" and ps[0]["description"] == "A", ps
    assert ps[2] == {"name": "c", "default": ""} and ps[3]["default"] == "0" and ps[4]["default"] == "register", ps
    assert len(log) == 2 and log[1] == "[既定値] api_spec[list].request_params[shop].default「なし(全店)」を空欄にそろえる", log
    fill(out, log)
    assert len(log) == 2 and out == _again(out), "冪等でない"
    # 判定語の全角半角ゆれ・前後の空白・(なし)・記号・N/A・未指定。実際の値・文字列でない値は当たらない
    for v in ("なし", "無し", "なし（全店）", " (なし) ", "-", "－", "—", "N/A", "n/a", "未指定"):
        assert is_no_default(v), v
    for v in ("", "0", "register", "2026-10-01", "全店", None, 0, ["なし"]):
        assert not is_no_default(v), v
    # description に同じ文が既にあれば足さない
    out = {"api_spec": [{"action": "x", "request_params": [{"name": "s", "default": "なし(全店)", "description": "店舗。未指定のときは全店"}]}]}
    fill(out, [])
    assert out["api_spec"][0]["request_params"][0]["description"] == "店舗。未指定のときは全店", out
    # default の欄が無い行・default が文字列でない行は触らない
    out = {"api_spec": [{"action": "x", "request_params": [{"name": "s"}, {"name": "t", "default": None}]}]}
    fill(out, [])
    assert out == {"api_spec": [{"action": "x", "request_params": [{"name": "s"}, {"name": "t", "default": None}]}]}, out
    print("[selftest OK] _compose_request_default (なし系の default を空欄にそろえる・括弧の意味は description へ・実際の値/欄なしは触らない・冪等)")


def _again(out):
    import copy
    c = copy.deepcopy(out)
    fill(c, [])
    return c


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_request_default.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
