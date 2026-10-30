import random

import pytest

from matching import choose_candidate, score_candidate

NOW = 1_800_000_000


def person(uid=1, **overrides):
    profile = dict(id=uid, age=25, city_key="almaty", interests=[], purpose="any", last_active=0)
    profile.update(overrides)
    return profile


def score(viewer, candidate, liked_profiles=()):
    return score_candidate(viewer, candidate, liked_profiles, now=NOW)


def test_shared_interests_raise_score_with_partial_overlap():
    viewer = person(interests=["books", "travel"])
    none = score(viewer, person(2, interests=["music", "gaming"]))
    partial = score(viewer, person(2, interests=["books", "music"]))
    exact = score(viewer, person(2, interests=["books", "travel"]))
    assert none < partial < exact


def test_same_explicit_purpose_raises_score_but_missing_is_neutral():
    viewer = person(purpose="friendship")
    assert score(viewer, person(2, purpose="friendship")) > score(viewer, person(2, purpose="relationship"))
    assert score(viewer, person(2)) == score(viewer, person(2, purpose="relationship"))
    assert score(person(), person(2)) == score(person(), person(2, purpose="friendship"))


def test_nearby_age_and_same_city_are_soft_bonuses():
    viewer = person()
    assert score(viewer, person(2)) > score(viewer, person(2, city_key="astana")) > 0
    assert score(viewer, person(2)) > score(viewer, person(2, age=40)) > 0
    assert score(viewer, person(2)) == score(viewer, person(2, city_key=" ALMATY "))


def test_activity_bonus_is_small_bounded_and_decays():
    viewer = person()
    missing = score(viewer, person(2))
    old = score(viewer, person(2, last_active=NOW - 60 * 86400))
    recent = score(viewer, person(2, last_active=NOW))
    future = score(viewer, person(2, last_active=NOW + 86400))
    assert missing < old < recent
    assert recent - missing == pytest.approx(0.5)
    assert future == recent


def test_old_profile_metadata_works_and_json_interests_match_list():
    assert score({}, {}) == 1
    viewer = person(interests=["books"])
    assert score(viewer, person(2, interests='["books"]')) == score(viewer, person(2, interests=["books"]))
    assert score(viewer, person(2, interests=None)) == score(viewer, person(2))


@pytest.mark.parametrize("interests", ["not-json", '"books"', "{}", ["unknown"], {"books": True}])
def test_unknown_or_malformed_interests_give_no_bonus(interests):
    assert score(person(interests=["books"]), person(2, interests=interests)) == score(person(), person(2))


@pytest.mark.parametrize("value", ["nan", float("inf"), "oops", True, None])
def test_invalid_activity_is_neutral(value):
    assert score(person(), person(2, last_active=value)) == score(person(), person(2))


def test_likes_learn_only_explicit_interests_and_bonus_is_bounded():
    viewer = person()
    candidate = person(2, interests=["books"])
    no_likes = score(viewer, candidate)
    likes = [person(3, interests=["books"])]
    assert score(viewer, candidate, likes) - no_likes == pytest.approx(1.0)
    assert score(viewer, candidate, likes * 1000) == score(viewer, candidate, likes)
    assert score(viewer, candidate, [person(3, age=25, city_key="almaty")]) == no_likes
    assert score(viewer, candidate, [person(3, interests=["music"])]) == no_likes


def test_only_latest_50_liked_profiles_are_considered():
    candidate = person(2, interests=["books"])
    latest = [person(3, interests=["music"])] * 50
    older = [person(4, interests=["books"])] * 1000
    assert score(person(), candidate, iter(latest + older)) == score(person(), candidate, latest)


def test_irrelevant_private_or_popularity_fields_do_not_affect_rank():
    candidate = person(2, interests=["books"])
    changed = dict(candidate, photo="different", gender="female", username="popular", likes_received=100000)
    assert score(person(), candidate) == score(person(), changed)


class BranchRng:
    """Record the selected strategy without statistical or seed-dependent assertions."""

    def __init__(self, draw):
        self.draw = draw
        self.weights = None

    def random(self):
        return self.draw

    def choice(self, candidates):
        return candidates[-1]

    def choices(self, candidates, weights, k):
        assert k == 1
        self.weights = weights
        return [candidates[0]]


def test_uniform_exploration_can_choose_weaker_profile():
    viewer = person(interests=["books"], purpose="friendship")
    strong = person(2, interests=["books"], purpose="friendship")
    weak = person(3, city_key="astana", age=60)
    rng = BranchRng(0.19)
    assert choose_candidate(viewer, [strong, weak], now=NOW, rng=rng) is weak
    assert rng.weights is None


def test_weighted_branch_preserves_positive_chance_for_weaker_profile():
    viewer = person(interests=["books"])
    strong, weak = person(2, interests=["books"]), person(3)
    rng = BranchRng(0.20)
    assert choose_candidate(viewer, [strong, weak], now=NOW, rng=rng) is strong
    assert rng.weights[0] > rng.weights[1] > 0


def test_seeded_selection_is_reproducible_and_varies():
    viewer = person(interests=["books"])
    candidates = [person(2, interests=["books"]), person(3), person(4, city_key="astana")]

    def draws(seed):
        rng = random.Random(seed)
        return [choose_candidate(viewer, candidates, now=NOW, rng=rng)["id"] for _ in range(100)]

    assert draws(42) == draws(42)
    assert len(set(draws(42))) == len(candidates)


def test_empty_pool_and_single_candidate():
    assert choose_candidate(person(), [], now=NOW) is None
    candidate = person(2)
    assert choose_candidate(person(), [candidate], now=NOW, rng=random.Random(2)) is candidate
