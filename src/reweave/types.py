"""Public contracts. JSON data is snapshotted by the engine, not by dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

JSON = Any


class ReweaveError(Exception):
    """A rejected request or an unavailable local operation."""


class InvalidJSON(ReweaveError, ValueError):
    """Data is outside the strict JSON contract."""


class StoreBusy(ReweaveError):
    """Another cooperating engine owns this store."""


class StoreCorrupt(ReweaveError):
    """Stored evidence failed its structural or digest checks."""


class BudgetExceeded(ReweaveError):
    """An explicit application budget prevented admission."""


class EngineClosed(ReweaveError):
    """The engine has stopped admitting operations."""


@dataclass(frozen=True)
class Limits:
    max_input_bytes: int = 65_536
    max_output_bytes: int = 1_048_576
    max_verdict_bytes: int = 65_536
    max_works: int = 256
    max_judgements: int = 2_048
    max_producer_calls: int = 1_024
    max_parents: int = 32
    max_depth: int = 32
    max_items: int = 10_000
    max_workers: int = 4
    max_pending: int = 8

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_workers > 64 or self.max_pending > 1_024:
            raise ValueError("At most 64 workers and 1,024 pending items are supported")
        if self.max_depth > 64 or self.max_parents > 256:
            raise ValueError("At most 64 dependency levels and 256 parents are supported")


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reason: str
    evidence: JSON = None


@dataclass(frozen=True)
class Stage:
    name: str
    producer_revision: str
    producer: Callable[[JSON, list[JSON]], JSON]
    verifier_revision: str
    verifier: Callable[[JSON, JSON, list[JSON]], Verdict]
    model_config: dict[str, JSON] = field(default_factory=dict)
    policy: dict[str, JSON] = field(default_factory=dict)
    effect: str = "pure"
    description: str = ""
    input_example: JSON = None


@dataclass(frozen=True)
class Receipt:
    status: str
    work_key: str
    judgement_key: str | None = None
    reason: str = ""
    output: JSON = None
    generation_reused: bool = False
    verification_reused: bool = False

    def to_dict(self) -> dict[str, JSON]:
        result = {
            "status": self.status,
            "work_key": self.work_key,
            "judgement_key": self.judgement_key,
            "reason": self.reason,
            "generation_reused": self.generation_reused,
            "verification_reused": self.verification_reused,
        }
        if self.status == "accepted":
            # A returned dictionary must not lend mutable evidence to a caller.
            from .codec import snapshot

            result["output"] = snapshot(self.output, max_bytes=None)
        return result
