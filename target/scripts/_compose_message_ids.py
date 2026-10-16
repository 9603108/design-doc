"""メッセージ ID を、種別ごと(MSE・MSW・MSI)に出現順で 00001 から機械的に振り直す。

呼出元: _units._run_fillers(out, log) が ORDER 順に fill(out, log) を呼ぶ。ORDER=125
  (応答の写し 110・写し 120 の後、取得エラーの例示 130 の前。_compose_literals 200 より前)。
WHY: モデルがメッセージ ID を毎回違う番号で振ると、同じ文言に別の ID が付き、参照(validations の message_id・
  error_patterns の message_id・dispatch の message_ref など)と食い違う。ソースに字面の無い ID は、ここで
  種別ごとの連番に揃える。ソース(react・backend 下書き)に字面が残っている ID は、ソースの ID のまま残し、
  その番号は採番で使わない。
接続: 対象は out の messages / messages_backend の各行(message_id・severity)。旧 ID を参照する欄は、
  out 全体の文字列値を走査して新 ID に書き換える(_rewrite)。ソースの字面は _config_loader.INTERMEDIATE_DIR の
  react 下書き・backend 下書きを読む(_source_text)。
冪等: 振り直した後に再実行しても、同じ行が同じ順で同じ番号を受けるので変わらない。
同じ旧 ID が複数行にあるときは対応が1つに決まらないので、その ID には触らず WARN を log に出す。
"""
import re
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))  # G17: python3 -I でも同じフォルダの import を通す
from pathlib import Path

ORDER = 125

# WHY: 種別(severity)と ID 接頭辞の対応は、既存の決め方(MSE=エラー、MSW=確認、MSI=通知)に合わせる。
#      MSGE(グローバルエラー)は行の種別が別なので対象外にし、触らない
_PREFIX_BY_SEVERITY = {"error": "MSE", "warning": "MSW", "info": "MSI"}
_ID_RE = re.compile(r"^(MSE|MSW|MSI)(\d{5})$")
_LIST_KEYS = ("messages", "messages_backend")


def _rows(out):
    """messages / messages_backend の行(dict)を、キーの順・出現順に返す。
    WHY: 合成の途中では messages_backend、合成後は messages に入るので、両方を見る。"""
    rows = []
    for key in _LIST_KEYS:
        for r in out.get(key) or []:
            if isinstance(r, dict):
                rows.append(r)
    return rows


def _source_text(directory):
    """react・backend 下書きの字面を1つの文字列にする。ID が字面としてソースに残っているかを見る材料。
    WHY: 下書きはソースの事実。読めないファイルは無いものとして飛ばす(_compose_error_example と同じ読み方)。"""
    parts = []
    for suffix in ("react_draft", "backend_draft"):
        for p in sorted(Path(directory).glob(f"*_{suffix}.json")):
            try:
                parts.append(p.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(parts)


def _in_source(msg_id, text):
    """ID が字面として text にあるか。前後が英数字でない完全一致だけを数える。
    WHY: MSE00001 が MSE000012 の一部として当たらないように、前後の境界を見る。"""
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(msg_id) + r"(?![0-9])", text) is not None


def _plan(rows, source):
    """振り直す行について、旧 ID -> 新 ID の対応と WARN の文を作る。戻り: (対応 dict, WARN の list)。
    振り直さない行の番号(ソースに残る ID・種別が合わない行・重複の ID)は採番で使わないよう reserved に入れる。
    WHY: 残す行の番号と新しい番号が重なると、同じ ID が2行に付き、参照先が1つに決まらなくなる。"""
    count = {}
    for r in rows:
        mid = r.get("message_id")
        if isinstance(mid, str):
            count[mid] = count.get(mid, 0) + 1

    reserved = {"MSE": set(), "MSW": set(), "MSI": set()}
    warns = []
    todo = []  # (旧 ID, 接頭辞)
    for r in rows:
        mid = r.get("message_id")
        m = _ID_RE.match(mid) if isinstance(mid, str) else None
        if not m:
            continue
        prefix, num = m.group(1), int(m.group(2))
        if _PREFIX_BY_SEVERITY.get(r.get("severity")) != prefix:
            reserved[prefix].add(num)
            continue
        if count[mid] > 1:
            if f"[メッセージID] {mid} は複数行にあり対応が決まらないため触らない" not in warns:
                warns.append(f"[WARN][メッセージID] {mid} は複数行にあり対応が決まらないため触らない")
            reserved[prefix].add(num)
            continue
        if _in_source(mid, source):
            reserved[prefix].add(num)
            continue
        todo.append((mid, prefix))

    mapping = {}
    nxt = {"MSE": 1, "MSW": 1, "MSI": 1}
    for mid, prefix in todo:
        if mid in mapping:
            continue
        while nxt[prefix] in reserved[prefix]:
            nxt[prefix] += 1
        mapping[mid] = f"{prefix}{nxt[prefix]:05d}"
        nxt[prefix] += 1
    return mapping, warns


# WHY: 文字列の途中に入った ID(例: 'YYYY-MM-DD 形式(MSE00003)')も拾う。前後に英数字が続く語(MSE000051 など)は ID ではないので拾わない
_EMBEDDED_ID_RE = re.compile(r"(?<![A-Za-z0-9])(MS[EWI]\d{5})(?![0-9])")


