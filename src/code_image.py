#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""code_image.py — 把代码渲染成一张「人类可读」的 PNG（发微信用）

为什么需要它：微信里发纯文本代码，长行会被折行、缩进会丢、看不出结构；发一张
排版好的代码图，手机上扫一眼就能看清逻辑。所以算法题的答案优先发代码图。

特点：
  * 本地渲染，不依赖浏览器/无头 Chrome，离线可用（pygments 着色 + Pillow 绘制）
  * 中文注释不会变豆腐块：等宽字体 + CJK 字体逐字混排（自动按字符宽度推进）
  * 深色 / 浅色主题（可指定任意 pygments 主题名）
  * 行号、标题栏、指定行高亮、页脚（复杂度/思路一行）
  * 自适应宽度：太长自动降字号，保证在手机上一屏能看全
  * 超长代码可 --split-lines 切成多张（part1/part2…）

用法::

    python code_image.py --in Solution.java --out code.png --title "T3 · 两数之和" --lang java
    python code_image.py --in Solution.java --title "解题代码" --highlight 5,6-8 --footer "O(n) 时间 / O(1) 空间"
    echo 'print(1)' | python code_image.py --in - --lang python --out a.png
    python code_image.py --in a.py --out a.png --theme light --font-size 30 --line-numbers
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.stderr.write("缺少依赖 Pillow: %s -m pip install pillow\n" % sys.executable)
    raise SystemExit(1)

try:
    from pygments import lex
    from pygments.lexers import get_lexer_by_name, get_lexer_for_filename, guess_lexer
    from pygments.styles import get_style_by_name
    from pygments.token import Token
    from pygments.util import ClassNotFound
except ImportError:
    sys.stderr.write("缺少依赖 pygments: %s -m pip install pygments\n" % sys.executable)
    raise SystemExit(1)

