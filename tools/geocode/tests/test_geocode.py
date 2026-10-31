import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geocode  # noqa: E402

# A real Census batch response (2026-10), plus a Tie row in the same shape as No_Match.
SAMPLE_RESPONSE = '''\
"1","1600 Pennsylvania Ave NW, Washington, DC, 20500","Match","Exact","1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500","-77.035189204737,38.898702605246","76225813","L","11","001","980000","1034"
"2","4600 Silver Hill Rd, Washington, DC, 20233","Match","Non_Exact","4600 SILVER HILL RD, WASHINGTON, DC, 20233","-76.92836638093,38.84505589808","657091557","L","24","033","802405","2004"
"4","123 Nowhere Imaginary Ln, Nowhereville, ZZ, 00000","No_Match"
"5","1 Main St, Springfield, , ","Tie"
'''


class FakeClient:
    """Stands in for CensusClient: matches any street containing 'MAIN', counts batches."""

    def __init__(self, fail=False):
        self.batches = []
        self.fail = fail

    def geocode_batch(self, addrs):
        self.batches.append(list(addrs))
        if self.fail:
            return [geocode.Result(geocode.ERROR, message="Census request failed: timeout") for _ in addrs]
        out = []
        for a in addrs:
            if "MAIN" in a.street.upper():
                out.append(geocode.Result(geocode.MATCHED, match_type="exact", matched_address=a.street.upper(),
                                          latitude="47.6", longitude="-122.3", state_fips="53",
                                          county_fips="033", tract="007101", block="2001"))
            else:
                out.append(geocode.Result(geocode.NO_MATCH, message="Census found no match"))
        return out


class ParseResponseTests(unittest.TestCase):
    def setUp(self):
        self.r = geocode.parse_response(SAMPLE_RESPONSE)

    def test_match_fields(self):
        wh = self.r["1"]
        self.assertEqual(wh.status, geocode.MATCHED)
        self.assertEqual(wh.match_type, "exact")
        self.assertEqual((wh.latitude, wh.longitude), ("38.898702605246", "-77.035189204737"))  # lon,lat swapped
        self.assertEqual((wh.state_fips, wh.county_fips, wh.tract, wh.block), ("11", "001", "980000", "1034"))
        self.assertEqual(self.r["2"].match_type, "non_exact")

    def test_no_match_and_tie(self):
        self.assertEqual(self.r["4"].status, geocode.NO_MATCH)
        self.assertEqual(self.r["5"].status, geocode.TIE)
        self.assertEqual(self.r["5"].latitude, "")

    def test_blank_lines_ignored(self):
        self.assertEqual(geocode.parse_response("\n\n"), {})


class GeoidTests(unittest.TestCase):
    def setUp(self):
        self.r = geocode.parse_response(SAMPLE_RESPONSE)["1"]

    def test_levels(self):
        self.assertEqual(self.r.block_group, "1")
        self.assertEqual(self.r.geoid("county"), "11001")
        self.assertEqual(self.r.geoid("tract"), "11001980000")
        self.assertEqual(self.r.geoid("block_group"), "110019800001")
        self.assertEqual(self.r.geoid("block"), "110019800001034")

    def test_lengths(self):
        self.assertEqual([len(self.r.geoid(lvl)) for lvl in geocode.CLUSTER_LEVELS], [5, 11, 12, 15])

    def test_empty_unless_matched(self):
        for status in (geocode.NO_MATCH, geocode.TIE, geocode.INVALID, geocode.ERROR):
            self.assertEqual(geocode.Result(status, state_fips="11", county_fips="001").geoid("county"), "")

    def test_columns(self):
        cols = self.r.columns()
        self.assertEqual(set(cols), set(geocode.OUT_COLUMNS))
        self.assertEqual(cols["block_group_geoid"], "110019800001")


class InputTests(unittest.TestCase):
    def test_clean_zip5(self):
        self.assertEqual(geocode.clean_zip5("98101-4321"), "98101")
        self.assertEqual(geocode.clean_zip5("2134"), "02134")
        self.assertEqual(geocode.clean_zip5("x"), "")

    def test_validate(self):
        self.assertIsNone(geocode.validate(geocode.Address("1 Main St", zip5="98101")))
        self.assertIsNone(geocode.validate(geocode.Address("1 Main St", city="Seattle", state="WA")))
        self.assertIsNotNone(geocode.validate(geocode.Address("", zip5="98101")))
        self.assertIsNotNone(geocode.validate(geocode.Address("1 Main St", city="Seattle")))

    def test_batch_csv_quotes_commas(self):
        data = geocode.batch_csv([("7", geocode.Address("400 Broad St, Unit 1", "Seattle", "wa", "98109"))])
        self.assertEqual(next(csv.reader(io.StringIO(data.decode()))),
                         ["7", "400 Broad St, Unit 1", "Seattle", "WA", "98109"])


