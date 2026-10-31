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
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geocode

# A real Census batch response (2026-10), plus a Tie row in the same shape as No_Match.
SAMPLE_RESPONSE = (
    '"1","1600 Pennsylvania Ave NW, Washington, DC, 20500","Match","Exact",'
    '"1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500","-77.035189204737,38.898702605246",'
    '"76225813","L","11","001","980000","1034"\n'
    '"2","4600 Silver Hill Rd, Washington, DC, 20233","Match","Non_Exact",'
    '"4600 SILVER HILL RD, WASHINGTON, DC, 20233","-76.92836638093,38.84505589808",'
    '"657091557","L","24","033","802405","2004"\n'
    '"4","123 Nowhere Imaginary Ln, Nowhereville, ZZ, 00000","No_Match"\n'
    '"5","1 Main St, Springfield, , ","Tie"\n'
)


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
    ADDRS: ClassVar[list[geocode.Address]] = [geocode.Address("1600 Pennsylvania Ave NW", "Washington", "DC", "20500"),
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
        resp = '"0","x","No_Match"\n"1","x","No_Match"\n'
        with mock.patch.object(c, "_post", side_effect=[TimeoutError("slow"), resp]), \
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


class FakeResp(io.BytesIO):
    """A urlopen() response usable as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AddressAndResultTests(unittest.TestCase):
    def test_key_normalizes_case_and_spaces(self):
        a = geocode.Address(" 1  main st ", "seattle", "wa", "98101")
        self.assertEqual(a.key(), "1 MAIN ST|SEATTLE|WA|98101")

    def test_geoid_empty_when_a_part_is_missing(self):
        r = geocode.Result(geocode.MATCHED, state_fips="11", county_fips="001", tract="", block="")
        self.assertEqual(r.geoid("county"), "11001")
        self.assertEqual([r.geoid(lvl) for lvl in ("tract", "block_group", "block")], ["", "", ""])
        self.assertEqual(geocode.Result(geocode.MATCHED, state_fips="11").geoid("county"), "")

    def test_columns_use_message_when_no_matched_address(self):
        cols = geocode.Result(geocode.NO_MATCH, message="Census found no match").columns()
        self.assertEqual(cols["geo_matched_address"], "Census found no match")
        self.assertEqual(cols["geo_status"], geocode.NO_MATCH)
        matched = geocode.Result(geocode.MATCHED, matched_address="1 MAIN ST", message="ignored").columns()
        self.assertEqual(matched["geo_matched_address"], "1 MAIN ST")


class CleanAndValidateTests(unittest.TestCase):
    def test_clean_zip5(self):
        cases = {"98101": "98101", "98101-4321": "98101", "981014321": "98101", "(981) 01": "98101",
                 "2134": "02134", "213": "00213", "12": "", "": "", None: ""}
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(geocode.clean_zip5(raw), want)

    def test_validate_messages(self):
        self.assertEqual(geocode.validate(geocode.Address("  ", zip5="98101")), "street is empty")
        self.assertEqual(geocode.validate(geocode.Address("1 Main St", city="Seattle", state=" ")),
                         "need a 5-digit ZIP, or a city and state")

    def test_batch_csv_exact_bytes(self):
        data = geocode.batch_csv([("0", geocode.Address(" 1 Main St ", " Seattle ", " wa ", "98101")),
                                  ("1", geocode.Address("2 Café Ln", "", "", "98102"))])
        self.assertEqual(data, "0,1 Main St,Seattle,WA,98101\n1,2 Café Ln,,,98102\n".encode())


class ParseResponseEdgeTests(unittest.TestCase):
    def test_short_rows_skipped_not_stopping(self):
        r = geocode.parse_response('"0","x"\n"1","x","No_Match"\n')
        self.assertEqual(list(r), ["1"])

    def test_messages(self):
        r = geocode.parse_response(SAMPLE_RESPONSE)
        self.assertEqual(r["4"].message, "Census found no match")
        self.assertEqual(r["5"].message, "several equally good matches; no location returned")
        self.assertEqual(r["1"].matched_address, "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500")
        self.assertEqual(r["2"].matched_address, "4600 SILVER HILL RD, WASHINGTON, DC, 20233")

    def test_match_without_geography_columns(self):
        r = geocode.parse_response('"0","x","Match","Exact","1 MAIN ST","-122.3,47.6"\n')["0"]
        self.assertEqual((r.status, r.latitude, r.longitude), (geocode.MATCHED, "47.6", "-122.3"))
        self.assertEqual((r.state_fips, r.county_fips, r.tract, r.block), ("", "", "", ""))

    def test_match_with_partial_geography(self):
        r = geocode.parse_response('"0","x","Match","Exact","A","-1,2","9","L","53","033"\n')["0"]
        self.assertEqual((r.state_fips, r.county_fips, r.tract, r.block), ("53", "033", "", ""))

    def test_match_with_too_few_columns_is_no_match(self):
        r = geocode.parse_response('"0","x","Match","Exact","A"\n')["0"]
        self.assertEqual(r.status, geocode.NO_MATCH)

    def test_no_match_with_extra_columns_stays_no_match(self):
        r = geocode.parse_response('"0","x","No_Match","","","","","","","","",""\n')["0"]
        self.assertEqual((r.status, r.latitude), (geocode.NO_MATCH, ""))


class CensusClientRequestTests(unittest.TestCase):
    ADDR = geocode.Address("1600 Pennsylvania Ave NW", "Washington", "DC", "20500")

    def capture(self, client, body=b'"0","x","No_Match"\n'):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append((req, timeout))
            return FakeResp(body)

        with mock.patch.object(geocode.urllib.request, "urlopen", fake_urlopen):
            result = client.geocode_batch([self.ADDR])
        return calls, result

    def test_rejects_non_https_url(self):
        for url in ("http://geocoding.geo.census.gov/x", "file:///etc/passwd", "ftp://x"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, r"must use https://, got '"):
                geocode.CensusClient(url=url)
        self.assertEqual(geocode.CensusClient(url="HTTPS://example.test/x").url, "HTTPS://example.test/x")

    def test_exact_multipart_body_and_headers(self):
        calls, _ = self.capture(geocode.CensusClient(benchmark="B", vintage="V"))
        (req, timeout), = calls
        self.assertEqual(timeout, 1800.0)
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, geocode.BATCH_URL)
        self.assertEqual(req.get_header("User-agent"), geocode.USER_AGENT)
        ctype = req.get_header("Content-type")
        self.assertRegex(ctype, r"^multipart/form-data; boundary=[0-9a-f]{32}$")
        b = ctype.rsplit("=", 1)[1]
        self.assertEqual(req.data.decode(), (
            f'--{b}\r\nContent-Disposition: form-data; name="benchmark"\r\n\r\nB\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="vintage"\r\n\r\nV\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="addressFile"; filename="addresses.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n0,1600 Pennsylvania Ave NW,Washington,DC,20500\n\r\n"
            f"--{b}--\r\n"))

    def test_custom_timeout_and_defaults(self):
        calls, _ = self.capture(geocode.CensusClient(timeout=5))
        self.assertEqual(calls[0][1], 5)
        c = geocode.CensusClient()
        self.assertEqual((c.benchmark, c.vintage, c.max_retries), ("Public_AR_Current", "Current_Current", 3))

    def test_invalid_utf8_in_response_is_replaced(self):
        _, result = self.capture(geocode.CensusClient(), b'"0","x","Match","Exact","CAF\xc9","-1,2"\n')
        self.assertEqual(result[0].matched_address, "CAF�")

    def test_full_batch_allowed(self):
        c = geocode.CensusClient()
        with mock.patch.object(c, "_post", return_value="") as post:
            r = c.geocode_batch([self.ADDR] * geocode.MAX_BATCH)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(r), geocode.MAX_BATCH)
        self.assertEqual((r[0].status, r[0].message), (geocode.ERROR, "missing from Census response"))

    def test_oversized_batch_message(self):
        with self.assertRaisesRegex(ValueError, r"^batch of 10001 exceeds Census limit of 10000$"):
            geocode.CensusClient().geocode_batch([self.ADDR] * (geocode.MAX_BATCH + 1))

    def test_default_retries_and_messages(self):
        c = geocode.CensusClient()
        err = io.StringIO()
        with mock.patch.object(c, "_post", side_effect=urllib.error.URLError("timed out")) as post, \
                mock.patch.object(geocode.time, "sleep"), contextlib.redirect_stderr(err):
            r = c.geocode_batch([self.ADDR, self.ADDR])
        self.assertEqual(post.call_count, 4)
        self.assertEqual([x.message for x in r], ["Census request failed: timed out"] * 2)
        self.assertEqual(err.getvalue().splitlines(), [
            "geocode: batch of 2 failed (timed out); retrying in 30s",
            "geocode: batch of 2 failed (timed out); retrying in 60s",
            "geocode: batch of 2 failed (timed out); retrying in 120s",
        ])

    def test_error_without_reason_uses_its_text(self):
        c = geocode.CensusClient(max_retries=0)
        with mock.patch.object(c, "_post", side_effect=TimeoutError("slow")), \
                mock.patch.object(geocode.time, "sleep") as sleep:
            r = c.geocode_batch([self.ADDR])
        self.assertEqual(r[0].message, "Census request failed: slow")
        sleep.assert_not_called()  # no retry left, so no wait


class CacheTests(unittest.TestCase):
    def test_saved_file_is_compact_and_sorted(self):
        path = Path(tempfile.mkdtemp()) / "c.json"
        cache = geocode.Cache(path)
        cache.put("B", geocode.Result(geocode.NO_MATCH, message="m"))
        cache.put("A", geocode.Result(geocode.TIE))
        cache.put("C", geocode.Result(geocode.ERROR))
        cache.save()
        fields = ("block", "county_fips", "latitude", "longitude", "match_type", "matched_address")
        def row(status, message):
            d = dict.fromkeys(fields, "")
            d.update(message=message, state_fips="", status=status, tract="")
            return json.dumps(d, sort_keys=True, separators=(",", ":"))
        self.assertEqual(path.read_text(), f'{{"A":{row("tie", "")},"B":{row("no_match", "m")}}}')
        self.assertEqual([p.name for p in path.parent.iterdir()], ["c.json"])  # temp file renamed away
        self.assertEqual(geocode.Cache(path).get("B"), geocode.Result(geocode.NO_MATCH, message="m"))
        self.assertIsNone(geocode.Cache(path).get("C"))

    def test_no_path_saves_nothing(self):
        cache = geocode.Cache(None)
        cache.put("A", geocode.Result(geocode.TIE))
        cache.save()
        self.assertEqual(cache.get("A"), geocode.Result(geocode.TIE))


class ProcessCsvDetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.out = self.tmp / "out.csv"

    def write(self, text, name="in.csv"):
        path = self.tmp / name
        path.write_bytes(text.encode("utf-8-sig"))
        return path

    def run_csv(self, inp, cols, client=None, **kw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            counts = geocode.process_csv(inp, self.out, cols, client or FakeClient(), geocode.Cache(None), **kw)
        return counts, err.getvalue()

    def test_progress_messages_and_counts(self):
        inp = self.write("Addr,Zip\n1 Main St,98101\n2 Main St,98101\n9 Elm St,98101\n,98101\n1 Main St,98101\n")
        counts, err = self.run_csv(inp, {"street": "Addr", "zip": "Zip"}, batch_size=2, workers=1)
        self.assertEqual(err.splitlines(), [
            "geocode: 5 rows, 1 cached or invalid, 3 to geocode in 2 batch(es) of up to 2",
            "geocode: batch 1/2 done (2/2 matched)",
            "geocode: batch 2/2 done (0/1 matched)",
        ])
        self.assertEqual(counts, {geocode.MATCHED: 3, geocode.NO_MATCH: 1, geocode.INVALID: 1, "census_lookups": 3})

    def test_exact_output_file(self):
        inp = self.write('Name,Addr,Zip\nA,"1 Main St, Rear",98101\nB,,98101\n')
        self.run_csv(inp, {"street": "Addr", "zip": "Zip", "secondary": None})
        header = "Name,Addr,Zip," + ",".join(geocode.OUT_COLUMNS)
        self.assertEqual(self.out.read_bytes().decode("utf-8"), (
            f"{header}\r\n"
            'A,"1 Main St, Rear",98101,47.6,-122.3,matched,exact,"1 MAIN ST, REAR",53,033,007101,2,2001,'
            "53033,53033007101,530330071012,530330071012001\r\n"
            "B,,98101,,,invalid_input,,street is empty,,,,,,,,,\r\n"))

    def test_multiline_field_and_bom_preserved(self):
        inp = self.write('Name,Addr,Zip\n"line1\r\nline2",1 Main St,98101\n')
        self.run_csv(inp, {"street": "Addr", "zip": "Zip"})
        with self.out.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["Name"], "line1\r\nline2")
        self.assertEqual(next(iter(rows[0])), "Name")  # BOM stripped from the first header

    def test_error_messages_list_all_columns(self):
        inp = self.write("Addr,Zip,latitude,longitude\n1 Main St,98101,1,2\n")
        with self.assertRaises(SystemExit) as cm:
            self.run_csv(inp, {"street": "Street", "zip": "Postal"})
        self.assertEqual(str(cm.exception), "geocode: column(s) not in in.csv: Street, Postal\n"
                                            "geocode: available columns: Addr, Zip, latitude, longitude")
        with self.assertRaises(SystemExit) as cm:
            self.run_csv(inp, {"street": "Addr", "zip": "Zip"})
        self.assertEqual(str(cm.exception), "geocode: input already has output column(s): latitude, longitude")

    def test_cluster_column_clash(self):
        inp = self.write("Addr,Zip,geo_cluster\n1 Main St,98101,x\n")
        with self.assertRaisesRegex(SystemExit, "output column\\(s\\): geo_cluster$"):
            self.run_csv(inp, {"street": "Addr", "zip": "Zip"}, cluster_level="county")
        self.run_csv(inp, {"street": "Addr", "zip": "Zip"})  # fine without --cluster-level

    def test_default_batch_size_and_workers(self):
        inp = self.write("Addr,Zip\n" + "".join(f"{i} Main St,98101\n" for i in range(2501)))
        client = FakeClient()
        pools = []
        real = geocode.ThreadPoolExecutor

        def recording_pool(max_workers):
            pools.append(max_workers)
            return real(max_workers=max_workers)

        with mock.patch.object(geocode, "ThreadPoolExecutor", recording_pool):
            self.run_csv(inp, {"street": "Addr", "zip": "Zip"}, client=client)
            self.run_csv(inp, {"street": "Addr", "zip": "Zip"}, client=FakeClient(), workers=1)
            self.run_csv(inp, {"street": "Addr", "zip": "Zip"}, client=FakeClient(), workers=0)
        self.assertEqual(sorted(len(b) for b in client.batches), [1, 2500])
        self.assertEqual(pools, [2, 1, 1])

    def test_client_returning_wrong_number_of_results_fails(self):
        class ShortClient(FakeClient):
            def geocode_batch(self, addrs):
                return super().geocode_batch(addrs)[:-1]

        inp = self.write("Addr,Zip\n1 Main St,98101\n2 Main St,98101\n")
        with self.assertRaises(ValueError):
            self.run_csv(inp, {"street": "Addr", "zip": "Zip"}, client=ShortClient())


class PositiveIntTests(unittest.TestCase):
    def test_bounds(self):
        parse = geocode.positive_int(4)
        self.assertEqual([parse("1"), parse("4")], [1, 4])
        for bad in ("0", "5", "-1", "abc", ""):
            with self.subTest(value=bad), \
                    self.assertRaisesRegex(geocode.argparse.ArgumentTypeError, f"^must be 1-4, got {bad!r}$"):
                parse(bad)


class ParserContractTests(unittest.TestCase):
    """The CLI surface: every option's flags, default, required, type, choices and help."""

    def actions(self, parser):
        return {a.dest: a for a in parser._actions if a.dest != "help"}

    def check(self, parser, expected):
        actions = self.actions(parser)
        self.assertEqual(sorted(actions), sorted(expected))
        for dest, (flags, default, required, typ, choices, help_text) in expected.items():
            with self.subTest(dest=dest):
                a = actions[dest]
                self.assertEqual(
                    (a.option_strings, a.default, a.required, a.type, a.choices, a.help),
                    (flags, default, required, typ, choices, help_text))

    def setUp(self):
        self.parser = geocode.build_parser()
        sub = next(a for a in self.parser._actions if a.dest == "cmd")
        self.sub = sub
        self.csv, self.lookup = sub.choices["csv"], sub.choices["lookup"]

    def test_top_level(self):
        self.assertEqual(self.parser.description, "Geocode addresses with the US Census Bureau batch geocoder.")
        self.assertTrue(self.sub.required)
        self.assertEqual({c.dest: c.help for c in self.sub._choices_actions}, {
            "csv": "add location and Census geography columns to a CSV", "lookup": "geocode a single address"})
        self.check(self.parser, {
            "benchmark": (["--benchmark"], "Public_AR_Current", False, None, None,
                          "Census address benchmark (default: %(default)s)"),
            "vintage": (["--vintage"], "Current_Current", False, None, None,
                        "Census geography vintage (default: %(default)s)"),
            "cmd": ([], None, True, None, self.sub.choices, None),
        })

    def test_csv(self):
        actions = self.actions(self.csv)
        batch_type, workers_type = actions["batch_size"].type, actions["workers"].type
        self.check(self.csv, {
            "input": ([], None, True, Path, None, "input CSV"),
            "output": (["-o", "--output"], None, False, Path, None, "output CSV (default: <input>_geocoded.csv)"),
            "street_col": (["--street-col"], None, True, None, None, "column with the street address"),
            "city_col": (["--city-col"], None, False, None, None, "column with the city"),
            "state_col": (["--state-col"], None, False, None, None, "column with the state"),
            "zip_col": (["--zip-col"], None, False, None, None, "column with the ZIP code"),
            "secondary_col": (["--secondary-col"], None, False, None, None,
                              "column with apartment/suite/unit, if separate (appended to the street)"),
            "cluster_level": (["--cluster-level"], None, False, None, ("county", "tract", "block_group", "block"),
                              "also add geo_cluster: the GEOID of the address's county, tract, block group or block"),
            "batch_size": (["--batch-size"], 2500, False, batch_type, None,
                           "addresses per Census request (1-10000, default: %(default)s)"),
            "workers": (["--workers"], 2, False, workers_type, None,
                        "batches sent at the same time (1-4, default: %(default)s)"),
            "cache": (["--cache"], None, False, Path, None,
                      "JSON cache of results (default: <input>_geocode-cache.json)"),
            "no_cache": (["--no-cache"], False, False, None, None, "do not read or write a cache file"),
        })
        self.assertEqual(batch_type("10000"), 10000)
        self.assertRaises(geocode.argparse.ArgumentTypeError, batch_type, "10001")
        self.assertEqual(workers_type("4"), 4)
        self.assertRaises(geocode.argparse.ArgumentTypeError, workers_type, "5")

    def test_lookup(self):
        self.check(self.lookup, {
            "street": (["--street"], None, True, None, None, None),
            "city": (["--city"], "", False, None, None, None),
            "state": (["--state"], "", False, None, None, None),
            "zip": (["--zip"], "", False, None, None, None),
        })


class MainDetailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inp = self.tmp / "in.csv"
        self.inp.write_text("Addr,Town,ST,Zip\n1 Main St,Seattle,WA,98101\n9 Elm St,Seattle,WA,98101\n")
        self.client = FakeClient()
        self.client_kwargs = []

        def make(**kw):
            self.client_kwargs.append(kw)
            return self.client

        patcher = mock.patch.object(geocode, "CensusClient", make)
        patcher.start()
        self.addCleanup(patcher.stop)

    run_main = MainTests.run_main

    def test_benchmark_and_vintage_passed_to_client(self):
        self.run_main("lookup", "--street", "1 Main St", "--zip", "98101")
        self.run_main("--benchmark", "B", "--vintage", "V", "lookup", "--street", "1 Main St", "--zip", "98101")
        self.assertEqual(self.client_kwargs, [{"benchmark": "Public_AR_Current", "vintage": "Current_Current"},
                                              {"benchmark": "B", "vintage": "V"}])

    def test_lookup_output_and_address(self):
        code, out, _ = self.run_main("lookup", "--street", "1 Main St", "--city", "Seattle", "--state", "WA",
                                     "--zip", "98101-1234")
        self.assertEqual(code, 0)
        expected = FakeClient().geocode_batch([geocode.Address("1 Main St")])[0].columns()
        self.assertEqual(out, json.dumps(expected, indent=2) + "\n")
        self.assertEqual(self.client.batches, [[geocode.Address("1 Main St", "Seattle", "WA", "98101")]])

    def test_lookup_no_match_exits_1(self):
        code, out, _ = self.run_main("lookup", "--street", "9 Elm St", "--zip", "98101")
        self.assertEqual((code, json.loads(out)["geo_status"]), (1, geocode.NO_MATCH))

    def test_lookup_invalid_message(self):
        _, out, _ = self.run_main("lookup", "--street", "1 Main St", "--city", "Seattle")
        self.assertEqual(json.loads(out)["geo_matched_address"], "need a 5-digit ZIP, or a city and state")

    def test_csv_summary_paths_and_columns(self):
        out = self.tmp / "custom.csv"
        cache = self.tmp / "my-cache.json"
        with mock.patch.object(geocode, "process_csv", wraps=geocode.process_csv) as process:
            code, stdout, err = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--zip-col", "Zip",
                                              "-o", str(out), "--cache", str(cache), "--batch-size", "1",
                                              "--workers", "3", "--cluster-level", "tract")
        self.assertEqual(process.call_args.kwargs, {"batch_size": 1, "workers": 3, "cluster_level": "tract"})
        self.assertEqual((code, stdout), (0, ""))
        self.assertEqual(err.splitlines()[-1],
                         f"geocode: wrote {out} (census_lookups=2, matched=1, no_match=1)")
        self.assertEqual([len(b) for b in self.client.batches], [1, 1])
        self.assertEqual(sorted(json.loads(cache.read_text())), ["1 MAIN ST|||98101", "9 ELM ST|||98101"])
        self.assertFalse((self.tmp / "in_geocoded.csv").exists())
        self.assertFalse((self.tmp / "in_geocode-cache.json").exists())
        with out.open(newline="") as f:
            self.assertEqual([r["geo_cluster"] for r in csv.DictReader(f)], ["53033007101", ""])

    def test_csv_city_state_columns_and_secondary(self):
        self.inp.write_text("Addr,Unit,Town,ST\n1 Main St,Apt 2,Seattle,WA\n")
        code, _, _ = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--secondary-col", "Unit",
                                   "--city-col", "Town", "--state-col", "ST", "--no-cache")
        self.assertEqual(code, 0)
        self.assertEqual(self.client.batches, [[geocode.Address("1 Main St Apt 2", "Seattle", "WA", "")]])

    def test_csv_needs_zip_or_city_and_state(self):
        for args in (("--city-col", "Town"), ("--state-col", "ST"), ()):
            with self.subTest(args=args):
                code, _, err = self.run_main("csv", str(self.inp), "--street-col", "Addr", *args)
                self.assertEqual(code, 2)
                self.assertTrue(err.endswith("error: csv needs --zip-col, or both --city-col and --state-col\n"))
        code, _, _ = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--zip-col", "Zip", "--no-cache")
        self.assertEqual(code, 0)

    def test_csv_refuses_to_overwrite_input(self):
        code, _, err = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--zip-col", "Zip",
                                     "-o", str(self.tmp / "." / "in.csv"))
        self.assertEqual(code, 2)
        self.assertTrue(err.endswith("error: output would overwrite the input\n"))
        self.assertEqual(self.client.batches, [])


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
