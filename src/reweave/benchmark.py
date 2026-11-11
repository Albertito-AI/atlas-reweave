"""Measured callback counts for a small, deterministic, explicitly defined workload."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from typing import Any

from .codec import canonical, digest, parse, snapshot
from .engine import Engine
from .examples import INTEGER_EXAMPLE, RATIONAL_EXAMPLE, _revision, _solve_elimination, make_stages
from .types import ReweaveError, Stage

_STRATEGIES = ("no_cache", "cache_invalidated_by_policy", "reweave")
_CHANGED_INPUT = {"matrix": [[2, 1], [1, -1]], "rhs": [7, 2]}
_INPUTS = {"integer": INTEGER_EXAMPLE, "rational": RATIONAL_EXAMPLE, "changed_input": _CHANGED_INPUT}
_PHASES = (
    ("initial_duplicates", False, False, ["integer", "integer", "rational", "rational", "integer"]),
    ("process_restart", False, False, ["integer", "rational"]),
    ("integer_only_policy", True, False, ["integer", "rational", "integer", "rational"]),
    ("producer_implementation_change", True, True, ["integer", "rational"]),
    ("changed_equation_input", True, True, ["changed_input", "changed_input", "integer"]),
)


class _Calls:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def wrap(self, stage: Stage) -> Stage:
        def producer(value: Any, parents: list[Any]) -> Any:
            with self.lock:
                self.events.append({"callback": "producer", "stage": stage.name, "input_digest": digest(value)})
            return stage.producer(value, parents)

        def verifier(value: Any, output: Any, parents: list[Any]) -> Any:
            with self.lock:
                self.events.append({"callback": "verifier", "stage": stage.name, "input_digest": digest(value)})
            return stage.verifier(value, output, parents)

        return replace(stage, producer=producer, verifier=verifier)

    def counts(self) -> dict[str, int]:
        counts = Counter(event["callback"] for event in self.events)
        return {"producer_calls": counts["producer"], "verifier_calls": counts["verifier"]}


def _stages(integer_only: bool, changed_producer: bool, calls: _Calls) -> list[Stage]:
    stages = make_stages(integer_only)
    if changed_producer:
        stages[0] = replace(stages[0], producer=_solve_elimination,
                            producer_revision=_revision("elimination.v1"))
    return [calls.wrap(stage) for stage in stages]


def _baseline_phase(strategy: str, state: Path, stage: Stage, requests: list[str]) -> list[dict]:
    """Two transparent comparison algorithms, not alternative production engines.

    The policy-invalidated cache persists complete decided request results across
    processes, including rejections. Its key adds verifier revision and policy to
    the ordinary work identity. A change to either therefore reruns generation.
    """
    cache_path = state / "baseline-cache.json"
    cache = parse(cache_path.read_text()) if cache_path.exists() else {}
    results = []
    for label in requests:
        value = snapshot(_INPUTS[label])
        work_key = digest({
            "schema": "reweave.work.v1", "scope": "benchmark", "stage": stage.name,
            "producer": {"revision": stage.producer_revision, "model_config": stage.model_config},
            "input": value, "parents": [],
        })
        cache_key = digest({"work_key": work_key,
                            "verifier": {"revision": stage.verifier_revision, "policy": stage.policy}})
        hit = strategy == "cache_invalidated_by_policy" and cache_key in cache
        if hit:
            result = snapshot(cache[cache_key])
        else:
            output = stage.producer(snapshot(value), [])
            verdict = stage.verifier(snapshot(value), snapshot(output), [])
            if type(verdict.accepted) is not bool:
                raise AssertionError("A benchmark verifier returned a non-Boolean decision")
            result = {"status": "accepted" if verdict.accepted else "rejected", "work_key": work_key}
            if verdict.accepted:
                result["output"] = snapshot(output)
            if strategy == "cache_invalidated_by_policy":
                cache[cache_key] = snapshot(result)
        result.update({"request": label, "generation_reused": hit, "verification_reused": hit})
        results.append(result)
    if strategy == "cache_invalidated_by_policy":
        cache_path.write_text(canonical(cache) + "\n", encoding="utf-8")
    return results


def _worker(strategy: str, phase_index: int, state: Path) -> dict:
    name, integer_only, changed_producer, requests = _PHASES[phase_index]
    calls = _Calls()
    stages = _stages(integer_only, changed_producer, calls)
    if strategy == "reweave":
        with Engine(state / "work.sqlite", stages, scope="benchmark") as engine:
            results = []
            for label in requests:
                receipt = engine.run("linear.solve", _INPUTS[label])
                result = receipt.to_dict()
                result.pop("reason")
                result.pop("judgement_key")
                result["request"] = label
                results.append(result)
            durable = engine.stats()
    else:
        results = _baseline_phase(strategy, state, stages[0], requests)
        durable = None
    return {"phase": name, "requests": results, **calls.counts(),
            "callback_events": calls.events, "engine_stats": durable}


def _safety_examples(directory: Path) -> dict:
    graph_path = directory / "parent-gate.sqlite"
    with Engine(graph_path, make_stages(), scope="gate-fixture") as engine:
        integer = engine.run("linear.solve", INTEGER_EXAMPLE)
        rational = engine.run("linear.solve", RATIONAL_EXAMPLE)
        child = engine.run("linear.aggregate", {"label": "cached aggregate"},
                           parents=[integer.work_key, rational.work_key])
        if child.status != "accepted":
            raise AssertionError("Initial parent-gate fixture did not accept")
    calls = _Calls()
    with Engine(graph_path, _stages(True, False, calls), scope="gate-fixture") as engine:
        blocked = engine.revalidate(child.work_key)
        if blocked.status != "blocked" or "output" in blocked.to_dict():
            raise AssertionError("A cached child bypassed its currently rejected parent")
        try:
            engine.export([child.work_key])
        except ReweaveError:
            export_refused = True
        else:
            raise AssertionError("Export accepted a blocked dependency closure")
        blocked_counts = calls.counts()
        if blocked_counts["producer_calls"] != 0:
            raise AssertionError("Parent rejection unexpectedly reran a producer")

    corrupt_path = directory / "corruption.sqlite"
    with Engine(corrupt_path, make_stages(), scope="corruption-fixture") as engine:
        original = engine.run("linear.solve", INTEGER_EXAMPLE)
        if original.status != "accepted":
            raise AssertionError("Initial corruption fixture did not accept")
    # Deliberate alteration of our closed, disposable fixture only. Do not update
    # either output_digest or record_digest; this tests detection, not repair.
    with closing(sqlite3.connect(corrupt_path)) as connection:
        with connection:
            connection.execute("UPDATE works SET output = ? WHERE work_key = ?",
                               (canonical({"solution": [999, 999]}), original.work_key))
    corrupt_calls = _Calls()
    with Engine(corrupt_path, _stages(False, False, corrupt_calls), scope="corruption-fixture") as engine:
        corrupted = engine.revalidate(original.work_key)
        if corrupted.status != "corrupt" or "output" in corrupted.to_dict():
            raise AssertionError("Corrupt stored output was exposed")
        if corrupt_calls.counts()["producer_calls"] != 0:
            raise AssertionError("Corrupt evidence was silently regenerated")
    return {
        "rejected_parent_cached_child": {
            "initial_status": child.status, "current_receipt": blocked.to_dict(),
            "export_refused": export_refused, **blocked_counts,
            "callback_events": calls.events,
        },
        "corruption": {"alteration": "Changed stored output without recomputing its digests",
                       "current_receipt": corrupted.to_dict(), **corrupt_calls.counts()},
    }


def run_benchmark() -> dict:
    """Run the same 16 requests for each strategy, with a fresh process per phase."""
    reports = {}
    with tempfile.TemporaryDirectory(prefix="reweave-benchmark-") as temporary:
        directory = Path(temporary)
        for strategy in _STRATEGIES:
            state = directory / strategy
            state.mkdir()
            phases = []
            for phase_index in range(len(_PHASES)):
                completed = subprocess.run(
                    [sys.executable, "-m", "reweave.benchmark", "--worker", strategy,
                     "--phase", str(phase_index), "--state", str(state)],
                    check=True, capture_output=True, text=True, timeout=20,
                )
                phases.append(parse(completed.stdout, max_bytes=200_000))
            reports[strategy] = {
                "producer_calls": sum(phase["producer_calls"] for phase in phases),
                "verifier_calls": sum(phase["verifier_calls"] for phase in phases),
                "processes_started": len(phases), "phases": phases,
            }
        reference = [[[item["status"], item.get("output")] for item in phase["requests"]]
                     for phase in reports["no_cache"]["phases"]]
        for strategy in _STRATEGIES[1:]:
            observed = [[[item["status"], item.get("output")] for item in phase["requests"]]
                        for phase in reports[strategy]["phases"]]
            if canonical(observed) != canonical(reference):
                raise AssertionError(f"The strategies disagree on accepted output: {strategy}")
        safety = _safety_examples(directory)
    return {
        "schema": "reweave.benchmark.v1", "fixture_only": True,
        "python": sys.version.split()[0],
        "measurement": "Actual producer and verifier callback invocations; no timing, token or monetary estimate",
        "workload": [{"phase": name, "integer_only": policy, "changed_producer": changed,
                      "requests": labels} for name, policy, changed, labels in _PHASES],
        "inputs": _INPUTS, "request_count_per_strategy": sum(len(phase[3]) for phase in _PHASES),
        "source": {"examples_sha256": sha256(Path(__file__).with_name("examples.py").read_bytes()).hexdigest(),
                   "benchmark_sha256": sha256(Path(__file__).read_bytes()).hexdigest()},
        "strategies": reports, "same_statuses_and_accepted_outputs": True,
        "separate_safety_examples": safety,
        "limitations": [
            "A small, selected, deterministic workload; not a general performance or efficiency benchmark.",
            "The policy-invalidated baseline persists decided results across processes and deliberately includes policy in its cache key.",
            "Each phase starts a new interpreter; crashes and concurrency are separate core tests.",
            "Producer change selects a different implemented solver and revision; input change separately alters the right-hand side.",
            "No model calls, tokens, expenditure, learning outcome or novelty result was measured.",
            "Corruption detection does not establish resistance to a writer who can rewrite all records and hashes.",
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the observed JSON report to this operator-selected path")
    parser.add_argument("--worker", choices=_STRATEGIES, help=argparse.SUPPRESS)
    parser.add_argument("--phase", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--state", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker is not None:
        if args.phase is None or not 0 <= args.phase < len(_PHASES) or args.state is None:
            parser.error("Internal worker requires a valid phase and state directory")
        report = _worker(args.worker, args.phase, args.state)
    else:
        report = run_benchmark()
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
