"""Deterministic software fixtures, with explicit mathematical and source checks.

These callbacks do not call a model, learn weights or establish scientific facts.
The solver's checker substitutes a candidate into the supplied equations; it does
not call either solver implementation. The excerpt checker binds text to supplied
source bytes, without authenticating the source or endorsing its claims.
"""

from __future__ import annotations

from fractions import Fraction
from hashlib import sha256
from math import gcd
from pathlib import Path
from typing import Any

from .codec import digest
from .types import Stage, Verdict

INTEGER_EXAMPLE = {"matrix": [[2, 1], [1, -1]], "rhs": [5, 1]}
RATIONAL_EXAMPLE = {"matrix": [[2, 1], [1, -1]], "rhs": [1, 0]}
EXCERPT_SOURCE = (
    "This is an invented documentation source. "
    "A stored computation can be checked under a later policy. "
    "Acceptance is separate from generation."
)
EXCERPT_EXAMPLE = {
    "source_id": "fixture:reweave-notes",
    "source_text": EXCERPT_SOURCE,
    "start": EXCERPT_SOURCE.index("A stored"),
    "end": EXCERPT_SOURCE.index(" Acceptance"),
}


def _fields(value: Any, names: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != names:
        raise ValueError(f"{label} must contain exactly {sorted(names)}")
    return value


def _fraction(value: Any, *, input_value: bool = False) -> Fraction:
    if type(value) is int:
        numerator, denominator = value, 1
    else:
        _fields(value, {"numerator", "denominator"}, "A rational")
        numerator, denominator = value["numerator"], value["denominator"]
        if type(numerator) is not int or type(denominator) is not int:
            raise ValueError("Rational components must be exact integers, not Booleans or floats")
        if denominator <= 0 or gcd(numerator, denominator) != 1:
            raise ValueError("A rational needs a positive denominator and reduced components")
    if input_value and (abs(numerator) > 1_000_000 or denominator > 1_000_000):
        raise ValueError("Input rational components must be at most 1,000,000 in magnitude")
    if numerator.bit_length() > 512 or denominator.bit_length() > 512:
        raise ValueError("Rational components exceed the fixture's arithmetic bound")
    return Fraction(numerator, denominator)


def _encoded(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _system(value: Any) -> tuple[list[list[Fraction]], list[Fraction]]:
    _fields(value, {"matrix", "rhs"}, "A linear-system input")
    matrix, rhs = value["matrix"], value["rhs"]
    if type(matrix) is not list or len(matrix) != 2:
        raise ValueError("The matrix must have two rows")
    if any(type(row) is not list or len(row) != 2 for row in matrix):
        raise ValueError("Each matrix row must have two entries")
    if type(rhs) is not list or len(rhs) != 2:
        raise ValueError("The right-hand side must have two entries")
    return (
        [[_fraction(item, input_value=True) for item in row] for row in matrix],
        [_fraction(item, input_value=True) for item in rhs],
    )


def _solution(value: Any) -> list[Fraction]:
    _fields(value, {"solution"}, "A solver output")
    values = value["solution"]
    if type(values) is not list or len(values) != 2:
        raise ValueError("A solution must contain exactly two rational components")
    return [_fraction(item) for item in values]


def _solve_cramer(value: Any, parents: list[Any]) -> dict:
    if parents:
        raise ValueError("The solver does not accept parent outputs")
    matrix, rhs = _system(value)
    (a, b), (c, d) = matrix
    u, v = rhs
    determinant = a * d - b * c
    if not determinant:
        raise ValueError("This fixture requires a nonsingular 2-by-2 system")
    return {"solution": [
        _encoded((u * d - b * v) / determinant),
        _encoded((a * v - u * c) / determinant),
    ]}


def _solve_elimination(value: Any, parents: list[Any]) -> dict:
    """A second actual implementation used only for the producer-change phase."""
    if parents:
        raise ValueError("The solver does not accept parent outputs")
    matrix, rhs = _system(value)
    rows = [matrix[index] + [rhs[index]] for index in range(2)]
    if not rows[0][0]:
        rows.reverse()
    if not rows[0][0]:
        raise ValueError("This fixture requires a nonsingular 2-by-2 system")
    pivot = rows[0][0]
    rows[0] = [item / pivot for item in rows[0]]
    multiplier = rows[1][0]
    rows[1] = [right - multiplier * left for left, right in zip(rows[0], rows[1])]
    if not rows[1][1]:
        raise ValueError("This fixture requires a nonsingular 2-by-2 system")
    second = rows[1][2] / rows[1][1]
    first = rows[0][2] - rows[0][1] * second
    return {"solution": [_encoded(first), _encoded(second)]}


def _check_system(value: Any, output: Any, parents: list[Any], *, integer_only: bool) -> Verdict:
    try:
        if parents:
            raise ValueError("The solver does not accept parent outputs")
        matrix, rhs = _system(value)
        if matrix[0][0] * matrix[1][1] == matrix[0][1] * matrix[1][0]:
            return Verdict(False, "This fixture requires a nonsingular 2-by-2 system")
        candidate = _solution(output)
        # Independent checking mechanism: substitute the candidate, without an
        # inverse, Cramer's rule or a call to either producer.
        residuals = [sum((a * x for a, x in zip(row, candidate)), Fraction()) - target
                     for row, target in zip(matrix, rhs)]
        if any(residuals):
            return Verdict(False, "Candidate fails exact substitution", {
                "residuals": [_encoded(item) for item in residuals],
            })
        if integer_only and any(item.denominator != 1 for item in candidate):
            return Verdict(False, "Exact solution does not satisfy the integer-only acceptance policy")
        return Verdict(True, "Candidate satisfies both equations by exact substitution", {
            "residuals": [_encoded(item) for item in residuals],
            "integer_only": integer_only,
        })
    except (ValueError, TypeError, KeyError) as exc:
        return Verdict(False, str(exc))


def _label(value: Any) -> str:
    _fields(value, {"label"}, "An aggregate input")
    label = value["label"]
    if type(label) is not str or not 1 <= len(label) <= 80:
        raise ValueError("Aggregate label must contain 1 to 80 characters")
    return label


def _aggregate(value: Any, parents: list[Any]) -> dict:
    label = _label(value)
    if not parents:
        raise ValueError("The aggregate needs at least one parent solution")
    totals = [Fraction(), Fraction()]
    for parent in parents:
        for index, item in enumerate(_solution(parent)):
            totals[index] += item
    return {"label": label, "parent_count": len(parents), "sum": [_encoded(item) for item in totals]}


def _check_aggregate(value: Any, output: Any, parents: list[Any]) -> Verdict:
    try:
        label = _label(value)
        if not parents:
            raise ValueError("The aggregate needs at least one parent solution")
        _fields(output, {"label", "parent_count", "sum"}, "An aggregate output")
        if output["label"] != label or type(output["parent_count"]) is not int:
            return Verdict(False, "Aggregate metadata does not match its input")
        if output["parent_count"] != len(parents):
            return Verdict(False, "Aggregate parent count is wrong")
        values = output["sum"]
        if type(values) is not list or len(values) != 2:
            return Verdict(False, "Aggregate sum needs two components")
        candidates = [_fraction(item) for item in values]
        columns = list(zip(*[_solution(parent) for parent in parents]))
        if any(candidate != sum(column, Fraction()) for candidate, column in zip(candidates, columns)):
            return Verdict(False, "Aggregate does not equal its supplied parent outputs")
        return Verdict(True, "Aggregate equals the exact component-wise sum of its parent outputs")
    except (ValueError, TypeError, KeyError) as exc:
        return Verdict(False, str(exc))


def _source(value: Any) -> tuple[str, str, int, int]:
    _fields(value, {"source_id", "source_text", "start", "end"}, "An excerpt input")
    source_id, text, start, end = (value[key] for key in ("source_id", "source_text", "start", "end"))
    if type(source_id) is not str or not 1 <= len(source_id) <= 128:
        raise ValueError("Source identifier must contain 1 to 128 characters")
    if type(text) is not str or not 1 <= len(text) <= 16_384:
        raise ValueError("Source text must contain 1 to 16,384 characters")
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
        raise ValueError("Excerpt positions must be exact integer code-point offsets within the source")
    if end - start > 2_048:
        raise ValueError("An excerpt must contain at most 2,048 characters")
    return source_id, text, start, end


def _excerpt(value: Any, parents: list[Any]) -> dict:
    if parents:
        raise ValueError("The excerpt stage does not accept parent outputs")
    source_id, text, start, end = _source(value)
    return {"source_id": source_id, "source_sha256": sha256(text.encode("utf-8")).hexdigest(),
            "start": start, "end": end, "quote": text[start:end]}


def _check_excerpt(value: Any, output: Any, parents: list[Any]) -> Verdict:
    try:
        if parents:
            raise ValueError("The excerpt stage does not accept parent outputs")
        source_id, text, start, end = _source(value)
        _fields(output, {"source_id", "source_sha256", "start", "end", "quote"}, "An excerpt output")
        if type(output["start"]) is not int or type(output["end"]) is not int:
            return Verdict(False, "Excerpt offsets are not exact integers")
        if output["start"] != start or output["end"] != end or output["source_id"] != source_id:
            return Verdict(False, "Excerpt metadata differs from the supplied source request")
        if output["source_sha256"] != sha256(text.encode("utf-8")).hexdigest():
            return Verdict(False, "Excerpt source digest differs from the supplied UTF-8 text")
        if output["quote"] != text[start:end]:
            return Verdict(False, "Excerpt is not the requested literal source span")
        return Verdict(True, "Literal span and digest match the supplied source; authenticity is not established")
    except (ValueError, TypeError, KeyError) as exc:
        return Verdict(False, str(exc))


def _revision(component: str) -> str:
    # An explicit module manifest. This does not discover hidden dependencies or
    # promise reproducibility across arbitrary Python/runtime implementations.
    return digest({"component": component, "module_sha256": sha256(Path(__file__).read_bytes()).hexdigest()})


def make_stages(integer_only: bool = False) -> list[Stage]:
    """Return three trusted fixtures; changing policy alone preserves producer identity."""
    if type(integer_only) is not bool:
        raise ValueError("integer_only must be an exact bool")

    def verify(value: Any, output: Any, parents: list[Any]) -> Verdict:
        return _check_system(value, output, parents, integer_only=integer_only)

    return [
        Stage("linear.solve", _revision("cramer.v1"), _solve_cramer,
              _revision("substitution.v1"), verify,
              model_config={"fixture": "exact-rational-arithmetic"},
              policy={"integer_only": integer_only},
              description="Solve a nonsingular rational 2-by-2 system and check by exact substitution.",
              input_example=INTEGER_EXAMPLE),
        Stage("linear.aggregate", _revision("sum.v1"), _aggregate,
              _revision("sum-check.v1"), _check_aggregate,
              description="Sum two-component solver outputs supplied by currently accepted parents.",
              input_example={"label": "combined solutions"}),
        Stage("text.excerpt", _revision("excerpt.v1"), _excerpt,
              _revision("span-check.v1"), _check_excerpt,
              description="Bind a literal excerpt to supplied UTF-8 source bytes and code-point offsets.",
              input_example=EXCERPT_EXAMPLE),
    ]


def run_demo(store_path: str | Path, *, integer_only: bool = False) -> dict:
    """Run invented examples in an operator-selected store and return actual receipts."""
    from .engine import Engine

    with Engine(store_path, make_stages(integer_only), scope="deterministic-demo") as engine:
        integer = engine.run("linear.solve", INTEGER_EXAMPLE)
        rational = engine.run("linear.solve", RATIONAL_EXAMPLE)
        aggregate = engine.run("linear.aggregate", {"label": "combined solutions"},
                               parents=[integer.work_key, rational.work_key])
        excerpt = engine.run("text.excerpt", EXCERPT_EXAMPLE)
        return {"fixture_only": True, "integer_only": integer_only,
                "receipts": {"integer": integer.to_dict(), "rational": rational.to_dict(),
                             "aggregate": aggregate.to_dict(), "excerpt": excerpt.to_dict()},
                "stats": engine.stats(),
                "limits": "Exact checks on supplied fixtures; no scientific, source-authenticity or learning claim."}
