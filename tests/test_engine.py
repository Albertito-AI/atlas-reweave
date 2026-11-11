"""Adversarial tests of observable execution and acceptance guarantees.

Callbacks below are deterministic fixtures, except for explicit event gates and
the crash fixture. Assertions target public outcomes, not internal call graphs.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

from reweave import Engine, Limits, Stage, Verdict
from reweave.codec import digest, snapshot
from reweave.types import BudgetExceeded, EngineClosed, ReweaveError, StoreCorrupt


def accept(_input, _output, _parents):
    return Verdict(True, "Synthetic fixture satisfies this test policy")


class EngineContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = self.directory / "evidence.sqlite"

    def stage(self, producer=None, verifier=accept, **changes):
        value = Stage("sample", "producer-v1", producer or (lambda item, parents: item), "verifier-v1", verifier)
        return replace(value, **changes)

    def assert_hidden(self, receipt, status):
        self.assertEqual(receipt.status, status, receipt)
        self.assertIsNone(receipt.output)
        self.assertNotIn("output", receipt.to_dict())

    def test_work_identity_covers_input_producer_config_and_scope(self):
        generated = []
        def produce(item, parents):
            generated.append(item)
            return {"constant": 1}
        original = self.stage(produce, model_config={"model": "fixture", "temperature": 0})
        with Engine(self.store, [original], scope="first") as engine:
            baseline = engine.run("sample", {"a": 1, "b": 2})
            repeated = engine.run("sample", {"b": 2, "a": 1})
            different_input = engine.run("sample", {"a": 2, "b": 2})
            self.assertEqual(baseline.work_key, repeated.work_key)
            self.assertTrue(repeated.generation_reused)
        keys = {baseline.work_key, different_input.work_key}
        variants = (
            (replace(original, producer_revision="producer-v2"), "first"),
            (replace(original, model_config={"model": "fixture", "temperature": 1}), "first"),
            (original, "second"),
        )
        for stage, scope in variants:
            with self.subTest(scope=scope, producer=stage.producer_revision, config=stage.model_config):
                with Engine(self.store, [stage], scope=scope) as engine:
                    result = engine.run("sample", {"a": 1, "b": 2})
                    self.assertEqual(result.status, "accepted")
                    keys.add(result.work_key)
        self.assertEqual(len(keys), 5)
        self.assertEqual(len(generated), 5)

    def test_checker_and_policy_changes_reuse_generation_but_not_acceptance(self):
        generations, checks = [], []
        def produce(item, parents):
            generations.append(item)
            return {"n": item}
        def verify(item, output, parents):
            checks.append(item)
            return Verdict(True, "Checked under the configured fixture policy")
        stage = self.stage(produce, verify, policy={"threshold": 1})
        with Engine(self.store, [stage]) as engine:
            initial = engine.run("sample", 1)
        judgement_keys = {initial.judgement_key}
        for changed in (replace(stage, verifier_revision="verifier-v2"), replace(stage, policy={"threshold": 2})):
            with Engine(self.store, [changed]) as engine:
                checked = engine.run("sample", 1)
                self.assertEqual(checked.work_key, initial.work_key)
                self.assertTrue(checked.generation_reused)
                self.assertFalse(checked.verification_reused)
                judgement_keys.add(checked.judgement_key)
        self.assertEqual(len(generations), 1)
        self.assertEqual(len(checks), 3)
        self.assertEqual(len(judgement_keys), 3)

    def test_equal_outputs_do_not_share_judgements_between_distinct_work(self):
        checked = []
        def verify(item, output, parents):
            checked.append(item)
            return Verdict(item == "allowed", "Input-specific fixture policy")
        with Engine(self.store, [self.stage(lambda item, parents: {"same": True}, verify)]) as engine:
            allowed = engine.run("sample", "allowed")
            rejected = engine.run("sample", "denied")
            self.assertEqual(allowed.status, "accepted")
            self.assert_hidden(rejected, "rejected")
            self.assertNotEqual(allowed.work_key, rejected.work_key)
            self.assertNotEqual(allowed.judgement_key, rejected.judgement_key)
            self.assertEqual(checked, ["allowed", "denied"])

    def test_current_parent_rejection_blocks_previously_cached_reduce(self):
        calls = {"parent": 0, "reduce": 0, "reduce_check": 0}
        def parent(item, parents):
            calls["parent"] += 1
            return item
        def reduce(item, parents):
            calls["reduce"] += 1
            return sum(parents)
        def reduce_check(item, output, parents):
            calls["reduce_check"] += 1
            return Verdict(True, "Synthetic sum accepted")
        first = self.stage(parent, name="parent")
        second = self.stage(reduce, reduce_check, name="reduce")
        with Engine(self.store, [first, second]) as engine:
            parent_result = engine.run("parent", 3)
            child = engine.run("reduce", None, parents=[parent_result.work_key])
            self.assertEqual(child.output, 3)
        deny = replace(first, verifier_revision="deny-v2", policy={"allow": False}, verifier=lambda *args: Verdict(False, "Parent is no longer accepted"))
        with Engine(self.store, [deny, second]) as engine:
            self.assert_hidden(engine.revalidate(child.work_key), "blocked")
            self.assert_hidden(engine.run("reduce", None, parents=[parent_result.work_key]), "blocked")
            with self.assertRaises(ReweaveError):
                engine.export([child.work_key])
        self.assertEqual(calls, {"parent": 1, "reduce": 1, "reduce_check": 1})

    def test_parent_identity_and_order_are_part_of_child_work(self):
        with Engine(self.store, [self.stage()]) as engine:
            first = engine.run("sample", 1)
            second = engine.run("sample", 2)
            left = engine.run("sample", 0, parents=[first.work_key, second.work_key])
            right = engine.run("sample", 0, parents=[second.work_key, first.work_key])
            self.assertNotEqual(left.work_key, right.work_key)
            with self.assertRaises(ReweaveError):
                engine.run("sample", 0, parents=[first.work_key, first.work_key])

    def test_new_accepted_parent_judgement_rechecks_cached_child(self):
        produced, checked = [], []
        def produce(item, parents):
            produced.append(item)
            return item
        def verify(item, output, parents):
            checked.append(item)
            return Verdict(True, "Synthetic closure accepted")
        parent_stage = self.stage(produce, verify, name="parent")
        child_stage = self.stage(produce, verify, name="child")
        with Engine(self.store, [parent_stage, child_stage]) as engine:
            parent = engine.run("parent", "parent")
            child = engine.run("child", "child", parents=[parent.work_key])
        with Engine(self.store, [replace(parent_stage, policy={"revision": 2}), child_stage]) as engine:
            current = engine.revalidate(child.work_key)
            self.assertEqual(current.status, "accepted")
            self.assertEqual(current.work_key, child.work_key)
            self.assertNotEqual(current.judgement_key, child.judgement_key)
            self.assertTrue(current.generation_reused)
            self.assertFalse(current.verification_reused)
        self.assertEqual(produced, ["parent", "child"])
        self.assertEqual(checked, ["parent", "child", "parent", "child"])

    def test_revalidation_with_changed_producer_is_stale_without_generation(self):
        generated = []
        def produce(item, parents):
            generated.append(item)
            return item
        original = self.stage(produce)
        with Engine(self.store, [original]) as engine:
            first = engine.run("sample", 1)
        with Engine(self.store, [replace(original, producer_revision="producer-v2")]) as engine:
            self.assert_hidden(engine.revalidate(first.work_key), "stale")
        self.assertEqual(generated, [1])

    def test_registry_and_callback_values_are_isolated_snapshots(self):
        model = {"nested": ["initial"]}
        policy = {"nested": ["initial"]}
        original_input = {"items": [1]}
        seen = []
        def produce(item, parents):
            item["items"].append(99)
            return item
        def verify(item, output, parents):
            seen.append(item)
            output["items"].append(100)
            return Verdict(item == {"items": [1]}, "Verifier receives the original input")
        stage = self.stage(produce, verify, model_config=model, policy=policy)
        with Engine(self.store, [stage]) as engine:
            model["nested"].append("later")
            policy["nested"].append("later")
            first = engine.run("sample", original_input)
            self.assertEqual(first.output, {"items": [1, 99]})
            first.output["items"].append("caller mutation")
            repeated = engine.run("sample", {"items": [1]})
            self.assertEqual(repeated.work_key, first.work_key)
            self.assertEqual(repeated.output, {"items": [1, 99]})
            self.assertTrue(repeated.verification_reused)
        self.assertEqual(original_input, {"items": [1]})
        self.assertEqual(seen, [{"items": [1]}])

    def test_non_boolean_verdict_is_an_error_and_never_cached_as_acceptance(self):
        checks = []
        generations = []
        def produce(item, parents):
            generations.append(item)
            return item
        def verify(*args):
            checks.append(1)
            return Verdict(1, "Truthiness is not an acceptance decision") if len(checks) == 1 else Verdict(True, "Valid boolean decision")
        with Engine(self.store, [self.stage(produce, verify)]) as engine:
            self.assert_hidden(engine.run("sample", 1), "error")
            self.assertEqual(engine.stats()["judgements"], 0)
            retry = engine.run("sample", 1)
            self.assertEqual(retry.status, "accepted")
            self.assertTrue(retry.generation_reused)
            self.assertFalse(retry.verification_reused)
        self.assertEqual(len(generations), 1)
        self.assertEqual(len(checks), 2)

    def test_nonfinite_output_and_bad_verdict_evidence_fail_closed(self):
        with Engine(self.store, [self.stage(lambda *args: {"n": float("nan")})]) as engine:
            self.assert_hidden(engine.run("sample", 1), "error")
            self.assertEqual(engine.stats()["works"], 0)
        for number, verdict in enumerate((Verdict(True, "", None), Verdict(True, "Bad evidence", float("inf"))), start=2):
            with self.subTest(verdict=verdict):
                with Engine(self.store, [self.stage(verifier=lambda *args, v=verdict: v)]) as engine:
                    self.assert_hidden(engine.run("sample", number), "error")
                    self.assertEqual(engine.stats()["judgements"], 0)

    def test_invalid_requests_do_not_call_the_producer(self):
        calls = []
        with Engine(self.store, [self.stage(lambda item, parents: calls.append(item))]) as engine:
            for stage, value, parents in (("missing", 1, []), ("sample", float("nan"), []), ("sample", 1, ["bad-key"])):
                with self.subTest(stage=stage, value=value, parents=parents), self.assertRaises(ReweaveError):
                    engine.run(stage, value, parents=parents)
        self.assertEqual(calls, [])

    def test_corrupt_work_and_judgement_never_release_output_or_regenerate(self):
        for table, column, replacement in (("works", "output", '{"forged":true}'), ("judgements", "verdict", '{"accepted":true,"reason":"forged","evidence":null}')):
            with self.subTest(table=table):
                path = self.directory / f"{table}.sqlite"
                generated = []
                checked = []
                def produce(item, parents):
                    generated.append(item)
                    return {"trusted": item}
                def verify(*args):
                    checked.append(1)
                    return Verdict(True, "Original fixture decision")
                stage = self.stage(produce, verify)
                with Engine(path, [stage]) as engine:
                    receipt = engine.run("sample", 7)
                with closing(sqlite3.connect(path)) as database:
                    with database:
                        database.execute(f"UPDATE {table} SET {column} = ?", (replacement,))
                with Engine(path, [stage]) as engine:
                    self.assert_hidden(engine.revalidate(receipt.work_key), "corrupt")
                    self.assert_hidden(engine.run("sample", 7), "corrupt")
                    with self.assertRaises(ReweaveError):
                        engine.export([receipt.work_key])
                self.assertEqual(generated, [7])
                self.assertEqual(checked, [1])

    def test_concurrent_duplicate_requests_have_one_producer_and_one_verifier(self):
        entered, release = threading.Event(), threading.Event()
        barrier = threading.Barrier(8)
        lock = threading.Lock()
        calls = {"produce": 0, "verify": 0}
        def produce(item, parents):
            with lock:
                calls["produce"] += 1
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Fixture producer was not released")
            return item
        def verify(*args):
            with lock:
                calls["verify"] += 1
            return Verdict(True, "Concurrent fixture accepted")
        with Engine(self.store, [self.stage(produce, verify)], limits=Limits(max_workers=2, max_pending=4)) as engine:
            with ThreadPoolExecutor(max_workers=8) as pool:
                def caller():
                    barrier.wait(timeout=5)
                    return engine.run("sample", 5)
                futures = [pool.submit(caller) for _ in range(8)]
                try:
                    self.assertTrue(entered.wait(5), "No producer entered")
                    self.assertEqual(calls["produce"], 1)
                finally:
                    release.set()
                results = [future.result(timeout=10) for future in futures]
        self.assertEqual({result.status for result in results}, {"accepted"})
        self.assertEqual(len({result.work_key for result in results}), 1)
        self.assertEqual(calls, {"produce": 1, "verify": 1})

    def test_stream_consumes_only_its_pending_window_before_callbacks_finish(self):
        release, two_started = threading.Event(), threading.Event()
        lock = threading.Lock()
        state = {"pulled": 0, "started": 0, "active": 0, "peak": 0}
        def inputs():
            for value in range(7):
                with lock:
                    state["pulled"] += 1
                yield value
        def produce(item, parents):
            with lock:
                state["started"] += 1
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                if state["started"] == 2:
                    two_started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("Fixture stream was not released")
                return item
            finally:
                with lock:
                    state["active"] -= 1
        with Engine(self.store, [self.stage(produce)], limits=Limits(max_workers=2, max_pending=2)) as engine:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: list(engine.stream("sample", inputs(), max_pending=2)))
                try:
                    self.assertTrue(two_started.wait(5), "Two independent producers did not enter")
                    with lock:
                        self.assertEqual(state["pulled"], 2)
                        self.assertEqual(state["active"], 2)
                finally:
                    release.set()
                results = future.result(timeout=10)
        self.assertEqual([result.output for result in results], list(range(7)))
        self.assertEqual(state["pulled"], 7)
        self.assertLessEqual(state["peak"], 2)

    def test_stream_snapshots_an_iterator_that_reuses_a_mutable_dictionary(self):
        release, filled = threading.Event(), threading.Event()
        def inputs():
            shared = {"n": None}
            for value in range(9):
                shared["n"] = value
                if value == 2:
                    filled.set()
                yield shared
        def produce(item, parents):
            if not release.wait(5):
                raise RuntimeError("Fixture mutable iterator was not released")
            return item["n"]
        with Engine(self.store, [self.stage(produce)], limits=Limits(max_workers=2, max_pending=3)) as engine:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: list(engine.stream("sample", inputs())))
                try:
                    self.assertTrue(filled.wait(5), "The pending window was not filled")
                finally:
                    release.set()
                results = future.result(timeout=10)
        self.assertEqual([result.output for result in results], list(range(9)))

    def test_stream_item_budget_allows_only_one_unsubmitted_lookahead(self):
        pulled, produced, results = [], [], []
        def inputs():
            for value in range(20):
                pulled.append(value)
                yield value
        def produce(item, parents):
            produced.append(item)
            return item
        with Engine(self.store, [self.stage(produce)], limits=Limits(max_items=3, max_workers=1, max_pending=2)) as engine:
            with self.assertRaises(BudgetExceeded):
                for receipt in engine.stream("sample", inputs()):
                    results.append(receipt.output)
        self.assertEqual(results, [0, 1, 2])
        self.assertEqual(produced, [0, 1, 2])
        self.assertEqual(pulled, [0, 1, 2, 3])

    def test_stream_refuses_invalid_pending_limits_before_consuming_input(self):
        pulled = []
        def inputs():
            pulled.append(1)
            yield 1
        with Engine(self.store, [self.stage()], limits=Limits(max_pending=2)) as engine:
            for limit in (0, -1, True, 1.5, 3):
                with self.subTest(limit=limit), self.assertRaises(ReweaveError):
                    list(engine.stream("sample", inputs(), max_pending=limit))
        self.assertEqual(pulled, [])

    def test_failed_attempts_consume_producer_budget_across_reopen(self):
        calls = []
        def produce(item, parents):
            calls.append(item)
            if item == "fail":
                raise ValueError("Synthetic producer failure")
            return item
        stage, limits = self.stage(produce), Limits(max_producer_calls=2)
        with Engine(self.store, [stage], limits=limits) as engine:
            success = engine.run("sample", "success")
            self.assert_hidden(engine.run("sample", "fail"), "error")
            self.assertEqual(engine.stats()["attempts_total"], 2)
        with Engine(self.store, [stage], limits=limits) as engine:
            reused = engine.run("sample", "success")
            self.assertEqual(reused.work_key, success.work_key)
            self.assertTrue(reused.generation_reused)
            self.assert_hidden(engine.run("sample", "third"), "budget")
            self.assertEqual(engine.stats()["attempts_total"], 2)
            self.assertEqual(engine.stats()["attempts_failed"], 1)
        self.assertEqual(calls, ["success", "fail"])

    def test_process_exit_inside_verifier_recovers_committed_work_without_generation(self):
        marker = self.directory / "producer-calls.txt"
        child = textwrap.dedent('''
            import os
            from pathlib import Path
            import sys
            from reweave import Engine, Stage, Verdict
            store, marker = map(Path, sys.argv[1:])
            def produce(item, parents):
                with marker.open('a', encoding='utf-8') as stream:
                    stream.write('called\\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                return {'value': item}
            def verify(*args):
                os._exit(73)
            with Engine(store, [Stage('sample', 'producer-v1', produce, 'verifier-v1', verify)]) as engine:
                engine.run('sample', 42)
        ''')
        process = subprocess.run([sys.executable, "-c", child, str(self.store), str(marker)], cwd=self.directory, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 73, process.stdout + process.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "called\n")
        def forbidden_producer(*args):
            raise AssertionError("Durable work was generated a second time")
        with Engine(self.store, [self.stage(forbidden_producer)]) as engine:
            recovered = engine.run("sample", 42)
            self.assertEqual(recovered.status, "accepted", recovered)
            self.assertEqual(recovered.output, {"value": 42})
            self.assertTrue(recovered.generation_reused)
            self.assertFalse(recovered.verification_reused)
            self.assertEqual(engine.stats()["attempts_total"], 1)
        self.assertEqual(marker.read_text(encoding="utf-8"), "called\n")

    def test_close_drains_callbacks_and_refuses_later_calls(self):
        entered, release, verified, closing = (threading.Event() for _ in range(4))
        def produce(item, parents):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Fixture close gate was not released")
            return item
        def verify(*args):
            verified.set()
            return Verdict(True, "Verification completed before close returned")
        engine = Engine(self.store, [self.stage(produce, verify)])
        self.addCleanup(engine.close)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(engine.run, "sample", 1)
            self.assertTrue(entered.wait(5))
            def close():
                closing.set()
                engine.close()
                return verified.is_set()
            closed = pool.submit(close)
            try:
                self.assertTrue(closing.wait(5))
                with self.assertRaises(FutureTimeout):
                    closed.result(timeout=0.05)
            finally:
                release.set()
            self.assertEqual(running.result(timeout=10).status, "accepted")
            self.assertTrue(closed.result(timeout=10))
        for operation in (lambda: engine.run("sample", 2), lambda: engine.revalidate("a" * 64), lambda: engine.export([]), lambda: list(engine.stream("sample", [2]))):
            with self.subTest(operation=operation), self.assertRaises(EngineClosed):
                operation()

    def test_export_contains_only_the_current_accepted_dependency_closure(self):
        from reweave.bundle import verify_bundle

        source = self.stage(name="source")
        reduce = self.stage(lambda item, parents: sum(parents), name="reduce")
        with Engine(self.store, [source, reduce]) as engine:
            parent = engine.run("source", 2)
            unrelated = engine.run("source", 100)
            child = engine.run("reduce", None, parents=[parent.work_key])
            bundle = engine.export([child.work_key])
            self.assertEqual(bundle["schema"], "reweave.bundle.v1")
            self.assertEqual(bundle["roots"], [child.work_key])
            self.assertEqual({record["work_key"] for record in bundle["works"]}, {parent.work_key, child.work_key})
            self.assertNotIn(unrelated.work_key, {record["work_key"] for record in bundle["works"]})
            self.assertTrue(all(record["verdict"]["accepted"] is True for record in bundle["judgements"]))
            self.assertEqual(len(bundle["judgements"]), 2)
            report = verify_bundle(bundle)
            self.assertTrue(report["valid"])
            self.assertFalse(report["semantic_verification"])
        deny = replace(source, verifier_revision="denied-v2", verifier=lambda *args: Verdict(False, "New policy denies this parent"))
        with Engine(self.store, [deny, reduce]) as engine:
            with self.assertRaises(ReweaveError):
                engine.export([child.work_key])

    def test_bundle_validation_rejects_incomplete_or_unaccepted_closure_even_with_new_manifest(self):
        from reweave.bundle import verify_bundle

        with Engine(self.store, [self.stage()]) as engine:
            parent = engine.run("sample", 1)
            child = engine.run("sample", 2, parents=[parent.work_key])
            original = engine.export([child.work_key])
        missing = snapshot(original)
        missing["works"] = [work for work in missing["works"] if work["work_key"] != parent.work_key]
        rejected = snapshot(original)
        rejected["judgements"][0]["verdict"]["accepted"] = False
        for bundle in (missing, rejected):
            with self.subTest(kind="missing" if bundle is missing else "rejected"):
                bundle.pop("manifest_digest")
                bundle["manifest_digest"] = digest(bundle)
                with self.assertRaises(StoreCorrupt):
                    verify_bundle(bundle)

    def test_inspect_is_historical_metadata_and_does_not_disclose_rejected_output(self):
        calls = []
        def verify(*args):
            calls.append(1)
            return Verdict(False, "Fixture rejected")
        with Engine(self.store, [self.stage(lambda *args: {"secret": "private fixture output"}, verify)]) as engine:
            rejected = engine.run("sample", 1)
            metadata = engine.inspect(rejected.work_key)
            self.assertEqual(metadata["current_acceptance"], "not_evaluated")
            self.assertNotIn("output", metadata)
            self.assertNotIn("private fixture output", str(metadata))
            self.assertEqual(calls, [1])

    def test_discovery_metadata_is_copied_and_does_not_change_work_identity(self):
        example = {"n": [1]}
        original = self.stage(description="Synthetic fixture", input_example=example)
        with Engine(self.store, [original]) as engine:
            example["n"].append(2)
            metadata = engine.list_stages()
            self.assertEqual(metadata[0]["input_example"], {"n": [1]})
            metadata[0]["input_example"]["n"].append(3)
            self.assertEqual(engine.list_stages()[0]["input_example"], {"n": [1]})
            first = engine.run("sample", 1)
        with Engine(self.store, [replace(original, description="Changed documentation", input_example=None)]) as engine:
            repeated = engine.run("sample", 1)
            self.assertEqual(repeated.work_key, first.work_key)
            self.assertTrue(repeated.generation_reused)

    def test_callbacks_cannot_reenter_their_engine(self):
        def produce(item, parents):
            return engine.run("sample", item + 1).to_dict()
        with Engine(self.store, [self.stage(produce)]) as engine:
            self.assert_hidden(engine.run("sample", 1), "error")
            self.assertEqual(engine.stats()["attempts_total"], 1)


if __name__ == "__main__":
    unittest.main()
