"""Migration must preserve independent credentials and avoid destructive writes."""
import base64
from copy import deepcopy
import json
import sqlite3

import pytest

from account_transfer import CREDENTIAL_FIELDS, make_document, read_document
from webui import db
from webui.account_transfer import export_document, import_document


def account(email="transfer@example.test", **overrides):
    return {"email": email, **{key: f"fixture-{key}" for key in CREDENTIAL_FIELDS},
            "created_at": 1700000000,
            "mail": {"kind": "outlook", "password": "mail-password", "client_id": "mail-client", "refresh_token": "mail-refresh"},
            "metadata": {"note": "中文备注", "session_info": {"accessToken": "session-at"}},
            "legacy_extra": {"retained": True}, "extra": {"custom": {"keep": False}}, **overrides}


def document(*rows):
    return make_document(list(rows), "turb-gpt-free-register")


def test_preview_import_roundtrip_and_repeat():
    row = account()
    doc = document(row)
    assert import_document(doc, dry_run=True)["inserted"] == 1
    assert db.get_registered(row["email"]) is None
    assert db.get_account(row["email"]) is None
    assert import_document(doc)["inserted"] == 1
    saved = db.get_registered(row["email"])
    for key in CREDENTIAL_FIELDS:
        assert saved[key] == row[key]
    mailbox = db.get_account(row["email"])
    assert mailbox["password"] == "mail-password"
    assert mailbox["refresh_token"] == "mail-refresh"
    assert mailbox["status"] == "done"
    exported = export_document()["accounts"][0]
    assert exported["metadata"] == row["metadata"]
    assert exported["extra"] == row["extra"]
    assert exported["legacy_extra"] == row["legacy_extra"]
    assert exported["created_at"] == row["created_at"]
    assert import_document(doc)["skipped"] == 1
    assert db.get_registered(row["email"]) == saved


def test_fill_missing_preserves_existing_tokens_password_metadata_and_totp_pair():
    initial = account(session_token="", totp_factor_id="")
    import_document(document(initial))
    incoming = account(password="different", totp_secret="different", extra={"custom": {"keep": True, "new": 1}})
    result = import_document(document(incoming), conflict="fill_missing")
    assert result["updated"] == 1
    saved = db.get_registered(initial["email"])
    assert saved["password"] == initial["password"]
    assert saved["access_token"] == initial["access_token"]
    assert saved["totp_factor_id"] == ""
    assert saved["session_token"] == incoming["session_token"]
    assert saved["extra"]["custom"] == {"keep": False, "new": 1}


def test_relay_url_exact_and_incomplete_accounts_can_be_reexported():
    url = "https://mail.example/box----token?code=a%2Bb====c&x=中文"
    row = account(password="", mail={"kind": "generic_api", "relay_url": url})
    assert import_document(document(row))["incomplete"] == 1
    assert db.count_registered() == 0
    assert db.get_account(row["email"])["relay_url"] == url
    assert export_document()["accounts"][0]["mail"]["relay_url"] == url


def test_duplicates_mixed_errors_and_unknown_fields_never_become_sql():
    row = account()
    bad = account("bad@example.test", access_token={"secret": "never echo"})
    result = import_document(document(row, {**row, "email": row["email"].upper()}, bad, {"email": "invalid"}))
    assert (result["inserted"], result["skipped"], result["failed"]) == (1, 1, 2)
    assert "never echo" not in json.dumps(result)


@pytest.mark.parametrize("bad", [{}, [], {"format": "gpt-account-transfer", "version": 99, "accounts": [{}]}, "{bad", "\ufeff[]"])
def test_invalid_document_has_no_writes(bad):
    with pytest.raises(ValueError):
        import_document(bad)
    with db._conn() as con:
        assert con.execute("SELECT COUNT(*) FROM registered").fetchone()[0] == 0


def test_running_mailbox_is_not_reassigned():
    row = account(mail={"kind": "icloud_relay", "relay_url": "https://mail.example/test"})
    db.import_accounts(f"{row['email']}----https://mail.example/test", kind="icloud_relay")
    db.claim_account(row["email"])
    assert import_document(document(row))["skipped"] == 1
    assert db.get_registered(row["email"]) is None
    assert db.get_account(row["email"])["status"] == "in_use"


def test_api_export_includes_partial_and_import_validates(monkeypatch):
    from fastapi.testclient import TestClient
    from webui.app import app
    import_document(document(account(), account("partial@example.test", totp_secret="")))
    client = TestClient(app)
    response = client.post('/api/registered/export', json={"format": "account_transfer", "all": True})
    assert response.status_code == 200
    result = response.json()
    assert result["count"] == 2
    content = base64.b64decode(result["b64"])
    assert len(read_document(content)[0]) == 2
    response = client.post('/api/registered/transfer/import', json={"text": content.decode(), "dry_run": True})
    assert response.status_code == 200
    assert response.json()["skipped"] == 2
    assert client.post('/api/registered/transfer/import', json={"text": "{}"}).status_code == 400


def test_export_missing_selection_fails_instead_of_silently_dropping():
    import_document(document(account()))
    with pytest.raises(ValueError, match="不存在"):
        export_document(["transfer@example.test", "missing@example.test"])


def test_fill_missing_does_not_mix_unrelated_login_sessions():
    import_document(document(account(session_token="", metadata={})))
    import_document(document(account(access_token="different-session-at", metadata={"session_info": {"sessionToken": "unrelated-session"}})), conflict="fill_missing")
    assert db.get_registered("transfer@example.test")["session_token"] == ""
    assert "session_info" not in export_document()["accounts"][0]["metadata"]


def test_export_exceeds_old_5000_cap_and_chunks_selected_queries():
    emails = [f"bulk{i}@example.test" for i in range(5001)]
    with db._conn() as con:
        con.executemany("INSERT INTO registered(email,password,totp_secret,access_token,created_at) VALUES(?,'pw','totp','AT',1)", [(e,) for e in emails])
    assert len(export_document()["accounts"]) == 5001
    assert len(export_document(emails[:1001])["accounts"]) == 1001


def test_storage_failure_rolls_back_accounts_and_mailboxes():
    with db._conn() as con:
        con.execute("CREATE TRIGGER fail_transfer BEFORE INSERT ON registered WHEN NEW.email='second@example.test' BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        import_document(document(account(), account("second@example.test")))
    assert db.get_registered("transfer@example.test") is None
    assert db.get_account("transfer@example.test") is None
