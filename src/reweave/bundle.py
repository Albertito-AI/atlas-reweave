"""Portable evidence integrity checks. These do not execute or trust a verifier."""

from __future__ import annotations

import re
from typing import Any

from .codec import canonical, digest
from .store import validate_judgement, validate_work
from .types import BudgetExceeded, InvalidJSON, Limits, StoreCorrupt

_KEY = re.compile(r"[0-9a-f]{64}\Z")


def verify_bundle(bundle: Any, limits: Limits | None = None) -> dict[str, Any]:
    """Check hashes, exact structure, accepted closure and a consistent registry.

    This can detect accidental corruption and incomplete evidence. A person who
    can rewrite both content and hashes can create another consistent bundle.
    No semantic truth, current external policy or authorship is certified.
    """
    limits = limits if limits is not None else Limits()
    try:
        canonical(bundle, None)
    except InvalidJSON as exc:
        raise StoreCorrupt("Bundle contains invalid JSON data") from exc
    expected = {"schema", "scope", "roots", "works", "judgements", "manifest_digest"}
    if type(bundle) is not dict or set(bundle) != expected:
        raise StoreCorrupt("Bundle fields do not match reweave.bundle.v1")
    if bundle["schema"] != "reweave.bundle.v1":
        raise StoreCorrupt("Unsupported bundle schema")
    scope = bundle["scope"]
    if type(scope) is not str or not scope or len(scope.encode("utf-8")) > 256:
        raise StoreCorrupt("Invalid bundle scope")
    for field in ("roots", "works", "judgements"):
        if type(bundle[field]) is not list:
            raise StoreCorrupt(f"Bundle {field} must be a list")
    if any(len(bundle[field]) > limits.max_works for field in ("roots", "works", "judgements")):
        raise BudgetExceeded("Bundle exceeds max_works")
    if len(bundle["judgements"]) > limits.max_judgements:
        raise BudgetExceeded("Bundle exceeds max_judgements")
    roots = bundle["roots"]
    if not roots or any(type(key) is not str or not _KEY.fullmatch(key) for key in roots):
        raise StoreCorrupt("Bundle roots must be nonempty work identifiers")
    if len(roots) != len(set(roots)):
        raise StoreCorrupt("Duplicate bundle root")
    manifest = {key: bundle[key] for key in expected - {"manifest_digest"}}
    if type(bundle["manifest_digest"]) is not str or bundle["manifest_digest"] != digest(manifest):
        raise StoreCorrupt("Bundle manifest digest does not match")
    works: dict[str, dict[str, Any]] = {}
    producer_specs: dict[str, str] = {}
    for item in bundle["works"]:
        record = validate_work(item, limits)
        key = record["work_key"]
        if key in works:
            raise StoreCorrupt("Duplicate work record in bundle")
        if record["envelope"]["scope"] != scope:
            raise StoreCorrupt("Bundle contains work from another scope")
        stage = record["envelope"]["stage"]
        specification = canonical(record["envelope"]["producer"], None)
        if stage in producer_specs and producer_specs[stage] != specification:
            raise StoreCorrupt("Bundle mixes producer contracts for one stage")
        producer_specs[stage] = specification
        works[key] = record
    judgements: dict[str, dict[str, Any]] = {}
    verifier_specs: dict[str, str] = {}
    for item in bundle["judgements"]:
        record = validate_judgement(item, limits)
        context = record["context"]
        key = context["work_key"]
        if key not in works or key in judgements:
            raise StoreCorrupt("Each bundled work needs exactly one matching judgement")
        if record["verdict"]["accepted"] is not True:
            raise StoreCorrupt("Export bundle contains rejected work")
        if context["output_digest"] != works[key]["output_digest"]:
            raise StoreCorrupt("Judgement does not bind the bundled output")
        stage = works[key]["envelope"]["stage"]
        specification = canonical(context["verifier"], None)
        if stage in verifier_specs and verifier_specs[stage] != specification:
            raise StoreCorrupt("Bundle mixes verifier contracts for one stage")
        verifier_specs[stage] = specification
        judgements[key] = record
    if set(works) != set(judgements) or not set(roots).issubset(works):
        raise StoreCorrupt("Bundle is missing work or its accepted judgement")
    for key, work in works.items():
        parent_keys = work["envelope"]["parents"]
        if any(parent not in works for parent in parent_keys):
            raise StoreCorrupt("Bundle is missing a dependency")
        expected_parents = [judgements[parent]["judgement_key"] for parent in parent_keys]
        if judgements[key]["context"]["parents"] != expected_parents:
            raise StoreCorrupt("Judgement does not bind the current bundled parent judgements")
    reachable: set[str] = set()
    visiting: set[str] = set()
    heights: dict[str, int] = {}

    def visit(key: str) -> int:
        if key in visiting:
            raise StoreCorrupt("Bundle contains a dependency cycle")
        reachable.add(key)
        if key in heights:
            return heights[key]
        if len(visiting) >= limits.max_depth:
            raise BudgetExceeded("Bundle dependency depth exceeds max_depth")
        visiting.add(key)
        height = 1 + max((visit(parent) for parent in works[key]["envelope"]["parents"]), default=0)
        visiting.remove(key)
        if height > limits.max_depth:
            raise BudgetExceeded("Bundle dependency depth exceeds max_depth")
        heights[key] = height
        return height

    for root in roots:
        visit(root)
    if reachable != set(works):
        raise StoreCorrupt("Bundle contains records outside the requested root closure")
    return {
        "valid": True, "schema": bundle["schema"], "scope": scope,
        "roots": len(roots), "works": len(works), "judgements": len(judgements),
        "manifest_digest": bundle["manifest_digest"],
        "semantic_verification": False, "authenticity_verification": False,
    }
