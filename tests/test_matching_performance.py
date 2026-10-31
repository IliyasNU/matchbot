"""Optimized batch scoring must preserve the discovery policy and its weights."""

import json
import math
import random
from itertools import islice

import pytest

import matching
from db.models import INTERESTS
from matching import MAX_LIKED_PROFILES, choose_candidate, score_candidate
from tests.test_matching import BranchRng, person

NOW = 1_800_000_000


def legacy_score(viewer, candidate, liked_profiles):
    """Independent pre-optimization formula, without production context helpers."""

    def tags(profile):
        value = profile.get("interests") or ()
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return set()
        if not isinstance(value, (list, tuple, set, frozenset)):
            return set()
        return {tag for tag in value if isinstance(tag, str) and tag in INTERESTS}

    def overlap(a, b):
        return len(a & b) / len(a | b) if a and b else 0.0

    def number(value):
        if isinstance(value, bool):
            return None
        try:
            value = float(value)
        except (ValueError, TypeError):
            return None
        return value if math.isfinite(value) else None

    score = 1.0 + 5 * overlap(tags(viewer), tags(candidate))
    purpose = viewer.get("purpose")
    if purpose in {"friendship", "relationship", "chat"} and purpose == candidate.get("purpose"):
        score += 3
    cities = [" ".join(str(p.get("city_key") or "").casefold().split()) for p in (viewer, candidate)]
    if cities[0] and cities[0] == cities[1]:
        score += 1.5
    a, b = number(viewer.get("age")), number(candidate.get("age"))
    if a is not None and b is not None and 18 <= a <= 100 and 18 <= b <= 100:
        score += math.exp(-abs(a - b) / 8)
    last_active = number(candidate.get("last_active"))
    if last_active is not None and last_active > 0:
        score += 0.5 / (1 + max(0.0, NOW - last_active) / 86400 / 7)
    liked = [tags(profile) for profile in islice(liked_profiles, MAX_LIKED_PROFILES)]
    liked = [value for value in liked if value]
    if liked and tags(candidate):
        score += sum(overlap(tags(candidate), value) for value in liked) / len(liked)
    return score


def test_prepared_batch_weights_match_independent_original_formula():
    rng = random.Random(1234)

    def profile(uid):
        interests = rng.sample(INTERESTS, rng.randint(0, 5))
        return person(
            uid,
            interests=json.dumps(interests) if uid % 2 else interests,
            age=rng.randint(18, 100),
            city_key=rng.choice(("ALMATY", " astana ", "", None)),
            purpose=rng.choice(("any", "friendship", "relationship", "chat")),
            last_active=rng.choice((0, None, NOW - rng.randint(0, 100) * 86400)),
        )

    for _ in range(8):
        viewer = profile(1)
        # Repeated interest sets must retain their frequency in the mean bonus.
        history = [profile(uid) for uid in range(200, 220)] * 3
        candidates = [profile(uid) for uid in range(2, 102)]
        capture = BranchRng(0.5)
        choose_candidate(viewer, candidates, iter(history), now=NOW, rng=capture)
        expected = [legacy_score(viewer, candidate, history) for candidate in candidates]
        assert capture.weights == pytest.approx(expected, rel=1e-12, abs=1e-12)
        for candidate, value in zip(candidates, expected, strict=True):
            assert score_candidate(viewer, candidate, iter(history), now=NOW) == pytest.approx(value)


def test_viewer_and_history_are_parsed_once_per_batch(monkeypatch):
    viewer = person(1, interests=["books"])
    history = [person(uid, interests=["books", "travel"]) for uid in range(100, 160)]
    candidates = [person(uid, interests=["books", "nature"]) for uid in range(1000, 2000)]
    original = matching._interests
    calls = []

    def observe(profile):
        calls.append(profile["id"])
        return original(profile)

    monkeypatch.setattr(matching, "_interests", observe)
    choose_candidate(viewer, candidates, iter(history), now=NOW, rng=BranchRng(0.5))
    assert calls.count(viewer["id"]) == 1
    assert all(calls.count(profile["id"]) == 1 for profile in history[:50])
    assert all(profile["id"] not in calls for profile in history[50:])
    assert len(calls) == len(candidates) + 1 + 50


def test_repeated_candidate_interests_share_request_local_overlap_work(monkeypatch):
    viewer = person(1, interests=["books"])
    history = [person(100, interests=["books"])] * 20 + [person(101, interests=["music"])] * 10
    candidates = [person(uid, interests=["books"]) for uid in range(1000, 11000)]
    original = matching._overlap
    calls = []

    def observe(a, b):
        calls.append((a, b))
        return original(a, b)

    monkeypatch.setattr(matching, "_overlap", observe)
    choose_candidate(viewer, candidates, history, now=NOW, rng=BranchRng(0.5))
    # One viewer overlap plus one overlap for each distinct history interest set.
    assert len(calls) == 3


def test_changed_likes_and_profile_are_visible_on_next_request():
    viewer = person(1, interests=["books"])
    candidates = [person(2, interests=["books"]), person(3, interests=["music"])]
    first = BranchRng(0.5)
    choose_candidate(viewer, candidates, [person(100, interests=["books"])], now=NOW, rng=first)
    viewer["interests"] = ["music"]
    second = BranchRng(0.5)
    choose_candidate(viewer, candidates, [person(100, interests=["music"])], now=NOW, rng=second)
    assert first.weights[0] > first.weights[1]
    assert second.weights[1] > second.weights[0]


def test_exploration_does_not_consume_history_generator():
    def history():
        raise AssertionError("Uniform exploration should not prepare a score context")
        yield  # pragma: no cover

    candidates = [person(2), person(3)]
    assert choose_candidate(person(), candidates, history(), now=NOW, rng=BranchRng(0.1)) is candidates[-1]
