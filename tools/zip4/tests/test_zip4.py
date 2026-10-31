import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zip4  # noqa: E402


class FakeClient:
    """Stands in for USPSClient; returns canned results and counts calls."""

    def __init__(self):
        self.calls = []

    def lookup(self, addr):
        self.calls.append(addr)
        reason = zip4.validate(addr)
        if reason:
            return zip4.Result(zip4.INVALID, message=reason)
        if "NOWHERE" in addr.street.upper():
            return zip4.Result(zip4.NOT_FOUND, message="Address Not Found.")
        return zip4.Result(zip4.MATCHED, zip5=addr.zip5 or "98101", zip4="1234", message="exact match")


class CleanZipTests(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(zip4.clean_zip5("98101"), "98101")
        self.assertEqual(zip4.clean_zip5("98101-4321"), "98101")
        self.assertEqual(zip4.clean_zip5("981014321"), "98101")
        self.assertEqual(zip4.clean_zip5("2134"), "02134")
        self.assertEqual(zip4.clean_zip5(""), "")
        self.assertEqual(zip4.clean_zip5("12"), "")


class ValidateTests(unittest.TestCase):
    def test_requires_street_state_and_city_or_zip(self):
        self.assertIsNone(zip4.validate(zip4.Address("1 Main St", city="Seattle", state="WA")))
        self.assertIsNone(zip4.validate(zip4.Address("1 Main St", state="WA", zip5="98101")))
        self.assertIsNotNone(zip4.validate(zip4.Address("", city="Seattle", state="WA")))
        self.assertIsNotNone(zip4.validate(zip4.Address("1 Main St", city="Seattle")))
        self.assertIsNotNone(zip4.validate(zip4.Address("1 Main St", state="WA")))


class ResponseParsingTests(unittest.TestCase):
    def test_error_message(self):
        body = {"error": {"message": "Address Not Found.", "errors": [{"detail": "bad street"}]}}
        self.assertEqual(zip4.error_message(body), "Address Not Found.; bad street")

    def test_match_text(self):
        body = {"matches": [{"code": "31", "text": "Single Response - exact match"}]}
        self.assertEqual(zip4.match_text(body), "Single Response - exact match")


class ProcessCsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inp = self.tmp / "in.csv"
        with self.inp.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Name", "Addr", "Town", "ST", "Postal"])
            w.writerow(["A", "1 Main St", "Seattle", "WA", "98101"])
            w.writerow(["B", "1  main st", "seattle", "wa", "98101-0000"])  # same address, different form
            w.writerow(["C", "9 Nowhere Rd", "Seattle", "WA", ""])
            w.writerow(["D", "", "Seattle", "WA", "98101"])
        self.cols = {"street": "Addr", "city": "Town", "state": "ST", "zip": "Postal", "secondary": None}

    def read(self, path):
        with path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_adds_columns_and_dedupes(self):
        out = self.tmp / "out.csv"
        client = FakeClient()
        counts = zip4.process_csv(self.inp, out, self.cols, client, zip4.Cache(None))
        rows = self.read(out)
        self.assertEqual([r["Name"] for r in rows], ["A", "B", "C", "D"])
        self.assertEqual(rows[0]["zip_plus4"], "98101-1234")
        self.assertEqual(rows[1]["zip4"], "1234")
        self.assertEqual(rows[2]["zip4_status"], zip4.NOT_FOUND)
        self.assertEqual(rows[3]["zip4_status"], zip4.INVALID)
        self.assertEqual(len(client.calls), 3)  # A and B share one lookup
        self.assertEqual(counts["usps_lookups"], 3)
        self.assertEqual(counts[zip4.MATCHED], 2)

    def test_cache_skips_lookups_on_rerun(self):
        cache_path = self.tmp / "cache.json"
        zip4.process_csv(self.inp, self.tmp / "o1.csv", self.cols, FakeClient(), zip4.Cache(cache_path))
        client = FakeClient()
        zip4.process_csv(self.inp, self.tmp / "o2.csv", self.cols, client, zip4.Cache(cache_path))
        self.assertEqual(client.calls, [])
        self.assertEqual(self.read(self.tmp / "o2.csv")[0]["zip_plus4"], "98101-1234")

    def test_missing_column_is_reported(self):
        cols = dict(self.cols, city="City")
        with self.assertRaises(SystemExit) as cm:
            zip4.process_csv(self.inp, self.tmp / "out.csv", cols, FakeClient(), zip4.Cache(None))
        self.assertIn("City", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
