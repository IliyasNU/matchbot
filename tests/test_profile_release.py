import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest

from handlers.profile import ProfileFlow
from i18n import tr
from tests.conftest import member


def make_flow(db):
    service = SimpleNamespace(db=db, send=AsyncMock(), card=AsyncMock(), menu=AsyncMock())
    return ProfileFlow(service), service


async def choose(flow, choice, query=None):
    await flow.accept(1, nonce=flow.db.session(1)[1]["nonce"], choice=choice, query=query)


def test_interest_toggle_edits_message_and_invalidates_its_previous_buttons(db):
    db.touch(1, lang="ru")
    flow, service = make_flow(db)
    query = SimpleNamespace(edit_message_text=AsyncMock())

    async def scenario():
        await flow.prompt(1, "interests", {})
        old_nonce = db.session(1)[1]["nonce"]
        await choose(flow, "interest_music", query)
        assert service.send.await_count == 1
        kwargs = query.edit_message_text.call_args.kwargs
        assert tr("ru", "interest_music") in kwargs["text"]
        current_nonce = db.session(1)[1]["nonce"]
        assert old_nonce != current_nonce
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        assert any(button.text == "✅ " + tr("ru", "interest_music") for button in buttons)
        assert all(button.callback_data.startswith(f"w:{current_nonce}:") for button in buttons)
        await flow.accept(1, nonce=old_nonce, choice="cancel", query=query)

    asyncio.run(scenario())
    query.edit_message_text.assert_awaited_once()
    assert db.session(1)[1]["interests"] == ["music"]
    assert service.send.call_args.args[1] == tr("ru", "draft_stale")


@pytest.mark.parametrize("error", [BadRequest("Message to edit not found"), TypeError("Message is inaccessible")])
def test_interest_toggle_falls_back_to_sending_same_current_prompt(db, error):
    db.touch(1)
    flow, service = make_flow(db)
    query = SimpleNamespace(edit_message_text=AsyncMock(side_effect=error))

    async def scenario():
        await flow.prompt(1, "interests", {})
        await choose(flow, "interest_music", query)

    asyncio.run(scenario())
    assert service.send.await_count == 2
    edit = query.edit_message_text.call_args.kwargs
    assert service.send.call_args.args == (1, edit["text"])
    assert service.send.call_args.kwargs["reply_markup"] == edit["reply_markup"]
    assert db.session(1)[1]["interests"] == ["music"]


@pytest.mark.parametrize("editing", [False, True])
def test_reopening_profile_flow_keeps_unsaved_input(db, editing):
    if editing:
        member(db, 1)
    else:
        db.touch(1)
    flow, _ = make_flow(db)

    async def scenario():
        await flow.begin(1, editing=editing)
        if not editing:
            await choose(flow, "agree")
        await flow.accept(1, message=SimpleNamespace(text="Draft name"))
        await flow.begin(1, editing=editing)

    asyncio.run(scenario())
    step, draft = db.session(1)
    assert step == "age" and draft["name"] == "Draft name" and draft["editing"] is editing
    assert db.user(1)["name"] == ("Person 1" if editing else None)


def test_filters_can_save_and_resume_a_profile_draft(db):
    member(db, 1)
    db.save_session(1, "interests", {"name": "Draft name", "interests": ["books"], "nonce": "before", "editing": True})
    flow, service = make_flow(db)

    async def scenario():
        await flow.open_settings(1, "range")
        await flow.accept(1, message=SimpleNamespace(text="20-33"))
        await flow.open_settings(1, "range")
        assert db.session(1)[0] == "location"
        await choose(flow, "local")

    asyncio.run(scenario())
    user = db.user(1)
    assert (user["min_age"], user["max_age"], user["local_only"]) == (20, 33, 1)
    step, draft = db.session(1)
    assert step == "interests" and draft["name"] == "Draft name" and draft["interests"] == ["books"]
    assert draft["nonce"] != "before" and "return_to" not in draft
    assert service.send.call_args.args[1].startswith(tr("en", "filters_saved"))
    service.menu.assert_not_awaited()


def test_switching_settings_never_restores_a_prior_delete_confirmation(db):
    member(db, 1)
    db.save_session(1, "bio", {"name": "Draft name", "nonce": "before", "editing": True})
    flow, _ = make_flow(db)

    async def scenario():
        await flow.open_settings(1, "delete_confirm")
        old_delete_nonce = db.session(1)[1]["nonce"]
        await flow.open_settings(1, "range")
        await flow.open_settings(1, "delete_confirm")
        await flow.accept(1, nonce=old_delete_nonce, choice="delete")
        step, settings = db.session(1)
        assert step == "delete_confirm" and settings["return_to"][0] == "bio"
        assert "return_to" not in settings["return_to"][1]
        await choose(flow, "cancel")
        assert db.session(1)[0] == "bio"
        assert db.session(1)[1]["name"] == "Draft name"
        # Cancellation while editing explicitly discards the unsaved draft.
        await flow.cancel(1)

    asyncio.run(scenario())
    assert db.session(1) is None and db.user(1)["name"] == "Person 1"


@pytest.mark.parametrize("has_draft", [False, True])
def test_leaving_settings_revokes_delete_confirmation_and_preserves_draft(db, has_draft):
    member(db, 1)
    if has_draft:
        db.save_session(1, "bio", {"name": "Draft name", "nonce": "before", "editing": True})
    flow, service = make_flow(db)

    async def scenario():
        await flow.open_settings(1, "delete_confirm")
        old_delete_nonce = db.session(1)[1]["nonce"]
        flow.leave_settings(1)
        await flow.accept(1, nonce=old_delete_nonce, choice="delete")

    asyncio.run(scenario())
    assert db.user(1)["name"] == "Person 1"
    assert service.send.call_args.args[1] == tr("en", "draft_stale")
    if has_draft:
        step, draft = db.session(1)
        assert step == "bio" and draft["name"] == "Draft name"
        assert draft["nonce"] != "before" and "return_to" not in draft
    else:
        assert db.session(1) is None


@pytest.mark.parametrize("ban_before_delete", [False, True])
def test_banned_member_cancel_does_not_resume_the_profile_draft(db, ban_before_delete):
    member(db, 1)
    db.save_session(1, "bio", {"name": "Draft name", "editing": True})
    flow, service = make_flow(db)

    async def scenario():
        if ban_before_delete:
            db.ban(1)
        await flow.open_settings(1, "delete_confirm")
        assert ("return_to" in db.session(1)[1]) is not ban_before_delete
        if not ban_before_delete:
            db.ban(1)
        await choose(flow, "cancel")

    asyncio.run(scenario())
    assert db.session(1) is None and db.user(1)["banned"]
    service.menu.assert_not_awaited()
