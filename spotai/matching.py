"""Matching a typed licence plate against what the LPR actually read.

Exact matching is not good enough. Measured across 521 live reads from one
site, **46% were shorter than a full plate**, and the shape distribution shows
characters are lost from the *left*, consistently:

    LLDDDDD  168   full read
     LDDDDD   57   one leading character lost
      DDDDD   71   two leading characters lost

So a claim for ``AB12345`` finds nothing whenever that car happened to be read
as ``12345``. It looks like "no footage" when the footage exists.

The approach here is to pull the candidate reads for a window and **rank them
locally** - a day at one site is a couple of hundred strings, which costs
nothing to score - rather than asking the API for exact matches on guessed
variants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Sequence

# Character groups an OCR engine, or a person reading a dirty plate, confuses.
_CONFUSABLE_GROUPS = (
    "0OQD", "1IL7T", "2Z", "5S", "8B", "6G", "4A", "MN", "VY", "CG", "UV",
)
_CONFUSABLE: set[tuple[str, str]] = set()
for _group in _CONFUSABLE_GROUPS:
    for _a in _group:
        for _b in _group:
            if _a != _b:
                _CONFUSABLE.add((_a, _b))

SUB_CONFUSABLE = 0.30   # a known look-alike swap barely counts
SUB_OTHER = 1.00        # an unrelated character counts fully
INDEL = 1.00

# Confidence thresholds. Calibrated against live data: real plates score >=0.78,
# and 300 plates never seen at the site never reached 0.78.
NEAR_CERTAIN = 0.92
LIKELY = 0.78
POSSIBLE = 0.62
FLOOR = 0.55            # below this, do not even offer it as a candidate

# Entry hygiene guards. Real exports contain "N/A", "TEST", "1111", "CEO",
# and pasted notes 46 characters long. Feeding those to a matcher produces
# confident nonsense.
MIN_USABLE_CHARS = 5
MAX_USABLE_CHARS = 10
PLACEHOLDERS = {
    "NA", "N/A", "NONE", "NULL", "TEST", "TESTING", "UNKNOWN", "UNK",
    "NOPLATE", "NOTAG", "NOTAGS", "TEMP", "TEMPTAG", "PENDING", "TBD",
    "XXXXX", "XXXXXXX",
    # NB: sequential digits like "12345" are deliberately NOT listed - a
    # left-truncated read of a real plate looks exactly like that.
}


def normalize(plate: str) -> str:
    """Upper-case and strip anything that is not alphanumeric.

    Absorbs the real entry noise: 13% of live entries are not upper-case and
    12% contain internal spaces (``C12 3456``).
    """
    return "".join(c for c in (plate or "").upper() if c.isalnum())


def is_usable(plate: str) -> bool:
    """Whether a typed plate is worth matching at all.

    A placeholder or a pasted note should go straight to the timestamp path
    rather than produce a confident wrong match.
    """
    p = normalize(plate)
    if not p or p in PLACEHOLDERS:
        return False
    if len(p) < MIN_USABLE_CHARS or len(p) > MAX_USABLE_CHARS:
        return False
    if len(set(p)) == 1:            # "11111", "XXXXX"
        return False
    return True


def weighted_distance(a: str, b: str) -> float:
    """Levenshtein where look-alike substitutions cost less than real ones."""
    if a == b:
        return 0.0
    if not a:
        return len(b) * INDEL
    if not b:
        return len(a) * INDEL

    previous = [j * INDEL for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        current = [i * INDEL]
        for j, cb in enumerate(b, 1):
            if ca == cb:
                sub = 0.0
            elif (ca, cb) in _CONFUSABLE:
                sub = SUB_CONFUSABLE
            else:
                sub = SUB_OTHER
            current.append(
                min(previous[j] + INDEL, current[j - 1] + INDEL, previous[j - 1] + sub)
            )
        previous = current
    return previous[-1]


def affix_score(typed: str, read: str) -> float:
    """Score truncation, which is the dominant real failure mode.

    A read that is a *suffix* of the typed plate scores highest, because the
    live data shows characters are lost from the left.
    """
    if not typed or not read:
        return 0.0
    longer, shorter = (typed, read) if len(typed) >= len(read) else (read, typed)
    if len(shorter) < 3 or shorter == longer:
        return 0.0
    ratio = len(shorter) / len(longer)
    if longer.endswith(shorter):
        return 0.55 + 0.45 * ratio
    if longer.startswith(shorter):
        return 0.45 + 0.45 * ratio
    if shorter in longer:
        return 0.35 + 0.40 * ratio
    return 0.0


def similarity(typed: str, read: str) -> float:
    """0.0 to 1.0. Combines confusion-weighted edit distance and truncation."""
    t, r = normalize(typed), normalize(read)
    if not t or not r:
        return 0.0
    if t == r:
        return 1.0
    edit = 1.0 - (weighted_distance(t, r) / max(len(t), len(r)))
    return max(0.0, edit, affix_score(t, r))


def confidence_band(score: float) -> str:
    """near-certain | likely | possible | weak."""
    if score >= NEAR_CERTAIN:
        return "near-certain"
    if score >= LIKELY:
        return "likely"
    if score >= POSSIBLE:
        return "possible"
    return "weak"


@dataclass
class PlateCandidate:
    """One LPR read, scored against what someone typed."""

    plate: str
    score: float
    band: str
    visits: int
    first_seen: datetime
    last_seen: datetime

    @property
    def auto_acceptable(self) -> bool:
        """Safe to attach footage without a human confirming."""
        return self.score >= LIKELY

    def to_dict(self) -> dict[str, Any]:
        return {
            "plate": self.plate,
            "score": round(self.score, 3),
            "band": self.band,
            "visits": self.visits,
        }


def rank_candidates(
    typed: str,
    sightings: Sequence[Any],
    limit: int = 5,
    floor: float = FLOOR,
) -> list[PlateCandidate]:
    """Rank LPR sightings against a typed plate, best first.

    ``sightings`` are PlateSighting objects (from ``lpr.lookup_plate``).
    Returns an empty list when nothing clears ``floor`` - which is the correct
    answer when the car was simply never read, and better than forcing a match
    onto the wrong vehicle's footage.
    """
    if not is_usable(typed):
        return []

    scored: list[PlateCandidate] = []
    for s in sightings:
        score = similarity(typed, s.plate)
        if score < floor:
            continue
        scored.append(
            PlateCandidate(
                plate=s.plate,
                score=score,
                band=confidence_band(score),
                visits=s.visits,
                first_seen=s.first_seen,
                last_seen=s.last_seen,
            )
        )
    scored.sort(key=lambda c: (-c.score, c.first_seen))
    return scored[:limit]


def is_ambiguous(candidates: Iterable[PlateCandidate], gap: float = 0.10) -> bool:
    """True when the top two candidates are too close to call.

    On live data the median gap to the runner-up was 0.43, so this should fire
    rarely - but when it does, a human should choose.
    """
    top = list(candidates)[:2]
    return len(top) == 2 and (top[0].score - top[1].score) < gap
