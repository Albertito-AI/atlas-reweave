"""Narrow regressions for independently reproduced review findings."""

from pathlib import Path
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest

from reweave import Engine, Limits, Stage, Verdict
from reweave.bundle import verify_bundle
import reweave.engine as engine_module
from reweave.types import BudgetExceeded, ReweaveError


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "review.sqlite3"
        self.generated = []

        def produce(value, parents):
            self.generated.append(value)
            return value

        self.stage = Stage(
            "node", "fixture-producer-v1", produce, "fixture-verifier-v1",
            lambda *args: Verdict(True, "Deterministic review fixture accepted"),
        )

    def _chain_bundle(self):
        with Engine(self.path, [self.stage]) as engine:
            leaf = engine.run("node", "leaf")
            middle = engine.run("node", "middle", parents=[leaf.work_key])
            root = engine.run("node", "root", parents=[middle.work_key])
            self.assertEqual(root.status, "accepted")
            return root.work_key, engine.export([root.work_key])

    def test_shared_subtree_on_longer_path_cannot_bypass_current_depth(self):
        with Engine(self.path, [self.stage], limits=Limits(max_depth=8)) as engine:
            leaf = engine.run("node", "leaf")
            shared = engine.run("node", "shared", parents=[leaf.work_key])
            middle = engine.run("node", "middle", parents=[shared.work_key])
            upper = engine.run("node", "upper", parents=[middle.work_key])
            parents = [shared.work_key, upper.work_key]
            root = engine.run("node", "root", parents=parents)
            self.assertEqual(root.status, "accepted")
        # The short path visits shared first. Its later occurrence has a total
        # root-to-leaf path of five records, which cannot fit a limit of four.
        with Engine(self.path, [self.stage], limits=Limits(max_depth=4)) as engine:
            for receipt in (
                engine.revalidate(root.work_key),
                engine.run("node", "root", parents=parents),
            ):
                self.assertIn(receipt.status, ("blocked", "budget"))
                self.assertIsNone(receipt.output)
                self.assertNotIn("output", receipt.to_dict())
            with self.assertRaises(ReweaveError):
                engine.export([root.work_key])
        self.assertEqual(len(self.generated), 5, "A depth refusal must not replay generation")

    def test_offline_bundle_obeys_current_judgement_count_limit(self):
        _, bundle = self._chain_bundle()
        self.assertTrue(verify_bundle(bundle, Limits(max_judgements=3))["valid"])
        with self.assertRaises(BudgetExceeded):
            verify_bundle(bundle, Limits(max_judgements=2))

    def test_export_obeys_current_complete_closure_record_limits(self):
        root, _ = self._chain_bundle()
        for limits in (Limits(max_works=2), Limits(max_judgements=2)):
            with self.subTest(limits=limits):
                with Engine(self.path, [self.stage], limits=limits) as engine:
                    self.assertEqual(engine.revalidate(root).status, "accepted")
                    with self.assertRaises(BudgetExceeded):
                        engine.export([root])
        with Engine(self.path, [self.stage], limits=Limits(max_works=3, max_judgements=3)) as engine:
            exported = engine.export([root])
            self.assertEqual(len(exported["works"]), 3)
            self.assertEqual(len(exported["judgements"]), 3)
        self.assertEqual(len(self.generated), 3)

    @unittest.skipUnless(
        hasattr(signal, "SIGALRM") and hasattr(signal, "setitimer"),
        "Actual signal interruption requires SIGALRM and setitimer",
    )
    def test_interrupted_close_keeps_lease_and_retry_finishes_after_drain(self):
        # A bounded child protects the suite from the original indefinite wait.
        # No private engine fields or implementation hooks are used.
        script = textwrap.dedent("""
            import json, signal, sys, threading
            from reweave import Engine, Limits, Stage, Verdict
            from reweave.store import Store
            from reweave.types import EngineClosed, StoreBusy

            started = threading.Event()
            release = threading.Event()
            worker_done = threading.Event()
            close_done = threading.Event()
            results = []
            failures = []
            def produce(value, parents):
                started.set()
                if not release.wait(6):
                    raise RuntimeError('Fixture producer was not released')
                return value
            stage = Stage('node', 'p1', produce, 'v1',
                          lambda *args: Verdict(True, 'Deterministic fixture accepted'))
            engine = Engine(sys.argv[1], [stage])
            def run():
                try:
                    results.append(engine.run('node', 1))
                except BaseException as exc:
                    failures.append(repr(exc))
                finally:
                    worker_done.set()
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            assert started.wait(2), 'Producer never entered'
            def interrupt(*args):
                raise KeyboardInterrupt('Deterministic interruption during close drain')
            signal.signal(signal.SIGALRM, interrupt)
            signal.setitimer(signal.ITIMER_REAL, 0.05)
            interrupted = False
            try:
                engine.close()
            except KeyboardInterrupt:
                interrupted = True
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            assert interrupted, 'Expected actual close interruption'
            try:
                engine.run('node', 2)
            except EngineClosed:
                pass
            else:
                raise AssertionError('Interrupted closing admitted new work')
            def assert_lease_held():
                try:
                    other = Store(sys.argv[1], Limits())
                except StoreBusy:
                    return
                other.close()
                raise AssertionError('Lease released before admitted work drained')
            assert_lease_held()
            def retry_close():
                try:
                    engine.close()
                except BaseException as exc:
                    failures.append(repr(exc))
                finally:
                    close_done.set()
            closer = threading.Thread(target=retry_close, daemon=True)
            closer.start()
            assert not close_done.wait(0.05), 'Retry returned before the producer finished'
            assert_lease_held()
            release.set()
            assert worker_done.wait(2), 'Admitted producer failed to drain'
            assert close_done.wait(2), 'Retry close remained stuck after drain'
            assert not failures, failures
            assert len(results) == 1 and results[0].status == 'accepted', results
            with Store(sys.argv[1], Limits()) as reopened:
                assert reopened.get_work(results[0].work_key) is not None
                assert reopened.stats()['attempts_running'] == 0
            print(json.dumps({'interrupted': True, 'admission_refused': True,
                              'lease_held_until_drain': True, 'retry_completed': True,
                              'completed_work_durable': True}))
        """)
        env = os.environ.copy()
        package_root = str(Path(engine_module.__file__).resolve().parents[1])
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", script, str(self.path)], cwd=self.directory, env=env,
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {
            "interrupted": True, "admission_refused": True,
            "lease_held_until_drain": True, "retry_completed": True,
            "completed_work_durable": True,
        })


if __name__ == "__main__":
    unittest.main()
