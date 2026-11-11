"""Bounded reuse of immutable work with a separate current acceptance graph."""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import re
import threading
from typing import Any, Iterable, Iterator

from .codec import canonical, digest, digest_text, snapshot
from .store import Store
from .types import (
    BudgetExceeded, EngineClosed, Limits, Receipt, ReweaveError, Stage,
    StoreCorrupt, Verdict,
)

_KEY = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")


@dataclass(frozen=True)
class _Registered:
    stage: Stage
    producer: dict[str, Any]
    verifier: dict[str, Any]
    discovery: dict[str, Any]


def _key(value: Any) -> str:
    if type(value) is not str or not _KEY.fullmatch(value):
        raise ReweaveError("Work identifiers must be lowercase SHA-256 strings")
    return value


def _text(value: Any, label: str, limit: int) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > limit:
        raise ReweaveError(f"{label} must be a nonempty string of at most {limit} UTF-8 bytes")
    return value


class Engine:
    """A fixed registry and one exclusive, local SQLite store.

    Callbacks are trusted pure computations over their supplied JSON snapshots.
    Register new versions when code, prompts, models or relevant dependencies
    change. Callbacks must not call back into this same engine.
    """

    def __init__(
        self, store: str | Path, stages: Iterable[Stage], *, scope: str = "default",
        limits: Limits | None = None,
    ) -> None:
        self.limits = limits if limits is not None else Limits()
        if not isinstance(self.limits, Limits):
            raise ReweaveError("limits must be a Limits instance")
        self.scope = _text(scope, "Scope", 256)
        self._registry: dict[str, _Registered] = {}
        for stage in stages:
            if len(self._registry) >= 256:
                raise BudgetExceeded("At most 256 stages may be registered")
            if not isinstance(stage, Stage):
                raise ReweaveError("The registry must contain Stage instances")
            if type(stage.name) is not str or not _NAME.fullmatch(stage.name):
                raise ReweaveError("Stage names must use lowercase letters, digits, dots, hyphens or underscores")
            if stage.name in self._registry:
                raise ReweaveError(f"Stage is registered twice: {stage.name}")
            if stage.effect != "pure" or type(stage.effect) is not str:
                raise ReweaveError("Only explicitly pure stages are supported")
            if not callable(stage.producer) or not callable(stage.verifier):
                raise ReweaveError("A stage requires callable producer and verifier functions")
            if type(stage.model_config) is not dict or type(stage.policy) is not dict:
                raise ReweaveError("Stage model_config and policy must be JSON objects")
            if type(stage.description) is not str or len(stage.description.encode("utf-8")) > 4096:
                raise ReweaveError("Stage description must be at most 4,096 UTF-8 bytes")
            producer = snapshot({
                "revision": _text(stage.producer_revision, "Producer revision", 512),
                "model_config": stage.model_config,
            }, self.limits.max_input_bytes)
            verifier = snapshot({
                "revision": _text(stage.verifier_revision, "Verifier revision", 512),
                "policy": stage.policy,
            }, self.limits.max_input_bytes)
            discovery = snapshot({
                "name": stage.name, "description": stage.description,
                "input_example": stage.input_example,
                "producer_revision": stage.producer_revision,
                "verifier_revision": stage.verifier_revision, "effect": stage.effect,
            }, self.limits.max_input_bytes)
            self._registry[stage.name] = _Registered(stage, producer, verifier, discovery)
        if not self._registry:
            raise ReweaveError("Register at least one stage")
        self._condition = threading.Condition()
        self._active = 0
        self._closing = False
        self._closed = False
        self._close_owner = False
        self._semaphore = threading.BoundedSemaphore(self.limits.max_workers)
        # Stripes bound bookkeeping even for a long stream of rejected requests.
        self._work_locks = [threading.Lock() for _ in range(64)]
        self._validation = threading.RLock()
        self._metric_lock = threading.Lock()
        self._callback = threading.local()
        self._metrics = {
            "producer_calls": 0, "verifier_calls": 0,
            "work_hits": 0, "judgement_hits": 0,
        }
        self._store = Store(store, self.limits)

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        if getattr(self._callback, "running", False):
            raise ReweaveError("A callback cannot reenter the same engine")
        with self._condition:
            if self._closing or self._closed:
                raise EngineClosed("The engine is closing or closed")
            self._active += 1
        try:
            with self._semaphore:
                yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def _count(self, name: str) -> None:
        with self._metric_lock:
            self._metrics[name] += 1

    def _invoke(self, callback: Any, *args: Any) -> Any:
        self._callback.running = True
        try:
            return callback(*args)
        finally:
            self._callback.running = False

    def _parents(self, parents: Iterable[str]) -> list[str]:
        if isinstance(parents, (str, bytes, dict)):
            raise ReweaveError("Parents must be an ordered iterable of work identifiers")
        result: list[str] = []
        for parent in parents:
            if len(result) >= self.limits.max_parents:
                raise BudgetExceeded("Parent count exceeds max_parents")
            result.append(_key(parent))
        if len(set(result)) != len(result):
            raise ReweaveError("Parent identifiers must be unique")
        return result

    @staticmethod
    def _failure(work_key: str, error: Exception, *, reused: bool = False) -> Receipt:
        status = "budget" if isinstance(error, BudgetExceeded) else (
            "corrupt" if isinstance(error, StoreCorrupt) else "error"
        )
        reason = f"{type(error).__name__}: {str(error)[:512]}"
        return Receipt(status, work_key, reason=reason, generation_reused=reused)

    def _parent_receipts(
        self, parents: list[str], trail: tuple[str, ...], memo: dict[str, tuple[Receipt, int]],
    ) -> list[Receipt]:
        return [self._revalidate(parent, trail, memo) for parent in parents]

    @staticmethod
    def _blocked(work_key: str, parents: list[Receipt], *, reused: bool) -> Receipt | None:
        failed = [p for p in parents if p.status != "accepted"]
        if failed:
            first = failed[0]
            return Receipt(
                "blocked", work_key,
                reason=f"Parent {first.work_key} is {first.status}: {first.reason[:256]}",
                generation_reused=reused,
            )
        return None

    def run(self, stage_name: str, input: Any, parents: Iterable[str] = ()) -> Receipt:
        """Run or reuse work, exposing output only after its current graph accepts."""
        with self._operation():
            registered = self._registry.get(stage_name) if type(stage_name) is str else None
            if registered is None:
                raise ReweaveError("Unknown registered stage")
            parent_keys = self._parents(parents)
            envelope = {
                "schema": "reweave.work.v1", "scope": self.scope, "stage": stage_name,
                "producer": registered.producer,
                "input": snapshot(input, self.limits.max_input_bytes), "parents": parent_keys,
            }
            work_key = digest_text(canonical(envelope, self.limits.max_input_bytes))
            lock = self._work_locks[int(work_key[:8], 16) % len(self._work_locks)]
            with lock:
                try:
                    existing = self._store.get_work(work_key)
                    if existing is not None:
                        self._count("work_hits")
                        with self._validation:
                            return self._revalidate(work_key, (), {})
                    with self._validation:
                        checked = self._parent_receipts(parent_keys, (work_key,), {})
                        blocked = self._blocked(work_key, checked, reused=False)
                        if blocked is not None:
                            return blocked
                    attempt = self._store.reserve_attempt(work_key)
                except Exception as exc:
                    return self._failure(work_key, exc)
                try:
                    self._count("producer_calls")
                    output = self._invoke(
                        registered.stage.producer,
                        snapshot(envelope["input"], self.limits.max_input_bytes),
                        [snapshot(parent.output, self.limits.max_output_bytes) for parent in checked],
                    )
                    output = snapshot(output, self.limits.max_output_bytes)
                    record = {
                        "work_key": work_key, "envelope": envelope,
                        "output": output, "output_digest": digest(output),
                    }
                    # Commit the expensive result before the checker can fail or exit.
                    self._store.finish_attempt(attempt, record)
                except Exception as exc:
                    try:
                        self._store.finish_attempt(attempt, None, error=f"{type(exc).__name__}: {str(exc)[:512]}")
                    except Exception as persistence_error:
                        return self._failure(work_key, persistence_error)
                    return self._failure(work_key, exc)
                with self._validation:
                    return replace(self._revalidate(work_key, (), {}), generation_reused=False)

    def _revalidate(
        self, work_key: str, trail: tuple[str, ...], memo: dict[str, tuple[Receipt, int]],
    ) -> Receipt:
        if work_key in trail:
            return Receipt("corrupt", work_key, reason="Dependency cycle detected")
        if len(trail) >= self.limits.max_depth:
            return Receipt("budget", work_key, reason="Dependency depth exceeds max_depth")
        if work_key in memo:
            cached, height = memo[work_key]
            if cached.status == "accepted" and len(trail) + height > self.limits.max_depth:
                return Receipt("budget", work_key, reason="Cached dependency subtree exceeds max_depth on this path", generation_reused=True)
            return cached
        reused = False
        height = 1
        try:
            work = self._store.get_work(work_key)
            if work is None:
                result = Receipt("stale", work_key, reason="No durable work record exists")
            else:
                reused = True
                envelope = work["envelope"]
                registered = self._registry.get(envelope["stage"])
                if envelope["scope"] != self.scope:
                    result = Receipt("stale", work_key, reason="Stored work belongs to a different scope", generation_reused=True)
                elif registered is None:
                    result = Receipt("stale", work_key, reason="Stage is absent from the current registry", generation_reused=True)
                elif canonical(envelope["producer"], None) != canonical(registered.producer, None):
                    result = Receipt("stale", work_key, reason="Producer revision or model configuration changed; run current work explicitly", generation_reused=True)
                else:
                    checked = self._parent_receipts(envelope["parents"], trail + (work_key,), memo)
                    blocked = self._blocked(work_key, checked, reused=True)
                    if blocked is None:
                        height = 1 + max((memo[parent][1] for parent in envelope["parents"]), default=0)
                    result = blocked if blocked is not None else self._judge(registered, work, checked)
        except Exception as exc:
            result = self._failure(work_key, exc, reused=reused)
        memo[work_key] = (result, height)
        return result

    def _judge(self, registered: _Registered, work: dict[str, Any], parents: list[Receipt]) -> Receipt:
        context = {
            "schema": "reweave.judgement.v1", "work_key": work["work_key"],
            "output_digest": work["output_digest"], "verifier": registered.verifier,
            "parents": [parent.judgement_key for parent in parents],
        }
        judgement_key = digest_text(canonical(context, self.limits.max_input_bytes))
        saved = self._store.get_judgement(judgement_key)
        verification_reused = saved is not None
        if saved is not None:
            self._count("judgement_hits")
            verdict = saved["verdict"]
        else:
            self._count("verifier_calls")
            checked = self._invoke(
                registered.stage.verifier,
                snapshot(work["envelope"]["input"], self.limits.max_input_bytes),
                snapshot(work["output"], self.limits.max_output_bytes),
                [snapshot(parent.output, self.limits.max_output_bytes) for parent in parents],
            )
            if type(checked) is not Verdict or type(checked.accepted) is not bool:
                raise ReweaveError("Verifier must return Verdict with an exact Boolean accepted field")
            if type(checked.reason) is not str or not checked.reason:
                raise ReweaveError("Verifier must supply a nonempty reason string")
            verdict = snapshot({
                "accepted": checked.accepted, "reason": checked.reason, "evidence": checked.evidence,
            }, self.limits.max_verdict_bytes)
            self._store.put_judgement({"judgement_key": judgement_key, "context": context, "verdict": verdict})
        accepted = verdict["accepted"]
        return Receipt(
            "accepted" if accepted else "rejected", work["work_key"], judgement_key,
            verdict["reason"],
            snapshot(work["output"], self.limits.max_output_bytes) if accepted else None,
            generation_reused=True, verification_reused=verification_reused,
        )

    def revalidate(self, work_key: str) -> Receipt:
        """Apply the current registry to the complete saved dependency closure.

        An unchanged pure checker may reuse its own immutable judgement. Change
        its declared revision/policy when its meaning or evidence changes.
        """
        with self._operation(), self._validation:
            return self._revalidate(_key(work_key), (), {})

    def list_stages(self) -> list[dict[str, Any]]:
        with self._operation():
            return [snapshot(self._registry[name].discovery, self.limits.max_input_bytes) for name in sorted(self._registry)]

    def inspect(self, work_key: str) -> dict[str, Any]:
        """Read historical metadata without returning output or writing a judgement."""
        with self._operation():
            key = _key(work_key)
            work = self._store.get_work(key)
            if work is None:
                return {"work_key": key, "exists": False, "current_acceptance": "not_evaluated"}
            envelope = work["envelope"]
            return {
                "work_key": key, "exists": True, "current_acceptance": "not_evaluated",
                "scope": envelope["scope"], "stage": envelope["stage"],
                "producer_revision": envelope["producer"]["revision"],
                "model_config_digest": digest(envelope["producer"]["model_config"]),
                "input_digest": digest(envelope["input"]), "output_digest": work["output_digest"],
                "parents": list(envelope["parents"]),
            }

    def stats(self) -> dict[str, int]:
        with self._operation():
            result = self._store.stats()
            with self._metric_lock:
                result.update(self._metrics)
            return result

    def stream(
        self, stage_name: str, inputs: Iterable[Any], parents: Iterable[str] = (),
        max_pending: int | None = None,
    ) -> Iterator[Receipt]:
        """Yield ordered results from a bounded pending window.

        At the item cap, one unsubmitted lookahead distinguishes exhaustion from
        overflow. Already admitted work can finish if iteration stops. Neither
        cancellation nor close forcibly kills a trusted Python callback.
        """
        if getattr(self._callback, "running", False):
            raise ReweaveError("A callback cannot reenter the same engine")
        window = self.limits.max_pending if max_pending is None else max_pending
        if type(window) is not int or not 1 <= window <= self.limits.max_pending:
            raise ReweaveError("max_pending must be a positive integer within the engine limit")
        parent_keys = self._parents(parents)
        iterator = iter(inputs)
        pending: deque[Any] = deque()
        submitted = 0
        exhausted = False
        overflow = False
        with ThreadPoolExecutor(max_workers=self.limits.max_workers, thread_name_prefix="reweave") as pool:
            def fill() -> None:
                nonlocal submitted, exhausted, overflow
                while not exhausted and not overflow and len(pending) < window:
                    with self._condition:
                        if self._closing or self._closed:
                            raise EngineClosed("The engine is closing or closed")
                    try:
                        item = next(iterator)
                    except StopIteration:
                        exhausted = True
                        break
                    if submitted >= self.limits.max_items:
                        overflow = True
                        break
                    # Snapshot on submission, including iterators that reuse a dict.
                    item = snapshot(item, self.limits.max_input_bytes)
                    pending.append(pool.submit(self.run, stage_name, item, parent_keys))
                    submitted += 1
            try:
                fill()
                while pending:
                    future = pending.popleft()
                    yield future.result()
                    fill()
                if overflow:
                    raise BudgetExceeded("Stream item count exceeds max_items; admitted items may have completed")
            finally:
                for future in pending:
                    future.cancel()

    def export(self, roots: Iterable[str]) -> dict[str, Any]:
        """Export only a fully accepted current closure as a portable audit bundle."""
        with self._operation(), self._validation:
            if isinstance(roots, (str, bytes, dict)):
                raise ReweaveError("Bundle roots must be an iterable of work identifiers")
            root_keys: list[str] = []
            for root in roots:
                if len(root_keys) >= self.limits.max_works:
                    raise BudgetExceeded("Bundle root count exceeds max_works")
                root_keys.append(_key(root))
            if not root_keys or len(root_keys) != len(set(root_keys)):
                raise ReweaveError("Bundle roots must be nonempty and unique")
            memo: dict[str, tuple[Receipt, int]] = {}
            for root in root_keys:
                result = self._revalidate(root, (), memo)
                if result.status != "accepted":
                    raise ReweaveError(f"Cannot export root {root}: {result.status}")
            if len(memo) > self.limits.max_works or len(memo) > self.limits.max_judgements:
                raise BudgetExceeded("Exported dependency closure exceeds current record limits")
            works = []
            judgements = []
            for key in sorted(memo):
                receipt = memo[key][0]
                if receipt.status != "accepted" or receipt.judgement_key is None:
                    raise ReweaveError("Bundle dependency closure is not fully accepted")
                work = self._store.get_work(key)
                judgement = self._store.get_judgement(receipt.judgement_key)
                if work is None or judgement is None:
                    raise StoreCorrupt("Evidence disappeared during export")
                works.append(work)
                judgements.append(judgement)
            bundle = {
                "schema": "reweave.bundle.v1", "scope": self.scope, "roots": root_keys,
                "works": works, "judgements": judgements,
            }
            return {**bundle, "manifest_digest": digest(bundle)}

    def close(self) -> None:
        """Stop admission, drain admitted calls, then release store ownership."""
        if getattr(self._callback, "running", False):
            raise ReweaveError("A callback cannot close its own engine")
        with self._condition:
            while self._close_owner:
                self._condition.wait()
            if self._closed:
                return
            self._closing = True
            # Interrupted draining retains the lease and stops admission, while
            # relinquishing closer ownership so another close can finish safely.
            try:
                self._close_owner = True
                while self._active:
                    self._condition.wait()
                self._store.close()
                self._closed = True
            finally:
                self._close_owner = False
                self._condition.notify_all()
