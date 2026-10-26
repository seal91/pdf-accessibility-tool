"""Tiny offline language detector for Danish, English and Spanish.

Stop-word frequency is plenty accurate for whole documents in these three
languages, and needs no model download. Short or mixed samples come back with
low confidence so callers can queue them for review rather than guess.
"""

from __future__ import annotations

import re
from collections import Counter

_RAW = {
    "en": """the of and to in is that for it as with was on be by this are from or not have has had were
        which their can will more also its been other than these they but all one would there about into
        our we you your who what when how such may should through between each those most any them could
        after before while because only during without""",
    "da": """og i at det en af til er den på som med for de har ikke der et var om fra kan vil man også skal
        blev efter eller sig men være sin hvor mere deres hvis disse blandt over ved mod ud under mellem samt
        både hele når hvad dette denne end meget nogle inden andre bliver blive kunne skulle været havde
        hvilke hvilket vores jeres""",
    "es": """de la que el en y a los del se las por un para con no una su al lo como más pero sus le ya o
        este porque esta entre cuando muy sin sobre también me hasta hay donde quien desde todo nos durante
        todos uno les ni contra otros ese eso ante ellos esto antes algunos qué unos otro otras otra tanto
        esa estos mucho quienes nada muchos cual poco ella estar estas algunas algo nosotros""",
}

# Drop words that appear in more than one list: they carry no signal.
_counts = Counter(w for words in _RAW.values() for w in set(words.split()))
STOP = {lang: {w for w in words.split() if _counts[w] == 1} for lang, words in _RAW.items()}
_TOKEN = re.compile(r"[a-zæøåáéíóúñü]+")


def detect_language(text: str) -> tuple[str | None, float, dict[str, float]]:
    """Return (language or None, confidence 0-1, per-language stop-word rate)."""
    tokens = _TOKEN.findall(text.lower())
    if len(tokens) < 80:
        return None, 0.0, {}
    scores = {lang: sum(1 for t in tokens if t in words) / len(tokens) for lang, words in STOP.items()}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, s1), (_, s2) = ranked[0], ranked[1]
    if s1 < 0.08:  # too few stop words: probably not one of our languages
        return None, 0.0, scores
    conf = min(1.0, (s1 - s2) / s1)
    return (best if conf >= 0.4 else None), round(conf, 2), {k: round(v, 3) for k, v in scores.items()}
