#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_generic_diagram_svg.py — 汎用構成図 SVG/HTML ビルダー（diagram-design スキル代替、画面/図種非依存）

【目的】
diagram-design スキル（marketplace インストール済だが現在無効）が使えない間の代替パイプラインとして、
17_構成図生成.md が定義する「対象となる図」のうち nodes/edges 形式（process_structure /
screen_transition / data_flow）と nested 形式（screen_structure）の2種類の spec を読み、
standalone HTML（inline SVG 埋め込み）を直接組み立てる。

【意味合い】
build_ipo_flowchart_svg.py は ipo_flowchart 専用（画面固有の trigger/front/backend/db レーン座標を
screen_config.json の svg_layout に外出しし、それをそのまま描画するだけの「座標再生エンジン」）。
本スクリプトはそれと異なり、座標情報を一切前提にしない汎用エンジンである。spec.nodes / spec.edges
（または spec.structure）という抽象的な構造だけを受け取り、レイアウト座標をこのスクリプト自身が
計算する（層別レイアウト = トリガー/decision等のスタイルに応じた形状とライン矢印の自動配置、
nested 構造 = 深さに応じた再帰的な矩形分割）。
COLOR 辞書・cylinder/rect_node/pill_node 等の描画プリミティブの値は build_ipo_flowchart_svg.py と
同一のものを踏襲し（視覚的統一のため）、ここでは意図的に値を複製している（別スクリプトである
build_ipo_flowchart_svg.py 自体は変更しない方針のため、import 経由の依存にはせず値を複製する）。

【接続情報】
- 入力: target/intermediate/{feature_name}_screen_config.json の diagrams.{diagram_id}
        （_config_loader.get_diagram_spec() 経由で取得。フォーマットは 17_構成図生成.md
        「中間JSON のセクション形式」節、実データは {機能名}_screen_config.json を参照）
- 出力: target/diagrams/{feature_name}_{diagram_id}.html
        （全画面で接頭辞あり。add_diagrams_to_intermediate.py が中間 JSON に書くパス、
        build_ipo_flowchart_svg.py の {feature_name}_ipo_flowchart.html と同じ規則）
