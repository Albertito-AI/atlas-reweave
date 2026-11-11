"""Meaningful fixture checks, including failures independent of the producer."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from reweave import Engine
from reweave.examples import (
    EXCERPT_EXAMPLE, INTEGER_EXAMPLE, RATIONAL_EXAMPLE,
    _revision, _solve_elimination, make_stages, run_demo,
)


class ExampleChecks(unittest.TestCase):
    def test_independent_substitution_rejects_incorrect_candidate(self):
        stage = make_stages()[0]
        output = stage.producer(INTEGER_EXAMPLE, [])
        self.assertEqual(output["solution"], [{"numerator": 2, "denominator": 1},
                                               {"numerator": 1, "denominator": 1}])
        self.assertTrue(stage.verifier(INTEGER_EXAMPLE, output, []).accepted)
        output["solution"][0]["numerator"] = 3
        self.assertFalse(stage.verifier(INTEGER_EXAMPLE, output, []).accepted)

    def test_rational_coefficients_and_zero_first_pivot(self):
        cases = [
            {"matrix": [[0, 2], [3, 1]], "rhs": [4, 5]},
            {"matrix": [[{"numerator": 1, "denominator": 2}, 1], [1, -1]], "rhs": [1, 0]},
        ]
        stage = make_stages()[0]
        for value in cases:
            with self.subTest(value=value):
                first = stage.producer(value, [])
                second = _solve_elimination(value, [])
                self.assertEqual(first, second)
                self.assertTrue(stage.verifier(value, first, []).accepted)

    def test_boolean_and_singular_inputs_do_not_pass(self):
        stage = make_stages()[0]
        invalid = deepcopy(INTEGER_EXAMPLE)
        invalid["matrix"][0][0] = True
        with self.assertRaises(ValueError):
            stage.producer(invalid, [])
        singular = {"matrix": [[1, 1], [2, 2]], "rhs": [2, 4]}
        with self.assertRaises(ValueError):
            stage.producer(singular, [])
        self.assertFalse(stage.verifier(singular, {"solution": [1, 1]}, []).accepted)

    def test_policy_change_rechecks_same_output(self):
        loose, strict = make_stages()[0], make_stages(True)[0]
        self.assertEqual(loose.producer_revision, strict.producer_revision)
        self.assertEqual(loose.verifier_revision, strict.verifier_revision)
        self.assertNotEqual(loose.policy, strict.policy)
        output = loose.producer(RATIONAL_EXAMPLE, [])
        self.assertTrue(loose.verifier(RATIONAL_EXAMPLE, output, []).accepted)
        self.assertFalse(strict.verifier(RATIONAL_EXAMPLE, output, []).accepted)

    def test_aggregate_uses_parent_outputs_and_checks_count_and_sum(self):
        solver, aggregate, _ = make_stages()
        parents = [solver.producer(INTEGER_EXAMPLE, []), solver.producer(RATIONAL_EXAMPLE, [])]
        value = {"label": "sum"}
        output = aggregate.producer(value, parents)
        self.assertEqual(output["sum"], [{"numerator": 7, "denominator": 3},
                                        {"numerator": 4, "denominator": 3}])
        self.assertTrue(aggregate.verifier(value, output, parents).accepted)
        output["parent_count"] = True
        self.assertFalse(aggregate.verifier(value, output, parents).accepted)
        output["parent_count"] = 2
        output["sum"][0]["numerator"] = 8
        self.assertFalse(aggregate.verifier(value, output, parents).accepted)

    def test_excerpt_checks_supplied_text_digest_offsets_and_literal_quote(self):
        stage = make_stages()[2]
        output = stage.producer(EXCERPT_EXAMPLE, [])
        self.assertTrue(stage.verifier(EXCERPT_EXAMPLE, output, []).accepted)
        for key, replacement in (("quote", "An unsupported quotation"),
                                 ("source_sha256", "0" * 64), ("start", True)):
            changed = deepcopy(output)
            changed[key] = replacement
            self.assertFalse(stage.verifier(EXCERPT_EXAMPLE, changed, []).accepted)
        source_changed = deepcopy(EXCERPT_EXAMPLE)
        source_changed["source_text"] += " Additional supplied text."
        self.assertFalse(stage.verifier(source_changed, output, []).accepted)

    def test_cached_child_is_blocked_after_parent_policy_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "work.sqlite"
            with Engine(path, make_stages()) as engine:
                first = engine.run("linear.solve", INTEGER_EXAMPLE)
                second = engine.run("linear.solve", RATIONAL_EXAMPLE)
                child = engine.run("linear.aggregate", {"label": "sum"},
                                   parents=[first.work_key, second.work_key])
                self.assertEqual(child.status, "accepted")
            with Engine(path, make_stages(True)) as engine:
                result = engine.revalidate(child.work_key)
                self.assertEqual(result.status, "blocked")
                self.assertNotIn("output", result.to_dict())
                self.assertEqual(engine.stats()["producer_calls"], 0)

    def test_changed_producer_and_input_make_new_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "work.sqlite"
            with Engine(path, make_stages()) as engine:
                old = engine.run("linear.solve", INTEGER_EXAMPLE)
            stages = make_stages()
            stages[0] = replace(stages[0], producer=_solve_elimination,
                                producer_revision=_revision("elimination.v1"))
            with Engine(path, stages) as engine:
                self.assertEqual(engine.revalidate(old.work_key).status, "stale")
                new = engine.run("linear.solve", INTEGER_EXAMPLE)
                self.assertNotEqual(new.work_key, old.work_key)
                self.assertEqual(new.output, old.output)
                value = deepcopy(INTEGER_EXAMPLE)
                value["rhs"] = [7, 2]
                changed = engine.run("linear.solve", value)
                self.assertNotEqual(changed.work_key, new.work_key)

    def test_demo_reports_actual_policy_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            report = run_demo(Path(temporary) / "demo.sqlite", integer_only=True)
            self.assertEqual(report["receipts"]["integer"]["status"], "accepted")
            self.assertEqual(report["receipts"]["rational"]["status"], "rejected")
            self.assertEqual(report["receipts"]["aggregate"]["status"], "blocked")
            self.assertEqual(report["receipts"]["excerpt"]["status"], "accepted")

    def test_actual_benchmark_agrees_on_outputs_and_separates_policy_cost(self):
        from reweave.benchmark import run_benchmark
        report = run_benchmark()
        self.assertEqual(report["request_count_per_strategy"], 16)
        self.assertTrue(report["same_statuses_and_accepted_outputs"])
        measured = report["strategies"]
        self.assertEqual((measured["no_cache"]["producer_calls"], measured["no_cache"]["verifier_calls"]), (16, 16))
        self.assertEqual((measured["cache_invalidated_by_policy"]["producer_calls"],
                          measured["cache_invalidated_by_policy"]["verifier_calls"]), (7, 7))
        self.assertEqual((measured["reweave"]["producer_calls"], measured["reweave"]["verifier_calls"]), (5, 7))
        phase = measured["reweave"]["phases"][2]
        self.assertEqual((phase["producer_calls"], phase["verifier_calls"]), (0, 2))


if __name__ == "__main__":
    unittest.main()
