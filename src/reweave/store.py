"""Durable, immutable evidence and bounded producer-attempt reservations.

The sidecar database holds an exclusive SQLite transaction for this object's
lifetime. Cooperating processes use the same canonical path; a process crash
releases the operating-system lock without a PID-file reclamation rule. This is
not protection against a writer that removes or replaces database files.

The complete envelope and judgement context each consume ``max_input_bytes``;
identity metadata and configuration are included. Output and verdict have their
own byte budgets. Existing evidence is never deleted to satisfy a lower limit.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Iterator
import uuid

from .codec import canonical, digest, digest_text, parse
from .types import BudgetExceeded, EngineClosed, InvalidJSON, Limits, StoreBusy, StoreCorrupt


_SCHEMA_VERSION = 1
_APPLICATION_ID = 0x52575631
_KEY = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ATTEMPT = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)
_ERROR_BYTES = 4_096
_COLUMNS = {
    "state": ("id", "schema_version", "producer_calls"),
    "works": ("work_key", "envelope", "output", "output_digest", "record_digest", "created_at"),
    "judgements": ("judgement_key", "context", "verdict", "record_digest", "created_at"),
    "attempts": ("attempt_id", "work_key", "status", "error", "started_at", "finished_at"),
}
_CREATE = (
    """CREATE TABLE state (
        id INTEGER PRIMARY KEY NOT NULL CHECK (id = 1),
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        producer_calls INTEGER NOT NULL CHECK (producer_calls >= 0))""",
    """CREATE TABLE works (
        work_key TEXT PRIMARY KEY NOT NULL,
        envelope TEXT NOT NULL, output TEXT NOT NULL, output_digest TEXT NOT NULL,
        record_digest TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE judgements (
        judgement_key TEXT PRIMARY KEY NOT NULL, context TEXT NOT NULL,
        verdict TEXT NOT NULL, record_digest TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY NOT NULL, work_key TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running','completed','failed','interrupted')),
        error TEXT, started_at TEXT NOT NULL, finished_at TEXT,
        CHECK ((status = 'running' AND finished_at IS NULL) OR
               (status != 'running' AND finished_at IS NOT NULL)))""",
)


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise StoreCorrupt(f"{label} has an invalid object shape")
    return value


def _text(value: Any, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise StoreCorrupt(f"{label} must be a nonempty string")
    return value


def _key(value: Any, label: str = "key") -> str:
    if type(value) is not str or _KEY.fullmatch(value) is None:
        raise StoreCorrupt(f"{label} must be a lowercase SHA-256 digest")
    return value


def _parents(value: Any, limits: Limits) -> None:
    if type(value) is not list:
        raise StoreCorrupt("Parents must be an ordered list")
    if len(value) > limits.max_parents:
        raise BudgetExceeded("Parent count exceeds max_parents")
    for parent in value:
        _key(parent, "parent key")
    if len(value) != len(set(value)):
        raise StoreCorrupt("Parent keys must be unique")


def _json(value: Any, limit: int) -> tuple[Any, str]:
    try:
        encoded = canonical(value, max_bytes=limit)
        return parse(encoded, max_bytes=limit), encoded
    except InvalidJSON as exc:
        raise StoreCorrupt(str(exc)) from exc


def validate_work(record: Any, limits: Limits) -> dict[str, Any]:
    """Return an isolated, structurally and cryptographically checked record.

    This verifies internal identity, not the existence or acceptance of parents.
    Malformed evidence raises StoreCorrupt; current byte/count limits raise
    BudgetExceeded. No verifier or producer is executed.
    """
    _object(record, {"work_key", "envelope", "output", "output_digest"}, "Work record")
    work_key = _key(record["work_key"], "work key")
    output_digest = _key(record["output_digest"], "output digest")
    envelope = _object(
        record["envelope"], {"schema", "scope", "stage", "producer", "input", "parents"},
        "Work envelope",
    )
    if type(envelope["schema"]) is not str or envelope["schema"] != "reweave.work.v1":
        raise StoreCorrupt("Unknown work schema")
    _text(envelope["scope"], "Scope")
    _text(envelope["stage"], "Stage")
    producer = _object(envelope["producer"], {"revision", "model_config"}, "Producer")
    _text(producer["revision"], "Producer revision")
    if type(producer["model_config"]) is not dict:
        raise StoreCorrupt("Model configuration must be an object")
    _parents(envelope["parents"], limits)
    envelope, envelope_text = _json(envelope, limits.max_input_bytes)
    output, output_text = _json(record["output"], limits.max_output_bytes)
    if digest_text(envelope_text) != work_key:
        raise StoreCorrupt("Work identity does not match its envelope")
    if digest_text(output_text) != output_digest:
        raise StoreCorrupt("Output digest does not match output")
    return {"work_key": work_key, "envelope": envelope, "output": output, "output_digest": output_digest}


def validate_judgement(record: Any, limits: Limits) -> dict[str, Any]:
    """Return an isolated checked decision, without attesting its truth."""
    _object(record, {"judgement_key", "context", "verdict"}, "Judgement record")
    judgement_key = _key(record["judgement_key"], "Judgement key")
    context = _object(
        record["context"], {"schema", "work_key", "output_digest", "verifier", "parents"},
        "Judgement context",
    )
    if type(context["schema"]) is not str or context["schema"] != "reweave.judgement.v1":
        raise StoreCorrupt("Unknown judgement schema")
    _key(context["work_key"], "Judged work key")
    _key(context["output_digest"], "Judged output digest")
    verifier = _object(context["verifier"], {"revision", "policy"}, "Verifier")
    _text(verifier["revision"], "Verifier revision")
    if type(verifier["policy"]) is not dict:
        raise StoreCorrupt("Verifier policy must be an object")
    _parents(context["parents"], limits)
    verdict = _object(record["verdict"], {"accepted", "reason", "evidence"}, "Verdict")
    if type(verdict["accepted"]) is not bool:
        raise StoreCorrupt("Verdict acceptance must be an exact boolean")
    _text(verdict["reason"], "Verdict reason")
    context, context_text = _json(context, limits.max_input_bytes)
    verdict, _ = _json(verdict, limits.max_verdict_bytes)
    if digest_text(context_text) != judgement_key:
        raise StoreCorrupt("Judgement identity does not match its context")
    return {"judgement_key": judgement_key, "context": context, "verdict": verdict}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _error(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise StoreCorrupt("Attempt error must be a string or None")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StoreCorrupt("Attempt error must contain valid Unicode") from exc
    if len(encoded) <= _ERROR_BYTES:
        return value
    suffix = " [truncated]"
    return encoded[: _ERROR_BYTES - len(suffix)].decode("utf-8", errors="ignore") + suffix


def _database_error(exc: sqlite3.DatabaseError) -> Exception:
    if isinstance(exc, sqlite3.OperationalError) and any(
        word in str(exc).lower() for word in ("locked", "busy")
    ):
        return StoreBusy("The SQLite store is busy")
    return StoreCorrupt(f"SQLite evidence is unavailable or invalid: {exc}")


class Store:
    """One cooperating owner, immutable evidence, and durable attempt budgets."""

    def __init__(self, path: str | Path, limits: Limits):
        if not isinstance(path, (str, Path)) or not str(path) or str(path) == ":memory:":
            raise ValueError("Store requires a durable filesystem path")
        if type(limits) is not Limits:
            raise TypeError("limits must be a Limits instance")
        self.limits = replace(limits)
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._closed = True
        self._db: sqlite3.Connection | None = None
        self._lease: sqlite3.Connection | None = None
        lease_path = self.path.with_name(self.path.name + ".lease.sqlite3")
        if lease_path.is_symlink():
            raise StoreCorrupt("The canonical store lease must not be a symbolic link")
        try:
            self._lease = sqlite3.connect(
                str(lease_path), timeout=0, isolation_level=None, check_same_thread=False,
            )
            self._lease.execute("PRAGMA busy_timeout = 0")
            self._lease.execute("PRAGMA journal_mode = DELETE")
            self._lease.execute("BEGIN EXCLUSIVE")
            self._db = sqlite3.connect(
                str(self.path), timeout=0, isolation_level=None, check_same_thread=False,
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA trusted_schema = OFF")
            self._initialise()
            self._closed = False
            with self._transaction() as db:
                self._counts(db)
                db.execute(
                    "UPDATE attempts SET status='interrupted', error=?, finished_at=? WHERE status='running'",
                    ("Previous owner exited before finalising this attempt", _now()),
                )
        except BaseException as exc:
            self._dispose()
            if isinstance(exc, sqlite3.DatabaseError):
                raise _database_error(exc) from exc
            raise

    def _initialise(self) -> None:
        assert self._db is not None
        db = self._db
        version = db.execute("PRAGMA user_version").fetchone()[0]
        objects = list(db.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ))
        tables = {row[1] for row in objects if row[0] == "table"}
        application_id = db.execute("PRAGMA application_id").fetchone()[0]
        fresh = version == 0 and application_id == 0 and not objects
        if not fresh and version != _SCHEMA_VERSION:
            raise StoreCorrupt(f"Unsupported store schema version {version}; expected {_SCHEMA_VERSION}")
        if not fresh:
            if tables != set(_COLUMNS):
                raise StoreCorrupt("Store tables do not match schema version 1")
            if application_id != _APPLICATION_ID:
                raise StoreCorrupt("Database is not a Reweave store")
            for table, names in _COLUMNS.items():
                columns = list(db.execute(f"PRAGMA table_info({table})"))
                if tuple(row[1] for row in columns) != names or columns[0][5] != 1:
                    raise StoreCorrupt(f"Invalid {table} table structure")
                for index, row in enumerate(columns):
                    expected_type = "INTEGER" if table == "state" else "TEXT"
                    nullable = table == "attempts" and row[1] in ("error", "finished_at")
                    if (row[2] != expected_type or row[3] != int(not nullable)
                            or row[4] is not None or row[5] != int(index == 0)):
                        raise StoreCorrupt(f"Invalid {table} column constraints")
            if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') LIMIT 1").fetchone():
                raise StoreCorrupt("Unexpected executable store schema")
            if [tuple(row) for row in db.execute("PRAGMA quick_check(1)")] != [("ok",)]:
                raise StoreCorrupt("SQLite integrity check failed")
        db.execute("PRAGMA journal_mode = DELETE")
        db.execute("PRAGMA synchronous = FULL")
        if fresh:
            db.execute("BEGIN IMMEDIATE")
            try:
                for statement in _CREATE:
                    db.execute(statement)
                db.execute("INSERT INTO state VALUES (1, ?, 0)", (_SCHEMA_VERSION,))
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _check_open(self) -> sqlite3.Connection:
        if self._owner_pid != os.getpid():
            raise EngineClosed("A Store cannot be used after fork; open a new instance")
        if self._closed or self._db is None:
            raise EngineClosed("Store is closed")
        return self._db

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = self._check_open()
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except BaseException as exc:
                if db.in_transaction:
                    db.rollback()
                if isinstance(exc, sqlite3.DatabaseError):
                    raise _database_error(exc) from exc
                raise

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = self._check_open()
            try:
                yield db
            except sqlite3.DatabaseError as exc:
                raise _database_error(exc) from exc

    def _counts(self, db: sqlite3.Connection) -> dict[str, int]:
        states = list(db.execute("SELECT id, schema_version, producer_calls FROM state"))
        if (len(states) != 1 or any(type(value) is not int for value in states[0])
                or tuple(states[0])[:2] != (1, _SCHEMA_VERSION)):
            raise StoreCorrupt("Invalid store state record")
        calls = states[0][2]
        if type(calls) is not int or calls < 0:
            raise StoreCorrupt("Invalid durable producer counter")
        counts = {status: 0 for status in ("running", "completed", "failed", "interrupted")}
        for row in db.execute("SELECT * FROM attempts"):
            if type(row["attempt_id"]) is not str or _ATTEMPT.fullmatch(row["attempt_id"]) is None:
                raise StoreCorrupt("Invalid attempt identity")
            _key(row["work_key"], "Reserved work key")
            status = row["status"]
            if type(status) is not str or status not in counts:
                raise StoreCorrupt("Invalid attempt status")
            if type(row["started_at"]) is not str or not row["started_at"]:
                raise StoreCorrupt("Invalid attempt timestamp")
            if (status == "running") != (row["finished_at"] is None):
                raise StoreCorrupt("Attempt state and completion timestamp disagree")
            if status != "running" and (type(row["finished_at"]) is not str or not row["finished_at"]):
                raise StoreCorrupt("Invalid completion timestamp")
            error = row["error"]
            if error is not None and (type(error) is not str or _error(error) != error):
                raise StoreCorrupt("Invalid attempt error record")
            if status in ("running", "completed") and error is not None:
                raise StoreCorrupt("A running or successful attempt cannot contain a failure")
            counts[status] += 1
        if sum(counts.values()) != calls:
            raise StoreCorrupt("Durable producer counter does not match attempt history")
        if db.execute(
            "SELECT 1 FROM attempts a LEFT JOIN works w ON a.work_key=w.work_key "
            "WHERE a.status='completed' AND w.work_key IS NULL LIMIT 1"
        ).fetchone():
            raise StoreCorrupt("Completed attempt is missing immutable work")
        return {
            "works": db.execute("SELECT count(*) FROM works").fetchone()[0],
            "judgements": db.execute("SELECT count(*) FROM judgements").fetchone()[0],
            "attempts_total": calls,
            "attempts_running": counts["running"],
            "attempts_interrupted": counts["interrupted"],
            "attempts_failed": counts["failed"],
        }

    @staticmethod
    def _stored_json(value: Any, limit: int) -> Any:
        if type(value) is not str:
            raise StoreCorrupt("Stored JSON must be text")
        try:
            decoded = parse(value, max_bytes=limit)
            if canonical(decoded, max_bytes=limit) != value:
                raise StoreCorrupt("Stored JSON is not canonical")
            return decoded
        except InvalidJSON as exc:
            raise StoreCorrupt(str(exc)) from exc

    def _work(self, db: sqlite3.Connection, key: str) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM works WHERE work_key=?", (key,)).fetchone()
        if row is None:
            return None
        record = validate_work({
            "work_key": row["work_key"],
            "envelope": self._stored_json(row["envelope"], self.limits.max_input_bytes),
            "output": self._stored_json(row["output"], self.limits.max_output_bytes),
            "output_digest": row["output_digest"],
        }, self.limits)
        if digest(record) != _key(row["record_digest"], "Stored work record digest"):
            raise StoreCorrupt("Complete work record integrity check failed")
        if type(row["created_at"]) is not str or not row["created_at"]:
            raise StoreCorrupt("Invalid work timestamp")
        return record

    def get_work(self, key: str) -> dict[str, Any] | None:
        _key(key, "Work key")
        with self._read() as db:
            return self._work(db, key)

    def reserve_attempt(self, work_key: str) -> str:
        _key(work_key, "Work key")
        with self._transaction() as db:
            counts = self._counts(db)
            if counts["attempts_total"] >= self.limits.max_producer_calls:
                raise BudgetExceeded("Durable producer-attempt budget exhausted")
            if counts["works"] + counts["attempts_running"] >= self.limits.max_works:
                raise BudgetExceeded("Work and active-reservation budget exhausted")
            attempt_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO attempts VALUES (?, ?, 'running', NULL, ?, NULL)",
                (attempt_id, work_key, _now()),
            )
            db.execute("UPDATE state SET producer_calls=producer_calls+1 WHERE id=1")
            return attempt_id

    def finish_attempt(
        self, attempt_id: str, record: dict[str, Any] | None = None, error: str | None = None,
    ) -> None:
        if type(attempt_id) is not str or _ATTEMPT.fullmatch(attempt_id) is None:
            raise StoreCorrupt("Invalid attempt identity")
        failure: Exception | None = None
        with self._transaction() as db:
            self._counts(db)
            attempt = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if attempt is None:
                raise StoreCorrupt("Unknown producer attempt")
            if attempt["status"] in ("failed", "interrupted") and record is None:
                return  # Preserve the first recorded failure, including recovery.
            if attempt["status"] == "completed":
                if error is None and record is not None:
                    checked = validate_work(record, self.limits)
                    existing = self._work(db, attempt["work_key"])
                    if checked["work_key"] == attempt["work_key"] and digest(checked) == digest(existing):
                        return
                raise StoreCorrupt("A completed attempt cannot be changed")
            if attempt["status"] != "running":
                raise StoreCorrupt("A finished attempt cannot commit new work")
            try:
                message = _error(error)
                if record is None:
                    checked = None
                else:
                    if error is not None:
                        raise StoreCorrupt("An attempt cannot supply both work and a failure")
                    checked = validate_work(record, self.limits)
                    if checked["work_key"] != attempt["work_key"]:
                        raise StoreCorrupt("Returned work does not match its reserved identity")
                    existing = self._work(db, checked["work_key"])
                    if existing is not None and digest(existing) != digest(checked):
                        raise StoreCorrupt("Immutable work conflicts with an existing record")
                    if existing is None:
                        if db.execute("SELECT count(*) FROM works").fetchone()[0] >= self.limits.max_works:
                            raise BudgetExceeded("Work-record budget exhausted")
                        db.execute(
                            "INSERT INTO works VALUES (?, ?, ?, ?, ?, ?)",
                            (checked["work_key"], canonical(checked["envelope"], max_bytes=None),
                             canonical(checked["output"], max_bytes=None), checked["output_digest"],
                             digest(checked), _now()),
                        )
                status = "completed" if checked is not None else "failed"
            except (StoreCorrupt, BudgetExceeded) as exc:
                failure = exc
                status, message = "failed", _error(str(exc))
            db.execute(
                "UPDATE attempts SET status=?, error=?, finished_at=? WHERE attempt_id=?",
                (status, message, _now(), attempt_id),
            )
        if failure is not None:
            raise failure

    def _judgement(self, db: sqlite3.Connection, key: str) -> dict[str, Any] | None:
        row = db.execute("SELECT * FROM judgements WHERE judgement_key=?", (key,)).fetchone()
        if row is None:
            return None
        record = validate_judgement({
            "judgement_key": row["judgement_key"],
            "context": self._stored_json(row["context"], self.limits.max_input_bytes),
            "verdict": self._stored_json(row["verdict"], self.limits.max_verdict_bytes),
        }, self.limits)
        if digest(record) != _key(row["record_digest"], "Stored judgement record digest"):
            raise StoreCorrupt("Complete judgement record integrity check failed")
        if type(row["created_at"]) is not str or not row["created_at"]:
            raise StoreCorrupt("Invalid judgement timestamp")
        self._check_judged_work(db, record)
        return record

    def _check_judged_work(self, db: sqlite3.Connection, record: dict[str, Any]) -> None:
        work = self._work(db, record["context"]["work_key"])
        if work is None or work["output_digest"] != record["context"]["output_digest"]:
            raise StoreCorrupt("Judgement does not refer to the stored work output")

    def get_judgement(self, key: str) -> dict[str, Any] | None:
        _key(key, "Judgement key")
        with self._read() as db:
            return self._judgement(db, key)

    def put_judgement(self, record: dict[str, Any]) -> None:
        checked = validate_judgement(record, self.limits)
        with self._transaction() as db:
            existing = self._judgement(db, checked["judgement_key"])
            if existing is not None:
                if digest(existing) != digest(checked):
                    raise StoreCorrupt("Immutable judgement conflicts with an existing record")
                return
            self._check_judged_work(db, checked)
            if db.execute("SELECT count(*) FROM judgements").fetchone()[0] >= self.limits.max_judgements:
                raise BudgetExceeded("Judgement-record budget exhausted")
            db.execute(
                "INSERT INTO judgements VALUES (?, ?, ?, ?, ?)",
                (checked["judgement_key"], canonical(checked["context"], max_bytes=None),
                 canonical(checked["verdict"], max_bytes=None), digest(checked), _now()),
            )

    def list_work_keys(self) -> list[str]:
        with self._read() as db:
            return [_key(row[0], "Stored work key") for row in db.execute("SELECT work_key FROM works ORDER BY work_key")]

    def stats(self) -> dict[str, int]:
        with self._read() as db:
            return self._counts(db)

    def _dispose(self) -> None:
        try:
            if self._db is not None:
                self._db.close()
        finally:
            self._db = None
            try:
                if self._lease is not None:
                    self._lease.close()
            finally:
                self._lease = None
                self._closed = True

    def close(self) -> None:
        with self._lock:
            if self._owner_pid != os.getpid():
                raise EngineClosed("A Store cannot be closed by a forked child")
            self._dispose()

    def __enter__(self) -> Store:
        with self._lock:
            self._check_open()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
