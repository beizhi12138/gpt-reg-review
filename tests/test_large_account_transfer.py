"""Large migrations must survive both the old row cap and the old byte cap."""
from contextlib import closing
import json

from fastapi.testclient import TestClient

from account_transfer import MAX_BYTES, document_bytes, document_chunks, make_document, read_document
from webui import account_transfer as transfer, db
from webui.app import TransferImportReq, app


def test_export_over_twenty_thousand_accounts_and_fifty_megabytes():
    count = 20001
    with closing(db._conn()) as con, con:
        con.executemany("INSERT INTO registered(email,password,access_token,created_at) VALUES(?,?,?,1)",
                        ((f"large-{i}@example.test", "fixture-password", "a" * 2700) for i in range(count)))
    rows, content, receipt = transfer.prepare_export_cleanup(include_content=False)
    assert len(rows) == count and content is None
    assert 50 * 1024 * 1024 < receipt["size"] < MAX_BYTES
    path = transfer.export_backup_path(receipt["export_id"])
    assert path.stat().st_size == receipt["size"]
    raw = path.read_bytes()
    prepared, report = read_document(raw)
    assert report["total"] == count and len(prepared) == count
    assert prepared[-1][1]["access_token"] == "a" * 2700
    with closing(db._conn()) as con:
        assert con.execute("SELECT COUNT(*) FROM registered").fetchone()[0] == count
        assert con.execute("SELECT completed_at FROM account_transfer_exports").fetchone()[0] is None
    # The HTTP request model used to reject documents above 50 MB independently.
    assert len(TransferImportReq(text=raw.decode(), dry_run=True).text) > 50 * 1024 * 1024


def test_backup_download_response_is_small_and_does_not_delete_accounts():
    db.save_registered({"email": "download@example.test", "password": "fixture-password"})
    client = TestClient(app)
    response = client.post('/api/registered/export', json={
        "format": "account_transfer", "all": True,
        "prepare_cleanup": True, "download_from_backup": True,
    })
    assert response.status_code == 200
    result = response.json()
    assert "b64" not in result and len(response.content) < 2048
    downloaded = client.get(result["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.headers['cache-control'] == 'no-store'
    assert len(downloaded.content) == result['size']
    assert len(read_document(downloaded.content)[0]) == 1
    assert db.get_registered("download@example.test")
    # Old clients can continue to request inline downloads.
    old = client.post('/api/registered/export', json={"format": "account_transfer", "all": True})
    assert old.status_code == 200 and old.json()['b64']
    assert client.post('/api/registered/export', json={
        "format": "account_transfer", "all": True, "download_from_backup": True,
    }).status_code == 400


def test_streamed_json_matches_existing_contract():
    doc = make_document([{"email": "unicode@example.test", "password": "中文密码", "mail": {}}], "test")
    expected = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    assert document_bytes(doc) == expected
    assert b''.join(document_chunks(doc)) == expected
