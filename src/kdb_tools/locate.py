"""Find where a quote sits on a PDF page, from the page's characters in content-stream order.

Page text from an extractor reorders lines (two columns at the same height come out
interleaved), so a sentence can be on the page and still be missing from its text. Matching
the character stream avoids that. Spacing, ligatures, curly quotes, dashes and a hyphen that
breaks a word across lines are ignored; a hyphen that ends a line is tried both dropped and kept.
"""

from __future__ import annotations

import unicodedata

QUOTES = {"‘": "'", "’": "'", "‚": "'", "“": '"', "”": '"', "„": '"'}
DASHES = {"‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-"}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(QUOTES.get(c, DASHES.get(c, c)) for c in text if not c.isspace())


def _reading_order(chars: list[dict]) -> list[dict]:
    """Characters line by line, top to bottom, and left to right within a line."""
    lines: list[list[dict]] = []
    for ch in sorted(chars, key=lambda c: c["bottom"]):
        if lines and abs(ch["bottom"] - lines[-1][0]["bottom"]) <= 2.5:
            lines[-1].append(ch)
        else:
            lines.append([ch])
    return [ch for line in lines for ch in sorted(line, key=lambda c: c["x0"])]


def _lines(chars: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for ch in sorted(chars, key=lambda c: c["bottom"]):
        if lines and abs(ch["bottom"] - lines[-1][0]["bottom"]) <= 2.5:
            lines[-1].append(ch)
        else:
            lines.append([ch])
    return lines


def _wide_gap(a: dict, b: dict) -> bool:
    return b["x0"] - a["x1"] > 2 * max(a.get("size", 10), b.get("size", 10))


def _column_order(page) -> list[dict]:
    """Characters column by column: each line is cut at its wide gaps, and the pieces in the left
    half of the page are read top to bottom before those in the right half."""
    pieces = []
    for line in _lines(page.chars):
        line.sort(key=lambda c: c["x0"])
        current = [line[0]]
        for a, b in zip(line, line[1:]):
            if _wide_gap(a, b):
                pieces.append(current)
                current = []
            current.append(b)
        pieces.append(current)
    middle = page.width / 2
    pieces.sort(key=lambda piece: (piece[0]["x0"] >= middle, piece[0]["top"], piece[0]["x0"]))
    return [ch for piece in pieces for ch in piece]


def _find(chars: list[dict], target: str, drop_breaks: bool) -> list[list[dict]]:
    flat, owner = [], []
    for i, ch in enumerate(chars):
        n = norm(ch["text"])
        nxt = chars[i + 1] if i + 1 < len(chars) else None
        if drop_breaks and n == "-" and nxt is not None and nxt["top"] > ch["bottom"]:
            continue  # a hyphen that breaks a word across lines
        flat.append(n)
        owner += [i] * len(n)
    stream = "".join(flat)
    out, at = [], stream.find(target)
    while at >= 0:
        out.append([chars[j] for j in sorted(set(owner[at : at + len(target)]))])
        at = stream.find(target, at + 1)
    return out


def _matches(page, text: str, allow_gaps: bool = False) -> list[list[dict]]:
    """The characters of every occurrence of text on the page.

    Three orders are tried in turn: the PDF's drawing order; reading order, for text drawn out of
    order, such as small capitals drawn after the rest of their line; and column order, for a
    two-column page drawn line by line straight across both columns.
    """
    target = norm(text)
    if not target:
        return []
    for order in (page.chars, _reading_order(page.chars), _column_order(page)):
        # A hyphen at the end of a line may split a word (manip-ulation) or belong to it
        # (LIBERO-SPATIAL), so both readings are tried.
        hits, seen = [], set()
        for hit in _find(order, target, True) + _find(order, target, False):
            key = tuple(id(c) for c in hit)
            if key in seen or (not allow_gaps and _crosses_columns(hit)):
                continue
            seen.add(key)
            hits.append(hit)
        if hits:
            return hits
    return []


def _crosses_columns(hit: list[dict]) -> bool:
    """True when a match jumps a wide gap within one line.

    Text read straight across a two-column page, in drawing order or in reading order, splices
    the columns into text the paper never contains, so such a match is not a quote. A table row
    has the same gaps between its cells; look one up with allow_gaps=True.
    """
    for line in _lines(hit):
        line.sort(key=lambda c: c["x0"])
        if any(_wide_gap(a, b) for a, b in zip(line, line[1:])):
            return True
    return False


def _box(page, chars: list[dict]) -> list[float]:
    top, bottom = min(c["top"] for c in chars), max(c["bottom"] for c in chars)
    return [round(min(c["x0"] for c in chars), 2), round(page.height - bottom, 2),
            round(max(c["x1"] for c in chars), 2), round(page.height - top, 2)]


def find_quote(page, text: str, allow_gaps: bool = False) -> list[list[float]]:
    """Return the bbox of every occurrence of text on a pdfplumber page.

    Each bbox is [x0, y0, x1, y1] in PDF points with the origin at the bottom left, the union
    of the matched characters. The list is empty when the text is not on the page. A match that
    jumps a gap wider than two characters within a line, such as text spliced from two columns,
    does not count unless allow_gaps is true, which a lookup of a whole table row needs.
    """
    return [_box(page, hit) for hit in _matches(page, text, allow_gaps)]


def find_quote_lines(page, text: str, allow_gaps: bool = False) -> list[list[list[float]]]:
    """Like find_quote, but give each occurrence as one box per line of text, top line first.

    A highlight drawn from these boxes covers only the quoted words, where one bbox for a
    quote that starts or ends partway through a line also covers its neighbours.
    """
    out = []
    for hit in _matches(page, text, allow_gaps):
        lines, current = [], [hit[0]]
        for prev, ch in zip(hit, hit[1:]):
            if ch["top"] > prev["bottom"] or ch["bottom"] < prev["top"] or ch["x0"] < prev["x0"] - 1:
                lines.append(current)
                current = []
            current.append(ch)
        lines.append(current)
        out.append([_box(page, line) for line in lines])
    return out


def inside(span: list[float], bbox: list[float], slack: float = 2.0) -> bool:
    """True when span lies within bbox, allowing a little slack on each side."""
    return (span[0] >= bbox[0] - slack and span[1] >= bbox[1] - slack
            and span[2] <= bbox[2] + slack and span[3] <= bbox[3] + slack)
