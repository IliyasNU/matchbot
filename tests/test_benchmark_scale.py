from types import SimpleNamespace

import pytest

from scripts.benchmark_scale import expected_ids, measure_case, population


def test_benchmark_population_is_reproducible_complete_and_wide():
    first = population(100, 42)
    assert first == population(100, 42)
    assert len(first) == 100
    assert all(len(person["bio"]) == 500 for person in first.values())
    assert expected_ids(first) == set(range(2, 101))
    assert all(person["photo"].startswith("synthetic-photo-") for person in first.values())


def test_fixture_oracle_checks_both_peoples_filters_and_all_exclusions():
    people = {uid: person for uid, person in population(100, 42).items() if uid <= 13}
    for person in people.values():
        person.update(
            age=28,
            gender="female",
            seeking="male",
            min_age=18,
            max_age=100,
            city="Almaty",
            city_key="almaty",
            local_only=0,
        )
    people[1].update(gender="male", seeking="female", min_age=24, max_age=35, local_only=1)
    people[2]["photo"] = None
    people[3]["banned"] = 1
    people[4]["active"] = 0
    people[5]["age"] = 50
    people[6]["gender"] = "other"
    people[7]["seeking"] = "female"
    people[8]["min_age"] = 30
    people[9].update(city="Astana", city_key="astana")
    assert expected_ids(people, blocks={10}, reactions={11}, pairs={12}) == {13}


def test_timing_harness_rejects_out_of_pool_profiles():
    invalid = population(100, 42)[2]
    fake_database = SimpleNamespace(candidates=lambda uid: invalid)
    with pytest.raises(AssertionError, match="eligibility"):
        measure_case(fake_database, {3}, requests=2)


def test_timing_harness_verifies_full_profile_not_only_rank_metadata():
    incomplete_payload = {"id": 2, "bio": "short", "photo": "synthetic-photo-2"}
    fake_database = SimpleNamespace(candidates=lambda uid: incomplete_payload)
    with pytest.raises(AssertionError):
        measure_case(fake_database, {2}, requests=2)


def test_timing_harness_records_counts_and_handles_empty_pools():
    fake_database = SimpleNamespace(candidates=lambda uid: None)
    result = measure_case(fake_database, set(), requests=2)
    assert result["requests"] == 2
    assert result["eligible_pool"] == 0
    assert result["filter_violations"] == 0
    assert result["distinct_selected_including_warmup"] == 0
