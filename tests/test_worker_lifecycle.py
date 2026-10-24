"""A dead registration worker must not keep its mailbox/export busy forever."""
import logging
import queue
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from auth_flow import AuthResult
from webui import auto_loop, db, registrar


def prepare_worker(tmp_path, monkeypatch, *, return_empty=False):
    email = "unfinished-worker@example.test"
    run_id = "unfinished-worker"
    log_path = tmp_path / "worker.log"
    db.set_setting("mail_source", "icloud_relay")
    db.import_accounts(
        f"{email}----https://relay.example/messages/TOKEN/{email}",
        kind="icloud_relay",
    )
    account = db.claim_account(email)
    db.create_run(run_id, email, str(log_path))
    db.save_registered({"email": email, "password": "previous-password",
                        "access_token": "previous-access"})

    class FlowFixture:
        def __init__(self, *_args, **_kwargs):
            self.result = AuthResult()
            self.result.email = email

        def run_register(self, _mail):
            if return_empty:
                return self.result
            raise registrar.RequestedCredentialError(["access_token", "session_token"])

    monkeypatch.setattr(registrar, "AuthFlow", FlowFixture)
    monkeypatch.setattr(registrar, "create_mail_provider", lambda *args: SimpleNamespace(
        display_name="Fixture mailbox",
    ))
    events = queue.Queue()
    monkeypatch.setattr(registrar, "_run_queues", {run_id: events})
    args = (run_id, account, {"want_2fa": False, "want_refresh_token": False}, log_path)
    return args, events


def run_row(run_id):
    with closing(db._conn()) as con:
        return dict(con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone())


def assert_resources_closed(run_id, events):
    assert registrar.get_run_queue(run_id) is None
    assert getattr(registrar._current_run, "run_id", None) is None
    assert not any(getattr(h, "run_id", None) == run_id for h in logging.getLogger().handlers)
    items = []
    while not events.empty():
        items.append(events.get_nowait())
    assert items[-1] is None


@pytest.mark.parametrize("return_empty", [False, True])
def test_missing_credentials_without_password_finishes_normally(tmp_path, monkeypatch, return_empty):
    args, events = prepare_worker(tmp_path, monkeypatch, return_empty=return_empty)
    registrar._do_register(*args)
    run = run_row(args[0])
    assert run["status"] == "failed" and run["finished_at"]
    assert run["error_category"] == "account"
    assert "注册流程未获取用户请求的凭证" in run["error"]
    assert db.get_account(args[1]["email"])["status"] == "failed"
    assert db.get_registered(args[1]["email"])["access_token"] == "previous-access"
    assert auto_loop.AutoLoopController()._wait_run_finish(args[0]) == (False, "account")
    assert_resources_closed(args[0], events)


@pytest.mark.parametrize("stage", ["log_open", "settings", "failure_handler", "probe_cleanup"])
def test_setup_and_secondary_errors_cannot_leave_running_records(tmp_path, monkeypatch, stage):
    args, events = prepare_worker(tmp_path, monkeypatch)

    def fail(*_args, **_kwargs):
        raise RuntimeError("fixture-unlabelled-secret")

    if stage == "log_open":
        monkeypatch.setattr(registrar, "QueueLogHandler", fail)
    elif stage == "settings":
        monkeypatch.setattr(db, "get_setting", fail)
    elif stage == "failure_handler":
        monkeypatch.setattr(db, "mark_failed", fail)
    else:
        monkeypatch.setattr(registrar.ProbeSession, "fail_open", fail)
    with pytest.raises(RuntimeError, match="fixture-unlabelled-secret"):
        registrar._do_register(*args)
    run = run_row(args[0])
    assert run["status"] == "failed" and run["finished_at"]
    assert run["error_category"] == ("account" if stage == "probe_cleanup" else "worker_crash")
    assert "fixture-unlabelled-secret" not in run["error"]
    assert db.get_account(args[1]["email"])["status"] == "failed"
    assert db.get_registered(args[1]["email"])["password"] == "previous-password"
    assert_resources_closed(args[0], events)


@pytest.mark.parametrize("status", ["done", "failed", "interrupted"])
def test_exit_guard_preserves_terminal_results(tmp_path, monkeypatch, status):
    args, _ = prepare_worker(tmp_path, monkeypatch)
    db.finish_run(args[0], status, "original error", category="original")
    before = run_row(args[0])
    mailbox = db.get_account(args[1]["email"])
    assert not db.fail_unfinished_run(args[0], "late cleanup error")
    assert run_row(args[0]) == before
    assert db.get_account(args[1]["email"]) == mailbox


@pytest.mark.parametrize("new_owner", ["running", "queued", "claimed_only"])
def test_exit_guard_preserves_new_mailbox_owner(tmp_path, monkeypatch, new_owner):
    args, _ = prepare_worker(tmp_path, monkeypatch)
    email = args[1]["email"]
    with closing(db._conn()) as con, con:
        if new_owner == "claimed_only":
            con.execute("UPDATE outlook_accounts SET claimed_at=? WHERE email=?",
                        (run_row(args[0])["started_at"] + 1, email))
        else:
            con.execute("INSERT INTO runs(run_id,email,status) VALUES('new-owner',?,?)", (email, new_owner))
    assert db.fail_unfinished_run(args[0], "worker exited")
    assert run_row(args[0])["status"] == "failed"
    assert db.get_account(email)["status"] == "in_use"


def test_exit_guard_rolls_back_if_mailbox_update_fails(tmp_path, monkeypatch):
    args, _ = prepare_worker(tmp_path, monkeypatch)
    with closing(db._conn()) as con, con:
        con.execute("CREATE TRIGGER fail_mailbox BEFORE UPDATE ON outlook_accounts "
                    "BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.fail_unfinished_run(args[0], "worker exited")
    assert run_row(args[0])["status"] == "running"
    assert db.get_account(args[1]["email"])["status"] == "in_use"


def test_interrupted_run_stops_worker_wait_immediately(tmp_path, monkeypatch):
    args, _ = prepare_worker(tmp_path, monkeypatch)
    db.finish_run(args[0], "interrupted", category="interrupted")
    def unexpected_wait(*_args):
        raise AssertionError("An interrupted run is already terminal")
    monkeypatch.setattr(auto_loop.time, "sleep", unexpected_wait)
    assert auto_loop.AutoLoopController()._wait_run_finish(args[0]) == (False, "interrupted")
