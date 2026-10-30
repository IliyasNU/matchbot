"""Small, explainable discovery ranker; scores are weights, not probabilities.

The database must apply both members' hard preferences and safety exclusions
before calling this module. Unknown optional metadata gives no bonus. A portion
of discovery stays uniformly random so newcomers and weaker scores remain visible.
"""

import json
import math
import random
import time
from itertools import islice

from db.models import INTERESTS, PURPOSES

EXPLORATION_RATE = 0.20
MAX_LIKED_PROFILES = 50
KNOWN_INTERESTS = frozenset(INTERESTS)
KNOWN_PURPOSES = PURPOSES - {"any"}


def _interests(profile):
    values = profile.get("interests") or ()
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (ValueError, TypeError):
            return frozenset()
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(value for value in values if isinstance(value, str) and value in KNOWN_INTERESTS)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _city(profile):
    return " ".join(str(profile.get("city_key") or "").casefold().split())


def _overlap(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def score_candidate(viewer, candidate, liked_profiles=(), now=None):
    """Return a positive discovery weight from explicit profile data.

    Bonuses: shared interests 0–5, identical explicit purpose 3, same city 1.5,
    nearby age 0–1, recent candidate activity 0–0.5, and similarity to interests
    on the last 50 liked profiles 0–1. The baseline is 1. No inferred sensitive
    attributes, photo analysis, popularity score or paid ranking is used.
    ``liked_profiles`` must be ordered newest first by the caller.
    """
    score = 1.0
    interests = _interests(candidate)
    score += 5.0 * _overlap(_interests(viewer), interests)

    purpose = viewer.get("purpose")
    if purpose in KNOWN_PURPOSES and purpose == candidate.get("purpose"):
        score += 3.0

    viewer_city, candidate_city = _city(viewer), _city(candidate)
    if viewer_city and viewer_city == candidate_city:
        score += 1.5

    viewer_age, candidate_age = _number(viewer.get("age")), _number(candidate.get("age"))
    if viewer_age is not None and candidate_age is not None and 18 <= viewer_age <= 100 and 18 <= candidate_age <= 100:
        score += math.exp(-abs(viewer_age - candidate_age) / 8.0)

    timestamp = _number(candidate.get("last_active"))
    current_time = _number(now) if now is not None else time.time()
    if timestamp is not None and timestamp > 0 and current_time is not None:
        days_ago = max(0.0, current_time - timestamp) / 86400.0
        score += 0.5 / (1.0 + days_ago / 7.0)

    liked_interests = [_interests(profile) for profile in islice(liked_profiles, MAX_LIKED_PROFILES)]
    liked_interests = [values for values in liked_interests if values]
    if interests and liked_interests:
        score += sum(_overlap(interests, values) for values in liked_interests) / len(liked_interests)
    return score


def choose_candidate(viewer, candidates, liked_profiles=(), now=None, rng=None):
    """Choose from eligible profiles: 20% uniform, 80% weighted random.

    Inject ``random.Random(seed)`` for reproducible simulations and tests. Every
    supplied candidate has a positive chance, including candidates with no new
    metadata. This function does not filter; eligibility belongs to the database.
    """
    candidates = list(candidates)
    if not candidates:
        return None
    rng = rng if rng is not None else random
    if rng.random() < EXPLORATION_RATE:
        return rng.choice(candidates)
    liked_profiles = tuple(islice(liked_profiles, MAX_LIKED_PROFILES))
    now = time.time() if now is None else now
    weights = [score_candidate(viewer, profile, liked_profiles, now) for profile in candidates]
    return rng.choices(candidates, weights=weights, k=1)[0]
