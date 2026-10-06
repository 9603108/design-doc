#!/usr/bin/env python3
# 目的: diagram-design スキルが生成する standalone HTML から、inline <svg>...</svg> タグだけを
#       抽出して別ファイルに保存する。
# 意味合い: diagram-design は HTML ベースで出力するが、docx 埋め込みは SVG 単体ファイルの方が
#           扱いやすい（docx-js の ImageRun({type:'svg'}) に直接渡せる）。
# 接続情報: 入力 = 第1引数 HTML パス / 出力 = 第2引数 SVG パス
#           呼出元 = design-doc スキル Wave 3.5 終端、または手動

import re
import sys
from pathlib import Path


def normalize_svg(svg: str) -> str:
    """SVG を XML 仕様準拠 + cairosvg/Pango フォント解決可能な形に正規化する。
    意味合い:
      1. diagram-design 生成 SVG の `<!-- ---- セクション ---- -->` 連続ハイフンは XML 仕様違反 → コメント全削除
      2. font-family 'Noto Sans JP' / 'Geist Mono' は WSL Ubuntu の fontconfig で解決できず日本語が□化 →
         実際にインストール済みの `'Noto Sans CJK JP'` にリネームする（描画は同じ Noto 系で再現）
    接続情報: svg_to_png.py（cairosvg）から呼ばれる前段の前処理
    過去経緯: v5 で日本語豆腐化が発生（PNG 内文字すべて□）→ font-family の表記差が原因と判明、本関数で吸収"""
    # 1. 全コメント削除
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.DOTALL)
    # 2. font-family 全強制統一（WSL Ubuntu にインストール済の Noto Sans CJK JP に置換）
    #    意味合い: 個別フォント名（'Instrument Serif', 'Geist Mono', 'Noto Sans JP' 等）の有無を
    #             調べずに、全て CJK JP に統一する。monospace 維持より文字化け解消を優先。
    #    過去経緯: v6→v7 で 'Instrument Serif' が WSL 未インストールで最下部の漢字が□化したため、
    #             個別リネームではなく全 font-family 強制統一に変更
    svg = re.sub(
        r'font-family="[^"]*"',
        'font-family="\'Noto Sans CJK JP\', sans-serif"',
        svg
    )
    svg = re.sub(
        r"font-family='[^']*'",
        "font-family=\"'Noto Sans CJK JP', sans-serif\"",
        svg
    )
    # 3. （v24 で追加）<text> に font-family 属性が無いケース対応
    #    意味合い: ipo_swimlane.svg のように、HTML側CSSで `svg text { font-family: ... }` を
    #             指定している SVG は、抽出後に CSS が失われて cairosvg で文字化けする。
    #             SVG 自身に <style> タグを強制注入し、フォント指定を内包させる。
    #    過去経緯: v24 で diagram-design 新版が生成した SVG（IPO swimlane）で日本語豆腐化発生 →
    #             font-family 属性置換だけでは対処できないと判明、<style> 注入方式を追加
    style_block = '<style>text{font-family:"Noto Sans CJK JP",sans-serif;}</style>'
    m = re.search(r'<svg[^>]*>', svg)
    if m and '<style>' not in svg[m.end():m.end()+200]:
        svg = svg[:m.end()] + style_block + svg[m.end():]
    return svg


def extract_svg(html_path: Path, svg_path: Path) -> int:
    """HTMLから先頭の <svg>...</svg> ブロックを抽出し、XML正規化してから保存。バイト数を返す"""
    if not html_path.exists():
        raise FileNotFoundError(f"HTML が見つからない: {html_path}")
    html = html_path.read_text(encoding="utf-8")
    # 注: 複数SVGが含まれる場合は最初の1つだけ採用（diagram-design は通常1図1HTML）
    m = re.search(r"<svg[^>]*>.*?</svg>", html, re.DOTALL)
    if not m:
        raise ValueError(f"SVG タグが HTML 内に見つからない: {html_path}")
    svg = normalize_svg(m.group(0))
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(svg, encoding="utf-8")
    return len(svg.encode("utf-8"))


def main():
    if len(sys.argv) < 3:
        print("usage: extract_svg.py <html_path> <svg_path>", file=sys.stderr)
        sys.exit(2)
    html_path = Path(sys.argv[1])
    svg_path = Path(sys.argv[2])
    size = extract_svg(html_path, svg_path)
    print(f"OK: {svg_path} ({size} bytes)")


if __name__ == "__main__":
    main()
