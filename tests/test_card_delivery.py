"""Discovery only records confirmed deliveries for a current card token."""

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from db.database import Database
from handlers.browse import BrowseFlow
from tests.conftest import member


def browse(db, card):
    service = SimpleNamespace(db=db, settings=SimpleNamespace(skip_days=7), card=card)
    return BrowseFlow(service)


def card_token(keyboard):
    return keyboard.inline_keyboard[0][0].callback_data.split(":")[1]


def impressions(db):
    return [
        tuple(row) for row in db.conn.execute("SELECT actor,target,last_shown FROM impressions ORDER BY actor,target")
    ]


def test_browse_confirms_only_after_card_send_finishes(db):
    member(db, 1)
    member(db, 2)
    tokens = []

    async def send(uid, profile, keyboard):
        token = card_token(keyboard)
        tokens.append(token)
        assert profile["id"] == 2
        assert db.view(uid, token)["mode"] == "pending:feed"
        assert db.react(uid, token, "like") is None
        assert not impressions(db)

    asyncio.run(browse(db, send).show(1))
    token = tokens[0]
    assert db.view(1, token)["mode"] == "feed"
    [(actor, target, shown_at)] = impressions(db)
    assert (actor, target) == (1, 2) and shown_at > 0
    assert not db.confirm_view(1, token)
    assert impressions(db) == [(actor, target, shown_at)]
    assert db.react(1, token, "like")["target"] == 2


@pytest.mark.parametrize("error", [BadRequest("invalid card"), Forbidden("bot blocked"), RetryAfter(1)])
def test_rejected_send_removes_its_token_without_impression(db, error):
    member(db, 1)
    member(db, 2)
    send = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        asyncio.run(browse(db, send).show(1))

    token = card_token(send.await_args.args[2])
    assert not db.view(1, token)
    assert db.react(1, token, "report", "abuse") is None
    assert not db.confirm_view(1, token)
    assert not impressions(db)


@pytest.mark.parametrize("error", [TimedOut(), asyncio.CancelledError()])
def test_ambiguous_send_retains_only_safety_actions_and_old_exposure(db, error):
    member(db, 1)
    member(db, 2)
    with db.conn:
        db.conn.execute("INSERT INTO impressions VALUES(1,2,123)")
    send = AsyncMock(side_effect=error)

    with pytest.raises(type(error)):
        asyncio.run(browse(db, send).show(1))

    token = card_token(send.await_args.args[2])
    assert impressions(db) == [(1, 2, 123)]
    assert db.view(1, token)["mode"] == "safety:feed"
    assert not db.confirm_view(1, token)
    assert db.react(1, token, "like") is None
    assert db.react(1, token, "skip") is None
    assert db.react(1, token, "report", "abuse")["target"] == 2


@pytest.mark.parametrize("change", ["edit", "pause_then_resume"])
def test_target_change_during_send_does_not_restore_likes(db, change):
    member(db, 1)
    member(db, 2)
    tokens = []

    async def send(uid, profile, keyboard):
        tokens.append(card_token(keyboard))
        if change == "edit":
            member(db, 2, name="Changed")
        else:
            db.set_user(2, active=0)
            db.set_user(2, active=1)

    asyncio.run(browse(db, send).show(1))
    token = tokens[0]
    assert db.view(1, token)["mode"] == "safety:feed"
    assert not impressions(db)
    assert db.react(1, token, "like") is None
    assert db.react(1, token, "block")["target"] == 2


@pytest.mark.parametrize("deleted", [1, 2])
def test_delete_during_send_does_not_recreate_view_or_impression(db, deleted):
    member(db, 1)
    member(db, 2)
    tokens = []

    async def send(uid, profile, keyboard):
        tokens.append(card_token(keyboard))
        db.delete_user(deleted)
        member(db, deleted)

    asyncio.run(browse(db, send).show(1))
    assert not db.view(1, tokens[0])
    assert not impressions(db)


@pytest.mark.parametrize("error", [None, Forbidden("rejected"), TimedOut()])
def test_late_delivery_result_cannot_modify_newer_view(db, error):
    member(db, 1)
    member(db, 2)
    tokens = []

    async def send(uid, profile, keyboard):
        tokens.append(card_token(keyboard))
        tokens.append(db.present(uid)[1])
        with db.conn:
            db.conn.execute("UPDATE impressions SET last_shown=123 WHERE actor=?", (uid,))
        if error:
            raise error

    if error:
        with pytest.raises(type(error)):
            asyncio.run(browse(db, send).show(1))
    else:
        asyncio.run(browse(db, send).show(1))

    old, new = tokens
    assert not db.view(1, old)
    assert db.view(1, new)["mode"] == "feed"
    assert impressions(db) == [(1, 2, 123)]


def test_confirmed_impression_and_token_commit_together(db):
    member(db, 1)
    member(db, 2)
    _, token = db.prepare_view(1)
    db.conn.execute(
        "CREATE TRIGGER fail_delivery BEFORE INSERT ON impressions "
        "BEGIN SELECT RAISE(ABORT,'simulated write error'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.confirm_view(1, token)
    assert db.view(1, token)["mode"] == "pending:feed"
    assert db.react(1, token, "like") is None
    assert not impressions(db)


@pytest.mark.parametrize("outcome", ["interrupted", "uncertain", "rejected"])
def test_pending_and_failed_delivery_safety_after_restart(tmp_path, outcome):
    path = tmp_path / "cards.sqlite3"
    db = Database(path)
    try:
        member(db, 1)
        member(db, 2)
        assert db.react(2, db.present(2)[1], "like")
        _, token = db.prepare_view(1, mode="likes")
        if outcome != "interrupted":
            db.abort_view(1, token, may_have_arrived=(outcome == "uncertain"))
    finally:
        db.close()

    db = Database(path)
    try:
        assert not db.conn.execute("SELECT 1 FROM impressions WHERE actor=1").fetchone()
        assert db.react(1, token, "like") is None
        assert db.react(1, token, "skip") is None
        report = db.react(1, token, "report", "spam")
        if outcome == "rejected":
            assert report is None
        else:
            assert report == {"matched": False, "target": 2, "mode": "likes"}
    finally:
        db.close()
