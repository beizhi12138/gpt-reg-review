"""Auto-cleanup must never delete beyond an unchanged, recoverable export."""
import base64
import json

import pytest

from account_transfer import CREDENTIAL_FIELDS, make_document
from webui import account_transfer as transfer, db


def seed(*emails):
    rows = [{"email": email, **{key: "synthetic-" + key for key in CREDENTIAL_FIELDS},
             "created_at": 1700000000, "mail": {"kind": "icloud_relay", "relay_url": "https://example.test/otp"}}
            for email in emails]
    transfer.import_document(make_document(rows, "test"))


def prepare(emails=None):
    return transfer.prepare_export_cleanup(emails)


def test_download_prepare_keeps_sources_and_cleanup_only_deletes_snapshot():
    seed("selected@example.test", "keep@example.test")
    rows, content, receipt = prepare(["selected@example.test"])
    assert len(rows) == 1 and db.get_registered("selected@example.test")
    assert transfer.download_export_backup(receipt["export_id"]) == content
    seed("arrived-after-export@example.test")
    result = transfer.cleanup_export(receipt["export_id"])
    assert (result["deleted_registered"], result["deleted_mailboxes"]) == (1, 1)
    assert db.get_registered("selected@example.test") is None
    assert db.get_account("selected@example.test") is None
    assert db.get_registered("keep@example.test")
    assert db.get_registered("arrived-after-export@example.test")
    assert transfer.download_export_backup(receipt["export_id"]) == content
    transfer.import_document(content)
    assert transfer.cleanup_export(receipt["export_id"])["already_completed"]
    assert db.get_registered("selected@example.test")


@pytest.mark.parametrize("change", ["credential", "mailbox", "missing", "running", "queued_probe", "mailbox_in_use"])
def test_changed_or_active_accounts_abort_the_entire_batch(change):
    seed("one@example.test", "two@example.test")
    _, _, receipt = prepare()
    with db._conn() as con:
        if change == "credential":
            con.execute("UPDATE registered SET password='new-password' WHERE email='two@example.test'")
        elif change == "mailbox":
            con.execute("UPDATE outlook_accounts SET relay_url='new-url' WHERE email='two@example.test'")
        elif change == "missing":
            con.execute("DELETE FROM registered WHERE email='two@example.test'")
        elif change == "running":
            con.execute("INSERT INTO runs(run_id,email,status) VALUES('test-run','two@example.test','running')")
        elif change == "queued_probe":
            con.execute("CREATE TABLE IF NOT EXISTS probe_batch_items(batch_id TEXT,email TEXT,status TEXT,PRIMARY KEY(batch_id,email))")
            con.execute("INSERT INTO probe_batch_items(batch_id,email,status) VALUES('test-batch','two@example.test','pending')")
        else:
            con.execute("UPDATE outlook_accounts SET status='in_use' WHERE email='two@example.test'")
    with pytest.raises(ValueError):
        transfer.cleanup_export(receipt["export_id"])
    assert db.get_registered("one@example.test")
    assert db.get_account("one@example.test") and db.get_account("two@example.test")


def test_delete_failure_rolls_back_both_tables_and_receipt():
    import sqlite3
    seed("one@example.test")
    _, _, receipt = prepare()
    with db._conn() as con:
        con.execute("CREATE TRIGGER fail_cleanup BEFORE DELETE ON outlook_accounts BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        transfer.cleanup_export(receipt["export_id"])
    assert db.get_registered("one@example.test") and db.get_account("one@example.test")
    with db._conn() as con:
        assert con.execute("SELECT completed_at FROM account_transfer_exports").fetchone()[0] is None
        con.execute("DROP TRIGGER fail_cleanup")
    assert transfer.cleanup_export(receipt["export_id"])["deleted_registered"] == 1


@pytest.mark.parametrize("name", ["gpt-account-transfer.json", "source-snapshot.json"])
def test_backup_corruption_prevents_deletion(name):
    seed("one@example.test")
    _, _, receipt = prepare()
    (transfer._backup_root() / receipt["export_id"] / name).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="校验失败"):
        transfer.cleanup_export(receipt["export_id"])
    assert db.get_registered("one@example.test") and db.get_account("one@example.test")


def test_backup_write_failure_keeps_source_and_does_not_issue_receipt(monkeypatch):
    seed("one@example.test")
    def fail(*args):
        raise OSError("simulated disk full")
    monkeypatch.setattr(transfer, "_write_backup", fail)
    with pytest.raises(OSError):
        prepare()
    assert db.get_registered("one@example.test") and db.get_account("one@example.test")
    with db._conn() as con:
        assert not con.execute("SELECT 1 FROM sqlite_master WHERE name='account_transfer_exports'").fetchone()


def test_all_export_includes_incomplete_and_chunked_cleanup():
    emails = [f"synthetic{i}@example.test" for i in range(1001)]
    with db._conn() as con:
        con.executemany("INSERT INTO registered(email,password,created_at) VALUES(?,'pw',1)", [(e,) for e in emails])
    rows, content, receipt = prepare()
    assert len(rows) == len(json.loads(content)["accounts"]) == 1001
    result = transfer.cleanup_export(receipt["export_id"])
    assert (result["deleted_registered"], result["deleted_mailboxes"]) == (1001, 0)


def test_oversized_export_never_creates_cleanup_receipt(monkeypatch):
    import account_transfer
    seed("one@example.test")
    monkeypatch.setattr(account_transfer, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="迁移文件超过"):
        prepare()
    assert db.get_registered("one@example.test") and db.get_account("one@example.test")
    assert not list(transfer._backup_root().iterdir())


def test_mailbox_already_in_use_when_exported_is_preserved():
    seed("one@example.test")
    with db._conn() as con:
        con.execute("UPDATE outlook_accounts SET status='in_use' WHERE email='one@example.test'")
    _, _, receipt = prepare()
    with pytest.raises(ValueError, match="正在使用"):
        transfer.cleanup_export(receipt["export_id"])
    assert db.get_registered("one@example.test") and db.get_account("one@example.test")


def test_api_requires_export_receipt_and_can_redownload_after_cleanup():
    from fastapi.testclient import TestClient
    from webui.app import app
    seed("one@example.test")
    client = TestClient(app)
    ordinary = client.post('/api/registered/export', json={"format": "account_transfer", "all": True}).json()
    assert "export_id" not in ordinary and db.get_registered("one@example.test")
    response = client.post('/api/registered/export', json={"format": "account_transfer", "all": True, "prepare_cleanup": True})
    assert response.status_code == 200
    prepared = response.json()
    assert db.get_registered("one@example.test")
    assert client.post('/api/registered/transfer/cleanup', json={"export_id": "../../webui.db"}).status_code == 422
    assert client.post('/api/registered/transfer/cleanup', json={"export_id": "0" * 32}).status_code == 409
    assert client.post('/api/registered/export', json={"format": "email_pw", "all": True, "prepare_cleanup": True}).status_code == 400
    response = client.post('/api/registered/transfer/cleanup', json={"export_id": prepared["export_id"]})
    assert response.status_code == 200 and response.json()["deleted_registered"] == 1
    downloaded = client.get('/api/registered/transfer/backups/' + prepared["export_id"])
    assert downloaded.status_code == 200 and downloaded.content == base64.b64decode(prepared["b64"])
    assert downloaded.headers['cache-control'] == 'no-store'
