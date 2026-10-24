"""Rolling IP exclusion must retain history and serialize independent writers."""
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext

import pytest

from webui import db, environment


IP = "203.0.113.21"
OTHER_IP = "203.0.113.22"
DAY = 24 * 60 * 60


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(db.time, "time", lambda: now[0])
    return now


def _env(run_id, ip=IP):
    return {
        "allocation_id": f"allocation-{run_id}", "run_id": run_id,
        "exit_ip": ip, "exit_country": "JP", "country_code": "JP",
        "browser_family": "firefox", "browser_engine": "auto",
        "fingerprint_id": f"profile-{run_id}",
        "fingerprint_signature": f"signature-{run_id}", "fingerprint": {},
    }


def _reserve(run_id, ip=IP):
    env = _env(run_id, ip)
    db.create_run(run_id, f"{run_id}@example.com", "fixture.log")
    assert db.reserve_environment(run_id, env)
    db.update_run_environment(run_id, env)
    return env


@pytest.mark.parametrize("age,occupied", [(DAY - 0.001, True), (DAY, False), (DAY + 1, False)])
@pytest.mark.parametrize("history", ["both", "runs_only", "ledger_only"])
def test_window_boundary_and_history_fallback(clock, age, occupied, history):
    old = _reserve("old")
    with closing(db._conn()) as con:
        if history == "runs_only":
            con.execute("DELETE FROM environment_ledger")
        elif history == "ledger_only":
            con.execute("DELETE FROM runs")
        con.commit()
    clock[0] += age
    assert db.environment_seen(exit_ip=IP) is occupied
    assert db.reserve_environment("new", _env("new")) is (not occupied)
    # Only IP exclusion expires; fingerprint history remains protected.
    assert db.environment_seen(fingerprint_id=old["fingerprint_id"])
    assert db.environment_seen(fingerprint_signature=old["fingerprint_signature"])


def test_reuse_retains_old_records_and_restarts_window(clock):
    old = _reserve("old")
    clock[0] += DAY
    assert not db.environment_seen(exit_ip=IP)
    _reserve("new")
    assert {r["run_id"] for r in db.list_environment_ledger()} == {"old", "new"}
    assert db.get_run_environment("old")["exit_ip"] == old["exit_ip"]
    clock[0] += DAY - 1
    assert db.environment_seen(exit_ip=IP)
    clock[0] += 1
    assert not db.environment_seen(exit_ip=IP)


def test_allocator_reuses_expired_ip_and_rejects_it_after_reservation(clock, monkeypatch):
    _reserve("old")
    clock[0] += DAY
    monkeypatch.setattr(environment, "_candidate_proxies", lambda options: ["http://fixture:8080"])
    monkeypatch.setattr(
        environment, "probe_exit", lambda *args, **kwargs: {"exit_ip": IP, "exit_country": "JP"},
    )
    allocated = environment.allocate_environment("new", {"fingerprint_browser_family": "chrome"})
    assert allocated["exit_ip"] == IP
    with pytest.raises(environment.EnvironmentAllocationError, match="24 小时"):
        environment.allocate_environment("another", {"fingerprint_browser_family": "chrome"})


@pytest.mark.parametrize("table,timestamp", [("runs", "started_at"), ("environment_ledger", "created_at")])
def test_legacy_rows_without_reservation_timestamp(clock, table, timestamp):
    _reserve("old")
    other = "runs" if table == "environment_ledger" else "environment_ledger"
    with closing(db._conn()) as con:
        con.execute(f"DELETE FROM {other}")
        con.execute(f"UPDATE {table} SET exit_ip_reserved_at=NULL, {timestamp}=?", (clock[0],))
        con.commit()
    clock[0] += DAY - 1
    assert db.environment_seen(exit_ip=IP)
    clock[0] += 1
    assert not db.environment_seen(exit_ip=IP)


