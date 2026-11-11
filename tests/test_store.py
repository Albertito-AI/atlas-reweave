"""Deterministic storage fixtures, including real subprocess crash/lock checks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from reweave.codec import canonical, digest
import reweave.store as store_module
from reweave.store import Store, validate_judgement, validate_work
from reweave.types import BudgetExceeded, EngineClosed, Limits, StoreBusy, StoreCorrupt


def work(value=1, *, output=None, parents=(), config=None):
    envelope = {
        "schema": "reweave.work.v1", "scope": "store-test", "stage": "deterministic-fixture",
        "producer": {"revision": "fixture-v1", "model_config": {} if config is None else config},
        "input": value, "parents": list(parents),
    }
    output = {"fixture": value} if output is None else output
    return {
        "work_key": digest(envelope), "envelope": envelope,
        "output": output, "output_digest": digest(output),
    }


def judgement(record, *, revision="checker-v1", accepted=True, policy=None, evidence=None, parents=()):
    context = {
        "schema": "reweave.judgement.v1", "work_key": record["work_key"],
        "output_digest": record["output_digest"],
        "verifier": {"revision": revision, "policy": {} if policy is None else policy},
        "parents": list(parents),
    }
    return {
        "judgement_key": digest(context), "context": context,
        "verdict": {"accepted": accepted, "reason": "Deterministic fixture decision", "evidence": evidence},
    }


def persist(store, record):
    attempt = store.reserve_attempt(record["work_key"])
    store.finish_attempt(attempt, record)
    return attempt


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.limits = Limits()

    def test_validators_snapshot_nested_evidence(self):
        record = work({"input": [1]}, output={"result": [2]}, config={"options": [3]})
        validated = validate_work(record, self.limits)
        validated["envelope"]["producer"]["model_config"]["options"].append(4)
        validated["output"]["result"].append(5)
        self.assertEqual(record["output"], {"result": [2]})
        self.assertEqual(record["envelope"]["producer"]["model_config"], {"options": [3]})
        decision = judgement(record, evidence={"checks": [True]})
        checked = validate_judgement(decision, self.limits)
        checked["verdict"]["evidence"]["checks"].clear()
        self.assertEqual(decision["verdict"]["evidence"], {"checks": [True]})

    def test_complete_envelope_output_context_and_verdict_byte_boundaries(self):
        record = work("é", output="é", config={"note": "é"})
        envelope_size = len(canonical(record["envelope"]).encode("utf-8"))
        output_size = len(canonical(record["output"]).encode("utf-8"))
        validate_work(record, replace(self.limits, max_input_bytes=envelope_size, max_output_bytes=output_size))
        for limits in (
            replace(self.limits, max_input_bytes=envelope_size - 1),
            replace(self.limits, max_output_bytes=output_size - 1),
        ):
            with self.subTest(limits=limits), self.assertRaises(BudgetExceeded):
                validate_work(record, limits)
        decision = judgement(record, policy={"note": "é"}, evidence="é")
        context_size = len(canonical(decision["context"]).encode("utf-8"))
        verdict_size = len(canonical(decision["verdict"]).encode("utf-8"))
        validate_judgement(decision, replace(self.limits, max_input_bytes=context_size, max_verdict_bytes=verdict_size))
        for limits in (
            replace(self.limits, max_input_bytes=context_size - 1),
            replace(self.limits, max_verdict_bytes=verdict_size - 1),
        ):
            with self.subTest(limits=limits), self.assertRaises(BudgetExceeded):
                validate_judgement(decision, limits)

    def test_work_shape_type_identity_and_parent_rejections(self):
        original = work()
        mutations = (
            lambda r: r.update(extra=True),
            lambda r: r.update(work_key="A" * 64),
            lambda r: r.update(output_digest="0" * 64),
            lambda r: r["envelope"].update(schema="reweave.work.v2"),
            lambda r: r["envelope"].update(stage=""),
            lambda r: r["envelope"].update(scope=True),
            lambda r: r["envelope"]["producer"].update(revision=1),
            lambda r: r["envelope"]["producer"].update(model_config=[]),
            lambda r: r["envelope"].update(parents=("0" * 64,)),
            lambda r: r["envelope"].update(parents=["0" * 64, "0" * 64]),
            lambda r: r["envelope"].update(parents=[False]),
            lambda r: r.update(output=float("nan")),
            lambda r: r.update(output=(1, 2)),
            lambda r: r.update(output="\ud800"),
        )
        for index, mutate in enumerate(mutations):
            record = deepcopy(original)
            mutate(record)
            with self.subTest(index=index), self.assertRaises(StoreCorrupt):
                validate_work(record, self.limits)
        parents = [digest(i) for i in range(3)]
        record = work(parents=parents)
        self.assertEqual(validate_work(record, replace(self.limits, max_parents=3))["envelope"]["parents"], parents)
        with self.assertRaises(BudgetExceeded):
            validate_work(record, replace(self.limits, max_parents=2))

    def test_judgement_rejects_coercible_boolean_and_malformed_evidence(self):
        original = judgement(work())
        mutations = (
            lambda r: r.update(extra=True),
            lambda r: r.update(judgement_key="0" * 64),
            lambda r: r["context"].update(schema="reweave.judgement.v2"),
            lambda r: r["context"].update(output_digest=1),
            lambda r: r["context"]["verifier"].update(policy=[]),
            lambda r: r["context"]["verifier"].update(revision=False),
            lambda r: r["context"].update(parents=["0" * 64, "0" * 64]),
            lambda r: r["verdict"].update(accepted=1),
            lambda r: r["verdict"].update(accepted="true"),
            lambda r: r["verdict"].update(reason="  "),
            lambda r: r["verdict"].update(evidence={1: "not a JSON key"}),
        )
        for index, mutate in enumerate(mutations):
            record = deepcopy(original)
            mutate(record)
            with self.subTest(index=index), self.assertRaises(StoreCorrupt):
                validate_judgement(record, self.limits)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "evidence.sqlite3"
        self.limits = Limits()

    def test_durable_round_trip_and_immutable_idempotence(self):
        record = work(output={"values": [1]})
        decision = judgement(record, evidence={"checks": [True]})
        with Store(self.path, self.limits) as store:
            attempt = persist(store, record)
            store.finish_attempt(attempt, record)
            store.put_judgement(decision)
            store.put_judgement(decision)
            returned = store.get_work(record["work_key"])
            returned["output"]["values"].append(2)
            self.assertEqual(store.get_work(record["work_key"]), record)
            self.assertEqual(store.list_work_keys(), [record["work_key"]])
            self.assertEqual(store.stats(), {
                "works": 1, "judgements": 1, "attempts_total": 1,
                "attempts_running": 0, "attempts_interrupted": 0, "attempts_failed": 0,
            })
        with Store(self.path, self.limits) as store:
            self.assertEqual(store.get_work(record["work_key"]), record)
            self.assertEqual(store.get_judgement(decision["judgement_key"]), decision)
            self.assertEqual(store.stats()["attempts_total"], 1)

    def test_running_reservations_and_failed_validation_consume_budget(self):
        limits = replace(self.limits, max_works=1, max_producer_calls=3, max_output_bytes=8)
        with Store(self.path, limits) as store:
            first = store.reserve_attempt(work(1)["work_key"])
            with self.assertRaises(BudgetExceeded):
                store.reserve_attempt(work(2)["work_key"])
            store.finish_attempt(first, error="Fixture producer failed")
            oversized = work(2, output="too many output bytes")
            second = store.reserve_attempt(oversized["work_key"])
            with self.assertRaises(BudgetExceeded):
                store.finish_attempt(second, oversized)
            store.finish_attempt(second, None, error="Second finalisation preserves first failure")
            self.assertEqual(store.stats()["attempts_failed"], 2)
            valid = work(3, output=3)
            persist(store, valid)
            with self.assertRaises(BudgetExceeded):
                store.reserve_attempt(work(4)["work_key"])
            self.assertEqual(store.stats()["attempts_total"], 3)
            self.assertEqual(store.stats()["works"], 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertIn("Encoded JSON exceeds", db.execute(
                "SELECT error FROM attempts WHERE attempt_id=?", (second,)
            ).fetchone()[0])

    def test_attempt_identity_and_work_conflicts_cannot_overwrite(self):
        original = work(1, output="original")
        with Store(self.path, self.limits) as store:
            first = persist(store, original)
            alternative = work(1, output="different but same envelope")
            duplicate = store.reserve_attempt(original["work_key"])
            with self.assertRaises(StoreCorrupt):
                store.finish_attempt(duplicate, alternative)
            store.finish_attempt(duplicate, error="Idempotent failure finalisation")
            with self.assertRaises(StoreCorrupt):
                store.finish_attempt(first, None, error="Cannot erase success")
            wrong = store.reserve_attempt(work(2)["work_key"])
            with self.assertRaises(StoreCorrupt):
                store.finish_attempt(wrong, work(3))
            with self.assertRaises(StoreCorrupt):
                store.finish_attempt("0" * 32, original)
            self.assertEqual(store.get_work(original["work_key"]), original)
            self.assertEqual(store.stats()["attempts_failed"], 2)
            self.assertEqual(store.stats()["works"], 1)

    def test_duplicate_reservations_have_separate_durable_call_cost(self):
        record = work()
        with Store(self.path, replace(self.limits, max_works=2)) as store:
            one = store.reserve_attempt(record["work_key"])
            two = store.reserve_attempt(record["work_key"])
            self.assertNotEqual(one, two)
            with self.assertRaises(BudgetExceeded):
                store.reserve_attempt(record["work_key"])
            store.finish_attempt(one, record)
            store.finish_attempt(two, record)
            self.assertEqual(store.stats()["works"], 1)
            self.assertEqual(store.stats()["attempts_total"], 2)

    def test_judgement_work_reference_conflict_and_capacity(self):
        record = work()
        decision = judgement(record)
        with Store(self.path, replace(self.limits, max_judgements=1)) as store:
            with self.assertRaises(StoreCorrupt):
                store.put_judgement(decision)
            persist(store, record)
            wrong = judgement(work(output="a different output"))
            with self.assertRaises(StoreCorrupt):
                store.put_judgement(wrong)
            store.put_judgement(decision)
            changed = judgement(record, accepted=False)
            with self.assertRaises(StoreCorrupt):
                store.put_judgement(changed)
            with self.assertRaises(BudgetExceeded):
                store.put_judgement(judgement(record, revision="checker-v2"))
            store.put_judgement(decision)
            self.assertEqual(store.get_judgement(decision["judgement_key"]), decision)
            self.assertEqual(store.stats()["judgements"], 1)

    def test_lower_limits_preserve_history_and_block_new_admission(self):
        records = [work(1), work(2)]
        with Store(self.path, self.limits) as store:
            for record in records:
                persist(store, record)
                store.put_judgement(judgement(record))
            store.finish_attempt(store.reserve_attempt(work(3)["work_key"]), error="Fixture failure")
        lower = replace(self.limits, max_works=1, max_judgements=1, max_producer_calls=1)
        with Store(self.path, lower) as store:
            self.assertEqual(store.stats()["works"], 2)
            self.assertEqual(store.stats()["judgements"], 2)
            self.assertEqual(store.stats()["attempts_total"], 3)
            self.assertEqual(store.get_work(records[0]["work_key"]), records[0])
            with self.assertRaises(BudgetExceeded):
                store.reserve_attempt(work(4)["work_key"])
            with self.assertRaises(BudgetExceeded):
                store.put_judgement(judgement(records[0], revision="new-checker"))
        with Store(self.path, replace(lower, max_output_bytes=2)) as store:
            with self.assertRaises(BudgetExceeded):
                store.get_work(records[0]["work_key"])
            self.assertEqual(store.stats()["works"], 2)

    def test_threads_cannot_overbook_active_reservations(self):
        barrier = threading.Barrier(8)
        with Store(self.path, replace(self.limits, max_works=2, max_producer_calls=3)) as store:
            def reserve(index):
                barrier.wait(timeout=5)
                try:
                    return store.reserve_attempt(work(index)["work_key"])
                except BudgetExceeded:
                    return None
            with ThreadPoolExecutor(max_workers=8) as pool:
                attempts = list(pool.map(reserve, range(8)))
            admitted = [item for item in attempts if item is not None]
            self.assertEqual(len(admitted), 2)
            self.assertEqual(store.stats()["attempts_running"], 2)
            for attempt in admitted:
                store.finish_attempt(attempt, error="Fixture callback failure")
            persist(store, work(10))
            self.assertEqual(store.stats()["attempts_total"], 3)
            with self.assertRaises(BudgetExceeded):
                store.reserve_attempt(work(11)["work_key"])

    def test_error_text_is_utf8_bounded_and_first_failure_is_retained(self):
        with Store(self.path, self.limits) as store:
            attempt = store.reserve_attempt(work()["work_key"])
            store.finish_attempt(attempt, error="é" * 10_000)
            store.finish_attempt(attempt, error="replacement is ignored")
        with closing(sqlite3.connect(self.path)) as db, db:
            error = db.execute("SELECT error FROM attempts").fetchone()[0]
        self.assertLessEqual(len(error.encode("utf-8")), 4_096)
        self.assertTrue(error.endswith(" [truncated]"))
        self.assertTrue(error.startswith("é"))

    def test_full_record_digest_catches_updated_output_and_component_digest(self):
        record = work()
        with Store(self.path, self.limits) as store:
            persist(store, record)
        replacement = {"fixture": "changed"}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE works SET output=?, output_digest=?", (canonical(replacement), digest(replacement)))
        with Store(self.path, self.limits) as store:
            with self.assertRaisesRegex(StoreCorrupt, "Complete work record"):
                store.get_work(record["work_key"])
            self.assertEqual(store.stats()["attempts_total"], 1)

    def test_judgement_digest_catches_changed_decision(self):
        record = work()
        decision = judgement(record)
        with Store(self.path, self.limits) as store:
            persist(store, record)
            store.put_judgement(decision)
        changed = deepcopy(decision["verdict"])
        changed["accepted"] = False
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE judgements SET verdict=?", (canonical(changed),))
        with Store(self.path, self.limits) as store:
            with self.assertRaisesRegex(StoreCorrupt, "Complete judgement record"):
                store.get_judgement(decision["judgement_key"])

    def test_duplicate_keys_noncanonical_text_and_blob_storage_are_refused(self):
        for index, corrupt in enumerate(('{"a":1,"a":2}', ' {"fixture":1}', b'{"fixture":1}')):
            path = self.path.with_name(f"corrupt-{index}.sqlite3")
            record = work()
            with Store(path, self.limits) as store:
                persist(store, record)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE works SET output=?", (corrupt,))
            with Store(path, self.limits) as store:
                with self.subTest(corrupt=corrupt), self.assertRaises(StoreCorrupt):
                    store.get_work(record["work_key"])

    def test_attempt_history_deletion_cannot_reset_durable_counter(self):
        with Store(self.path, self.limits) as store:
            store.finish_attempt(store.reserve_attempt(work()["work_key"]), error="Fixture failure")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM attempts")
        with self.assertRaisesRegex(StoreCorrupt, "counter does not match"):
            Store(self.path, self.limits)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT producer_calls FROM state").fetchone()[0], 1)

    def test_unknown_version_and_foreign_database_are_never_reset(self):
        with Store(self.path, self.limits) as store:
            persist(store, work())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA user_version=99")
        for _ in range(2):
            with self.assertRaisesRegex(StoreCorrupt, "Unsupported store schema"):
                Store(self.path, self.limits)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(db.execute("SELECT count(*) FROM works").fetchone()[0], 1)
        foreign = self.path.with_name("foreign.sqlite3")
        with closing(sqlite3.connect(foreign)) as db, db:
            db.execute("CREATE TABLE unrelated (value TEXT)")
            db.execute("INSERT INTO unrelated VALUES ('retain me')")
        with self.assertRaises(StoreCorrupt):
            Store(foreign, self.limits)
        with closing(sqlite3.connect(foreign)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM unrelated").fetchone()[0], "retain me")

    def test_non_database_file_is_not_replaced(self):
        content = b"not a SQLite database\n"
        self.path.write_bytes(content)
        with self.assertRaises(StoreCorrupt):
            Store(self.path, self.limits)
        self.assertEqual(self.path.read_bytes(), content)

    def test_view_only_foreign_database_is_not_treated_as_empty(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE VIEW existing_evidence AS SELECT 'retain me' AS value")
        with self.assertRaises(StoreCorrupt):
            Store(self.path, self.limits)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM existing_evidence").fetchone()[0], "retain me")
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
        foreign_id = self.path.with_name("foreign-application.sqlite3")
        with closing(sqlite3.connect(foreign_id)) as db, db:
            db.execute("PRAGMA application_id=123")
        with self.assertRaises(StoreCorrupt):
            Store(foreign_id, self.limits)
        with closing(sqlite3.connect(foreign_id)) as db, db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], 123)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)

    def test_store_column_constraints_are_part_of_versioned_schema(self):
        with Store(self.path, self.limits):
            pass
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE judgements")
            db.execute("CREATE TABLE judgements (judgement_key TEXT PRIMARY KEY, context TEXT, "
                       "verdict TEXT, record_digest TEXT, created_at TEXT)")
        with self.assertRaisesRegex(StoreCorrupt, "column constraints"):
            Store(self.path, self.limits)

    def test_close_is_idempotent_and_releases_lease(self):
        store = Store(self.path, self.limits)
        store.close()
        store.close()
        with self.assertRaises(EngineClosed):
            store.stats()
        with Store(self.path, self.limits) as second:
            store.close()
            with self.assertRaises(StoreBusy):
                Store(self.path, self.limits)
            self.assertEqual(second.stats()["attempts_total"], 0)

    def _environment(self):
        env = os.environ.copy()
        package_root = str(Path(store_module.__file__).resolve().parents[1])
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        return env

    def test_actual_second_process_and_canonical_alias_are_busy(self):
        code = """from reweave.store import Store
