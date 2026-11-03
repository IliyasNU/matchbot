import sqlite3

import pytest

from scripts.backup import create_backup
from tests.conftest import member, react


def test_backup_copies_committed_wal_without_stopping_bot(db, tmp_path):
    member(db, 1)
    member(db, 2)
    react(db, 1)
    react(db, 2)
    source = tmp_path / "test.sqlite3"
    output = tmp_path / "backups" / "copy.sqlite3"
    create_backup(source, output)
    member(db, 3)
    with sqlite3.connect(output) as copy:
        assert copy.execute("SELECT count(*) FROM users").fetchone()[0] == 2
        assert copy.execute("SELECT count(*) FROM matches").fetchone()[0] == 1
        assert copy.execute("SELECT count(*) FROM outbox WHERE delivered=0").fetchone()[0] == 2
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not copy.execute("PRAGMA foreign_key_check").fetchall()


def test_backup_refuses_to_replace_an_existing_file(db, tmp_path):
    member(db, 1)
    output = tmp_path / "existing.sqlite3"
    output.write_bytes(b"keep this file")
    with pytest.raises(FileExistsError):
        create_backup(tmp_path / "test.sqlite3", output)
    assert output.read_bytes() == b"keep this file"
    assert db.user(1)["photo"]


def test_failed_backup_does_not_leave_a_broken_restore_file(tmp_path):
    source = tmp_path / "corrupt.sqlite3"
    source.write_bytes(b"not a SQLite database")
    output = tmp_path / "copy.sqlite3"
    with pytest.raises(sqlite3.DatabaseError):
        create_backup(source, output)
    assert not output.exists()
    assert source.read_bytes() == b"not a SQLite database"


def test_backup_rejects_missing_source_without_creating_output(tmp_path):
    output = tmp_path / "copy.sqlite3"
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "missing.sqlite3", output)
    assert not output.exists()