class CensusClientTests(unittest.TestCase):
    ADDRS = [geocode.Address("1600 Pennsylvania Ave NW", "Washington", "DC", "20500"),
             geocode.Address("4600 Silver Hill Rd", "Washington", "DC", "20233")]

    def test_results_in_input_order(self):
        c = geocode.CensusClient()
        # Census numbers rows from our IDs; respond out of order to check mapping
        resp = ('"1","x","Match","Exact","SILVER HILL","-76.9,38.8","1","L","24","033","802405","2004"\n'
                '"0","x","No_Match"\n')
        with mock.patch.object(c, "_post", return_value=resp):
            r = c.geocode_batch(self.ADDRS)
        self.assertEqual([x.status for x in r], [geocode.NO_MATCH, geocode.MATCHED])

    def test_missing_row_is_error(self):
        c = geocode.CensusClient()
        with mock.patch.object(c, "_post", return_value='"0","x","No_Match"\n'):
            r = c.geocode_batch(self.ADDRS)
        self.assertEqual(r[1].status, geocode.ERROR)

    def test_retries_then_errors(self):
        c = geocode.CensusClient(max_retries=2)
        err = urllib.error.URLError("timed out")
        with mock.patch.object(c, "_post", side_effect=err) as post, \
                mock.patch.object(geocode.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
            r = c.geocode_batch(self.ADDRS)
        self.assertEqual(post.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [30.0, 60.0])
        self.assertTrue(all(x.status == geocode.ERROR and "timed out" in x.message for x in r))

    def test_retry_succeeds(self):
        c = geocode.CensusClient()
        with mock.patch.object(c, "_post", side_effect=[TimeoutError("slow"), '"0","x","No_Match"\n"1","x","No_Match"\n']), \
                mock.patch.object(geocode.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            r = c.geocode_batch(self.ADDRS)
        self.assertEqual([x.status for x in r], [geocode.NO_MATCH, geocode.NO_MATCH])

    def test_rejects_oversized_batch(self):
        with self.assertRaises(ValueError):
            geocode.CensusClient().geocode_batch([self.ADDRS[0]] * (geocode.MAX_BATCH + 1))

    def test_multipart_request(self):
        c = geocode.CensusClient(benchmark="B", vintage="V")
        captured = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            captured["req"] = req
            return Resp(b'"0","x","No_Match"\n')

        with mock.patch.object(geocode.urllib.request, "urlopen", fake_urlopen):
            c.geocode_batch(self.ADDRS[:1])
        req = captured["req"]
        body = req.data.decode()
        self.assertEqual(req.full_url, geocode.BATCH_URL)
        self.assertIn("multipart/form-data; boundary=", req.get_header("Content-type"))
        self.assertIn('name="benchmark"\r\n\r\nB\r\n', body)
        self.assertIn('name="vintage"\r\n\r\nV\r\n', body)
        self.assertIn('name="addressFile"; filename="addresses.csv"', body)
        self.assertIn("0,1600 Pennsylvania Ave NW,Washington,DC,20500\n", body)


class ProcessCsvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inp = self.tmp / "in.csv"
        with self.inp.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Name", "Addr", "Unit", "Town", "ST", "Postal"])
            w.writerow(["A", "1 Main St", "", "Seattle", "WA", "98101"])
            w.writerow(["B", "1  MAIN st", "", "seattle", "wa", "98101-0000"])  # same address
            w.writerow(["C", "9 Nowhere Rd", "", "Seattle", "WA", ""])
            w.writerow(["D", "", "", "Seattle", "WA", "98101"])  # invalid: no street
            w.writerow(["E", "5 Main St", "Apt 2", "", "", "98101"])  # ZIP only, with unit
        self.cols = {"street": "Addr", "city": "Town", "state": "ST", "zip": "Postal", "secondary": "Unit"}

    def run_csv(self, client, cache=None, **kw):
        out = self.tmp / "out.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            counts = geocode.process_csv(self.inp, out, self.cols, client, cache or geocode.Cache(None), **kw)
        with out.open(newline="", encoding="utf-8") as f:
            return counts, list(csv.DictReader(f))

    def test_adds_columns_keeps_rows_and_dedupes(self):
        client = FakeClient()
        counts, rows = self.run_csv(client)
        self.assertEqual([r["Name"] for r in rows], ["A", "B", "C", "D", "E"])
        self.assertEqual(list(rows[0])[-len(geocode.OUT_COLUMNS):], list(geocode.OUT_COLUMNS))
        self.assertEqual([r["geo_status"] for r in rows],
                         [geocode.MATCHED, geocode.MATCHED, geocode.NO_MATCH, geocode.INVALID, geocode.MATCHED])
        self.assertEqual(rows[0]["tract_geoid"], "53033007101")
        sent = [a.street for b in client.batches for a in b]
        self.assertEqual(sent, ["1 Main St", "9 Nowhere Rd", "5 Main St Apt 2"])  # A/B once, D never, unit appended
        self.assertEqual(counts["census_lookups"], 3)

    def test_batching(self):
        client = FakeClient()
        self.run_csv(client, batch_size=2, workers=1)
        self.assertEqual([len(b) for b in client.batches], [2, 1])

    def test_cluster_level(self):
        _, rows = self.run_csv(FakeClient(), cluster_level="block_group")
        self.assertEqual([r["geo_cluster"] for r in rows], ["530330071012", "530330071012", "", "", "530330071012"])
        _, rows = self.run_csv(FakeClient())
        self.assertNotIn("geo_cluster", rows[0])

    def test_cache_resume_and_errors_not_cached(self):
        cache_path = self.tmp / "cache.json"
        counts, rows = self.run_csv(FakeClient(fail=True), geocode.Cache(cache_path))
        self.assertEqual(counts[geocode.ERROR], 4)
        self.assertEqual(json.loads(cache_path.read_text()), {})

        self.run_csv(FakeClient(), geocode.Cache(cache_path))
        client = FakeClient()
        _, rows = self.run_csv(client, geocode.Cache(cache_path), cluster_level="tract")
        self.assertEqual(client.batches, [])  # everything came from the cache
        self.assertEqual(rows[0]["geo_cluster"], "53033007101")

    def test_missing_and_clashing_columns(self):
        with self.assertRaises(SystemExit) as cm:
            geocode.process_csv(self.inp, self.tmp / "o.csv", dict(self.cols, city="City"), FakeClient(),
                                geocode.Cache(None))
        self.assertIn("City", str(cm.exception))
        clash = self.tmp / "clash.csv"
        clash.write_text("Addr,Town,ST,Postal,Unit,latitude\n1 Main St,Seattle,WA,98101,,1\n")
        with self.assertRaises(SystemExit) as cm:
            geocode.process_csv(clash, self.tmp / "o.csv", self.cols, FakeClient(), geocode.Cache(None))
        self.assertIn("latitude", str(cm.exception))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inp = self.tmp / "in.csv"
        self.inp.write_text("Addr,Town,ST\n1 Main St,Seattle,WA\n")
        self.client = FakeClient()
        patcher = mock.patch.object(geocode, "CensusClient", lambda **kw: self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = geocode.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    BASE = ("--street-col", "Addr", "--city-col", "Town", "--state-col", "ST")

    def test_csv_defaults(self):
        code, _, _ = self.run_main("csv", str(self.inp), *self.BASE, "--cluster-level", "county")
        self.assertEqual(code, 0)
        with (self.tmp / "in_geocoded.csv").open(newline="") as f:
            self.assertEqual(next(csv.DictReader(f))["geo_cluster"], "53033")
        self.assertTrue((self.tmp / "in_geocode-cache.json").exists())

    def test_csv_no_cache(self):
        self.run_main("csv", str(self.inp), *self.BASE, "--no-cache")
        self.assertFalse((self.tmp / "in_geocode-cache.json").exists())

    def test_csv_exits_3_on_errors(self):
        self.client.fail = True
        code, _, _ = self.run_main("csv", str(self.inp), *self.BASE)
        self.assertEqual(code, 3)

    def test_csv_argument_checks(self):
        cases = [
            (("--street-col", "Addr", "--city-col", "Town"), "--zip-col"),
            ((*self.BASE, "--batch-size", "10001"), "1-10000"),
            ((*self.BASE, "--workers", "9"), "1-4"),
            ((*self.BASE, "--cluster-level", "zip"), "invalid choice"),
            ((*self.BASE, "-o", str(self.inp)), "overwrite"),
        ]
        for args, msg in cases:
            with self.subTest(args=args):
                code, _, err = self.run_main("csv", str(self.inp), *args)
                self.assertEqual(code, 2)
                self.assertIn(msg, err)

    def test_lookup(self):
        code, out, _ = self.run_main("lookup", "--street", "1 Main St", "--zip", "98101")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["latitude"], "47.6")
        code, out, _ = self.run_main("lookup", "--street", "1 Main St")
        self.assertEqual((code, json.loads(out)["geo_status"]), (1, geocode.INVALID))
        self.assertEqual(len(self.client.batches), 1)  # the invalid one was never sent


@unittest.skipUnless(os.environ.get("GEOCODE_LIVE") == "1", "set GEOCODE_LIVE=1 to call the real Census API")
class LiveTests(unittest.TestCase):
    def test_white_house(self):
        r = geocode.CensusClient().geocode_batch(
            [geocode.Address("1600 Pennsylvania Ave NW", "Washington", "DC", "20500")])[0]
        self.assertEqual(r.status, geocode.MATCHED)
        self.assertEqual(r.geoid("county"), "11001")
        self.assertAlmostEqual(float(r.latitude), 38.8987, places=2)


if __name__ == "__main__":
    unittest.main()
