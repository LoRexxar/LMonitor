"""Frozen official Maxroll oracle; no network, database or JS at test time."""

import copy
import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from botend.wow.talents import maxroll


FIXTURE = Path(__file__).with_name("fixtures") / "maxroll_talent_oracle.json"


class MaxrollTalentCodecTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture = json.loads(FIXTURE.read_text())

    def test_all_312_official_exports(self):
        rows = self.fixture["rows"]
        self.assertEqual(len(rows), 312)
        self.assertEqual(len({(r["spec"], r["source"]) for r in rows}), 312)
        self.assertEqual(len({r["hash"] for r in rows}), 23)
        for row in rows:
            with self.subTest(spec=row["spec"], source=row["source"]):
                self.assertEqual(
                    maxroll.convert_maxroll_code(row["source"], row["spec"]),
                    row["native"],
                )
                self.assertEqual(
                    maxroll.convert_maxroll_code(
                        row["source"].replace("+", "-").replace("/", "_")
                    ),
                    row["native"],
                )

    def test_omitted_gift_is_restored_not_payload_copied(self):
        original, omitted = self.fixture["gift_probe"]
        self.assertNotEqual(original["source"], omitted["source"])
        self.assertEqual(original["native"], omitted["native"])
        for row in (original, omitted):
            self.assertEqual(
                maxroll.convert_maxroll_code(row["source"], row["spec"]), row["native"]
            )

    def test_malformed_headers_and_tails(self):
        row = self.fixture["rows"][0]
        source = row["source"]

        def change(start, width, value):
            r = maxroll._Bits(source)
            bits = (r.value & ~(((1 << width) - 1) << start)) | (value << start)
            return "".join(
                maxroll._ALPHABET[(bits >> i) & 63]
                for i in range(0, len(source) * 6, 6)
            )

        cases = [
            (None, None),
            ("", None),
            (source + "!", None),
            (source + "=", None),
            (" " + source, None),
            (source[:20], None),
            (source + "A" * 4096, None),
            (source + "B" * 300, None),
            (source + "A" * 300, None),
            (change(0, 8, 131), None),
            (change(24, 128, 0), None),
            (change(8, 16, 65535), None),
            (change(8, 16, 71), None),
            (source, 9999),
            (source, "265"),
            (source, True),
            (row["native"], None),
        ]
        for code, spec in cases:
            with (
                self.subTest(code=code, expected_spec=spec),
                self.assertRaises(ValueError),
            ):
                maxroll.convert_maxroll_code(code, spec)

    def test_catalog_integrity_and_schema(self):
        catalog = maxroll._load_catalog()
        self.assertEqual(len(catalog["trees"]), 23)
        with patch.object(Path, "read_bytes", return_value=b"corrupt"):
            with self.assertRaisesRegex(ValueError, "catalog_digest"):
                maxroll._load_catalog.__wrapped__()
        mutations = [
            lambda c: c.update(schema=2),
            lambda c: c.update(unknown=True),
            lambda c: next(iter(c["trees"].values()))["nodes"].pop(
                next(iter(next(iter(c["trees"].values()))["nodes"]))
            ),
            lambda c: next(
                iter(next(iter(c["trees"].values()))["nodes"].values())
            ).update(Flags=512),
            lambda c: next(
                iter(next(iter(c["trees"].values()))["nodes"].values())
            ).update(Type=99),
            lambda c: next(iter(next(iter(c["trees"].values()))["nodes"].values()))[
                "Entries"
            ][0].update(MaxRanks=64),
        ]
        for mutate in mutations:
            c = copy.deepcopy(catalog)
            mutate(c)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                maxroll._validate_catalog(c)
        c = copy.deepcopy(catalog)
        tree = next(iter(c["trees"].values()))
        node = int(next(iter(tree["nodes"])))
        tree["edges"].append([node, node])
        with self.assertRaisesRegex(ValueError, "cycle"):
            maxroll._validate_catalog(c)

    def _single_node_source(self, nid, payload):
        row = self.fixture["rows"][0]
        tree = maxroll._load_catalog()["trees"][row["hash"]]
        header = maxroll._Bits(row["source"]).value & ((1 << 152) - 1)
        # Test-only bit assembly: all unspecified nodes are unselected.
        bits = (
            "".join(
                payload if int(key) == nid else "0"
                for key in sorted(tree["nodes"], key=int)
            )
            + "000"
        )
        value = header | (sum(int(bit) << i for i, bit in enumerate(bits)) << 152)
        return "".join(
            maxroll._ALPHABET[(value >> i) & 63] for i in range(0, 152 + len(bits), 6)
        )

    def test_illegal_node_shapes_and_unproven_gift(self):
        row = self.fixture["rows"][0]
        tree = maxroll._load_catalog()["trees"][row["hash"]]
        ordinary = next(
            int(n)
            for n, v in tree["nodes"].items()
            if not v["Flags"] & 256
            and len(v["Entries"]) == 1
            and v["Entries"][0]["MaxRanks"] > 0
            and not any(c["CondType"] == 2 for c in v.get("Conditions", []))
        )
        apex = next(int(n) for n, v in tree["nodes"].items() if v["Flags"] & 256)
        cases = [
            (ordinary, "1110000000", "zero_partial"),
            (ordinary, "1111111110", "rank_overflow"),
            (ordinary, "110111", "choice_out_of_range"),
            (apex, "1101", "apex_choice_flag"),
            (apex, "1111111110", "rank_overflow"),
            (ordinary, "10", "unproven_granted_node"),
        ]
        for node, payload, reason in cases:
            with (
                self.subTest(reason=reason),
                self.assertRaisesRegex(ValueError, reason),
            ):
                maxroll.convert_maxroll_code(self._single_node_source(node, payload))

    def test_unknown_conditions_and_references_rejected(self):
        for field, value in (
            ("CondType", 99),
            ("Flags", 8),
            ("SpecSetID", 999999),
            ("TraitNodeID", 999999),
            ("TraitNodeGroupID", 999999),
            ("TraitCurrencyID", 999999),
            ("GrantedRanks", True),
        ):
            catalog = copy.deepcopy(maxroll._load_catalog())
            tree = next(iter(catalog["trees"].values()))
            condition = next(
                c for n in tree["nodes"].values() for c in n.get("Conditions", [])
            )
            condition[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                maxroll._validate_catalog(catalog)
        with self.assertRaisesRegex(ValueError, "duplicate_key"):
            json.loads(
                '{"schema":1,"schema":1}', object_pairs_hook=maxroll._unique_object
            )
        catalog = copy.deepcopy(maxroll._load_catalog())
        tree = next(iter(catalog["trees"].values()))
        nodes = list(map(int, tree["nodes"]))
        tree["edges"] = [[a, b] for a, b in zip(nodes, nodes[1:])]
        with self.assertRaisesRegex(ValueError, "graph_depth"):
            maxroll._validate_catalog(catalog)
        catalog = copy.deepcopy(maxroll._load_catalog())
        tree = next(iter(catalog["trees"].values()))
        first, second = list(tree["nodes"].values())[:2]
        second["Entries"][0]["ID"] = first["Entries"][0]["ID"]
        with self.assertRaisesRegex(ValueError, "duplicate_entry"):
            maxroll._validate_catalog(catalog)

    def test_corrupt_payload_digest(self):
        packed = gzip.compress(b"{}", mtime=0)
        with (
            patch.object(Path, "read_bytes", return_value=packed),
            patch.object(
                maxroll, "_CATALOG_SHA256", hashlib.sha256(packed).hexdigest()
            ),
            self.assertRaisesRegex(ValueError, "catalog_payload_digest"),
        ):
            maxroll._load_catalog.__wrapped__()

    def test_runtime_does_not_access_network_or_javascript(self):
        row = self.fixture["rows"][0]
        with (
            patch("socket.socket", side_effect=AssertionError("network")),
            patch("subprocess.Popen", side_effect=AssertionError("subprocess")),
        ):
            self.assertEqual(maxroll.convert_maxroll_code(row["source"]), row["native"])
