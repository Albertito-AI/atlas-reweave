"""Independent public-contract checks for strict JSON and value boundaries."""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from reweave.codec import canonical, digest, digest_text, fingerprint_files, parse, snapshot
from reweave.types import BudgetExceeded, InvalidJSON, Limits, Receipt


class StrictJSONTests(unittest.TestCase):
    def test_known_encoding_and_hash_ignore_object_insertion_order(self):
        value = {"u": "é", "b": [1, 2.5, None], "a": True}
        expected = '{"a":true,"b":[1,2.5,null],"u":"é"}'
        self.assertEqual(canonical(value), expected)
        self.assertEqual(digest(value), hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertEqual(digest(value), digest(dict(reversed(list(value.items())))))
        self.assertEqual(digest_text(expected), digest(value))

    def test_numbers_and_boolean_remain_type_distinct(self):
        self.assertEqual(len({digest(True), digest(1), digest(1.0), digest("1")}), 4)
        self.assertNotEqual(digest(0.0), digest(-0.0))

    def test_parser_rejects_duplicate_keys_at_every_depth(self):
        for text in ('{"x":1,"x":2}', '{"a":{"x":1,"x":2}}', '{"x":1,"\\u0078":2}'):
            with self.subTest(text=text), self.assertRaises(InvalidJSON):
                parse(text)

    def test_nonfinite_numbers_cannot_enter_by_value_or_text(self):
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(number=number), self.assertRaises(InvalidJSON):
                canonical({"nested": [number]})
        for text in ("NaN", "Infinity", "-Infinity", "1e9999", '{"n":NaN}'):
            with self.subTest(text=text), self.assertRaises(InvalidJSON):
                parse(text)

    def test_only_exact_json_builtin_types_are_admitted(self):
        class Integer(int):
            pass

        class String(str):
            pass

        class Dictionary(dict):
            pass

        for value in ((1, 2), {1, 2}, b"x", object(), Integer(1), Dictionary(a=1), {1: "x"}, {String("a"): 1}):
            with self.subTest(kind=type(value).__name__), self.assertRaises(InvalidJSON):
                canonical(value)

    def test_invalid_unicode_and_malformed_text_fail_closed(self):
        for value in ("\ud800", {"\udfff": 1}):
            with self.subTest(value=repr(value)), self.assertRaises(InvalidJSON):
                canonical(value)
        for text in ('"\\ud800"', '"\ud800"', "{", "01", "true false", '[1,]', b"{}"):
            with self.subTest(text=repr(text)), self.assertRaises(InvalidJSON):
                parse(text)

    def test_cycles_are_rejected_but_repeated_values_are_legal(self):
        cycle = []
        cycle.append(cycle)
        with self.assertRaises(InvalidJSON):
            canonical(cycle)
        child = {"n": [1]}
        self.assertEqual(parse(canonical([child, child])), [child, child])

    def test_byte_limit_counts_utf8_and_accepts_exact_boundary(self):
        self.assertEqual(canonical("é", max_bytes=4), '"é"')
        self.assertEqual(parse('"é"', max_bytes=4), "é")
        for function, value in ((canonical, "é"), (parse, '"é"')):
            with self.subTest(function=function.__name__), self.assertRaises(BudgetExceeded):
                function(value, max_bytes=3)
        with self.assertRaises(BudgetExceeded):
            parse(" " * 12 + "{}", max_bytes=8)

    def test_depth_and_node_limits_prevent_unbounded_walks(self):
        with self.assertRaises(InvalidJSON):
            canonical([[[[0]]]], max_depth=3)
        self.assertEqual(canonical([[[0]]], max_depth=3), "[[[0]]]")
        with self.assertRaises(InvalidJSON):
            canonical([0] * 100_001, max_bytes=None)
        with self.assertRaises(InvalidJSON):
            canonical(1 << 4096)

    def test_snapshot_copies_every_mutable_layer(self):
        original = {"items": [{"n": 1}]}
        copied = snapshot(original)
        copied["items"][0]["n"] = 9
        self.assertEqual(original, {"items": [{"n": 1}]})
        original["items"].append(2)
        self.assertEqual(copied, {"items": [{"n": 9}]})

    def test_file_fingerprint_uses_labels_and_bytes_not_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.txt"
            second = Path(temporary) / "other.txt"
            first.write_bytes(b"fixture\n")
            second.write_bytes(first.read_bytes())
            before = fingerprint_files({"source": first})
            self.assertEqual(before, fingerprint_files({"source": second}))
            self.assertNotEqual(before, fingerprint_files({"prompt": first}))
            second.write_bytes(b"changed\n")
            self.assertNotEqual(before, fingerprint_files({"source": second}))


class BoundaryTypesTests(unittest.TestCase):
    def test_limits_require_positive_exact_integers(self):
        for field in vars(Limits()):
            for value in (0, -1, True, 1.0, "1"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    Limits(**{field: value})

    def test_unaccepted_receipts_never_serialise_output(self):
        for status in ("blocked", "rejected", "stale", "corrupt", "error", "budget"):
            with self.subTest(status=status):
                receipt = Receipt(status, "a" * 64, output={"secret": "must not escape"})
                self.assertNotIn("output", receipt.to_dict())

    def test_accepted_receipt_dictionary_does_not_lend_mutable_output(self):
        receipt = Receipt("accepted", "a" * 64, output={"items": [1]})
        result = receipt.to_dict()
        result["output"]["items"].append(2)
        self.assertEqual(receipt.output, {"items": [1]})


if __name__ == "__main__":
    unittest.main()
