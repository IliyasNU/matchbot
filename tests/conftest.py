import pytest

from db.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


def member(db, uid, **overrides):
    db.touch(uid, f"user{uid}", "en")
    profile = dict(
        name=f"Person {uid}",
        age=25,
        gender="male",
        seeking="any",
        city="Almaty",
        bio="Hello",
        photo=f"photo{uid}",
        consent=1,
    )
    profile.update(overrides)
    db.save_profile(uid, profile)
    return db.user(uid)


def react(db, uid, kind="like", mode="feed"):
    result = db.present(uid, mode)
    assert result is not None
    return db.react(uid, result[1], kind)