from reweave.types import Limits, StoreBusy
import sys
try:
    Store(sys.argv[1], Limits())
except StoreBusy:
    print('busy')
else:
    raise SystemExit('second process acquired the live lease')
"""
        with Store(self.path, self.limits):
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.path)], env=self._environment(),
                cwd=self.temp.name, capture_output=True, text=True, timeout=15, check=True,
            )
            self.assertEqual(result.stdout.strip(), "busy")
            alias = self.path.parent / "alias.sqlite3"
            alias.symlink_to(self.path)
            with self.assertRaises(StoreBusy):
                Store(alias, self.limits)
            again = subprocess.run(
                [sys.executable, "-c", code, str(self.path)], env=self._environment(),
                cwd=self.temp.name, capture_output=True, text=True, timeout=15, check=True,
            )
            self.assertEqual(again.stdout.strip(), "busy")

    def test_actual_process_crash_releases_lease_and_retains_attempt_cost(self):
        completed = work(1)
        interrupted = work(2)
        code = """from reweave.store import Store
from reweave.types import Limits
import json, sys
store = Store(sys.argv[1], Limits(max_works=3, max_producer_calls=3))
record = json.loads(sys.argv[2])
attempt = store.reserve_attempt(record['work_key'])
store.finish_attempt(attempt, record)
store.reserve_attempt(sys.argv[3])
print(json.dumps(store.stats()), flush=True)
sys.stdin.read()
"""
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path), json.dumps(completed), interrupted["work_key"]],
            env=self._environment(), cwd=self.temp.name,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        ready = threading.Event()
        readiness = []

        def read_ready_line():
            try:
                readiness.append(process.stdout.readline())
            except Exception as exc:
                readiness.append(exc)
            finally:
                ready.set()

        reader = threading.Thread(target=read_ready_line, daemon=True)
        reader.start()
        try:
            self.assertTrue(ready.wait(timeout=15), "Crash fixture did not become ready")
            if isinstance(readiness[0], Exception):
                raise readiness[0]
            if not readiness[0]:
                process.wait(timeout=15)
                self.fail("Crash fixture exited before readiness: " + process.stderr.read())
            before = json.loads(readiness[0])
            self.assertEqual(before["attempts_running"], 1)
            self.assertEqual(before["attempts_total"], 2)
            with self.assertRaises(StoreBusy):
                Store(self.path, self.limits)
            process.kill()
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=15)
            reader.join(timeout=15)
            # The child has exited, so its pipe reaches EOF even when readiness
            # timed out. Do not race communicate() against a live stdout reader.
            try:
                self.assertFalse(reader.is_alive(), "Crash-fixture readiness reader did not stop")
                process.communicate(timeout=15)
            finally:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
        limits = replace(self.limits, max_works=3, max_producer_calls=3)
        with Store(self.path, limits) as store:
            self.assertEqual(store.get_work(completed["work_key"]), completed)
            self.assertIsNone(store.get_work(interrupted["work_key"]))
            self.assertEqual(store.stats(), {
                "works": 1, "judgements": 0, "attempts_total": 2,
                "attempts_running": 0, "attempts_interrupted": 1, "attempts_failed": 0,
            })
            last = store.reserve_attempt(work(3)["work_key"])
            store.finish_attempt(last, error="Fixture callback failure")
            with self.assertRaises(BudgetExceeded):
                store.reserve_attempt(work(4)["work_key"])
        with Store(self.path, limits) as store:
            self.assertEqual(store.stats()["attempts_total"], 3)
            self.assertEqual(store.stats()["attempts_interrupted"], 1)
        self.assertTrue(self.path.with_name(self.path.name + ".lease.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