def _rewrite(node, mapping):
    """JSON の値を再帰で走査し、文字列の中の旧 ID を新 ID に置き換える。戻り: 置換した文字列値の件数。
    WHY: 旧 ID を参照する欄は validations・error_patterns・dispatch・events など複数あり、欄名を決め打ちできない。
      ID が文字列の途中に入る欄もある(例: request_params の validation 'YYYY-MM-DD 形式(MSE00003)')ので、完全一致だけでは足りない。
    対応は1回のスキャンで当てるので、A->B と B->C が連鎖して書き換わることは無い。
    接続: fill から呼ばれ、自分自身を再帰で呼ぶ。"""
    def sub_ids(s):
        return _EMBEDDED_ID_RE.sub(lambda m: mapping.get(m.group(1), m.group(1)), s)

    n = 0
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                new = sub_ids(v)
                if new != v:
                    node[k] = new
                    n += 1
            else:
                n += _rewrite(v, mapping)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                new = sub_ids(v)
                if new != v:
                    node[i] = new
                    n += 1
            else:
                n += _rewrite(v, mapping)
    return n


def fill(out, log):
    """メッセージ ID を種別ごとに振り直し、out 全体の参照を揃える。振り直す ID が無いときは何もしない。
    呼出元: _units._run_fillers。呼出先: _rows・_source_text・_plan・_rewrite。"""
    from _config_loader import INTERMEDIATE_DIR

    rows = _rows(out)
    if not rows:
        return
    mapping, warns = _plan(rows, _source_text(INTERMEDIATE_DIR))
    log.extend(warns)
    changed = {old: new for old, new in mapping.items() if old != new}
    if not changed:
        return
    n = _rewrite(out, mapping)
    for old, new in changed.items():
        log.append(f"[メッセージID] {old} -> {new}")
    log.append(f"[メッセージID] 参照の書き換え {n} 件")


def _selftest():
    import json
    import tempfile
    import _config_loader

    def run(out, source):
        old = _config_loader.INTERMEDIATE_DIR
        with tempfile.TemporaryDirectory() as td:
            Path(td, "x_react_draft.json").write_text(source, encoding="utf-8")
            _config_loader.INTERMEDIATE_DIR = Path(td)
            try:
                log = []
                fill(out, log)
                return log
            finally:
                _config_loader.INTERMEDIATE_DIR = old

    def row(mid, sev):
        return {"message_id": mid, "code": mid, "severity": sev, "message": "文言"}

    def mk():
        return {
            "messages_backend": [row("MSE00005", "error"), row("MSW00009", "warning"),
                                 row("MSE00007", "error"), row("MSI00002", "info")],
            "validations": [{"message_id": "MSE00005"}, {"message_id": "MSE00007"}],
            "error_patterns": [{"message_id": "MSW00009"}],
            "triggers": [{"dispatch": [{"message_ref": "MSI00002"}]}],
            # WHY: ID が文字列の途中に入る欄(埋め込み)も振り直しの対象。MSE000051 は別の ID なので触らない
            "request_params": [{"validation": "YYYY-MM-DD 形式(MSE00005)", "note": "MSE000051 は触らない"}],
        }

    # 1. ソースに字面が無い: 種別ごとに出現順で 00001 から振り直し、参照も揃う
    out = mk()
    log = run(out, "{}")
    assert [r["message_id"] for r in out["messages_backend"]] == ["MSE00001", "MSW00001", "MSE00002", "MSI00001"], out
    assert all(r["code"] == r["message_id"] for r in out["messages_backend"]), out
    assert out["validations"] == [{"message_id": "MSE00001"}, {"message_id": "MSE00002"}], out
    assert out["error_patterns"] == [{"message_id": "MSW00001"}], out
    assert out["triggers"][0]["dispatch"][0]["message_ref"] == "MSI00001", out
    assert any("MSE00005 -> MSE00001" in s for s in log), log
    assert out["request_params"][0] == {"validation": "YYYY-MM-DD 形式(MSE00001)", "note": "MSE000051 は触らない"}, out

    # 2. 冪等: 2回目は何も変えず、ログも空
    before = json.dumps(out, ensure_ascii=False, sort_keys=True)
    assert run(out, "{}") == [], out
    assert json.dumps(out, ensure_ascii=False, sort_keys=True) == before

    # 3. ソースに字面がある ID は残し、その番号を採番で使わない
    out = mk()
    out["messages_backend"].insert(0, row("MSE00002", "error"))
    out["validations"].append({"message_id": "MSE00002"})
    run(out, '{"msg": "MSE00002"}')
    ids = [r["message_id"] for r in out["messages_backend"]]
    assert ids[0] == "MSE00002" and out["validations"][-1] == {"message_id": "MSE00002"}, ids
    assert ids == ["MSE00002", "MSE00001", "MSW00001", "MSE00003", "MSI00001"], ids

    # 4. 同じ旧 ID が複数行 -> 触らず WARN、その番号は使わない
    out = {"messages": [row("MSE00004", "error"), row("MSE00004", "error"), row("MSE00009", "error")],
           "validations": [{"message_id": "MSE00004"}]}
    log = run(out, "{}")
    assert [r["message_id"] for r in out["messages"]] == ["MSE00004", "MSE00004", "MSE00001"], out
    assert out["validations"] == [{"message_id": "MSE00004"}], out
    assert any(s.startswith("[WARN][メッセージID] MSE00004") for s in log), log

    # 5. 種別(severity)と接頭辞が合わない行は触らず、その番号は使わない
    out = {"messages": [row("MSE00003", "warning"), row("MSE00008", "error")]}
    run(out, "{}")
    assert [r["message_id"] for r in out["messages"]] == ["MSE00003", "MSE00001"], out

    # 6. MSGE は対象外
    out = {"messages": [row("MSGE00001", "error")]}
    assert run(out, "{}") == [] and out["messages"][0]["message_id"] == "MSGE00001", out

    print("[selftest OK] _compose_message_ids (種別ごとの採番・参照の書き換え・ソースの ID の保持・冪等・重複と種別不一致は触らない)")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("usage: _compose_message_ids.py --selftest (fill は _units._run_fillers から呼ばれる)")
        sys.exit(2)