def test_profile_checks_are_not_limited_by_ip_window(clock):
    old = _reserve("old")
    clock[0] += DAY * 3
    assert not db.environment_seen(exit_ip=IP)
    assert db.environment_seen(exit_ip=IP, fingerprint_id=old["fingerprint_id"])
    for key in ("fingerprint_id", "fingerprint_signature"):
        candidate = _env("new", OTHER_IP)
        candidate[key] = old[key]
        assert not db.reserve_environment("new", candidate)


def test_recent_reservation_overrides_old_run_start(clock):
    db.create_run("delayed", "delayed@example.com", "fixture.log")
    clock[0] += DAY + 30
    env = _env("delayed")
    assert db.reserve_environment("delayed", env)
    db.update_run_environment("delayed", env)
    with closing(db._conn()) as con:
        con.execute("DELETE FROM environment_ledger")
        con.commit()
    assert db.environment_seen(exit_ip=IP)
    clock[0] += DAY
    assert not db.environment_seen(exit_ip=IP)


def test_persisting_run_does_not_extend_ip_window(clock):
    env = _env("old")
    db.create_run("old", "old@example.com", "fixture.log")
    assert db.reserve_environment("old", env)
    clock[0] += 60
    db.update_run_environment("old", env)
    clock[0] += DAY - 60
    assert not db.environment_seen(exit_ip=IP)


def test_rebind_to_expired_ip_preserves_history_and_starts_window(clock):
    _reserve("old")
    clock[0] += DAY
    active = _reserve("active", OTHER_IP)
    clock[0] += 30
    assert db.rebind_environment_exit("active", dict(active, exit_ip=IP), old_exit_ip=OTHER_IP)
    assert db.environment_seen(exit_ip=IP)
    assert not db.environment_seen(exit_ip=OTHER_IP)
    ledger = {r["run_id"]: r for r in db.list_environment_ledger()}
    assert ledger["old"]["exit_ip"] == IP
    assert ledger["active"]["exit_ip"] == IP
    assert ledger["active"]["exit_ip_reserved_at"] == clock[0]
    assert ledger["active"]["created_at"] == clock[0] - 30
    # The run copy retains the same clock even if the ledger is later cleared.
    with closing(db._conn()) as con:
        con.execute("DELETE FROM environment_ledger")
        con.commit()
    clock[0] += DAY - 1
    assert db.environment_seen(exit_ip=IP)
    clock[0] += 1
    assert not db.environment_seen(exit_ip=IP)


def test_concurrent_reservations_without_python_lock_have_one_winner(clock, monkeypatch):
    _reserve("old")
    clock[0] += DAY
    # Each call uses its own connection. Disable the process-local lock to
    # exercise the SQLite serialization that also protects separate processes.
    monkeypatch.setattr(db, "_lock", nullcontext())
    barrier = threading.Barrier(8)

    def reserve(i):
        barrier.wait(timeout=10)
        return db.reserve_environment(f"new-{i}", _env(f"new-{i}"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))
    assert sum(results) == 1
    assert len(db.list_environment_ledger()) == 2


def test_rebind_and_new_reservation_share_writer_guard(clock, monkeypatch):
    _reserve("old")
    clock[0] += DAY
    active = _reserve("active", OTHER_IP)
    monkeypatch.setattr(db, "_lock", nullcontext())
    barrier = threading.Barrier(2)

    def compete(rebind):
        barrier.wait(timeout=10)
        if rebind:
            return db.rebind_environment_exit("active", dict(active, exit_ip=IP), old_exit_ip=OTHER_IP)
        return db.reserve_environment("new", _env("new"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, [False, True]))
    assert sum(results) == 1
    assert db.environment_seen(exit_ip=IP)


