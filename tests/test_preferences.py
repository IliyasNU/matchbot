import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers.profile import ProfileFlow
from i18n import tr
from tests.conftest import member


def make_flow(db):
    service = SimpleNamespace(db=db, send=AsyncMock(), card=AsyncMock(), menu=AsyncMock())
    return ProfileFlow(service), service


async def choose(flow, db, uid, choice):
    await flow.accept(uid, nonce=db.session(uid)[1]["nonce"], choice=choice)


def message(text=None, photo=None):
    return SimpleNamespace(text=text, photo=photo or [])


def test_registration_collects_and_saves_preferences(db):
    db.touch(1)
    flow, service = make_flow(db)
    draft = dict(name="Alex", age=24, gender="other", seeking="any", consent=1)

    async def scenario():
        await flow.prompt(1, "city", draft)
        await flow.accept(1, message=message("Almaty"))
        assert db.session(1)[0] == "purpose"
        await choose(flow, db, 1, "friendship")
        assert db.session(1)[0] == "interests"
        await choose(flow, db, 1, "interest_nature")
        await choose(flow, db, 1, "interest_travel")
        await choose(flow, db, 1, "done")
        assert db.session(1)[0] == "bio"
        await choose(flow, db, 1, "skip")
        await flow.accept(1, message=message(photo=[SimpleNamespace(file_id="photo1")]))
        assert db.session(1)[0] == "preview"
        await choose(flow, db, 1, "publish")

    asyncio.run(scenario())
    user = db.user(1)
    assert user["purpose"] == "friendship"
    assert set(user["interests"]) == {"nature", "travel"}
    assert user["active"] and db.session(1) is None
    service.card.assert_awaited_once()


def test_sixth_interest_is_rejected_and_selection_can_be_changed(db):
    db.touch(1)
    flow, service = make_flow(db)

    async def scenario():
        await flow.prompt(1, "interests", {})
        for key in ("music", "movies", "books", "gaming", "sports"):
            await choose(flow, db, 1, "interest_" + key)
        await choose(flow, db, 1, "interest_tech")
        assert set(db.session(1)[1]["interests"]) == {"music", "movies", "books", "gaming", "sports"}
        assert any(call.args[1] == tr("en", "interests_limit") for call in service.send.call_args_list)
        await choose(flow, db, 1, "interest_movies")
        await choose(flow, db, 1, "interest_tech")
        await choose(flow, db, 1, "done")

    asyncio.run(scenario())
    assert db.session(1)[0] == "bio"
    assert set(db.session(1)[1]["interests"]) == {"music", "tech", "books", "gaming", "sports"}


def test_interest_toggle_rotates_nonce_and_rejects_previous_button(db):
    db.touch(1)
    flow, service = make_flow(db)

    async def scenario():
        await flow.prompt(1, "interests", {})
        old_nonce = db.session(1)[1]["nonce"]
        await flow.accept(1, nonce=old_nonce, choice="interest_music")
        assert db.session(1)[1]["nonce"] != old_nonce
        await flow.accept(1, nonce=old_nonce, choice="interest_tech")

    asyncio.run(scenario())
    assert db.session(1)[1]["interests"] == ["music"]
    assert service.send.call_args.args[1] == tr("en", "draft_stale")


@pytest.mark.parametrize("choice", ["skip", "done"])
def test_interests_are_optional(db, choice):
    db.touch(1)
    flow, _ = make_flow(db)

    async def scenario():
        await flow.prompt(1, "interests", {})
        if choice == "skip":
            await choose(flow, db, 1, "interest_music")
        await choose(flow, db, 1, choice)

    asyncio.run(scenario())
    assert db.session(1)[0] == "bio"
    assert db.session(1)[1]["interests"] == []


def test_edit_keep_uses_saved_interests_after_draft_toggles(db):
    member(db, 1, interests=["books"], purpose="relationship")
    flow, _ = make_flow(db)

    async def scenario():
        await flow.begin(1, editing=True)
        draft = db.session(1)[1]
        assert draft["purpose"] == "relationship" and draft["interests"] == ["books"]
        await flow.prompt(1, "interests", draft)
        await choose(flow, db, 1, "interest_tech")
        assert set(db.session(1)[1]["interests"]) == {"books", "tech"}
        await choose(flow, db, 1, "keep")

    asyncio.run(scenario())
    assert db.session(1)[0] == "bio"
    assert db.session(1)[1]["interests"] == ["books"]
    assert db.user(1)["interests"] == ["books"]


def test_old_preview_draft_without_preferences_can_publish(db):
    user = member(db, 1)
    flow, _ = make_flow(db)
    old_draft = {key: user[key] for key in ("name", "age", "gender", "seeking", "city", "bio", "photo", "consent")}
    old_draft["editing"] = True

    async def scenario():
        await flow.prompt(1, "preview", old_draft)
        await choose(flow, db, 1, "publish")

    asyncio.run(scenario())
    assert db.user(1)["purpose"] == "any"
    assert db.user(1)["interests"] == []
    assert db.session(1) is None


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_selected_interests_are_translated_and_checked(db, lang):
    db.touch(1)
    db.set_user(1, lang=lang)
    flow, service = make_flow(db)
    asyncio.run(flow.prompt(1, "interests", {"interests": ["music", "tech"]}))
    sent = service.send.call_args
    assert tr(lang, "interest_music") in sent.args[1]
    assert tr(lang, "interest_tech") in sent.args[1]
    labels = [button.text for row in sent.kwargs["reply_markup"].inline_keyboard for button in row]
    assert "✅ " + tr(lang, "interest_music") in labels
    assert "✅ " + tr(lang, "interest_tech") in labels


@pytest.mark.parametrize("choice", ["interest_unknown", "friendship", "", None])
def test_invalid_interest_actions_do_not_change_draft(db, choice):
    db.touch(1)
    flow, service = make_flow(db)

    async def scenario():
        await flow.prompt(1, "interests", {"interests": ["books"]})
        await choose(flow, db, 1, choice)

    asyncio.run(scenario())
    assert db.session(1)[0] == "interests"
    assert db.session(1)[1]["interests"] == ["books"]
    assert service.send.call_args.args[1] == tr("en", "invalid")
