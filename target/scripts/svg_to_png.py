#!/usr/bin/env python3
# 目的: SVG ファイルを高DPI PNG に変換する。docx 埋め込み時、Word 2016以前や
#       一部の Word バージョンが SVG を表示できない問題（"この画像は表示できません"）の根本対処。
# 意味合い: design-doc スキルの Wave 3.5 末端処理。diagram-design 生成の SVG を Word 互換 PNG に変換し、
#           generate_docx.js が ImageRun({type:'png'}) で確実に埋め込めるようにする。
# 接続情報: 入力 = 第1引数 SVG パス / 出力 = 第2引数 PNG パス / 第3引数 = 出力幅px（デフォルト1200=高DPI）
#           呼出元 = design-doc Wave 3.5、または手動

import sys
from pathlib import Path

try:
    import cairosvg
except ImportError as e:
    print(f"FATAL: cairosvg がインストールされていない: {e}", file=sys.stderr)
    print("対処: pip install cairosvg", file=sys.stderr)
    sys.exit(1)


def svg_to_png(svg_path: Path, png_path: Path, output_width: int = 1200) -> int:
    """SVGをPNGに変換。output_widthはPNG出力幅px（高DPI想定）。バイト数を返す"""
    if not svg_path.exists():
        raise FileNotFoundError(f"SVG が見つからない: {svg_path}")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    cairosvg.svg2png(
        url=str(svg_path),
        write_to=str(png_path),
        output_width=output_width,
    )
    return png_path.stat().st_size


def main():
    if len(sys.argv) < 3:
        print("usage: svg_to_png.py <svg_path> <png_path> [output_width_px]", file=sys.stderr)
        sys.exit(2)
    svg_path = Path(sys.argv[1])
    png_path = Path(sys.argv[2])
    output_width = int(sys.argv[3]) if len(sys.argv) > 3 else 1200
    size = svg_to_png(svg_path, png_path, output_width)
    print(f"OK: {png_path} ({size} bytes, width={output_width}px)")


if __name__ == "__main__":
    main()