def test_rebind_rechecks_after_optimistic_lookup(clock, monkeypatch):
    active = _reserve("active", OTHER_IP)
    _reserve("owner", IP)
    monkeypatch.setattr(db, "environment_seen", lambda **kwargs: False)
    assert not db.rebind_environment_exit("active", dict(active, exit_ip=IP), old_exit_ip=OTHER_IP)
    assert db.get_run_environment("active")["exit_ip"] == OTHER_IP


def _legacy_ledger(con, created_at):
    con.executescript("""
        CREATE TABLE environment_ledger (
            allocation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE,
            proxy TEXT, proxy_fingerprint TEXT NOT NULL,
            exit_ip TEXT NOT NULL UNIQUE, exit_country TEXT,
            country_code TEXT, country_source TEXT,
            browser_family TEXT NOT NULL, browser_engine TEXT NOT NULL,
            fingerprint_id TEXT NOT NULL UNIQUE,
            fingerprint_signature TEXT NOT NULL UNIQUE,
            fingerprint_json TEXT NOT NULL, created_at REAL NOT NULL,
            extra_note TEXT
        );
        CREATE INDEX fixture_country ON environment_ledger(exit_country);
        CREATE TABLE fixture_audit (run_id TEXT);
        CREATE TRIGGER fixture_insert AFTER INSERT ON environment_ledger
        BEGIN INSERT INTO fixture_audit(run_id) VALUES (NEW.run_id); END;
    """)
    con.execute(
        "INSERT INTO environment_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("allocation-old", "old", "", "direct", IP, "JP", "JP", "proxy", "firefox", "auto",
         "profile-old", "signature-old", "{}", created_at, "retained custom column"),
    )
    con.commit()


def test_legacy_schema_migrates_without_losing_history(clock, tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as con:
        _legacy_ledger(con, clock[0] - DAY)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.init_db()
    assert not db.environment_seen(exit_ip=IP)
    assert db.environment_seen(fingerprint_id="profile-old")
    assert db.reserve_environment("new", _env("new"))
    with closing(db._conn()) as con:
        rows = con.execute("SELECT run_id, extra_note, created_at FROM environment_ledger ORDER BY run_id").fetchall()
        assert [tuple(r) for r in rows] == [
            ("new", None, clock[0]), ("old", "retained custom column", clock[0] - DAY),
        ]
        assert [r[0] for r in con.execute("SELECT run_id FROM fixture_audit ORDER BY rowid")] == ["old", "new"]
        assert "fixture_country" in {r[1] for r in con.execute("PRAGMA index_list(environment_ledger)")}
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_failed_migration_rolls_back_old_ledger(clock, tmp_path):
    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "ALTER TABLE environment_ledger_ip_window RENAME TO environment_ledger":
                raise sqlite3.OperationalError("simulated rename failure")
            return super().execute(sql, *args, **kwargs)

    path = tmp_path / "rollback.db"
    with closing(sqlite3.connect(path, factory=FailingConnection)) as con:
        _legacy_ledger(con, clock[0])
        with pytest.raises(sqlite3.OperationalError, match="simulated rename failure"):
            db._migrate_environment_ip_window(con)
        assert con.execute("SELECT run_id, extra_note FROM environment_ledger").fetchone() == (
            "old", "retained custom column",
        )
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='environment_ledger_ip_window'"
        ).fetchone()
        assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("table,timestamp,index", [
    ("runs", db._RUN_IP_RESERVED_AT, "idx_runs_exit_ip_reserved_at"),
    ("environment_ledger", db._LEDGER_IP_RESERVED_AT, "idx_environment_exit_ip_reserved_at"),
])
def test_window_lookup_uses_ip_and_time_index(table, timestamp, index):
    with closing(db._conn()) as con:
        plan = " ".join(r[3] for r in con.execute(
            f"EXPLAIN QUERY PLAN SELECT 1 FROM {table} WHERE exit_ip=? AND {timestamp}>? LIMIT 1",
            (IP, 1_800_000_000),
        ))
    assert index in plan
    assert "SEARCH" in plan
    assert "SCAN" not in plan
