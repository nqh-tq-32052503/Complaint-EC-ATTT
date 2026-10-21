"""Assign a Markdown heading depth (1-6) to each layout block labeled
'SectionHeader'.

Surya's layout model tells us a block IS a heading, but not what *level* it
is — there's no font-size field in LayoutBox, and even if there were, level
in a Vietnamese hanh-chinh document is conventionally signalled by numbering
scheme, not type size (e.g. "I. TINH HINH CHUNG" and "1. Tam tu..." are often
rendered in a similar weight). So level is derived, in priority order, from:

  1. The numbering prefix of the heading's own OCR'd text:
       "I."/"II." (Roman)                          -> level 1
       "PHAN"/"CHUONG"/"MUC" keyword prefix          -> level 1
       "1." / "2." ...        (1 numeric segment)    -> level 2
       "1.1." / "3.5." ...    (2 numeric segments)    -> level 3
       "1.1.1." / "3.5.2."... (3 numeric segments)    -> level 4
       "a)" / "b)" (letter)                          -> one level below
                                                          the previous heading
  2. If no numbering is found: relative box height vs. the page's median
     text-line height (from the Detection API), as a font-size proxy.
  3. Hierarchy is kept sane by never letting the level jump down by more
     than one step from the previous heading in reading order (you can't go
     straight from a level-1 to a level-4 heading; you *can* jump back up
     from level-4 to level-1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from bs4 import BeautifulSoup, NavigableString, Tag
import statistics
from typing import List

_WS_RE = re.compile(r"[ \t]+")


_ROMAN_RE = re.compile(r"^\s*[IVXLCDM]+\.\s+\S")
_NUMERIC_RE = re.compile(r"^\s*((?:\d+\.)+)\s*\S")
_LETTER_RE = re.compile(r"^\s*[a-zđA-ZĐ]\)\s+\S")
_KEYWORD_RE = re.compile(r"^\s*(PH[ẦA]N|CH[ƯU][ƠO]NG|M[ỤU]C)\b", re.IGNORECASE)

MAX_LEVEL = 6


def _numbering_level(text: str) -> Optional[int]:
    t = text.strip()
    if _KEYWORD_RE.match(t):
        return 1
    if _ROMAN_RE.match(t):
        return 1
    m = _NUMERIC_RE.match(t)
    if m:
        segments = [s for s in m.group(1).split(".") if s]
        return min(len(segments) + 1, MAX_LEVEL)
    return None


def _is_lettered(text: str) -> bool:
    return bool(_LETTER_RE.match(text.strip()))


@dataclass
class HeadingLevelTracker:
    """Stateful: call classify() once per heading, in reading order, for one
    document (reset per document, not per page — numbering spans pages)."""

    _last_level: int = 0

    def classify(
        self,
        text: str,
        box_height: float,
        page_median_line_height: Optional[float],
    ) -> int:
        level = _numbering_level(text)

        if level is None and _is_lettered(text):
            level = min((self._last_level or 3) + 1, MAX_LEVEL)

        if level is None:
            level = self._font_size_level(box_height, page_median_line_height)

        # Don't allow an unexplained jump of more than one level deeper than
        # the previous heading (a missing "3." between "I." and "3.1." would
        # otherwise produce an invalid H1 -> H3 jump).
        if self._last_level and level > self._last_level + 1:
            level = self._last_level + 1

        self._last_level = level
        return level

    @staticmethod
    def _font_size_level(
        box_height: float, page_median_line_height: Optional[float]
    ) -> int:
        if not page_median_line_height or page_median_line_height <= 0:
            return 2
        ratio = box_height / page_median_line_height
        if ratio >= 2.0:
            return 1
        if ratio >= 1.6:
            return 2
        if ratio >= 1.25:
            return 3
        return 4


def _format_inline_tag(name: str, inner: str, node: Tag) -> str:
    stripped = inner.strip()
    if not stripped:
        return ""
    if name in ("b", "strong"):
        return f"**{stripped}**"
    if name in ("i", "em"):
        return f"*{stripped}*"
    if name == "del":
        return f"~~{stripped}~~"
    if name == "u":
        return f"<u>{stripped}</u>"
    if name in ("sup", "sub"):
        return f"<{name}>{stripped}</{name}>"
    if name == "code":
        return f"`{stripped}`"
    if name == "a":
        href = node.get("href")
        return f"[{stripped}]({href})" if href else inner
    return inner


def _inline(node) -> str:
    """Render inline-level content (b/i/u/del/sup/sub/a/span/br/code/text)."""
    if isinstance(node, NavigableString):
        return _WS_RE.sub(" ", str(node))
    if not isinstance(node, Tag):
        return ""

    name = node.name.lower()
    if name == "br":
        return "\n"

    inner = "".join(_inline(c) for c in node.children)
    return _format_inline_tag(name, inner, node)


def _list_to_md(node: Tag, ordered: bool, depth: int = 0) -> str:
    lines = []
    idx = 1
    for li in node.find_all("li", recursive=False):
        prefix = "  " * depth + (f"{idx}. " if ordered else "- ")
        # Separate nested lists from the li's own inline text
        nested = li.find(["ul", "ol"], recursive=False)
        text_parts = []
        for c in li.children:
            if isinstance(c, Tag) and c.name.lower() in ("ul", "ol"):
                continue
            text_parts.append(_inline(c))
        text = _WS_RE.sub(" ", "".join(text_parts)).strip()
        lines.append(prefix + text)
        if nested is not None:
            lines.append(_list_to_md(nested, nested.name.lower() == "ol", depth + 1))
        idx += 1
    return "\n".join(lines)


def _render_soup_node(node) -> Optional[str]:
    """Chuyển đổi một node con của soup thành chuỗi Markdown."""
    if isinstance(node, NavigableString):
        text = _WS_RE.sub(" ", str(node)).strip()
        return text if text else None
    if not isinstance(node, Tag):
        return None
    name = node.name.lower()
    if name in ("ul", "ol"):
        return _list_to_md(node, ordered=(name == "ol"))
    if name == "hr":
        return "---"
    if name in ("h1", "h2", "h3", "h4", "h5"):
        level = int(name[1])
        text = _WS_RE.sub(" ", _inline(node)).strip()
        return f"{'#' * level} {text}" if text else None
    text = _WS_RE.sub(" ", _inline(node)).strip()
    return text if text else None


def block_html_to_markdown(html: str) -> str:
    """Convert one block's OCR HTML into a Markdown string (no heading
    markers are added here — heading level is a layout-level decision made
    by pipeline.heading, not something this function infers from content)."""
    if not html or not html.strip():
        return ""
    soup = BeautifulSoup(html, "html.parser")

    # If the fragment has no block-level wrapper (just inline text), treat
    # the whole thing as one paragraph.
    block_tags = soup.find_all(
        ["p", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "hr"], recursive=False
    )
    if not block_tags:
        return _WS_RE.sub(" ", _inline(soup)).strip()

    parts = []
    for node in soup.children:
        rendered = _render_soup_node(node)
        if rendered:
            parts.append(rendered)

    return "\n\n".join(p for p in parts if p.strip())



# Running headers/footers repeat on every page and aren't document content.
_DROP_LABELS = {"PageHeader", "PageFooter"}
# Never OCR'd by design (see SKIP_OCR_LABELS in surya.inference.prompts).
_VISUAL_LABELS = {"Picture", "Figure", "Diagram", "BlankPage"}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _plain_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    return " ".join(soup.get_text(" ").split())


def _box_height(polygon) -> float:
    ys = [p[1] for p in polygon]
    return max(ys) - min(ys)


def _page_median_line_height(detection_result) -> Optional[float]:
    if detection_result is None:
        return None
    heights = [
        h
        for h in (_box_height(b.polygon) for b in detection_result.bboxes)
        if h > 0
    ]
    return statistics.median(heights) if heights else None


# ---------------------------------------------------------------------------
# Page/document assembly (operates on native surya result objects)
# ---------------------------------------------------------------------------


def _process_layout_pair(
    box,
    blk,
    tracker: HeadingLevelTracker,
    median_h: Optional[float],
) -> Optional[str]:
    """Xử lý một cặp bounding box và khối nhận dạng OCR thành Markdown."""
    label = box.label
    if label in _DROP_LABELS or blk.skipped or label in _VISUAL_LABELS or blk.error or not blk.html:
        return None

    if label == "SectionHeader":
        text = _plain_text(blk.html)
        if not text:
            return None
        level = tracker.classify(text, _box_height(box.polygon), median_h)
        return "#" * level + " " + text

    md = block_html_to_markdown(blk.html)
    if not md.strip():
        return None

    if label in ("Footnote", "Caption"):
        return f"*{md}*"
    if label == "Code":
        return f"```\n{md}\n```"
    return md


def page_to_markdown(
    layout_result,       # surya.layout.schema.LayoutResult
    recognition_result,  # surya.recognition.schema.PageOCRResult
    detection_result,    # surya.detection.schema.TextDetectionResult | None
    tracker: HeadingLevelTracker,
) -> str:
    median_h = _page_median_line_height(detection_result)

    # Recognition builds its blocks 1:1 over layout_result.bboxes (same
    # index, `reading_order` copied from `position`); zip is safe, sorting
    # by position is just a defensive no-op.
    pairs = sorted(
        zip(layout_result.bboxes, recognition_result.blocks),
        key=lambda p: p[0].position,
    )

    out_lines: List[str] = []
    for box, blk in pairs:
        line = _process_layout_pair(box, blk, tracker, median_h)
        if line:
            out_lines.append(line)

    return "\n\n".join(out_lines)