- 後段: extract_svg.py → svg_to_png.py → generate_docx.js（17_構成図生成.md「実装パイプライン」節）
- CLI: python3 build_generic_diagram_svg.py <feature_name> <diagram_id>
"""
import sys
from collections import deque
from pathlib import Path

from _config_loader import get_diagram_spec, get_feature_name_from_argv

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent.parent
DIAGRAMS_DIR = SKILL_DIR / "target" / "diagrams"


# 色定義（build_ipo_flowchart_svg.py の COLOR 辞書と同一値。視覚的統一のため意図的に複製、
# 別スクリプトへの import 依存を避けるため値そのものをここに持つ）
COLOR = {
    "bg":          "#faf7f2",  # paper
    "ink":         "#2d3142",  # main text / ink stroke
    "muted":       "#4f5d75",  # secondary
    "accent":      "#b5523a",  # rust (focal / decision)
    "link":        "#2e5aa8",  # link blue
    "scenario3":   "#2d3142",  # ink
    "scenario4":   "#7a6b5e",  # muted brown（本スクリプトでは back edge/loop 色として使用）
    "node_fill":   "#ffffff",
    "trigger_fill":"#e7eef9",
    "backend_fill":"#eeece6",
    "data_fill":   "#f4eee5",
    "border":      "#4f5d75",
}


def _esc(s) -> str:
    """XML特殊文字をエスケープする。

    目的: 生成SVGが後段の cairosvg 変換（17_構成図生成.md「extract_svg.py の正規化処理」節）で
          ParseError を起こさないようにする。
    意味合い: build_ipo_flowchart_svg.py はラベルをエスケープせず挿入しているが（画面固有の
              ハードコードラベルのみ扱うため安全）、本スクリプトは任意画面の spec データ（将来的に
              項目名等 "&" "<" ">" を含みうる文字列）を受け取る汎用エンジンのため防御的に処理する。
    接続情報: 全描画プリミティブ（pill_node/rect_node/diamond_node/cylinder_generic/arrow/
              back_edge_path/_layout_nested_node）がテキスト挿入前に必ず経由する。
    """
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _normalize_newlines(label) -> str:
    """cylinder（データノード）向けラベルの改行表記を正規化する。

    意味合い: {機能名}_screen_config.json の ipo_flowchart.spec.nodes（例: d_cache の
              label="キャッシュ\\nlock:{key}:{ym}"、repr確認済み＝リテラルのバックスラッシュ+n、
              実改行ではない）で、論理名/物理名の2行表現がリテラル "\\n" 記法で書かれている
              実例を確認した。data_flow（本スクリプトが今後実際に描画する図種、テーブルを
              nodes 化する）でも同じ表記慣習が使われる可能性が高いため、実改行(\n)とリテラル
              "\\n" の両方を2行分割の区切りとして扱えるよう正規化する。
    """
    if not label:
        return ""
    return str(label).replace("\\n", "\n")


def _text_width_px(s: str, per_char: float = 13.0) -> float:
    """ラベル文字数から概算描画幅(px)を求める。

    意味合い: 実フォントメトリクスは持たないため、arrow() の既存ラベル幅算出
              （len(label) * 7、9px フォント時）と同じ比率思想で「文字数 × 係数」の粗い近似を使う。
              ノード幅を自動で広げることで長い業務ラベル（例:「明細データ入力（複数パターン対応）」）
              が箱からはみ出す事故を防ぐ。
    """
    return len(s) * per_char


# ============================================================
# 描画プリミティブ（build_ipo_flowchart_svg.py の cylinder/rect_node/pill_node/arrow と
# 同一の見た目規約。diamond_node と back_edge_path は本スクリプト新設）
# ============================================================

def cylinder_generic(x, y, w, h, fill, stroke, label):
    """円柱形（データストア/テーブルノード）。

    意味合い: build_ipo_flowchart_svg.py の cylinder() は label_main/label_sub の2引数分離だが、
              17_構成図生成.md の nodes/edges 形式は単一の "label" フィールドしか持たないため、
              ラベル内に実改行が含まれていれば2行（論理名+物理名想定）、なければ1行として描画する
              汎用版として新設。
    """
    rx = w / 2
    ry = 8
    top_ellipse = f'<ellipse cx="{x + rx:.1f}" cy="{y + ry:.1f}" rx="{rx:.1f}" ry="{ry}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    side = (
        f'<path d="M {x:.1f} {y + ry:.1f} '
        f'L {x:.1f} {y + h - ry:.1f} '
        f'A {rx:.1f} {ry} 0 0 0 {x + w:.1f} {y + h - ry:.1f} '
        f'L {x + w:.1f} {y + ry:.1f} '
        f'A {rx:.1f} {ry} 0 0 0 {x:.1f} {y + ry:.1f} Z" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    )
    bottom_ellipse = (
        f'<path d="M {x:.1f} {y + h - ry:.1f} '
        f'A {rx:.1f} {ry} 0 0 0 {x + w:.1f} {y + h - ry:.1f}" '
        f'fill="none" stroke="{stroke}" stroke-width="1.5"/>'
    )
    lines = _normalize_newlines(label).split("\n") if label else [""]
    if len(lines) == 1:
        text = (
            f'<text x="{x + rx:.1f}" y="{y + h/2 + 4:.1f}" text-anchor="middle" '
            f'font-size="12" fill="{COLOR["ink"]}" font-weight="600">{_esc(lines[0])}</text>'
        )
    else:
        text_main = (
            f'<text x="{x + rx:.1f}" y="{y + h/2 - 2:.1f}" text-anchor="middle" '
            f'font-size="12" fill="{COLOR["ink"]}" font-weight="600">{_esc(lines[0])}</text>'
        )
        text_sub = (
            f'<text x="{x + rx:.1f}" y="{y + h/2 + 15:.1f}" text-anchor="middle" '
            f'font-size="9" fill="{COLOR["muted"]}" font-style="italic">{_esc(lines[1])}</text>'
        )
        text = text_main + "\n" + text_sub
    return side + "\n" + top_ellipse + "\n" + bottom_ellipse + "\n" + text


def rect_node(x, y, w, h, fill, stroke, label, rx=8):
    """通常の矩形ノード（処理）。build_ipo_flowchart_svg.py の rect_node と同一仕様。"""
    body = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" ry="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    text = f'<text x="{x + w/2:.1f}" y="{y + h/2 + 4:.1f}" text-anchor="middle" font-size="12" fill="{COLOR["ink"]}" font-weight="600">{_esc(label)}</text>'
    return body + "\n" + text


def pill_node(x, y, w, h, fill, stroke, label):
    """楕円形ノード（トリガー/開始・終了）。build_ipo_flowchart_svg.py の pill_node と同一仕様。"""
    rx = h / 2
    body = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx:.1f}" ry="{rx:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    text = f'<text x="{x + w/2:.1f}" y="{y + h/2 + 4:.1f}" text-anchor="middle" font-size="11" fill="{COLOR["ink"]}" font-weight="600">{_esc(label)}</text>'
    return body + "\n" + text


def diamond_node(x, y, w, h, fill, stroke, label):
    """菱形ノード（decision/分岐）。process_structure の「検証」等で使用する新設プリミティブ。

    意味合い: build_ipo_flowchart_svg.py の trigger/front/backend/db レーン構造には分岐ノードが
              存在しないため diamond 形状が無かった。17_構成図生成.md の style="decision" を
              描画するために新設する。(x, y, w, h) は他プリミティブと同じ「外接矩形の左上+幅+高さ」
              引数規約に合わせ、内部で中心座標に変換する。
    """
    cx, cy = x + w / 2, y + h / 2
    hw, hh = w / 2, h / 2
    points = f"{cx:.1f},{cy - hh:.1f} {cx + hw:.1f},{cy:.1f} {cx:.1f},{cy + hh:.1f} {cx - hw:.1f},{cy:.1f}"
    body = f'<polygon points="{points}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    text = f'<text x="{cx:.1f}" y="{cy + 4:.1f}" text-anchor="middle" font-size="12" fill="{COLOR["ink"]}" font-weight="600">{_esc(label)}</text>'
    return body + "\n" + text


def arrow(x1, y1, x2, y2, color, marker_id, label=None, dashed=False, label_offset=(0, -4)):
    """矢印 + （任意）ラベル。直線描画。build_ipo_flowchart_svg.py の arrow() と同一仕様
    （複数ラベルの縦積み表示=B7 拡張含む）。前方向きエッジ（layer が増える方向）専用。
    """
    dash = ' stroke-dasharray="6,3"' if dashed else ""
    path = f"M {x1:.1f} {y1:.1f} L {x2:.1f} {y2:.1f}"
    line = f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6"{dash} marker-end="url(#{marker_id})"/>'
    label_svg = ""
    if label:
        lx = (x1 + x2) / 2 + label_offset[0]
        ly = (y1 + y2) / 2 + label_offset[1]
        if isinstance(label, (list, tuple)) and len(label) > 1:
            lines = [str(x) for x in label if x]
            line_h = 12
            text_len = max(max(len(l) * 7 for l in lines), 30)
            box_h = line_h * len(lines) + 4
            box_top = ly - box_h / 2
            texts = "".join(
                f'<text x="{lx:.1f}" y="{box_top + line_h * (i + 1) - 2:.1f}" text-anchor="middle" '
                f'font-size="9" fill="{color}" font-weight="500">{_esc(l)}</text>'
                for i, l in enumerate(lines)
            )
            label_svg = (
                f'<rect x="{lx - text_len/2:.1f}" y="{box_top:.1f}" width="{text_len}" height="{box_h}" '
                f'fill="white" stroke="{color}" stroke-width="0.5" rx="2"/>' + texts
            )
        else:
            if isinstance(label, (list, tuple)):
                label = label[0] if label else ""
            text_len = max(len(label) * 7, 30)
            label_svg = (
                f'<rect x="{lx - text_len/2:.1f}" y="{ly - 9:.1f}" width="{text_len}" height="14" '
                f'fill="white" stroke="{color}" stroke-width="0.5" rx="2"/>'
                f'<text x="{lx:.1f}" y="{ly + 2:.1f}" text-anchor="middle" font-size="9" fill="{color}" font-weight="500">{_esc(label)}</text>'
            )
    return line + "\n" + label_svg


def back_edge_path(x1, y1, x2, y2, lane_x, color, marker_id, label=None):
    """ループ（back edge、例: 検証NGで入力に戻る）用の迂回経路。

    目的: 通常の arrow() は前方向き（layer が増える方向）のエッジしか自然に描けない。後方への
          エッジをそのまま直線で引くと手前のノード群を突っ切って重なる。
    意味合い: ノード右端から外側の「ループレーン」(lane_x) を垂直に通って戻り先ノードの右端へ
              接続するL字経路。build_ipo_flowchart_svg.py には対応物がない
              （ipo_flowchart は screen_config.json 側で座標を固定しているため back edge の
              自動迂回が不要だった）。任意の nodes/edges spec には back edge が何本現れるか
              事前に分からないため、本スクリプト固有の汎用処理として新設する。
    接続情報: 呼出元は build_flowchart_svg() の back edge 描画ループ。lane_x は呼出元で
              ループ本数に応じて複数レーンにずらして重なりを避ける。
    """
    path = f"M {x1:.1f} {y1:.1f} L {lane_x:.1f} {y1:.1f} L {lane_x:.1f} {y2:.1f} L {x2:.1f} {y2:.1f}"
    line = f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" stroke-dasharray="6,3" marker-end="url(#{marker_id})"/>'
    label_svg = ""
    if label:
        ly = (y1 + y2) / 2
        text_len = max(len(label) * 8, 30)
        label_svg = (
            f'<rect x="{lane_x - text_len/2:.1f}" y="{ly - 9:.1f}" width="{text_len}" height="14" '
            f'fill="{COLOR["bg"]}" stroke="{color}" stroke-width="0.5" rx="2"/>'
            f'<text x="{lane_x:.1f}" y="{ly + 2:.1f}" text-anchor="middle" font-size="9" fill="{color}" font-weight="500">{_esc(label)}</text>'
        )
    return line + "\n" + label_svg


def _wrap_svg(view_w, view_h, body_parts, defs_parts=None):
    """SVG全体組み立て（viewBox + font-family強制注入 + defs + body）。

    意味合い: build_ipo_flowchart_svg.py の build_svg() 末尾と同じ組み立てパターン（<style> タグで
              font-family を強制注入し、SVG単体抽出時の文字化けを防ぐ）を、flowchart/nested 両方の
              呼出元から共有するための共通関数として抽出。
    """
    style_block = '<style>text{font-family:"Noto Sans CJK JP",sans-serif;}</style>'
    defs = ("<defs>" + "".join(defs_parts) + "</defs>") if defs_parts else ""
    return (
        f'<svg viewBox="0 0 {view_w:.1f} {view_h:.1f}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">'
        + style_block + defs + "".join(body_parts) + "</svg>"
    )


# ============================================================
# flowchart系（nodes/edges 形式: process_structure / screen_transition / data_flow）
# ============================================================

# レイアウト定数（層別レイアウト。座標はこのスクリプトが計算する＝ build_ipo_flowchart_svg.py の
# 「screen_config.json 座標をそのまま再生する」方式との最大の違い）
_MARGIN_X = 40
_MARGIN_TOP = 36
_MARGIN_BOTTOM = 56
_NODE_GAP_X = 34
_GAP_BETWEEN_LAYERS = 68
_LOOP_LANE_GAP = 36
_MIN_VIEW_WIDTH = 480

#   input/formula/intermediate/result は derivation_chain 専用（build_derivation_chain_spec.py
#   が生成、decision は既存styleを流用のため追加不要）。形状は input/result が pill_node
#   （start/end 相当）、formula/intermediate が rect_node（process 相当）のため、それぞれの
#   既存styleと同じ寸法値を踏襲する（2026-07-19 追加）。
_NODE_MIN_W = {
    "start": 160, "end": 160, "process": 170, "decision": 190, "data": 170,
    "input": 160, "formula": 170, "intermediate": 170, "result": 160,
}
_NODE_H = {
    "start": 44, "end": 44, "process": 48, "decision": 92, "data": 60,
    "input": 44, "formula": 48, "intermediate": 48, "result": 44,
}
# 未掲載styleは per_char 既定値13（_node_dims の .get フォールバック）を使う。
# input/formula/intermediate/result は既存の start/end/process と同様に既定値で足りるため
# （decision/data のみ形状比率上の理由で個別値を持つ既存パターンを踏襲し、ここには追加しない）。
_STYLE_PER_CHAR = {"decision": 16, "data": 12}


def _node_dims(style: str, label: str):
    """node.style からノード形状の分類キーと自動計算した幅/高さを返す。

    意味合い: 17_構成図生成.md に明示された style 語彙は start/process/decision/end
              （+ ipo_flowchart 側 spec.nodes で先例のある data）。derivation_chain 専用の
              input/formula/intermediate/result（decision は既存流用）も 2026-07-19 に対応済み
              （以前はここが「本タスクのスコープ外」として process フォールバックのみだったが
              実装した）。それでもなお未知の style は process（矩形）にフォールバックし、
              将来追加されうる未対応図種が誤って本関数を通っても描画自体は落ちないようにする。
    """
    if style not in _NODE_H:
        style = "process"
    per_char = _STYLE_PER_CHAR.get(style, 13)
    normalized = _normalize_newlines(label) if style == "data" else str(label or "")
    first_line = normalized.split("\n")[0] if normalized else ""
    w = max(_NODE_MIN_W[style], _text_width_px(first_line, per_char))
    w = min(w, 340)  # 過度な横長化を防ぐ上限
    return style, w, _NODE_H[style]


def _compute_layers(nodes, edges):
    """nodes/edges から各ノードの層番号(layer)を計算し、layer 逆行エッジ（back edge）を分離する。

    目的: process_structure の「検証NG→入力へ戻る」のような後方エッジを含む一般的な有向グラフを、
          座標指定なしで上から下へのフローチャートとしてレイアウトする。
    意味合い:
      1. DFS で閉路検出し、閉路を作る辺（後退辺=back edge）を層計算から除外する
         （除外しないと Kahn 法のトポロジカルソートが完了しない＝無限ループ相当のバグになる）。
      2. 残った前方向きエッジ（DAG）についてのみ Kahn 法で位相順を求め、最長路長でノードの層を
         決定する（複数の親を持つノードは最も深い親+1の層に置かれる）。
      3. 念のため、計算後に layer[to] <= layer[from] となる辺が残っていないか再検査し、
         残っていれば back edge 側に追加する（DFS の cross edge 等、稀なケースの安全網）。
    接続情報: 戻り値 layer / valid_edges / back_idx は build_flowchart_svg() が座標決定と
              エッジ描画方式（前方=直線 / 後方=迂回レーン）の分岐に使う。
    """
    node_ids = [n["id"] for n in nodes]
    id_set = set(node_ids)

    valid_edges = []
    for e in edges:
        if e.get("from") not in id_set or e.get("to") not in id_set:
            print(f"WARN: edge が未知のノードIDを参照しているためスキップ: {e}", file=sys.stderr)
            continue
        valid_edges.append(e)

    # 1. DFS による閉路検出（後退辺=back edge の特定）
    adj = {nid: [] for nid in node_ids}
    for idx, e in enumerate(valid_edges):
        adj[e["from"]].append((idx, e["to"]))

    color = {nid: 0 for nid in node_ids}  # 0=白 1=灰(探索中) 2=黒(確定)
    back_idx = set()
    sys.setrecursionlimit(max(1000, len(node_ids) * 4 + 100))

    def _dfs(u):
        color[u] = 1
        for idx, v in adj[u]:
            if color[v] == 0:
                _dfs(v)
            elif color[v] == 1:
                back_idx.add(idx)
            # color[v] == 2（確定済み）の cross edge は前方辺として扱う（3.の安全網で再検査）
        color[u] = 2

    for nid in node_ids:
        if color[nid] == 0:
            _dfs(nid)

    # 2. 前方向きエッジのみで Kahn 法トポロジカルソート → 最長路長で層決定
    forward_edges = [e for i, e in enumerate(valid_edges) if i not in back_idx]
    indeg = {nid: 0 for nid in node_ids}
    fwd_adj = {nid: [] for nid in node_ids}
    for e in forward_edges:
        fwd_adj[e["from"]].append(e["to"])
        indeg[e["to"]] += 1

    layer = {nid: 0 for nid in node_ids}
    queue = deque([nid for nid in node_ids if indeg[nid] == 0])
    indeg_work = dict(indeg)
    topo_order = []
    while queue:
        u = queue.popleft()
        topo_order.append(u)
        for v in fwd_adj[u]:
            indeg_work[v] -= 1
            if indeg_work[v] == 0:
                queue.append(v)
    for u in topo_order:
        for v in fwd_adj[u]:
            if layer[v] < layer[u] + 1:
                layer[v] = layer[u] + 1

    # 3. 安全網: 層計算後に前方性が崩れている辺があれば back edge に追加
    final_back = set(back_idx)
    for i, e in enumerate(valid_edges):
        if layer[e["to"]] <= layer[e["from"]]:
            final_back.add(i)

    return layer, valid_edges, final_back


_STYLE_LEGEND_LABELS = {
    "start": "開始/終了(丸)",
    "end": "開始/終了(丸)",
    "process": "処理(四角)",
    "decision": "分岐(菱形)",
    "data": "データ(円柱)",
    # derivation_chain 専用4style（2026-07-19 追加、decision は既存流用のためここに重複追加しない）
    "input": "入力値(丸)",
    "formula": "計算式(四角)",
    "intermediate": "中間結果(四角・灰色地)",
    "result": "最終結果(丸)",
}


def build_flowchart_svg(spec, embed_size=None):
    """nodes/edges 形式の spec から層別レイアウトの flowchart SVG を組み立てる。

    目的: process_structure / screen_transition / data_flow（いずれも 17_構成図生成.md で
          type=flowchart もしくは同等の nodes/edges 契約を持つ図種）を画面非依存に描画する。
    意味合い: build_ipo_flowchart_svg.py が「screen_config.json の座標をそのまま並べる」のに対し、
              本関数は _compute_layers() の層番号から座標を自ら計算する「汎用レイアウトエンジン」。
              ノード形状/色は style（start/end/process/decision/data）で分岐し、COLOR 辞書の
              値を踏襲する。
    接続情報: 呼出元は build_svg()。embed_size は現状レイアウト計算に使わない
              （generate_docx.js の calcAspectFit はPNG実寸とdisplayWidthPxのみを見るため、
              本関数が出すSVGの内部解像度は独立して自動計算してよい。17_構成図生成.md
              「generate_docx.js での埋め込み」節参照）。
    """
    nodes = spec.get("nodes", [])
    edges = spec.get("edges", [])

    if not nodes:
        # 空spec: silent fallback ではなく、spec不備を明示するプレースホルダを描画する
        return _wrap_svg(_MIN_VIEW_WIDTH, 120, [
            f'<rect width="100%" height="100%" fill="{COLOR["bg"]}"/>',
            f'<text x="20" y="60" font-size="13" fill="{COLOR["muted"]}">nodes が空です（spec不備）</text>',
        ])

    layer, valid_edges, back_idx = _compute_layers(nodes, edges)

    dims = {}
    for n in nodes:
        style, w, h = _node_dims(n.get("style", "process"), n.get("label", n["id"]))
        dims[n["id"]] = {"style": style, "w": w, "h": h}

    layers = {}
    for n in nodes:
        layers.setdefault(layer[n["id"]], []).append(n["id"])
    max_layer = max(layers.keys()) if layers else 0

    row_widths = {
        ln: sum(dims[i]["w"] for i in ids) + _NODE_GAP_X * (len(ids) - 1)
        for ln, ids in layers.items()
    }
    max_row_width = max(row_widths.values()) if row_widths else (_MIN_VIEW_WIDTH - 2 * _MARGIN_X)

    has_back = len(back_idx) > 0
    view_width = max(_MIN_VIEW_WIDTH, max_row_width + 2 * _MARGIN_X + (_LOOP_LANE_GAP * 3 if has_back else 0))

    coords = {}
    cursor_y = _MARGIN_TOP
    for ln in range(0, max_layer + 1):
        ids = layers.get(ln, [])
        if not ids:
            continue
        row_w = row_widths[ln]
        x_cursor = _MARGIN_X + (max_row_width - row_w) / 2
        layer_h = max(dims[i]["h"] for i in ids)
        for nid in ids:
            w, h = dims[nid]["w"], dims[nid]["h"]
            coords[nid] = {
                "x": x_cursor, "y": cursor_y, "w": w, "h": h,
                "cx": x_cursor + w / 2, "cy_top": cursor_y, "cy_bot": cursor_y + h,
                "cy_mid": cursor_y + h / 2, "x_right": x_cursor + w,
            }
            x_cursor += w + _NODE_GAP_X
        cursor_y += layer_h + _GAP_BETWEEN_LAYERS

    content_bottom = cursor_y - _GAP_BETWEEN_LAYERS
    view_height = content_bottom + _MARGIN_BOTTOM

    # ノード描画
    bg_parts = [f'<rect width="100%" height="100%" fill="{COLOR["bg"]}"/>']
    node_parts = []
    for n in nodes:
        nid = n["id"]
        c = coords[nid]
        style = dims[nid]["style"]
        label = n.get("label", nid)
        # input/result は derivation_chain の入力元/最終結果（start/end と同じ pill 形状で
        # 意味合いを踏襲）。formula/intermediate は同じく derivation_chain 専用で、formula は
        # process と同一見た目（計算式そのもの）、intermediate は視覚区別のため backend_fill+
        # muted枠（承認済みCOLORパレット内のみ、新色追加なし）にする（2026-07-19 追加）。
        if style in ("start", "end", "input", "result"):
            node_parts.append(pill_node(c["x"], c["y"], c["w"], c["h"], COLOR["trigger_fill"], COLOR["border"], label))
        elif style == "decision":
            node_parts.append(diamond_node(c["x"], c["y"], c["w"], c["h"], COLOR["node_fill"], COLOR["accent"], label))
        elif style == "data":
            node_parts.append(cylinder_generic(c["x"], c["y"], c["w"], c["h"], COLOR["data_fill"], COLOR["border"], label))
        elif style == "intermediate":
            node_parts.append(rect_node(c["x"], c["y"], c["w"], c["h"], COLOR["backend_fill"], COLOR["muted"], label))
        elif style == "formula":
            node_parts.append(rect_node(c["x"], c["y"], c["w"], c["h"], COLOR["node_fill"], COLOR["border"], label))
        else:
            node_parts.append(rect_node(c["x"], c["y"], c["w"], c["h"], COLOR["node_fill"], COLOR["border"], label))

    # エッジ描画: 前方/後方(back edge)を分けて集約（同一 from/to の複数エッジは
    # build_ipo_flowchart_svg.py のフェーズB施策B7 同様、1本のエッジ+複数行ラベルに統合）
    def _group(idx_iterable):
        groups = {}
        order = []
        for i in idx_iterable:
            e = valid_edges[i]
            key = (e["from"], e["to"])
            if key not in groups:
                groups[key] = {"labels": [], "dashed": False}
                order.append(key)
            g = groups[key]
            if e.get("label") and e["label"] not in g["labels"]:
                g["labels"].append(e["label"])
            g["dashed"] = g["dashed"] or bool(e.get("dashed", False))
        return order, groups

    forward_idx = [i for i in range(len(valid_edges)) if i not in back_idx]
    fwd_order, fwd_groups = _group(forward_idx)
    back_order, back_groups = _group(sorted(back_idx))

    marker_defs = [
        f'<marker id="a_border" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{COLOR["border"]}"/></marker>',
        f'<marker id="a_accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{COLOR["accent"]}"/></marker>',
        f'<marker id="a_muted" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="{COLOR["scenario4"]}"/></marker>',
    ]

    arrow_parts = []
    for key in fwd_order:
        f_id, t_id = key
        group = fwd_groups[key]
        from_c, to_c = coords[f_id], coords[t_id]
        is_decision_edge = dims[f_id]["style"] == "decision" or dims[t_id]["style"] == "decision"
        color = COLOR["accent"] if is_decision_edge else COLOR["border"]
        marker = "a_accent" if is_decision_edge else "a_border"
        labels = group["labels"]
        label = None if not labels else (labels[0] if len(labels) == 1 else labels)
        arrow_parts.append(arrow(from_c["cx"], from_c["cy_bot"], to_c["cx"], to_c["cy_top"], color, marker, label=label, dashed=group["dashed"]))

    for lane_i, key in enumerate(back_order):
        f_id, t_id = key
        group = back_groups[key]
        from_c, to_c = coords[f_id], coords[t_id]
        lane_x = view_width - _MARGIN_X / 2 - lane_i * _LOOP_LANE_GAP
        labels = group["labels"]
        label = "/".join(labels) if labels else None
        arrow_parts.append(back_edge_path(from_c["x_right"], from_c["cy_mid"], to_c["x_right"], to_c["cy_mid"], lane_x, COLOR["scenario4"], "a_muted", label=label))

    # 凡例（使われているstyleとback edge有無だけをテキストで簡潔に表示）
    # start/end は表示テキストが同一("開始/終了(丸)")のため、style単位ではなく表示テキスト単位で
    # 重複排除する（styleキーだけで dedupe すると start と end が別項目として二重表示されてしまう）。
    styles_used = sorted({dims[n["id"]]["style"] for n in nodes}, key=lambda s: list(_NODE_H.keys()).index(s))
    legend_items = []
    for s in styles_used:
        lbl = _STYLE_LEGEND_LABELS.get(s, s)
        if lbl not in legend_items:
            legend_items.append(lbl)
    if has_back:
        legend_items.append("ループ(点線)")
    legend_y = view_height - 22
    legend_parts = [
        f'<line x1="{_MARGIN_X}" y1="{legend_y - 14:.1f}" x2="{view_width - _MARGIN_X:.1f}" y2="{legend_y - 14:.1f}" stroke="{COLOR["border"]}" stroke-width="0.6" opacity="0.4"/>',
        f'<text x="{_MARGIN_X}" y="{legend_y:.1f}" font-size="10" fill="{COLOR["muted"]}">凡例: {_esc("　".join(legend_items))}</text>',
    ]

    body = bg_parts + arrow_parts + node_parts + legend_parts
    return _wrap_svg(view_width, view_height, body, defs_parts=marker_defs)


# ============================================================
# nested系（階層構造: screen_structure = 画面→エリア→項目の入れ子）
# ============================================================

_NESTED_MARGIN = 20
_NESTED_GAP = 14
_NESTED_LABEL_H = 20


def _nested_colors(depth):
    """深さに応じた塗り/枠線色を返す。COLOR辞書の値を深さでローテーションし、
    入れ子の階層を視覚的に区別する（値自体は flowchart 系と同じパレットを踏襲）。
    """
    palette = [
        (COLOR["trigger_fill"], COLOR["border"]),
        (COLOR["backend_fill"], COLOR["border"]),
        (COLOR["data_fill"], COLOR["border"]),
        (COLOR["node_fill"], COLOR["muted"]),
    ]
    return palette[depth % len(palette)]


def _layout_nested_node(node, x, y, w, h, depth, parts):
    """階層構造ノード1件を再帰的に描画する（screen_structure の中核ロジック）。

    目的: 画面→エリア→項目のような入れ子構造を、事前座標なしで矩形の再帰分割により描画する。
    意味合い: 各ノードの子要素は、親の「利用可能領域」を軸方向に等分割して割り当てる
              （treemap的分割）。この方式は分割を再帰しても割当領域の合計が常に親の領域内に
              収まる性質を持つため、階層が深くなっても view の外にはみ出さない。
              分割方向は「深さ0（最上位、画面全体の領域分割=サイドバー/メインエリア等の横並び）は
              横(row)、それ以外（深さ1以降=業務ブロックの積み上げ）は縦(col)」という固定規則にする。
              spec.structure にはレイアウト方向を指定するフィールドが無いため、業務画面構成に多い
              「最上位だけ横割り、以降は縦積み」という一般的なUI構造の慣習を既定値として採用した
              （移行元案件の {機能名}_screen_config.json の screen_structure 実データはこの規則と一致する）。
    接続情報: 呼出元は build_nested_svg()。parts はSVG断片を溜め込む可変リスト（副作用で追記）。
    """
    w = max(w, 10)
    h = max(h, 10)
    label = node.get("label", "")
    note = node.get("note")
    children = node.get("children") or []
    fill, stroke = _nested_colors(depth)

    parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>')

    if children:
        # コンテナ表記: ラベルを枠線上にマスク（背景色矩形）+ テキストで載せる
        label_w = _text_width_px(label, 9) + 16
        parts.append(f'<rect x="{x + 8:.1f}" y="{y - 8:.1f}" width="{label_w:.1f}" height="16" fill="{COLOR["bg"]}"/>')
        parts.append(f'<text x="{x + 14:.1f}" y="{y + 4:.1f}" font-size="11" font-weight="600" fill="{COLOR["ink"]}">{_esc(label)}</text>')
        if note:
            parts.append(f'<text x="{x + w - 10:.1f}" y="{y + 4:.1f}" font-size="9" fill="{COLOR["muted"]}" text-anchor="end">{_esc(note)}</text>')

        inner_x = x + _NESTED_GAP
        inner_y = y + _NESTED_LABEL_H + _NESTED_GAP / 2
        inner_w = max(w - 2 * _NESTED_GAP, 10)
        inner_h = max(h - _NESTED_LABEL_H - 1.5 * _NESTED_GAP, 10)
        n = len(children)
        row = (depth == 0)
        if row:
            slice_w = inner_w / n
            for i, child in enumerate(children):
                cx0 = inner_x + i * slice_w
                cw = max(slice_w - (_NESTED_GAP if i < n - 1 else 0), 10)
                _layout_nested_node(child, cx0, inner_y, cw, inner_h, depth + 1, parts)
        else:
            slice_h = inner_h / n
            for i, child in enumerate(children):
                cy0 = inner_y + i * slice_h
                ch = max(slice_h - (_NESTED_GAP if i < n - 1 else 0), 10)
                _layout_nested_node(child, inner_x, cy0, inner_w, ch, depth + 1, parts)
    else:
        cx, cy = x + w / 2, y + h / 2
        # note込み2行表示は、note行の下端(概算 cy+16)が箱の下端(y+h)をはみ出さない高さ
        # （h>=36、cy+16<=y+h-2 を満たす条件から逆算）でのみ行う。深い入れ子で箱が
        # 極端に薄くなるケース（treemap分割の性質上、末端ノードは数十px未満になりうる）で
        # note テキストが箱の外に描画されてしまう問題への対処。
        if note and h >= 36:
            parts.append(f'<text x="{cx:.1f}" y="{cy - 2:.1f}" font-size="11" font-weight="600" fill="{COLOR["ink"]}" text-anchor="middle">{_esc(label)}</text>')
            parts.append(f'<text x="{cx:.1f}" y="{cy + 13:.1f}" font-size="9" fill="{COLOR["muted"]}" text-anchor="middle">{_esc(note)}</text>')
        else:
            parts.append(f'<text x="{cx:.1f}" y="{cy + 4:.1f}" font-size="11" font-weight="600" fill="{COLOR["ink"]}" text-anchor="middle">{_esc(label)}</text>')


def build_nested_svg(spec, embed_size=None):
    """spec.structure（階層構造）から nested 系 SVG を組み立てる。

    目的: screen_structure（画面→エリア→項目の論理ブロック構成図）を画面非依存に描画する。
    意味合い: トップレベルの structure[] は通常1件（画面全体を表す単一ルート、
              移行元案件の {機能名}_screen_config.json 実データで確認済み）だが、複数件でも横並びに
              並べられるよう一般化する。
    接続情報: 呼出元は build_svg()。embed_size.width_px/height_px があればそれを view の
              初期サイズに使う（無ければ既定 640x400）。flowchart系と異なり内容量に応じた
              自動拡張はしない（再帰分割は常に指定領域内に収まる treemap 的性質を持つため）。
    """
    structure = spec.get("structure", [])
    view_w = (embed_size or {}).get("width_px") or 640
    view_h = (embed_size or {}).get("height_px") or 400

    parts = [f'<rect width="100%" height="100%" fill="{COLOR["bg"]}"/>']
    if not structure:
        parts.append(f'<text x="20" y="40" font-size="13" fill="{COLOR["muted"]}">structure が空です（spec不備）</text>')
        return _wrap_svg(view_w, view_h, parts)

    n = len(structure)
    slice_w = (view_w - 2 * _NESTED_MARGIN) / n
    for i, node in enumerate(structure):
        x0 = _NESTED_MARGIN + i * slice_w
        w0 = max(slice_w - (_NESTED_GAP if i < n - 1 else 0), 10)
        _layout_nested_node(node, x0, _NESTED_MARGIN, w0, view_h - 2 * _NESTED_MARGIN, 0, parts)

    return _wrap_svg(view_w, view_h, parts)


# ============================================================
# ディスパッチ + CLI
# ============================================================

def build_svg(feature_name, diagram_id):
    """diagram_id の type に応じて flowchart系/nested系のいずれかへ振り分ける。

    意味合い: type=="nested" のみ nested 系（screen_structure）、それ以外は全て flowchart系
              （nodes/edges 契約、process_structure/screen_transition/data_flow）として扱う。
              17_構成図生成.md「対象となる図」表では data_flow の diagram-design type が
              "architecture" と記載されているが、これは旧 diagram-design スキル固有の type
              分類であり、本スクリプトが読む spec.nodes/spec.edges という中間JSON側のデータ契約
              自体は type 文字列に関わらず共通（同ファイル「フィールド」表参照）。従って
              type 文字列の厳密一致ではなく "nested かどうか" のみで振り分けるのが安全。
    """
    entry = get_diagram_spec(diagram_id, feature_name)
    diagram_type = entry.get("type", "flowchart")
    title = entry.get("title", diagram_id)
    spec = entry.get("spec", {})
    embed_size = entry.get("embed_size")

    if diagram_type == "nested":
        svg = build_nested_svg(spec, embed_size)
    else:
        svg = build_flowchart_svg(spec, embed_size)

    return svg, title, entry


def _wrap_html(svg, title):
    """standalone HTML化。build_ipo_flowchart_svg.py の main() と同一の CSS/構造を踏襲する
    （body背景色・.frame の padding/box-shadow・svg の width:100%等、値まで完全一致させる）。
    """
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ margin: 0; background: #f0eee8; font-family: "Noto Sans CJK JP", sans-serif; }}
  .frame {{ max-width: 1100px; margin: 24px auto; background: #faf7f2; padding: 16px; border-radius: 8px; box-shadow: 0 2px 12px rgba(0,0,0,0.05); }}
  svg {{ width: 100%; height: auto; display: block; }}
</style>
</head>
<body>
  <div class="frame">{svg}</div>
</body>
</html>
"""


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 build_generic_diagram_svg.py <feature_name> <diagram_id>", file=sys.stderr)
        raise SystemExit(1)

    # feature_name は sys.argv[1]（get_feature_name_from_argv 経由、既存パイプラインとの規約統一）。
    # diagram_id は本スクリプト固有の第2引数（process_structure / screen_transition / data_flow /
    # screen_structure 等、screen_config.json の diagrams.{id} キー）。
    feature_name = get_feature_name_from_argv()
    diagram_id = sys.argv[2]

    svg, title, entry = build_svg(feature_name, diagram_id)
    html = _wrap_html(svg, title)

    # 2026-10-05 汎用化: 出力ファイル名は全画面で常に {feature_name}_{diagram_id}.html。
    # 目的/理由: 既定の機能名（_config_loader の旧 DEFAULT 定数）を廃止したため、
    #   「既定の画面だけ接頭辞なし」の分岐を持たない。
    # 接続情報: add_diagrams_to_intermediate.py が中間 JSON に書く html/svg/png のパスと
    #   同じ stem にすること（build_ipo_flowchart_svg.py の {feature_name}_ipo_flowchart も同じ規則）。
    out_path = DIAGRAMS_DIR / f"{feature_name}_{diagram_id}.html"

    DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    spec = entry.get("spec", {})
    diagram_type = entry.get("type", "flowchart")
    print(f"OK: {out_path} ({len(html.encode('utf-8'))} bytes)")
    if diagram_type == "nested":
        print(f"  type: nested / root items: {len(spec.get('structure', []))}")
    else:
        print(f"  type: {diagram_type} / nodes: {len(spec.get('nodes', []))} / edges: {len(spec.get('edges', []))}")


if __name__ == "__main__":
    main()
