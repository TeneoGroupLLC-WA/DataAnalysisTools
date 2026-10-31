import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import zip4  # noqa: E402


class FakeClient:
    """Stands in for USPSClient; returns canned results and counts calls."""

    def __init__(self, quota=None):
        self.calls = []
        self.quota = quota  # raise QuotaExceeded after this many lookups

    def lookup(self, addr):
        if self.quota is not None and len(self.calls) >= self.quota:
            raise zip4.QuotaExceeded("quota exceeded")
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


class ClusterTests(unittest.TestCase):
    def setUp(self):
        self.r = zip4.Result(zip4.MATCHED, zip5="98101", zip4="1234")

    def test_each_digit_count(self):
        self.assertEqual(self.r.cluster(0), "98101")
        self.assertEqual(self.r.cluster(1), "98101-1")
        self.assertEqual(self.r.cluster(2), "98101-12")
        self.assertEqual(self.r.cluster(3), "98101-123")
        self.assertEqual(self.r.cluster(4), "98101-1234")

    def test_full_digits_equals_zip_plus4(self):
        self.assertEqual(self.r.cluster(4), self.r.zip_plus4)

    def test_empty_unless_matched(self):
        for status in (zip4.NOT_FOUND, zip4.INVALID, zip4.ERROR):
            self.assertEqual(zip4.Result(status, zip5="98101").cluster(0), "")
        self.assertEqual(zip4.Result(zip4.MATCHED).cluster(2), "")

    def test_arg_accepts_0_to_4_only(self):
        for ok in ("0", "2", "4"):
            self.assertEqual(zip4.cluster_digits_arg(ok), int(ok))
        for bad in ("-1", "5", "two", ""):
            with self.assertRaises(zip4.argparse.ArgumentTypeError):
                zip4.cluster_digits_arg(bad)


def ok(address, matches=None):
    return 200, {"address": address, "matches": matches or []}, {}


