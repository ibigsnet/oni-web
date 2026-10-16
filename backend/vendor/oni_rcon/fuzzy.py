"""Typo-tolerant search shared by every search box (IBIGS 2026-10-16).

A query is split into terms (quotes keep a phrase together); every term must match the text. A term matches when it
is a plain substring, or when some word of the text is within a small edit distance of it (one slip for words of
4-7 letters, two from 8): swapped letters count as one slip, so "gluch" finds "Gulch" and "valhala" finds
"Valhalla". A term also matches the start of a longer word with the same tolerance, so "valhal" works while typing.
Terms under 4 letters must match exactly: short typos would match nearly everything.
"""
from __future__ import annotations

import re
import shlex
import unicodedata
from functools import lru_cache

WORD = re.compile(r"\w+")


def norm(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def terms(query: str) -> list[str]:
    q = norm(query)
    try:
        return [t for t in shlex.split(q) if t]
    except ValueError:  # a half-typed quote must not break live search
        return q.replace('"', " ").replace("'", " ").split()


def limit_for(term: str) -> int:
    return 0 if len(term) < 4 else 1 if len(term) < 8 else 2


@lru_cache(maxsize=16384)
def distance(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein (adjacent swaps) distance, or limit + 1 once it is clearly over the limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prevprev, prev = None, list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        row = [i]
        for j, y in enumerate(b, 1):
            cost = min(row[-1] + 1, prev[j] + 1, prev[j - 1] + (x != y))
            if i > 1 and j > 1 and x == b[j - 2] and a[i - 2] == y:
                cost = min(cost, prevprev[j - 2] + 1)
            row.append(cost)
        if min(row) > limit:
            return limit + 1
        prevprev, prev = prev, row
    return prev[-1]


def term_score(term: str, text: str) -> int | None:
    """0 = exact substring, 1/2 = that many slips, None = no match. `text` must already be norm()ed."""
    if term in text:
        return 0
    limit = limit_for(term)
    if not limit:
        return None
    best = limit + 1
    words = WORD.findall(text)
    # the term may span words ("highground" vs "high ground"): also try neighbouring pairs joined
    candidates = set(words) | {a + b for a, b in zip(words, words[1:])}
    for word in candidates:
        best = min(best, distance(term, word, limit))
        if len(word) > len(term):  # still typing: compare with the start of the word
            best = min(best, distance(term, word[:len(term)], limit))
        if best == 1 and limit == 1:
            break
    return best if best <= limit else None


def score(query: str, *texts) -> int | None:
    """Total slips over all terms (lower is better), or None if any term doesn't match. Empty query: 0."""
    hay = norm(" ".join(str(t or "") for t in texts))
    total = 0
    for t in terms(query):
        s = term_score(t, hay)
        if s is None:
            return None
        total += s
    return total


def matches(query: str, *texts) -> bool:
    return score(query, *texts) is not None


def rank(query: str, items, text_of):
    """Items that match, closest first (stable among equals)."""
    scored = [(score(query, *text_of(x)), i, x) for i, x in enumerate(items)]
    return [x for s, i, x in sorted((s, i, x) for s, i, x in scored if s is not None)]