def _console_safe() -> None:
    """Windows 控制台默认是 GBK(cp936)：打印 ✓ ✗ ⚠ 这类符号会直接 UnicodeEncodeError 崩掉。
    这里只把错误策略放宽成 replace —— 保留控制台原生编码（中文照常显示），个别符号降级成 ?。
    想全 UTF-8：set PYTHONUTF8=1 或 chcp 65001。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


_console_safe()


VERSION = "1.0.0"

THEME_ALIASES = {"dark": "one-dark", "light": "friendly", "mono": "bw"}

MONO_CANDIDATES: Sequence[Tuple[str, int]] = (
    ("/System/Library/Fonts/Menlo.ttc", 0),                    # macOS
    ("/System/Library/Fonts/Monaco.ttf", 0),
    ("/System/Library/Fonts/SFNSMono.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Courier New.ttf", 0),
    ("C:/Windows/Fonts/consola.ttf", 0),                       # Windows
    ("C:/Windows/Fonts/Consolas.ttf", 0),
    ("C:/Windows/Fonts/cour.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 0),  # Linux
    ("/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansMono-Regular.ttf", 0),
)
MONO_BOLD_CANDIDATES: Sequence[Tuple[str, int]] = (
    ("/System/Library/Fonts/Menlo.ttc", 1),
    ("/System/Library/Fonts/Supplemental/Courier New Bold.ttf", 0),
    ("C:/Windows/Fonts/consolab.ttf", 0),
    ("C:/Windows/Fonts/courbd.ttf", 0),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 0),
    ("/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf", 0),
)
CJK_CANDIDATES: Sequence[Tuple[str, int]] = (
    ("/System/Library/Fonts/PingFang.ttc", 0),                 # macOS 简体
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("C:/Windows/Fonts/msyh.ttc", 0),                          # Windows 微软雅黑
    ("C:/Windows/Fonts/msyh.ttf", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),  # Linux
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
)


# ----------------------------- 字体 -----------------------------

def load_first(candidates: Sequence[Tuple[str, int]], size: int) -> Optional[ImageFont.FreeTypeFont]:
    for path, index in candidates:
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size=size, index=index)
        except OSError:
            continue
    return None


class Fonts:
    def __init__(self, size: int) -> None:
        self.size = size
        self.mono = load_first(MONO_CANDIDATES, size)
        self.mono_bold = load_first(MONO_BOLD_CANDIDATES, size) or self.mono
        self.cjk = load_first(CJK_CANDIDATES, size) or self.mono
        self.cjk_bold = self.cjk
        if self.mono is None:
            self.mono = ImageFont.load_default(size=size)
            self.mono_bold = self.mono
            self.cjk = self.cjk or self.mono
        self._width_cache: Dict[Tuple[int, str], float] = {}

    def pick(self, ch: str, bold: bool = False) -> ImageFont.FreeTypeFont:
        wide = unicodedata.east_asian_width(ch) in ("W", "F") or ord(ch) > 0x2E7F
        if wide:
            return self.cjk_bold if bold else self.cjk          # type: ignore[return-value]
        return self.mono_bold if bold else self.mono            # type: ignore[return-value]

    def width(self, text: str, font: ImageFont.FreeTypeFont) -> float:
        key = (id(font), text)
        cached = self._width_cache.get(key)
        if cached is None:
            try:
                cached = float(font.getlength(text))
            except AttributeError:                              # 老 Pillow 兜底
                cached = float(font.getsize(text)[0])
            if len(self._width_cache) > 20000:
                self._width_cache.clear()
            self._width_cache[key] = cached
        return cached

    def metrics(self) -> Tuple[int, int]:
        try:
            ascent, descent = self.mono.getmetrics()
        except AttributeError:
            ascent, descent = self.size, int(self.size * 0.25)
        return int(ascent), int(descent)


# ----------------------------- 着色 -----------------------------

def style_map(theme: str) -> Tuple[Any, str, str]:
    name = THEME_ALIASES.get(theme, theme)
    try:
        style = get_style_by_name(name)
    except ClassNotFound:
        sys.stderr.write("[警告] 未知主题 %r，回退到 one-dark\n" % theme)
        style = get_style_by_name("one-dark")
    bg = style.background_color or "#1e1e1e"
    fg = "#d4d4d4"
    for token in (Token.Text, Token):
        entry = style.style_for_token(token)
        if entry.get("color"):
            fg = "#" + entry["color"]
            break
    return style, bg, fg


def lex_lines(code: str, lang: str, filename: str, theme: str) -> List[List[Tuple[str, str, bool, Optional[str], bool]]]:
    """代码 -> 每行的 [(文本, 前景色, 粗体, 背景色, 斜体)]"""
    lexer = None
    if lang:
        try:
            lexer = get_lexer_by_name(lang)
        except ClassNotFound:
            lexer = None
    if lexer is None and filename and os.path.exists(filename):
        try:
            lexer = get_lexer_for_filename(filename)
        except ClassNotFound:
            lexer = None
    if lexer is None:
        try:
            lexer = guess_lexer(code)
        except Exception:
            from pygments.lexers.special import TextLexer
            lexer = TextLexer()

    style, _bg, _fg = style_map(theme)
    lines: List[List[Tuple[str, str, bool, Optional[str], bool]]] = [[]]
    for ttype, value in lex(code, lexer):
        entry = style.style_for_token(ttype)
        color = ("#" + entry["color"]) if entry.get("color") else _fg
        bgcolor = ("#" + entry["bgcolor"]) if entry.get("bgcolor") else None
        bold, italic = bool(entry.get("bold")), bool(entry.get("italic"))
        parts = value.split("\n")
        for i, part in enumerate(parts):
            if i:
                lines.append([])
            if part:
                lines[-1].append((part, color, bold, bgcolor, italic))
    while lines and not lines[-1]:
        lines.pop()
    return lines or [[]]


def expand_tabs(text: str, tab: int = 4) -> str:
    return text.replace("\t", " " * tab)


# ----------------------------- 渲染 -----------------------------

def parse_highlights(spec: str) -> List[int]:
    out: List[int] = []
    for chunk in (spec or "").replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            if a.isdigit() and b.isdigit():
                out.extend(range(int(a), int(b) + 1))
        elif chunk.isdigit():
            out.append(int(chunk))
    return out


def render(code: str, opts: argparse.Namespace) -> List[Image.Image]:
    style, bg, fg = style_map(opts.theme)
    lexed = lex_lines(code, opts.lang, opts.input, opts.theme)
    lines = [expand_tabs("".join(part for part, *_ in line_parts)) for line_parts in lexed]
    if not lines:
        lines = [""]
    highlight = set(parse_highlights(opts.highlight))
    start_line = int(opts.start_line)

    scale = max(1, int(opts.scale))
    padding = int(opts.padding)
    title_h = 0
    if opts.title:
        # 注意：这里必须是设备像素（乘过 scale），否则标题区会和代码第一行重叠
        title_h = int(opts.font_size * scale * (2.6 if opts.subtitle else 2.1))
    footer_h = int(opts.font_size * scale * 1.9) if opts.footer else 0

    # 自适应字号：宽度超限就逐档降字号，保证手机上字够大又不被压扁
    chosen: Optional[Tuple[Fonts, int, int, List[float]]] = None
    size = int(opts.font_size)
    while True:
        fonts = Fonts(size * scale)
        ascent, descent = fonts.metrics()
        line_h = ascent + descent + int(size * scale * 0.42)
        gutter = 0
        if opts.line_numbers:
            digits = len(str(start_line + len(lines) - 1))
            gutter = int(fonts.width("0" * digits, fonts.mono) + size * scale * 1.1)
        widths = []
        for line_parts in lexed:
            w = 0.0
            for part, _c, bold, _bgc, _i in line_parts:
                for ch in expand_tabs(part):
                    w += fonts.width(ch, fonts.pick(ch, bold))
            widths.append(w)
        code_w = max(widths) if widths else 0
        total_w = int(padding * 2 * scale + gutter + code_w + (padding * scale * 0.5))
        if opts.max_width <= 0 or total_w <= opts.max_width or size <= 12:
            chosen = (fonts, line_h, gutter, widths)
            break
        size -= 1

    fonts, line_h, gutter, widths = chosen                     # type: ignore[misc]
    ascent, descent = fonts.metrics()
    pad = padding * scale
    body_h = line_h * len(lines)
    width = int(max(pad * 2 + gutter + (max(widths) if widths else 0) + pad * 0.5,
                    fonts.width(opts.title or "", fonts.cjk) + pad * 2 if opts.title else 0,
                    fonts.width(opts.subtitle or "", fonts.cjk) + pad * 2 if opts.subtitle else 0,
                    fonts.width(opts.footer or "", fonts.cjk) + pad * 2 if opts.footer else 0,
                    opts.min_width))
    height = int(pad * 1.4 + title_h + body_h + pad * (1.2 if footer_h else 0.9) + footer_h)

    img = Image.new("RGB", (max(1, width), max(1, height)), bg)
    draw = ImageDraw.Draw(img)

    # 标题栏
    y = int(pad * 0.9)
    if opts.title:
        draw.text((pad, y), opts.title, font=fonts.cjk, fill=opts.title_color or fg)
        if opts.subtitle:
            sub_size = max(9, int(opts.font_size * 0.72)) * scale
            sub_font = Fonts(sub_size).cjk
            draw.text((pad, y + int(opts.font_size * scale * 1.35)), opts.subtitle, font=sub_font, fill=opts.dim_color)
        y += title_h
    code_top = y
    # 分隔线
    if opts.title and opts.divider:
        draw.line([(pad * 0.6, code_top - int(pad * 0.28)), (width - pad * 0.6, code_top - int(pad * 0.28))],
                  fill=opts.dim_color, width=max(1, scale // 2 or 1))

    # 代码区
    for idx, line_parts in enumerate(lexed):
        cy = code_top + idx * line_h
        line_no = start_line + idx
        if line_no in highlight:
            draw.rectangle([(pad * 0.35, cy - int(line_h * 0.08)), (width - pad * 0.35, cy + line_h - int(line_h * 0.12))],
                           fill=opts.highlight_bg)
        if opts.line_numbers:
            num = str(line_no)
            num_w = fonts.width(num, fonts.mono)
            draw.text((pad * 0.5 + gutter - num_w - size * scale * 0.35, cy + int(size * scale * 0.06)),
                      num, font=fonts.mono, fill=opts.dim_color)
        if not line_parts:
            continue
        x = pad + gutter
        baseline = cy + int(size * scale * 0.06)
        # 先铺 token 背景（例如错误/删除标记），再画文字
        run_x = x
        for part, color, bold, bgcolor, _italic in line_parts:
            part = expand_tabs(part)
            for ch in part:
                font = fonts.pick(ch, bold)
                w = fonts.width(ch, font)
                if bgcolor:
                    draw.rectangle([(run_x, cy), (run_x + w, cy + line_h)], fill="#" + bgcolor.lstrip("#"))
                draw.text((run_x, baseline), ch, font=font, fill=color,
                          stroke_width=1 if bold and fonts.mono_bold is fonts.mono else 0,
                          stroke_fill=color)
                run_x += w

    # 页脚
    if opts.footer:
        fy = height - pad * 0.8 - footer_h + int(size * scale * 0.25)
        draw.text((pad, fy), opts.footer, font=fonts.cjk, fill=opts.dim_color)

    if opts.border:
        draw.rectangle([(0, 0), (width - 1, height - 1)], outline=opts.border)
    return [img]


def split_code(code: str, split_lines: int) -> List[str]:
    if split_lines <= 0:
        return [code]
    lines = code.splitlines()
    if len(lines) <= split_lines:
        return [code]
    return ["\n".join(lines[i:i + split_lines]) for i in range(0, len(lines), split_lines)]


# ----------------------------- 入口 -----------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="把代码渲染成 PNG（发微信用）")
    ap.add_argument("--in", dest="input", default="-", help="源码文件（- 表示 stdin）")
    ap.add_argument("--out", default="code.png", help="输出 PNG（多张时自动加 _p1/_p2）")
    # 注意：默认留空是有意的 —— 留空才会按文件名猜 lexer。
    # 设成 "java" 会让 lex_lines() 的 if lang: 分支永远命中，渲染 .py/.cpp 也用 Java 着色。
    ap.add_argument("--lang", default="", help="语言（java/python/cpp/go/... 留空则按文件名猜）")
    ap.add_argument("--title", default="", help="标题（如：T3 · 两数之和）")
    ap.add_argument("--subtitle", default="", help="副标题（如：Python · ACM 模式）")
    ap.add_argument("--footer", default="", help="页脚（如：O(n) 时间 / O(1) 空间）")
    ap.add_argument("--theme", default="dark", help="主题：dark/light 或任意 pygments 主题名")
    ap.add_argument("--font-size", type=int, default=26, help="逻辑字号（默认 26）")
    ap.add_argument("--scale", type=int, default=2, help="渲染倍率，2=视网膜清晰（默认 2）")
    ap.add_argument("--max-width", type=int, default=1800, help="最大像素宽，超出自动降字号（0=不限）")
    ap.add_argument("--min-width", type=int, default=0, help="最小像素宽")
    ap.add_argument("--padding", type=int, default=22, help="内边距（逻辑像素）")
    ap.add_argument("--line-numbers", action="store_true", default=True, help="显示行号（默认开）")
    ap.add_argument("--no-line-numbers", dest="line_numbers", action="store_false", help="不显示行号")
    ap.add_argument("--start-line", type=int, default=1, help="起始行号（默认 1）")
    ap.add_argument("--highlight", default="", help="高亮行，如 3,5-7")
    ap.add_argument("--highlight-bg", default="#3a3f4b", help="高亮行底色")
    ap.add_argument("--divider", action="store_true", default=True, help="标题下画分隔线")
    ap.add_argument("--no-divider", dest="divider", action="store_false")
    ap.add_argument("--border", default="", help="外框颜色（如 #444）")
    ap.add_argument("--dim-color", default="#8b93a7", help="弱化色（行号/副标题/页脚）")
    ap.add_argument("--title-color", default="", help="标题色")
    ap.add_argument("--split-lines", type=int, default=0, help="超过 N 行则切成多张（0=不切）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果（路径/尺寸）")
    return ap


def main() -> int:
    opts = build_parser().parse_args()
    if opts.input == "-":
        code = sys.stdin.read()
        filename = ""
    else:
        filename = opts.input
        if not os.path.exists(filename):
            sys.stderr.write("[错误] 源码文件不存在: %s\n" % filename)
            return 2
        with open(filename, "r", encoding="utf-8") as f:
            code = f.read()
    if not code.strip():
        sys.stderr.write("[错误] 源码为空\n")
        return 2
    code = code.rstrip("\n") + "\n"

    parts = split_code(code, int(opts.split_lines))
    results: List[Dict[str, Any]] = []
    for index, part in enumerate(parts, 1):
        if len(parts) == 1:
            out_path = opts.out
        else:
            base, ext = os.path.splitext(opts.out)
            out_path = "%s_p%d%s" % (base, index, ext or ".png")
        images = render(part, opts)
        img = images[0]
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        img.save(out_path, "PNG", optimize=True)
        results.append({"path": os.path.abspath(out_path), "width": img.width, "height": img.height,
                        "lines": len(part.splitlines()), "bytes": os.path.getsize(out_path)})
        print("[代码图] %s  %dx%d  %d 行  %.0f KB"
              % (out_path, img.width, img.height, len(part.splitlines()), os.path.getsize(out_path) / 1024))
    if opts.json:
        print(json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