class USPSClientTests(unittest.TestCase):
    """USPSClient with HTTP stubbed out: _request returns queued (status, body, headers)."""

    ADDR = zip4.Address("1 Main St", city="Seattle", state="wa", zip5="98101", secondary="Apt 2")
    TOKEN = (200, {"access_token": "tok", "expires_in": 3600}, {})

    def client(self, responses, rate_per_hour=0):
        c = zip4.USPSClient("id", "secret", base_url="https://usps.test/", rate_per_hour=rate_per_hour)
        self.requests = []
        queue = list(responses)

        def fake_request(req):
            self.requests.append(req)
            return queue.pop(0)

        c._request = fake_request
        return c

    def lookup_requests(self):
        return [r for r in self.requests if "/addresses/v3/address" in r.full_url]

    def test_match_parses_zip4_and_sends_params(self):
        c = self.client([self.TOKEN, ok({"ZIPCode": "98101", "ZIPPlus4": "4321"},
                                         [{"code": "31", "text": "exact match"}])])
        r = c.lookup(self.ADDR)
        self.assertEqual((r.status, r.zip5, r.zip4, r.message), (zip4.MATCHED, "98101", "4321", "exact match"))
        token_req, lookup_req = self.requests
        self.assertEqual(token_req.full_url, "https://usps.test/oauth2/v3/token")
        self.assertEqual(json.loads(token_req.data)["grant_type"], "client_credentials")
        query = dict(zip4.urllib.parse.parse_qsl(zip4.urllib.parse.urlsplit(lookup_req.full_url).query))
        self.assertEqual(query, {"streetAddress": "1 Main St", "secondaryAddress": "Apt 2", "city": "Seattle",
                                 "state": "WA", "ZIPCode": "98101"})
        self.assertEqual(lookup_req.get_header("Authorization"), "Bearer tok")

    def test_token_is_reused(self):
        addr = {"ZIPCode": "98101", "ZIPPlus4": "4321"}
        c = self.client([self.TOKEN, ok(addr), ok(addr)])
        c.lookup(self.ADDR)
        c.lookup(self.ADDR)
        self.assertEqual(len(self.requests), 3)  # one token request, two lookups

    def test_401_refreshes_token_and_retries(self):
        c = self.client([self.TOKEN, (401, {}, {}), (200, {"access_token": "tok2"}, {}),
                         ok({"ZIPCode": "98101", "ZIPPlus4": "4321"})])
        r = c.lookup(self.ADDR)
        self.assertEqual(r.status, zip4.MATCHED)
        self.assertEqual(self.lookup_requests()[-1].get_header("Authorization"), "Bearer tok2")

    def test_429_waits_and_retries(self):
        c = self.client([self.TOKEN, (429, {}, {"Retry-After": "7"}), ok({"ZIPCode": "98101", "ZIPPlus4": "4321"})])
        with mock.patch.object(zip4.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
            r = c.lookup(self.ADDR)
        sleep.assert_called_once_with(7.0)
        self.assertEqual(r.status, zip4.MATCHED)

    def test_gives_up_after_max_retries(self):
        c = self.client([self.TOKEN] + [(503, {}, {})] * 6)
        with mock.patch.object(zip4.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            r = c.lookup(self.ADDR)
        self.assertEqual(r.status, zip4.ERROR)
        self.assertEqual(len(self.lookup_requests()), 6)  # first try + 5 retries

    def test_429_after_max_retries_raises_quota_exceeded(self):
        c = self.client([self.TOKEN] + [(429, {"error": {"message": "Too Many Requests"}}, {})] * 6)
        with mock.patch.object(zip4.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(zip4.QuotaExceeded, "Too Many Requests"):
                c.lookup(self.ADDR)
        self.assertEqual(len(self.lookup_requests()), 6)

    def test_lookups_are_paced_to_rate(self):
        addr = {"ZIPCode": "98101", "ZIPPlus4": "4321"}
        c = self.client([self.TOKEN, ok(addr), ok(addr), ok(addr)], rate_per_hour=120)  # one per 30 s
        self.assertEqual(c.min_interval, 30.0)
        clock = [1000.0]
        with mock.patch.object(zip4.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(zip4.time, "sleep", side_effect=lambda t: clock.__setitem__(0, clock[0] + t)) as sleep:
            c.lookup(self.ADDR)            # first call: no wait
            clock[0] += 10                 # 10 s of other work
            c.lookup(self.ADDR)            # waits the remaining 20 s
            clock[0] += 45                 # longer than the interval
            c.lookup(self.ADDR)            # no wait
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [20.0])

    def test_rate_zero_disables_pacing(self):
        addr = {"ZIPCode": "98101", "ZIPPlus4": "4321"}
        c = self.client([self.TOKEN, ok(addr), ok(addr)], rate_per_hour=0)
        with mock.patch.object(zip4.time, "sleep") as sleep:
            c.lookup(self.ADDR)
            c.lookup(self.ADDR)
        sleep.assert_not_called()

    def test_default_rate_is_one_per_minute(self):
        self.assertEqual(zip4.USPSClient("id", "secret").min_interval, 60.0)

    def test_404_is_not_found(self):
        c = self.client([self.TOKEN, (404, {"error": {"message": "Address Not Found."}}, {})])
        r = c.lookup(self.ADDR)
        self.assertEqual((r.status, r.message), (zip4.NOT_FOUND, "Address Not Found."))

    def test_match_without_plus4_is_not_found(self):
        c = self.client([self.TOKEN, ok({"ZIPCode": "98101", "ZIPPlus4": None})])
        r = c.lookup(self.ADDR)
        self.assertEqual((r.status, r.zip5, r.zip4), (zip4.NOT_FOUND, "98101", ""))

    def test_invalid_input_makes_no_request(self):
        c = self.client([])
        r = c.lookup(zip4.Address("1 Main St", state="WA"))
        self.assertEqual(r.status, zip4.INVALID)
        self.assertEqual(self.requests, [])

    def test_token_failure_raises(self):
        c = self.client([(401, {"error": {"message": "bad client"}}, {})])
        with self.assertRaisesRegex(RuntimeError, "bad client"):
            c.lookup(self.ADDR)

    def test_retry_after_fallback_backoff(self):
        self.assertEqual(zip4.retry_after({}, 0), 5.0)
        self.assertEqual(zip4.retry_after({}, 2), 20.0)
        self.assertEqual(zip4.retry_after({}, 10), 300.0)
        self.assertEqual(zip4.retry_after({"retry-after": "junk"}, 1), 10.0)
        self.assertEqual(zip4.retry_after({"Retry-After": "120"}, 0), 120.0)
        self.assertEqual(zip4.retry_after({"Retry-After": "99999"}, 0), zip4.MAX_RETRY_WAIT)


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
        self.assertEqual(len(client.calls), 2)  # A and B share one lookup; D is invalid, never sent
        self.assertEqual(counts["usps_lookups"], 2)
        self.assertEqual(counts[zip4.MATCHED], 2)

    def test_cache_skips_lookups_on_rerun(self):
        cache_path = self.tmp / "cache.json"
        zip4.process_csv(self.inp, self.tmp / "o1.csv", self.cols, FakeClient(), zip4.Cache(cache_path))
        client = FakeClient()
        zip4.process_csv(self.inp, self.tmp / "o2.csv", self.cols, client, zip4.Cache(cache_path))
        self.assertEqual(client.calls, [])
        self.assertEqual(self.read(self.tmp / "o2.csv")[0]["zip_plus4"], "98101-1234")

    def test_no_cluster_column_by_default(self):
        out = self.tmp / "out.csv"
        zip4.process_csv(self.inp, out, self.cols, FakeClient(), zip4.Cache(None))
        self.assertNotIn(zip4.CLUSTER_COLUMN, self.read(out)[0])

    def test_cluster_digits_adds_column(self):
        out = self.tmp / "out.csv"
        zip4.process_csv(self.inp, out, self.cols, FakeClient(), zip4.Cache(None), cluster_digits=2)
        rows = self.read(out)
        self.assertEqual(list(rows[0])[-1], zip4.CLUSTER_COLUMN)
        self.assertEqual([r[zip4.CLUSTER_COLUMN] for r in rows], ["98101-12", "98101-12", "", ""])
        self.assertEqual(rows[0]["zip_plus4"], "98101-1234")  # full code is still kept

    def test_cluster_zero_digits_is_zip5(self):
        out = self.tmp / "out.csv"
        zip4.process_csv(self.inp, out, self.cols, FakeClient(), zip4.Cache(None), cluster_digits=0)
        self.assertEqual(self.read(out)[0][zip4.CLUSTER_COLUMN], "98101")

    def test_cluster_from_cache_without_new_lookups(self):
        cache_path = self.tmp / "cache.json"
        zip4.process_csv(self.inp, self.tmp / "o1.csv", self.cols, FakeClient(), zip4.Cache(cache_path))
        client = FakeClient()
        zip4.process_csv(self.inp, self.tmp / "o2.csv", self.cols, client, zip4.Cache(cache_path),
                         cluster_digits=3)
        self.assertEqual(client.calls, [])
        self.assertEqual(self.read(self.tmp / "o2.csv")[0][zip4.CLUSTER_COLUMN], "98101-123")

    def test_existing_output_column_is_refused(self):
        inp = self.tmp / "clash.csv"
        inp.write_text("Addr,Town,ST,zip_cluster\n1 Main St,Seattle,WA,x\n")
        with self.assertRaises(SystemExit) as cm:
            zip4.process_csv(inp, self.tmp / "out.csv", self.cols, FakeClient(), zip4.Cache(None),
                             cluster_digits=2)
        self.assertIn("zip_cluster", str(cm.exception))

    def test_quota_marks_rest_pending_and_rerun_finishes(self):
        cache_path = self.tmp / "cache.json"
        out = self.tmp / "out.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            counts = zip4.process_csv(self.inp, out, self.cols, FakeClient(quota=1), zip4.Cache(cache_path))
        rows = self.read(out)
        self.assertEqual([r["zip4_status"] for r in rows],
                         [zip4.MATCHED, zip4.MATCHED, zip4.PENDING, zip4.INVALID])  # B reuses A's result
        self.assertEqual(counts[zip4.PENDING], 1)
        self.assertNotIn("9 NOWHERE RD", cache_path.read_text().upper())  # pending is never cached

        client = FakeClient()
        with contextlib.redirect_stderr(io.StringIO()):
            counts = zip4.process_csv(self.inp, out, self.cols, client, zip4.Cache(cache_path))
        self.assertEqual([a.street for a in client.calls], ["9 Nowhere Rd"])  # only the pending one
        self.assertNotIn(zip4.PENDING, counts)
        self.assertEqual(self.read(out)[2]["zip4_status"], zip4.NOT_FOUND)

    def test_error_results_are_not_cached(self):
        cache = zip4.Cache(None)
        cache.put("k", zip4.Result(zip4.ERROR, message="HTTP 503"))
        self.assertIsNone(cache.get("k"))

    def test_missing_column_is_reported(self):
        cols = dict(self.cols, city="City")
        with self.assertRaises(SystemExit) as cm:
            zip4.process_csv(self.inp, self.tmp / "out.csv", cols, FakeClient(), zip4.Cache(None))
        self.assertIn("City", str(cm.exception))



class MainTests(unittest.TestCase):
    """Command-line parsing; USPS is replaced by FakeClient."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.inp = self.tmp / "in.csv"
        self.inp.write_text("Addr,Town,ST\n1 Main St,Seattle,WA\n")
        patcher = mock.patch.object(zip4, "make_client", lambda args: FakeClient())
        patcher.start()
        self.addCleanup(mock.patch.stopall)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = zip4.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_csv_default_output_name_and_cluster(self):
        code, _, _ = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--city-col", "Town",
                                   "--state-col", "ST", "--cluster-digits", "1")
        self.assertEqual(code, 0)
        with (self.tmp / "in_zip4.csv").open(newline="") as f:
            self.assertEqual(next(csv.DictReader(f))["zip_cluster"], "98101-1")

    def test_csv_writes_default_cache(self):
        self.run_main("csv", str(self.inp), "--street-col", "Addr", "--city-col", "Town", "--state-col", "ST")
        self.assertTrue((self.tmp / "in_zip4-cache.json").exists())

    def test_csv_no_cache(self):
        self.run_main("csv", str(self.inp), "--street-col", "Addr", "--city-col", "Town", "--state-col", "ST",
                      "--no-cache")
        self.assertFalse((self.tmp / "in_zip4-cache.json").exists())

    def test_csv_exits_3_when_rows_pending(self):
        with mock.patch.object(zip4, "make_client", lambda args: FakeClient(quota=0)):
            code, _, err = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--city-col", "Town",
                                         "--state-col", "ST")
        self.assertEqual(code, 3)
        self.assertIn("rerun", err)

    def test_rate_per_hour_reaches_client(self):
        seen = {}
        with mock.patch.dict(zip4.os.environ, {"USPS_CLIENT_ID": "id", "USPS_CLIENT_SECRET": "s"}):
            mock.patch.stopall()  # use the real make_client
            with mock.patch.object(zip4, "process_csv", lambda *a, **k: seen.setdefault("client", a[3]) and {}):
                self.run_main("--rate-per-hour", "600", "csv", str(self.inp), "--street-col", "Addr",
                              "--city-col", "Town", "--state-col", "ST")
        self.assertEqual(seen["client"].min_interval, 6.0)

    def test_negative_rate_is_rejected(self):
        code, _, err = self.run_main("--rate-per-hour", "-1", "csv", str(self.inp), "--street-col", "Addr",
                                     "--city-col", "Town", "--state-col", "ST")
        self.assertEqual(code, 2)
        self.assertIn("--rate-per-hour", err)

    def test_csv_rejects_bad_cluster_digits(self):
        code, _, err = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--city-col", "Town",
                                     "--state-col", "ST", "--cluster-digits", "5")
        self.assertEqual(code, 2)
        self.assertIn("must be 0-4", err)

    def test_csv_needs_city_or_zip_column(self):
        code, _, err = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--state-col", "ST")
        self.assertEqual(code, 2)
        self.assertIn("--city-col or --zip-col", err)

    def test_csv_refuses_to_overwrite_input(self):
        code, _, err = self.run_main("csv", str(self.inp), "-o", str(self.inp), "--street-col", "Addr",
                                     "--city-col", "Town", "--state-col", "ST")
        self.assertEqual(code, 2)
        self.assertIn("overwrite", err)

    def test_lookup_prints_json_with_cluster(self):
        code, out, _ = self.run_main("lookup", "--street", "1 Main St", "--city", "Seattle", "--state", "WA",
                                     "--cluster-digits", "2")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual((data["zip_plus4"], data["zip_cluster"]), ("98101-1234", "98101-12"))

    def test_lookup_not_found_exits_1(self):
        code, out, _ = self.run_main("lookup", "--street", "9 Nowhere Rd", "--city", "Seattle", "--state", "WA")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], zip4.NOT_FOUND)
        self.assertNotIn("zip_cluster", json.loads(out))


if __name__ == "__main__":
    unittest.main()
