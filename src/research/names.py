"""Matching our players' display names against names in tournament data.

Broadcast data writes names as "Sarin, Nihal" or "GM Nihal Sarin"; our
`player_information.player_name` holds "Nihal Sarin". Matches come in grades so
the clash list (and later the LLM) can tell a certain match from a guess:

    exact    same set of name words                     "Sarin, Nihal" ~ "Nihal Sarin"
    strong   one set inside the other, ≥ 2 words shared "Firouzja, Alireza (FRA)" ~ "Alireza Firouzja"
    partial  our surname (last word, ≥ 5 letters) plus a matching first initial, or the
             surname alone; a shared given name never matches  "Maghsoodloo, P." ~ "Parham Maghsoodloo"
    hint     a surname in a free-text players line      "Nihal, Maghsoodloo, ..." ~ "Nihal Sarin"
"""
from __future__ import annotations

import re
import unicodedata

GRADES = ('exact', 'strong', 'partial', 'hint')
_TITLES = {'gm', 'im', 'fm', 'cm', 'wgm', 'wim', 'wfm', 'wcm', 'nm', 'ca'}
_PARTIAL_MIN_LEN = 5


def normalize(name: str | None) -> str:
    """'Sarin, Nihal' → 'nihal sarin': accents, punctuation and titles removed, lowercase."""
    if not name:
        return ''
    if ',' in name:
        last, first = name.split(',', 1)
        name = f'{first} {last}'
    name = unicodedata.normalize('NFKD', name)
    name = ''.join(c for c in name if not unicodedata.combining(c)).lower()
    name = re.sub(r'\([^)]*\)', ' ', name)              # "(FRA)"
    words = re.findall(r'[a-z]+', name)
    return ' '.join(w for w in words if w not in _TITLES)


def words(name: str | None) -> frozenset[str]:
    return frozenset(normalize(name).split())


def match_grade(ours: str, theirs: str) -> str | None:
    """How well our display name matches a full name from tournament data, or None."""
    a, b = words(ours), words(theirs)
    if not a or not b:
        return None
    if a == b:
        return 'exact'
    shared = a & b
    if len(shared) >= 2 and (a <= b or b <= a):
        return 'strong'
    ours_list = normalize(ours).split()
    surname = ours_list[-1]
    if len(ours_list) >= 2 and len(surname) >= _PARTIAL_MIN_LEN and surname in b:
        others = b - {surname}
        if not others or any(w[0] == ours_list[0][0] for w in others):
            return 'partial'
    return None


def hint_match(ours: str, players_hint: str | None) -> bool:
    """A free-text players line ("Nihal, Maghsoodloo, Yakubboev") names one of our words."""
    if not players_hint:
        return False
    ours_words = {w for w in words(ours) if len(w) >= 4}
    if not ours_words:
        return False
    for chunk in re.split(r'[,;/&]| vs\.? | and ', players_hint):
        chunk_words = set(normalize(chunk).split())
        if chunk_words and (chunk_words <= ours_words or (len(chunk_words) > 1 and ours_words <= chunk_words)):
            return True
    return False


def better(g1: str | None, g2: str | None) -> str | None:
    """The stronger of two grades."""
    ranked = [g for g in (g1, g2) if g]
    return min(ranked, key=GRADES.index) if ranked else None
