"""Saved optional metadata remains readable when the public catalog changes."""

import asyncio
import json

import pytest

import db.models as models
from tests.conftest import member
from tests.test_bot import service
from tests.test_preferences import make_flow


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, []),
        ("[broken", []),
        ({"books": True}, []),
        ('["retired", 1, null, " BOOKS ", "books", "music"]', ["books", "music"]),
        (list(models.INTERESTS), list(models.INTERESTS[:5])),
    ],
)
def test_legacy_interest_reader_keeps_only_supported_unique_tags(value, expected):
    assert models.read_interests(value) == expected


def test_lowered_interest_limit_trims_reads_but_keeps_writes_strict(monkeypatch):
    monkeypatch.setattr(models, "MAX_INTERESTS", 2)
    assert models.read_interests(["books", "music", "travel"]) == ["books", "music"]
    with pytest.raises(ValueError):
        models.normalize_interests(["books", "music", "travel"])


@pytest.mark.parametrize("value", ["retired", None, 1, [], {}])
def test_legacy_purpose_reader_is_neutral_for_unsupported_values(value):
    assert models.read_purpose(value) == "any"


@pytest.mark.parametrize("value", ["[broken", ["retired", "books"], [None], list(models.INTERESTS[:6]), None])
def test_new_interest_input_remains_strict(value):
    with pytest.raises(ValueError):
        models.normalize_interests(value)


def test_retired_interest_does_not_break_stored_profile_or_discovery(db, monkeypatch):
    member(db, 1, interests=["books", "music"])
    member(db, 2, interests=["books", "travel"])
    monkeypatch.setattr(models, "INTERESTS", tuple(tag for tag in models.INTERESTS if tag != "books"))
    assert db.user(1)["interests"] == ["music"]
    assert db.touch(1)["interests"] == ["music"]
    assert db.candidates(1)["interests"] == ["travel"]
    # Read compatibility does not silently rewrite the saved value.
    assert json.loads(db.conn.execute("SELECT interests FROM users WHERE id=1").fetchone()[0]) == ["books", "music"]


@pytest.mark.parametrize("stored", ["not json", "null", '"books"', "{}", '["retired", null, " MUSIC ", "music"]'])
def test_malformed_optional_metadata_is_neutral_on_read(db, stored):
    member(db, 1)
    with db.conn:
        db.conn.execute("UPDATE users SET interests=?,purpose='retired-purpose' WHERE id=1", (stored,))
    profile = db.user(1)
    assert profile["interests"] == (["music"] if "MUSIC" in stored else [])
    assert profile["purpose"] == "any"


def test_old_interest_draft_resumes_and_can_publish_with_current_metadata(db):
    profile = member(db, 1)
    flow, output = make_flow(db)
    draft = {key: profile[key] for key in ("name", "age", "gender", "seeking", "city", "bio", "photo", "consent")}
    draft.update(interests=["retired", "books"], purpose="retired-purpose", editing=True)

    async def scenario():
        await flow.prompt(1, "preview", draft)
        assert db.session(1)[1]["interests"] == ["books"]
        assert db.session(1)[1]["purpose"] == "any"
        await flow.accept(1, nonce=db.session(1)[1]["nonce"], choice="publish")

    asyncio.run(scenario())
    assert db.session(1) is None
    assert db.user(1)["interests"] == ["books"]
    assert db.user(1)["purpose"] == "any"
    assert output.card.call_args.args[1]["interests"] == ["books"]


def test_old_preview_can_publish_directly_without_resuming_prompt(db):
    profile = member(db, 1)
    flow, _ = make_flow(db)
    draft = {key: profile[key] for key in ("name", "age", "gender", "seeking", "city", "bio", "photo", "consent")}
    draft.update(interests=["retired", "books"], purpose="retired-purpose", editing=True, nonce="old-nonce")
    db.save_session(1, "preview", draft)
    asyncio.run(flow.accept(1, nonce="old-nonce", choice="publish"))
    assert db.session(1) is None
    assert db.user(1)["interests"] == ["books"]
    assert db.user(1)["purpose"] == "any"


@pytest.mark.parametrize("interests", [None, "[broken", {"books": True}])
def test_malformed_draft_interests_can_resume_without_losing_other_fields(db, interests):
    member(db, 1)
    flow, _ = make_flow(db)
    db.save_session(1, "interests", {"interests": interests, "name": "Draft name", "nonce": "old"})
    asyncio.run(flow.accept(1, nonce="old", choice="done"))
    step, draft = db.session(1)
    assert step == "bio"
    assert draft["interests"] == []
    assert draft["name"] == "Draft name"


def test_card_renders_unknown_optional_metadata_without_catalog_lookups(db):
    profile = member(db, 1)
    profile.update(interests=["retired", "books", None], purpose="retired-purpose")
    bot = service(db)
    asyncio.run(bot.card(1, profile))
    caption = bot.application.bot.send_photo.call_args.kwargs["caption"]
    assert "retired" not in caption
    assert "Books" in caption
