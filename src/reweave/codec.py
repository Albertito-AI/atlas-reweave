"""Versioned Python JSON encoding, not an implementation of RFC 8785."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .types import BudgetExceeded, InvalidJSON


def canonical(value: Any, max_bytes: int | None = 1_048_576, max_depth: int = 64) -> str:
    """Encode exact builtin JSON values with finite, type-distinct numbers.

    The byte limit bounds the encoded record. A callback has already materialised
    its value, and a large encoder token can allocate memory before this check.
    This function is therefore not a hard memory sandbox.
    """
    ancestors: set[int] = set()
    nodes = 0

    def check(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 100_000 or depth > max_depth:
            raise InvalidJSON("JSON nesting or node limit exceeded")
        kind = type(item)
        if item is None or kind is bool:
            return
        if kind is int:
            if item.bit_length() > 4_096:
                raise InvalidJSON("JSON integer exceeds 4,096 bits")
            return
        if kind is float:
            if not math.isfinite(item):
                raise InvalidJSON("JSON numbers must be finite")
            return
        if kind is str:
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise InvalidJSON("JSON strings must contain valid Unicode") from exc
            return
        if kind not in (list, dict):
            raise InvalidJSON("Only exact builtin JSON types are accepted")
        identity = id(item)
        if identity in ancestors:
            raise InvalidJSON("JSON cannot contain a cycle")
        ancestors.add(identity)
        try:
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        raise InvalidJSON("JSON object keys must be strings")
                    check(key, depth + 1)
                    check(child, depth + 1)
            else:
                for child in item:
                    check(child, depth + 1)
        finally:
            ancestors.remove(identity)

    check(value, 0)
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    pieces: list[str] = []
    size = 0
    for piece in encoder.iterencode(value):
        size += len(piece.encode("utf-8"))
        if max_bytes is not None and size > max_bytes:
            raise BudgetExceeded(f"Encoded JSON exceeds {max_bytes} bytes")
        pieces.append(piece)
    return "".join(pieces)


def parse(text: str, max_bytes: int | None = 1_048_576) -> Any:
    if type(text) is not str:
        raise InvalidJSON("JSON text must be a string")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise InvalidJSON("JSON text must contain valid Unicode") from exc
    if max_bytes is not None and size > max_bytes:
        raise BudgetExceeded(f"JSON text exceeds {max_bytes} bytes")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise InvalidJSON("Duplicate JSON object key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise InvalidJSON("JSON numbers must be finite")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
        canonical(value, max_bytes=max_bytes)
        return value
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, InvalidJSON):
            raise
        raise InvalidJSON("Invalid JSON text") from exc


def snapshot(value: Any, max_bytes: int | None = 1_048_576) -> Any:
    return parse(canonical(value, max_bytes=max_bytes), max_bytes=max_bytes)


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(value: Any) -> str:
    return digest_text(canonical(value, max_bytes=None))


def fingerprint_files(files: Mapping[str, str | Path]) -> str:
    """Fingerprint explicit stable labels and file bytes, without storing paths.

    Include source, prompts and a dependency lock file as appropriate. This is an
    explicit manifest helper; it cannot discover hidden or external dependencies.
    """
    entries = []
    for label, filename in sorted(files.items()):
        if type(label) is not str or not label:
            raise ValueError("Fingerprint labels must be nonempty strings")
        hasher = hashlib.sha256()
        with Path(filename).open("rb") as source:
            for chunk in iter(lambda: source.read(65_536), b""):
                hasher.update(chunk)
        entries.append({"label": label, "sha256": hasher.hexdigest()})
    return digest({"schema": "reweave.files.v1", "files": entries})
