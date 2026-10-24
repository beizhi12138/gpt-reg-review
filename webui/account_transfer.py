"""Offline account migration; writes credentials and used mailboxes atomically."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from account_transfer import (
    CREDENTIAL_FIELDS, MAIL_FIELDS, MAX_ACCOUNTS, STATE_KEY, make_document,
    document_chunks, merge_missing, object_value, read_document, record_result,
)
from . import db


def _portable(row, mailbox=None):
    extra = object_value(row.get("extra_json"))
    saved = object_value(extra.pop(STATE_KEY, None))
    mail = dict(saved.get("mail") or {})
    if mailbox:
        mail.update({key: mailbox.get(key) or "" for key in MAIL_FIELDS})
    mail["email"] = row["email"]
    return {"email": row["email"], **{key: row.get(key) or "" for key in CREDENTIAL_FIELDS},
            "created_at": row.get("created_at"), "mail": mail, "extra": extra,
            "legacy_extra": saved.get("legacy_extra") or {}, "metadata": saved.get("metadata") or {}}


def _snapshot(con, emails=None):
    if emails is None:
        records = con.execute("SELECT * FROM registered ORDER BY created_at DESC LIMIT ?", (MAX_ACCOUNTS + 1,)).fetchall()
    else:
        wanted = list(dict.fromkeys(e.strip().lower() for e in emails if isinstance(e, str) and e.strip()))
        if not wanted or len(wanted) > MAX_ACCOUNTS:
            raise ValueError(f"请选择 1 至 {MAX_ACCOUNTS} 个账号")
        records = []
        for offset in range(0, len(wanted), 500):
            batch = wanted[offset:offset + 500]
            records.extend(con.execute(f"SELECT * FROM registered WHERE email IN ({','.join('?' for _ in batch)})", batch).fetchall())
        if len(records) != len(wanted):
            raise ValueError("部分选中账号已不存在，请刷新列表后重新导出")
        records.sort(key=lambda row: row["created_at"] or 0, reverse=True)
    if len(records) > MAX_ACCOUNTS:
        raise ValueError(f"单次最多迁移 {MAX_ACCOUNTS} 个账号，请勾选后分批导出")
    registered = [dict(row) for row in records]
    mailboxes = {}
    for offset in range(0, len(registered), 500):
        batch = [row["email"] for row in registered[offset:offset + 500]]
        for row in con.execute(f"SELECT * FROM outlook_accounts WHERE email IN ({','.join('?' for _ in batch)})", batch):
            mailboxes[row["email"].lower()] = dict(row)
    return {"registered": registered, "outlook_accounts": mailboxes}


def _rows(snapshot):
    return [_portable(row, snapshot["outlook_accounts"].get(row["email"].lower())) for row in snapshot["registered"]]


def export_rows(emails=None):
    with closing(db._conn()) as con:
        con.execute("BEGIN")
        return _rows(_snapshot(con, emails))


def export_document(emails=None):
    return make_document(export_rows(emails), "gpt-reg-review")


def _backup_root():
    # Relative to the active database so isolated tests never write production backups.
    return Path(db.DB_PATH).resolve().parent / "transfer_backups"


def _file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_backup(path, chunks):
    digest = hashlib.sha256()
    size = 0
    with path.open("xb") as stream:
        for chunk in chunks:
            stream.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    if _file_digest(path) != digest.hexdigest():
        raise OSError("互转备份校验失败，账号未删除")
    return digest.hexdigest(), size


def _ensure_export_table(con):
    con.execute("""CREATE TABLE IF NOT EXISTS account_transfer_exports (
        id TEXT PRIMARY KEY, created_at REAL NOT NULL,
        export_sha256 TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
        completed_at REAL, deleted_registered INTEGER, deleted_mailboxes INTEGER
    )""")


def prepare_export_cleanup(emails=None, *, include_content=True):
    """Durably save the exact export and source rows; no source accounts are deleted."""
    with closing(db._conn()) as con:
        con.execute("BEGIN")
        snapshot = _snapshot(con, emails)
    rows = _rows(snapshot)
    export_id = uuid.uuid4().hex
    folder = _backup_root() / export_id
    folder.parent.mkdir(parents=True, exist_ok=True)
    export_path = folder / "gpt-account-transfer.json"
    with tempfile.TemporaryDirectory(prefix=".pending-", dir=folder.parent) as pending:
        staging = Path(pending)
        export_hash, size = _write_backup(staging / export_path.name,
                                          document_chunks(make_document(rows, "gpt-reg-review")))
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, allow_nan=False)
        snapshot_hash, _ = _write_backup(staging / "source-snapshot.json",
                                         (chunk.encode("utf-8") for chunk in encoder.iterencode(snapshot)))
        staging.rename(folder)
    with db._lock, closing(db._conn()) as con, con:
        _ensure_export_table(con)
        con.execute("INSERT INTO account_transfer_exports(id,created_at,export_sha256,snapshot_sha256) VALUES(?,?,?,?)",
                    (export_id, time.time(), export_hash, snapshot_hash))
    content = export_path.read_bytes() if include_content else None
    return rows, content, {"export_id": export_id, "backup_path": str(export_path), "size": size}


def _export_record(con, export_id):
    if not re.fullmatch(r"[0-9a-f]{32}", export_id or ""):
        raise ValueError("无效的互转导出记录")
    exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_transfer_exports'").fetchone()
    record = con.execute("SELECT * FROM account_transfer_exports WHERE id=?", (export_id,)).fetchone() if exists else None
    if not record:
        raise ValueError("互转导出记录不存在，请重新导出")
    return record


def _verified_backup_path(export_id, filename, expected):
    path = _backup_root() / export_id / filename
    try:
        actual = _file_digest(path)
    except OSError as exc:
        raise ValueError("互转备份不可读，已停止清理，请重新导出") from exc
    if actual != expected:
        raise ValueError("互转备份校验失败，已停止清理，请重新导出")
    return path


def _verified_backup(export_id, filename, expected):
    return _verified_backup_path(export_id, filename, expected).read_bytes()


def export_backup_path(export_id):
    with closing(db._conn()) as con:
        record = _export_record(con, export_id)
    return _verified_backup_path(export_id, "gpt-account-transfer.json", record["export_sha256"])


def download_export_backup(export_id):
    return export_backup_path(export_id).read_bytes()


def cleanup_export(export_id):
    """Clean only an unchanged, backed-up snapshot; receipt and both deletes commit together."""
    with db._lock, closing(db._conn()) as con, con:
        con.execute("BEGIN IMMEDIATE")
        record = _export_record(con, export_id)
        result = {"ok": True, "export_id": export_id,
                  "backup_path": str(_backup_root() / export_id / "gpt-account-transfer.json")}
        if record["completed_at"] is not None:
            return {**result, "deleted_registered": record["deleted_registered"],
                    "deleted_mailboxes": record["deleted_mailboxes"], "already_completed": True}
        _verified_backup_path(export_id, "gpt-account-transfer.json", record["export_sha256"])
        snapshot = json.loads(_verified_backup(export_id, "source-snapshot.json", record["snapshot_sha256"]))
        emails = [row["email"] for row in snapshot["registered"]]
        current = _snapshot(con, emails)
        if ({row["email"]: row for row in current["registered"]} !=
                {row["email"]: row for row in snapshot["registered"]} or
                current["outlook_accounts"] != snapshot["outlook_accounts"]):
            raise ValueError("导出后账号或邮箱信息发生变化，未删除任何账号；请重新导出")
        task_tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('runs','probe_batch_items')")]
        for offset in range(0, len(emails), 500):
            batch = emails[offset:offset + 500]
            placeholders = ','.join('?' for _ in batch)
            for table in task_tables:
                active = con.execute(f"SELECT 1 FROM {table} WHERE email IN ({placeholders}) AND status IN ('running','queued','pending') LIMIT 1", batch).fetchone()
                if active:
                    raise ValueError("这批账号仍有任务运行或排队，未删除任何账号；请任务结束后重新导出")
        if any(row["status"] in {"in_use", "running", "queued"} for row in current["outlook_accounts"].values()):
            raise ValueError("这批邮箱正在使用，未删除任何账号；请稍后重新导出")
        counts = {"registered": 0, "outlook_accounts": 0}
        for table in counts:
            for offset in range(0, len(emails), 500):
                batch = emails[offset:offset + 500]
                counts[table] += con.execute(f"DELETE FROM {table} WHERE email IN ({','.join('?' for _ in batch)})", batch).rowcount
        con.execute("UPDATE account_transfer_exports SET completed_at=?,deleted_registered=?,deleted_mailboxes=? WHERE id=?",
                    (time.time(), counts["registered"], counts["outlook_accounts"], export_id))
        return {**result, "deleted_registered": counts["registered"],
                "deleted_mailboxes": counts["outlook_accounts"], "already_completed": False}


def import_document(document, *, conflict="skip", dry_run=False):
    if conflict not in {"skip", "fill_missing"}:
        raise ValueError("重复账号策略必须是 skip 或 fill_missing")
    prepared, report = read_document(document)
    report.update(dry_run=dry_run, mail_unavailable=0)
    from mail_providers import list_providers
    supported = {provider["kind"] for provider in list_providers()}
    with db._lock, closing(db._conn()) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            for index, incoming in prepared:
                email = incoming["email"]
                record = con.execute("SELECT * FROM registered WHERE email=? COLLATE NOCASE", (email,)).fetchone()
                if record and conflict == "skip":
                    record_result(report, "skipped", index, email, "账号已存在")
                    continue
                mailbox = con.execute("SELECT * FROM outlook_accounts WHERE email=? COLLATE NOCASE", (email,)).fetchone()
                running = con.execute("SELECT 1 FROM runs WHERE email=? COLLATE NOCASE AND status='running' LIMIT 1", (email,)).fetchone()
                if running or (mailbox and mailbox["status"] == "in_use"):
                    record_result(report, "skipped", index, email, "账号正在执行任务，请稍后导入")
                    continue
                current = _portable(dict(record), dict(mailbox) if mailbox else None) if record else None
                account = merge_missing(current, incoming) if current else incoming
                if current == account:
                    record_result(report, "skipped", index, email, "没有需要补全的字段")
                    continue
                if not all(account.get(key) for key in ("password", "totp_secret", "access_token")):
                    report["incomplete"] += 1
                mail = account["mail"]
                kind = mail.get("kind") or ""
                if kind not in supported and mail.get("relay_url"):
                    kind = "icloud_relay"
                usable_mail = kind in supported
                if kind and not usable_mail:
                    report["mail_unavailable"] += 1
                if not dry_run:
                    extra = object_value(account["extra"])
                    extra[STATE_KEY] = {"mail": mail, "metadata": account["metadata"], "legacy_extra": account["legacy_extra"]}
                    names = ["email", *CREDENTIAL_FIELDS, "created_at", "extra_json"]
                    values = [email, *(account[key] for key in CREDENTIAL_FIELDS),
                              account["created_at"] if account["created_at"] is not None else time.time(),
                              json.dumps(extra, ensure_ascii=False)]
                    if record:
                        con.execute(f"UPDATE registered SET {','.join(name + '=?' for name in names[1:])} WHERE email=?",
                                    [*values[1:], record["email"]])
                    else:
                        con.execute(f"INSERT INTO registered ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", values)
                    if usable_mail:
                        credentials = {key: mail.get(key) or "" for key in MAIL_FIELDS}
                        credentials["kind"] = kind
                        if mailbox:
                            credentials = merge_missing(dict(mailbox), credentials)
                            con.execute("UPDATE outlook_accounts SET password=?, client_id=?, refresh_token=?, relay_url=?, kind=?, status='done', finished_at=COALESCE(finished_at,?) WHERE email=?",
                                        [credentials[key] for key in ("password", "client_id", "refresh_token", "relay_url", "kind")] + [time.time(), mailbox["email"]])
                        else:
                            con.execute("INSERT INTO outlook_accounts(email,password,client_id,refresh_token,relay_url,kind,status,imported_at,finished_at) VALUES(?,?,?,?,?,?,'done',?,?)",
                                        [email] + [credentials[key] for key in ("password", "client_id", "refresh_token", "relay_url", "kind")] + [time.time(), time.time()])
                record_result(report, "updated" if record else "inserted", index, email)
            con.rollback() if dry_run else con.commit()
        except Exception:
            con.rollback()
            raise
    return report
