from __future__ import annotations

import csv
import html
import math
import re
import tempfile
from pathlib import Path

from messengers.base import PhotoLimits

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # Tables fall back to text until the venv is re-synced.
    Image = ImageDraw = ImageFont = None

_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s*```")
_INLINE_MD = re.compile(r"\*\*|__|`|~~")
_BR = re.compile(r"\s*<br\s*/?>\s*", re.IGNORECASE)

FONT_SIZE = 28
PAD_X, PAD_Y = 18, 12
LINE_GAP = 8
MAX_TABLE_WIDTH = 1100
MIN_COL_WIDTH = 90
# Safety cap for huge tables, which get the CSV attachment anyway.
MAX_IMAGE_HEIGHT = 16000

BG = (255, 255, 255)
HEADER_BG = (232, 236, 242)
STRIPE_BG = (248, 249, 251)
BORDER = (205, 210, 218)
TEXT = (28, 31, 36)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    parts = re.split(r"(?<!\\)\|", line)
    # <br> before unescaping, so an escaped "&lt;br&gt;" stays visible text rather than a line break.
    return [html.unescape(_BR.sub("\n", _INLINE_MD.sub("", p.replace("\\|", "|")))).strip() for p in parts]


def split_tables(text: str) -> list[tuple[str, object]]:
    lines = text.split("\n")
    segments: list[tuple[str, object]] = []
    buf: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):
            in_fence = not in_fence
        is_table = (
            not in_fence
            and "|" in line
            and i + 1 < len(lines)
            and _SEPARATOR.match(lines[i + 1])
            and len(_cells(line)) == len(_cells(lines[i + 1]))
        )
        if not is_table:
            buf.append(line)
            i += 1
            continue
        header = _cells(line)
        rows = [header]
        i += 2
        while i < len(lines) and "|" in lines[i] and lines[i].strip():
            row = _cells(lines[i])
            rows.append((row + [""] * len(header))[: len(header)])
            i += 1
        if buf:
            segments.append(("text", "\n".join(buf)))
            buf = []
        segments.append(("table", rows))
    if buf:
        segments.append(("text", "\n".join(buf)))
    return [(kind, val) for kind, val in segments if kind == "table" or val.strip()]


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n") or [""]:
        line = ""
        for word in re.split(r"(\s+)", paragraph):
            candidate = line + word
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line.strip():
                lines.append(line.rstrip())
            line = word.lstrip()
            # Long URLs and unspaced Korean runs have no space to wrap at.
            while font.getlength(line) > width and len(line) > 1:
                cut = len(line)
                while cut > 1 and font.getlength(line[:cut]) > width:
                    cut -= 1
                lines.append(line[:cut])
                line = line[cut:]
        lines.append(line.rstrip())
    return lines or [""]


def _column_widths(rows: list[list[str]], font: ImageFont.FreeTypeFont) -> list[int]:
    cols = len(rows[0])

    def text_width(cell: str) -> int:
        return max(math.ceil(font.getlength(line)) for line in cell.split("\n"))

    natural = [max(text_width(r[c]) for r in rows) + 2 * PAD_X + 1 for c in range(cols)]
    budget = MAX_TABLE_WIDTH
    widths = natural[:]
    # Shrink the widest columns first, so short columns keep their natural width.
    while sum(widths) > budget:
        widest = max(range(cols), key=lambda c: widths[c])
        if widths[widest] <= MIN_COL_WIDTH:
            break
        widths[widest] -= max(1, (sum(widths) - budget) // cols or 1)
        widths[widest] = max(widths[widest], MIN_COL_WIDTH)
    return widths


def _draw_rows(block, widths, font, line_h, stripe_offset) -> Image.Image:
    height = sum(h for _, h in block) + 1
    img = Image.new("RGB", (sum(widths) + 1, height), BG)
    draw = ImageDraw.Draw(img)
    y = 0
    for idx, (wrapped, h) in enumerate(block):
        is_header = idx == 0
        bg = HEADER_BG if is_header else (STRIPE_BG if (idx + stripe_offset) % 2 == 0 else BG)
        draw.rectangle([0, y, sum(widths), y + h], fill=bg)
        x = 0
        for col, cell_lines in enumerate(wrapped):
            for n, text in enumerate(cell_lines):
                draw.text((x + PAD_X, y + PAD_Y + n * line_h), text, font=font, fill=TEXT)
            x += widths[col]
        draw.line([0, y, sum(widths), y], fill=BORDER)
        y += h
    draw.line([0, height - 1, sum(widths), height - 1], fill=BORDER)
    x = 0
    for w in widths:
        draw.line([x, 0, x, height], fill=BORDER)
        x += w
    draw.line([sum(widths), 0, sum(widths), height], fill=BORDER)
    return img


def render_table(
    rows: list[list[str]],
    font_path: Path,
    out_dir: Path,
    photo_limits: PhotoLimits | None = None,
    name: str = "table",
) -> list[Path]:
    if ImageFont is None:
        raise RuntimeError("Pillow is not installed")
    font = ImageFont.truetype(str(font_path), FONT_SIZE)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent + LINE_GAP
    widths = _column_widths(rows, font)
    laid_out = []
    for row in rows:
        wrapped = [_wrap(cell, font, w - 2 * PAD_X) for cell, w in zip(row, widths, strict=True)]
        laid_out.append((wrapped, max(len(c) for c in wrapped) * line_h + 2 * PAD_Y - LINE_GAP))

    header, body = laid_out[0], laid_out[1:]
    width = sum(widths) + 1
    max_height = MAX_IMAGE_HEIGHT
    if photo_limits:
        max_height = min(max_height, width * photo_limits.max_ratio, photo_limits.max_sum - width) - 1
    pages, current, used = [], [], header[1]
    for row in body:
        if current and used + row[1] > max_height:
            pages.append(current)
            current, used = [], header[1]
        current.append(row)
        used += row[1]
    pages.append(current)

    paths, stripe_offset = [], 0
    for n, page in enumerate(pages, 1):
        path = out_dir / (f"{name}.png" if len(pages) == 1 else f"{name}_{n}.png")
        _draw_rows([header, *page], widths, font, line_h, stripe_offset).save(path, optimize=True)
        stripe_offset += len(page)
        paths.append(path)
    return paths


def to_markdown(rows: list[list[str]]) -> str:
    def cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", "<br>")

    lines = ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    lines.insert(1, "|" + "---|" * len(rows[0]))
    return "\n".join(lines)


def write_attachments(rows: list[list[str]], out_dir: Path, name: str = "table") -> list[Path]:
    csv_path = out_dir / f"{name}.csv"
    # utf-8-sig so Excel opens Korean text correctly instead of mojibake.
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    md_path = out_dir / f"{name}.md"
    md_path.write_text(to_markdown(rows) + "\n", encoding="utf-8")
    return [csv_path, md_path]


def new_output_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="linkgravity_table_"))
