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

import zip4


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


class StubbedClient:
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


class USPSClientTests(StubbedClient, unittest.TestCase):

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
        with mock.patch.object(zip4.time, "sleep"), contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(zip4.QuotaExceeded, "Too Many Requests"):
            c.lookup(self.ADDR)
        self.assertEqual(len(self.lookup_requests()), 6)

    def test_lookups_are_paced_to_rate(self):
        addr = {"ZIPCode": "98101", "ZIPPlus4": "4321"}
        c = self.client([self.TOKEN, ok(addr), ok(addr), ok(addr)], rate_per_hour=120)  # one per 30 s
        self.assertEqual(c.min_interval, 30.0)
        clock = [1000.0]
        advance = lambda t: clock.__setitem__(0, clock[0] + t)  # noqa: E731
        with mock.patch.object(zip4.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(zip4.time, "sleep", side_effect=advance) as sleep:
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
        self.assertEqual(r, zip4.Result("invalid_input", message="need a city or a 5-digit ZIP"))
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


class MainHelpers:
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


class MainTests(MainHelpers, unittest.TestCase):

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



class ConstantsTests(unittest.TestCase):
    """Values that users see in output files and on the command line."""

    def test_status_values_and_columns(self):
        self.assertEqual((zip4.MATCHED, zip4.NOT_FOUND, zip4.INVALID, zip4.ERROR, zip4.PENDING),
                         ("matched", "not_found", "invalid_input", "error", "pending"))
        self.assertEqual(zip4.OUT_COLUMNS, ("zip4", "zip_plus4", "zip4_status", "zip4_message"))
        self.assertEqual(zip4.CLUSTER_COLUMN, "zip_cluster")

    def test_defaults(self):
        self.assertEqual(zip4.DEFAULT_BASE_URL, "https://apis.usps.com")
        self.assertEqual(zip4.DEFAULT_RATE_PER_HOUR, 60.0)
        self.assertEqual(zip4.MAX_RETRY_WAIT, 3600.0)
        self.assertEqual(zip4.USER_AGENT, "TeneoGroupLLC-DataAnalysisTools-zip4/1.0")


class AddressAndResultTests(unittest.TestCase):
    def test_key_normalizes_case_and_spaces(self):
        a = zip4.Address(" 1  main st ", city="seattle", state="wa", zip5="98101", secondary="apt  2")
        self.assertEqual(a.key(), "1 MAIN ST|APT 2|SEATTLE|WA|98101")
        self.assertEqual(zip4.Address("1 Main St").key(), "1 MAIN ST||||")

    def test_zip_plus4_needs_both_parts(self):
        self.assertEqual(zip4.Result(zip4.MATCHED, zip5="98101", zip4="1234").zip_plus4, "98101-1234")
        self.assertEqual(zip4.Result(zip4.MATCHED, zip5="98101").zip_plus4, "")
        self.assertEqual(zip4.Result(zip4.MATCHED, zip4="1234").zip_plus4, "")

    def test_json_round_trip(self):
        r = zip4.Result(zip4.MATCHED, zip5="98101", zip4="1234", message="exact")
        self.assertEqual(r.to_json(), {"status": "matched", "zip5": "98101", "zip4": "1234", "message": "exact"})
        self.assertEqual(zip4.Result.from_json(r.to_json()), r)

    def test_from_json_defaults(self):
        self.assertEqual(zip4.Result.from_json({}), zip4.Result("error", "", "", ""))

    def test_clean_zip5_edge_cases(self):
        self.assertEqual(zip4.clean_zip5(None), "")
        self.assertEqual(zip4.clean_zip5("123"), "00123")
        self.assertEqual(zip4.clean_zip5("  98101 "), "98101")
        self.assertEqual(zip4.clean_zip5("ZIP 02134-0001"), "02134")

    def test_validate_messages(self):
        self.assertEqual(zip4.validate(zip4.Address("  ", city="Seattle", state="WA")), "street is empty")
        self.assertEqual(zip4.validate(zip4.Address("1 Main St", city="Seattle", state=" ")), "state is empty")
        self.assertEqual(zip4.validate(zip4.Address("1 Main St", city=" ", state="WA")),
                         "need a city or a 5-digit ZIP")

    def test_cluster_digits_arg_message(self):
        with self.assertRaisesRegex(zip4.argparse.ArgumentTypeError, r"^must be 0-4, got '5'$"):
            zip4.cluster_digits_arg("5")
        with self.assertRaisesRegex(zip4.argparse.ArgumentTypeError, r"^must be 0-4, got 'x'$"):
            zip4.cluster_digits_arg("x")


class MessageTests(unittest.TestCase):
    def test_error_message_shapes(self):
        self.assertEqual(zip4.error_message({"error": "invalid_client"}), "invalid_client")
        self.assertEqual(zip4.error_message({"message": "Unauthorized"}), "Unauthorized")
        self.assertEqual(zip4.error_message({}), "unknown error")
        self.assertEqual(zip4.error_message({"error": {}}), "unknown error")
        self.assertEqual(zip4.error_message({"error": {"message": None, "errors": None}}), "unknown error")
        body = {"error": {"message": "", "errors": [{"title": "Bad state"}, "junk", {"detail": "", "title": ""},
                                                    {"detail": "d2", "title": "t2"}]}}
        self.assertEqual(zip4.error_message(body), "Bad state; d2")

    def test_match_text_joins_and_skips(self):
        body = {"matches": [{"text": "one"}, "junk", {"code": "1"}, {"text": ""}, {"text": "two"}]}
        self.assertEqual(zip4.match_text(body), "one; two")
        self.assertEqual(zip4.match_text({"matches": None}), "")
        self.assertEqual(zip4.match_text({}), "")

    def test_retry_after_bounds(self):
        self.assertEqual(zip4.retry_after({"Retry-After": "0"}, 3), 1.0)
        self.assertEqual(zip4.retry_after({"Retry-After": "2.5"}, 3), 2.5)
        self.assertEqual(zip4.retry_after({"X-Other": "9"}, 1), 10.0)
        self.assertEqual(zip4.retry_after({}, 5), 160.0)
        self.assertEqual(zip4.retry_after({}, 6), 300.0)


class FakeResponse:
    def __init__(self, status, body, headers):
        self.status, self._body, self.headers = status, body, headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body, headers=None):
    return zip4.urllib.error.HTTPError("https://usps.test/x", code, "err", headers, io.BytesIO(body))


class RequestTests(unittest.TestCase):
    """USPSClient._request against a stubbed urlopen."""

    def setUp(self):
        self.c = zip4.USPSClient("id", "secret", base_url="https://usps.test", timeout=12.5)
        self.req = zip4.urllib.request.Request("https://usps.test/x")

    def send(self, effect):
        if isinstance(effect, zip4.urllib.error.HTTPError):
            self.addCleanup(effect.close)
        with mock.patch.object(zip4.urllib.request, "urlopen", side_effect=[effect]) as urlopen:
            result = self.c._request(self.req)
        urlopen.assert_called_once_with(self.req, timeout=12.5)
        return result

    def test_success(self):
        self.assertEqual(self.send(FakeResponse(200, b'{"a": 1}', {"H": "v"})), (200, {"a": 1}, {"H": "v"}))

    def test_success_empty_body(self):
        self.assertEqual(self.send(FakeResponse(204, b"", {})), (204, {}, {}))

    def test_http_error_json(self):
        self.assertEqual(self.send(http_error(404, b'{"error": {"message": "nope"}}', {"Retry-After": "3"})),
                         (404, {"error": {"message": "nope"}}, {"Retry-After": "3"}))

    def test_http_error_empty_body_and_no_headers(self):
        self.assertEqual(self.send(http_error(500, b"")), (500, {}, {}))

    def test_http_error_text_body_is_truncated(self):
        status, body, _ = self.send(http_error(502, b"<html>" + b"x" * 300))
        self.assertEqual(status, 502)
        self.assertEqual(body, {"error": {"message": "<html>" + "x" * 194}})


class ClientSetupTests(unittest.TestCase):
    def test_rejects_non_https_base_url(self):
        for url in ("http://apis.usps.com", "file:///etc/passwd", "apis.usps.com"):
            with self.assertRaisesRegex(ValueError, "must start with https://"):
                zip4.USPSClient("id", "secret", base_url=url)

    def test_defaults_and_rate(self):
        c = zip4.USPSClient("id", "secret", base_url="https://usps.test///")
        self.assertEqual((c.base_url, c.max_retries, c.timeout), ("https://usps.test", 5, 30.0))
        self.assertEqual(zip4.USPSClient("i", "s", rate_per_hour=7200).min_interval, 0.5)
        self.assertEqual(zip4.USPSClient("i", "s", rate_per_hour=-5).min_interval, 0.0)
        self.assertEqual(zip4.USPSClient("i", "s", rate_per_hour=0.5).min_interval, 7200.0)

    def test_default_timeout_reaches_urlopen(self):
        c = zip4.USPSClient("id", "secret")
        with mock.patch.object(zip4.urllib.request, "urlopen", return_value=FakeResponse(200, b"{}", {})) as u:
            c._request(zip4.urllib.request.Request("https://usps.test/x"))
        self.assertEqual(u.call_args.kwargs, {"timeout": 30.0})


class USPSClientMoreTests(StubbedClient, unittest.TestCase):

    def test_token_request_contents(self):
        c = self.client([self.TOKEN, ok({"ZIPCode": "98101", "ZIPPlus4": "4321"})])
        c.lookup(self.ADDR)
        token_req = self.requests[0]
        self.assertEqual(token_req.get_method(), "POST")
        self.assertEqual(json.loads(token_req.data),
                         {"grant_type": "client_credentials", "client_id": "id", "client_secret": "secret"})
        self.assertEqual(token_req.get_header("Content-type"), "application/json")
        self.assertEqual(token_req.get_header("User-agent"), zip4.USER_AGENT)

    def test_lookup_request_url_and_headers(self):
        c = self.client([self.TOKEN, ok({"ZIPCode": "98101", "ZIPPlus4": "4321"})])
        c.lookup(zip4.Address(" 1 Main St ", city=" ", state=" wa ", zip5="98101", secondary="  "))
        req = self.lookup_requests()[0]
        self.assertEqual(req.full_url,
                         "https://usps.test/addresses/v3/address?streetAddress=1+Main+St&state=WA&ZIPCode=98101")
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Accept"), "application/json")
        self.assertEqual(req.get_header("User-agent"), zip4.USER_AGENT)

    def test_lookup_without_zip_sends_city_only(self):
        c = self.client([self.TOKEN, ok({"ZIPCode": "98101", "ZIPPlus4": "4321"})])
        c.lookup(zip4.Address("1 Main St", city=" Seattle ", state="WA"))
        self.assertTrue(self.lookup_requests()[0].full_url.endswith("?streetAddress=1+Main+St&state=WA&city=Seattle"))

    def test_token_expiry_window(self):
        addr = {"ZIPCode": "98101", "ZIPPlus4": "4321"}
        c = self.client([(200, {"access_token": "t1", "expires_in": 120}, {}), ok(addr), ok(addr),
                         (200, {"access_token": "t2"}, {}), ok(addr), ok(addr), (200, {"access_token": "t3"}, {}),
                         ok(addr)])
        now = [1000.0]
        with mock.patch.object(zip4.time, "time", lambda: now[0]):
            c.lookup(self.ADDR)          # token t1 expires at 1120, refreshed from 1060
            now[0] = 1059.0
            c.lookup(self.ADDR)
            now[0] = 1060.0
            c.lookup(self.ADDR)          # t2, no expires_in: assumed 3600 s, so expires at 4660
            now[0] = 4599.0
            c.lookup(self.ADDR)
            now[0] = 4600.0
            c.lookup(self.ADDR)
        self.assertEqual([r.get_header("Authorization") for r in self.lookup_requests()],
                         ["Bearer t1", "Bearer t1", "Bearer t2", "Bearer t2", "Bearer t3"])

    def test_token_failures(self):
        cases = [((200, {"token": "x"}, {}), "HTTP 200"), ((500, {"access_token": "x"}, {}), "HTTP 500"),
                 ((401, {"error": {"message": "bad client"}}, {}), "HTTP 401")]
        for resp, http in cases:
            c = self.client([resp])
            with self.assertRaises(RuntimeError) as cm:
                c.lookup(self.ADDR)
            self.assertTrue(str(cm.exception).startswith(f"USPS token request failed ({http}): "), str(cm.exception))
        self.assertEqual(str(cm.exception), "USPS token request failed (HTTP 401): bad client")

    def test_match_with_missing_fields(self):
        c = self.client([self.TOKEN, ok({"ZIPCode": None, "ZIPPlus4": "4321"}, [{"text": "m"}])])
        self.assertEqual(c.lookup(self.ADDR), zip4.Result("matched", zip5="", zip4="4321", message="m"))

    def test_no_address_in_body_is_not_found(self):
        c = self.client([self.TOKEN, (200, {"address": None}, {})])
        self.assertEqual(c.lookup(self.ADDR),
                         zip4.Result("not_found", zip5="", zip4="", message="USPS returned no ZIP+4 for this address"))

    def test_401_on_last_attempt_is_error(self):
        c = self.client([self.TOKEN, (401, {}, {}), (200, {"access_token": "t2"}, {}), (401, {}, {})])
        c.max_retries = 1
        self.assertEqual(c.lookup(self.ADDR), zip4.Result("error", message="HTTP 401: unknown error"))
        self.assertEqual(len(self.lookup_requests()), 2)

    def test_429_backoff_and_message(self):
        c = self.client([self.TOKEN, (429, {}, {}), (429, {}, {}), ok({"ZIPCode": "98101", "ZIPPlus4": "4321"})])
        err = io.StringIO()
        with mock.patch.object(zip4.time, "sleep") as sleep, contextlib.redirect_stderr(err):
            self.assertEqual(c.lookup(self.ADDR).status, "matched")
        self.assertEqual([call.args for call in sleep.call_args_list], [(5.0,), (10.0,)])
        self.assertEqual(err.getvalue(), "zip4: USPS rate limit (HTTP 429), waiting 5s before retrying\n"
                                         "zip4: USPS rate limit (HTTP 429), waiting 10s before retrying\n")

    def test_429_sleeps_between_every_retry(self):
        c = self.client([self.TOKEN] + [(429, {}, {"Retry-After": "2"})] * 6)
        with mock.patch.object(zip4.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(zip4.QuotaExceeded, "^unknown error$"):
            c.lookup(self.ADDR)
        self.assertEqual([call.args for call in sleep.call_args_list], [(2.0,)] * 5)

    def test_server_errors_are_retried(self):
        for status in (500, 502, 503, 504):
            c = self.client([self.TOKEN, (status, {}, {"Retry-After": "4"}), ok({"ZIPCode": "98101", "ZIPPlus4": "1"})])
            err = io.StringIO()
            with mock.patch.object(zip4.time, "sleep") as sleep, contextlib.redirect_stderr(err):
                self.assertEqual(c.lookup(self.ADDR).status, "matched")
            sleep.assert_called_once_with(4.0)
            self.assertEqual(err.getvalue(), f"zip4: HTTP {status} from USPS, waiting 4s before retrying\n")

    def test_server_error_backoff_grows(self):
        c = self.client([self.TOKEN] + [(503, {}, {})] * 6)
        with mock.patch.object(zip4.time, "sleep") as sleep, contextlib.redirect_stderr(io.StringIO()):
            r = c.lookup(self.ADDR)
        self.assertEqual(r, zip4.Result("error", message="HTTP 503: unknown error"))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5.0, 10.0, 20.0, 40.0, 80.0])

    def test_other_statuses_are_not_retried(self):
        for status, expected in ((400, zip4.Result("not_found", message="bad")),
                                 (404, zip4.Result("not_found", message="bad")),
                                 (501, zip4.Result("error", message="HTTP 501: bad")),
                                 (403, zip4.Result("error", message="HTTP 403: bad"))):
            c = self.client([self.TOKEN, (status, {"error": {"message": "bad"}}, {})])
            with mock.patch.object(zip4.time, "sleep") as sleep:
                self.assertEqual(c.lookup(self.ADDR), expected)
            sleep.assert_not_called()

    def test_pacing_waits_only_when_needed(self):
        addr = {"ZIPCode": "98101", "ZIPPlus4": "4321"}
        c = self.client([self.TOKEN, ok(addr), ok(addr), ok(addr)], rate_per_hour=120)
        clock = [1000.0]
        with mock.patch.object(zip4.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(zip4.time, "sleep") as sleep:
            c.lookup(self.ADDR)
            clock[0] += 30           # exactly the interval: no wait
            c.lookup(self.ADDR)
            clock[0] += 29.5
            c.lookup(self.ADDR)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "cache.json"

    def test_save_format_and_reload(self):
        c = zip4.Cache(self.path)
        c.put("b", zip4.Result("matched", "98101", "1234", "m"))
        c.put("a", zip4.Result("not_found", message="x"))
        c.save()
        self.assertEqual(self.path.read_text(), json.dumps(
            {"a": {"message": "x", "status": "not_found", "zip4": "", "zip5": ""},
             "b": {"message": "m", "status": "matched", "zip4": "1234", "zip5": "98101"}}, indent=1, sort_keys=True))
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["cache.json"])
        self.assertEqual(zip4.Cache(self.path).get("b"), zip4.Result("matched", "98101", "1234", "m"))

    def test_missing_file_and_no_path(self):
        self.assertEqual(zip4.Cache(self.path).data, {})
        c = zip4.Cache(None)
        c.put("a", zip4.Result("matched", "98101", "1234"))
        c.save()  # nothing to write to
        self.assertEqual(c.get("a"), zip4.Result("matched", "98101", "1234"))
        self.assertIsNone(c.get("missing"))


class CountingCache(zip4.Cache):
    def __init__(self):
        super().__init__(None)
        self.saves = 0

    def save(self):
        self.saves += 1


class ProcessCsvMoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cols = {"street": "Addr", "city": "Town", "state": "ST", "zip": "Postal", "secondary": "Unit"}

    def write(self, text, name="in.csv"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_csv(self, inp, client=None, cache=None, **kw):
        out = self.tmp / "out.csv"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            counts = zip4.process_csv(inp, out, self.cols, client or FakeClient(), cache or zip4.Cache(None), **kw)
        return counts, out.read_text(encoding="utf-8"), err.getvalue()

    def test_exact_output_and_counts(self):
        inp = self.write("Name,Addr,Unit,Town,ST,Postal\n"
                         "A,1 Main St,Apt 2,Seattle,WA,98101\n"
                         "B,9 Nowhere Rd,,Seattle,WA,\n"
                         "C,,,Seattle,WA,98101\n"
                         "D,5 Pine St\n")
        client = FakeClient()
        counts, text, err = self.run_csv(inp, client)
        self.assertEqual(text.splitlines(), [
            "Name,Addr,Unit,Town,ST,Postal,zip4,zip_plus4,zip4_status,zip4_message",
            "A,1 Main St,Apt 2,Seattle,WA,98101,1234,98101-1234,matched,exact match",
            "B,9 Nowhere Rd,,Seattle,WA,,,,not_found,Address Not Found.",
            "C,,,Seattle,WA,98101,,,invalid_input,street is empty",
            "D,5 Pine St,,,,,,,invalid_input,state is empty",
        ])
        self.assertEqual(counts, {"matched": 1, "not_found": 1, "invalid_input": 2, "usps_lookups": 2})
        self.assertEqual(client.calls[0], zip4.Address("1 Main St", "Seattle", "WA", "98101", "Apt 2"))
        self.assertEqual(err, "zip4: 4 rows, 2 new addresses to look up\n")

    def test_eta_uses_client_interval(self):
        inp = self.write("Addr,Unit,Town,ST,Postal\n1 Main St,,Seattle,WA,\n")
        client = FakeClient()
        client.min_interval = 180.0
        _, _, err = self.run_csv(inp, client)
        self.assertEqual(err, "zip4: 1 rows, 1 new addresses to look up, about 0.1 h at the current rate\n")

    def test_nothing_new_prints_no_plan(self):
        inp = self.write("Addr,Unit,Town,ST,Postal\n,,Seattle,WA,\n")
        counts, _, err = self.run_csv(inp)
        self.assertEqual((counts, err), ({"invalid_input": 1, "usps_lookups": 0}, ""))

    def test_cache_saved_every_25_lookups_and_at_end(self):
        for n in (24, 25, 26):
            rows = "".join(f"{i} Main St,,Seattle,WA,\n" for i in range(n))
            cache = CountingCache()
            counts, _, _ = self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n" + rows), cache=cache)
            self.assertEqual((counts["usps_lookups"], cache.saves), (n, 1 if n < 25 else 2))
            self.assertEqual(len(cache.data), n)

    def test_save_every_is_honored(self):
        rows = "".join(f"{i} Main St,,Seattle,WA,\n" for i in range(5))
        cache = CountingCache()
        self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n" + rows), cache=cache, save_every=2)
        self.assertEqual(cache.saves, 3)  # after lookups 2 and 4, then at the end

    def test_progress_every_50_rows(self):
        rows = "".join(",,Seattle,WA,\n" for _ in range(100))
        _, _, err = self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n" + rows))
        self.assertEqual(err, "zip4: 50/100 rows\nzip4: 100/100 rows\n")
        _, _, err = self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n" + rows[: -len(",,Seattle,WA,\n")]))
        self.assertEqual(err, "zip4: 50/99 rows\n")

    def test_error_results_are_reused_within_a_run(self):
        class Failing(FakeClient):
            def lookup(self, addr):
                self.calls.append(addr)
                return zip4.Result(zip4.ERROR, message="HTTP 500: boom")
        client = Failing()
        counts, _, _ = self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n1 Main St,,Seattle,WA,\n"
                                               "1 Main St,,Seattle,WA,\n"), client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(counts, {"error": 2, "usps_lookups": 1})

    def test_plan_counts_only_uncached_addresses(self):
        cache = zip4.Cache(None)
        cache.put("1 MAIN ST||SEATTLE|WA|", zip4.Result("matched", "98101", "1111"))
        _, text, err = self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n1 Main St,,Seattle,WA,\n"
                                               "2 Main St,,Seattle,WA,\n"), cache=cache)
        self.assertEqual(err, "zip4: 2 rows, 1 new addresses to look up\n")
        self.assertIn("1 Main St,,Seattle,WA,,1111,98101-1111,matched,", text)

    def test_quoted_line_break_in_a_field_is_kept(self):
        inp = self.tmp / "crlf.csv"
        inp.write_bytes(b'Addr,Unit,Town,ST,Postal\r\n"1 Main St\r\nRear",,Seattle,WA,\r\n')
        client = FakeClient()
        self.run_csv(inp, client)
        self.assertEqual(client.calls[0].street, "1 Main St\r\nRear")

    def test_quota_messages(self):
        inp = self.write("Addr,Unit,Town,ST,Postal\n1 Main St,,Seattle,WA,\n2 Main St,,Seattle,WA,\n"
                         "1 Main St,,Seattle,WA,\n")
        client = FakeClient(quota=0)
        counts, text, err = self.run_csv(inp, client)
        pending = "pending,not looked up yet (USPS quota); rerun to finish"
        self.assertEqual(text.splitlines()[1:], [f"1 Main St,,Seattle,WA,,,,{pending}",
                                                 f"2 Main St,,Seattle,WA,,,,{pending}",
                                                 f"1 Main St,,Seattle,WA,,,,{pending}"])
        self.assertEqual(counts, {"pending": 3, "usps_lookups": 0})
        self.assertEqual(err, "zip4: 3 rows, 2 new addresses to look up\n"
                              "zip4: USPS quota exhausted (quota exceeded); stopping lookups\n"
                              "zip4: rerun the same command later to look up the pending rows\n")

    def test_quota_without_message(self):
        class Silent:
            def lookup(self, addr):
                raise zip4.QuotaExceeded()
        _, _, err = self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n1 Main St,,Seattle,WA,\n"), Silent())
        self.assertIn("zip4: USPS quota exhausted (rate limit); stopping lookups\n", err)

    def test_column_errors_are_exact(self):
        inp = self.write("Addr,Town\n1 Main St,Seattle\n")
        with self.assertRaises(SystemExit) as cm:
            self.run_csv(inp)
        self.assertEqual(str(cm.exception), "zip4: column(s) not in in.csv: ST, Postal, Unit\n"
                                            "zip4: available columns: Addr, Town")
        self.cols = {"street": "Addr", "city": "Town"}
        inp = self.write("Addr,Town,zip4,zip4_message\n1 Main St,Seattle,x,y\n")
        with self.assertRaises(SystemExit) as cm:
            self.run_csv(inp)
        self.assertEqual(str(cm.exception), "zip4: input already has output column(s): zip4, zip4_message")

    def test_empty_file(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_csv(self.write(""))
        self.assertTrue(str(cm.exception).endswith("zip4: available columns: "))

    def test_reads_bom_input_and_writes_plain_utf8(self):
        inp = self.tmp / "bom.csv"
        inp.write_text("Addr,Unit,Town,ST,Postal\n1 Rue Été,,Seattle,WA,\n", encoding="utf-8-sig")
        _, text, _ = self.run_csv(inp)
        self.assertTrue(text.startswith("Addr,"))
        self.assertIn("1 Rue Été", text)
        self.assertFalse((self.tmp / "out.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_lookup_results_are_cached(self):
        cache = zip4.Cache(None)
        self.run_csv(self.write("Addr,Unit,Town,ST,Postal\n1 Main St,,Seattle,WA,\n"), cache=cache)
        self.assertEqual(cache.get("1 MAIN ST||SEATTLE|WA|"), zip4.Result("matched", "98101", "1234", "exact match"))


class ParserContractTests(unittest.TestCase):
    """The command-line interface, option by option."""

    def actions(self, parser):
        return [(a.option_strings, a.dest, a.default, a.required, a.type, a.metavar, a.nargs, a.help)
                for a in parser._actions if not isinstance(a, (zip4.argparse._HelpAction,
                                                               zip4.argparse._SubParsersAction))]

    def parser(self, env=None):
        with mock.patch.dict(zip4.os.environ, env or {}, clear=True):
            return zip4.build_parser()

    def test_top_level(self):
        p = self.parser()
        self.assertEqual(p.description, "Add ZIP+4 codes to addresses using the USPS Addresses API.")
        self.assertEqual(self.actions(p), [
            (["--base-url"], "base_url", "https://apis.usps.com", False, None, None, None,
             "USPS API base URL (default: %(default)s; testing: https://apis-tem.usps.com)"),
            (["--rate-per-hour"], "rate_per_hour", 60.0, False, float, None, None,
             "max USPS address lookups per hour, spaced evenly; 0 = no pacing (default: %(default)g)"),
        ])
        sub = next(a for a in p._actions if isinstance(a, zip4.argparse._SubParsersAction))
        self.assertEqual((sub.dest, sub.required), ("cmd", True))
        self.assertEqual({a.dest: a.help for a in sub._choices_actions},
                         {"csv": "add ZIP+4 columns to a CSV", "lookup": "look up a single address"})

    def test_env_defaults(self):
        p = self.parser({"USPS_BASE_URL": "https://apis-tem.usps.com", "USPS_RATE_PER_HOUR": "600"})
        args = p.parse_args(["lookup", "--street", "x", "--state", "WA"])
        self.assertEqual((args.base_url, args.rate_per_hour), ("https://apis-tem.usps.com", 600.0))

    def test_csv_options(self):
        sub = next(a for a in self.parser()._actions if isinstance(a, zip4.argparse._SubParsersAction))
        self.assertEqual(self.actions(sub.choices["csv"]), [
            ([], "input", None, True, Path, None, None, "input CSV"),
            (["-o", "--output"], "output", None, False, Path, None, None, "output CSV (default: <input>_zip4.csv)"),
            (["--street-col"], "street_col", None, True, None, None, None, "column with the street address"),
            (["--city-col"], "city_col", None, False, None, None, None, "column with the city"),
            (["--state-col"], "state_col", None, True, None, None, None, "column with the state"),
            (["--zip-col"], "zip_col", None, False, None, None, None, "column with the ZIP code"),
            (["--secondary-col"], "secondary_col", None, False, None, None, None,
             "column with apartment/suite/unit, if separate"),
            (["--cache"], "cache", None, False, Path, None, None,
             "JSON cache of results; reruns skip addresses already looked up (default: <input>_zip4-cache.json)"),
            (["--no-cache"], "no_cache", False, False, None, None, 0, "do not read or write a cache file"),
            (["--cluster-digits"], "cluster_digits", None, False, zip4.cluster_digits_arg, "N", None,
             "also add zip_cluster: ZIP plus the first N (0-4) digits of the +4"),
        ])

    def test_lookup_options(self):
        sub = next(a for a in self.parser()._actions if isinstance(a, zip4.argparse._SubParsersAction))
        self.assertEqual(self.actions(sub.choices["lookup"]), [
            (["--street"], "street", None, True, None, None, None, None),
            (["--secondary"], "secondary", "", False, None, None, None, None),
            (["--city"], "city", "", False, None, None, None, None),
            (["--state"], "state", None, True, None, None, None, None),
            (["--zip"], "zip", "", False, None, None, None, None),
            (["--cluster-digits"], "cluster_digits", None, False, zip4.cluster_digits_arg, "N", None,
             "also print zip_cluster: ZIP plus the first N (0-4) digits of the +4"),
        ])


class RecordingClient(FakeClient):
    instances = None  # a list, reset by MainMoreTests.setUp

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        RecordingClient.instances.append(self)


class MainMoreTests(MainHelpers, unittest.TestCase):

    def setUp(self):
        super().setUp()
        RecordingClient.instances = []
        mock.patch.object(zip4, "make_client", lambda args: RecordingClient()).start()

    def csv_args(self, *extra):
        return ["csv", str(self.inp), "--street-col", "Addr", "--city-col", "Town", "--state-col", "ST", *extra]

    def test_csv_summary_and_exit_code(self):
        code, out, err = self.run_main(*self.csv_args())
        self.assertEqual((code, out), (0, ""))
        self.assertTrue(err.endswith(f"zip4: wrote {self.tmp / 'in_zip4.csv'} (matched=1, usps_lookups=1)\n"), err)

    def test_csv_custom_output_and_cache(self):
        out, cache = self.tmp / "custom.csv", self.tmp / "my-cache.json"
        code, _, _ = self.run_main(*self.csv_args("-o", str(out), "--cache", str(cache)))
        self.assertEqual(code, 0)
        self.assertTrue(out.exists())
        self.assertIn("1 MAIN ST||SEATTLE|WA|", json.loads(cache.read_text()))
        self.assertFalse((self.tmp / "in_zip4.csv").exists())
        self.assertFalse((self.tmp / "in_zip4-cache.json").exists())

    def test_csv_columns_reach_lookup(self):
        self.inp.write_text("Addr,Town,ST,Z,U\n1 Main St,Seattle,WA,98101-1,#4\n")
        code, _, _ = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--zip-col", "Z", "--state-col",
                                   "ST", "--secondary-col", "U")
        self.assertEqual(code, 0)
        self.assertEqual(RecordingClient.instances[0].calls, [zip4.Address("1 Main St", "", "WA", "98101", "#4")])

    def test_rate_zero_is_allowed(self):
        code, _, _ = self.run_main("--rate-per-hour", "0", *self.csv_args())
        self.assertEqual(code, 0)

    def test_http_base_url_is_a_usage_error(self):
        code, _, err = self.run_main("--base-url", "http://apis.usps.com", *self.csv_args())
        self.assertEqual(code, 2)
        self.assertIn("error: --base-url must start with https://, got 'http://apis.usps.com'", err)
        self.assertEqual(RecordingClient.instances, [])

    def test_usage_error_messages(self):
        _, _, err = self.run_main("--rate-per-hour", "-1", *self.csv_args())
        self.assertIn("error: --rate-per-hour must be 0 or more\n", err)
        _, _, err = self.run_main("csv", str(self.inp), "--street-col", "Addr", "--state-col", "ST")
        self.assertIn("error: csv needs --city-col or --zip-col (USPS requires a city or ZIP)\n", err)
        _, _, err = self.run_main(*self.csv_args("-o", str(self.inp)))
        self.assertIn("error: output would overwrite the input\n", err)
        self.assertEqual(RecordingClient.instances, [])

    def test_lookup_exact_output(self):
        code, out, _ = self.run_main("lookup", "--street", "1 Main St", "--secondary", "Apt 2", "--city", "Seattle",
                                     "--state", "WA", "--zip", "98101-9999", "--cluster-digits", "0")
        self.assertEqual(code, 0)
        self.assertEqual(RecordingClient.instances[0].calls, [zip4.Address("1 Main St", "Seattle", "WA", "98101",
                                                                           "Apt 2")])
        self.assertEqual(out, json.dumps({"status": "matched", "zip5": "98101", "zip4": "1234",
                                          "message": "exact match", "zip_plus4": "98101-1234",
                                          "zip_cluster": "98101"}, indent=2) + "\n")

    def test_lookup_defaults(self):
        code, out, _ = self.run_main("lookup", "--street", "1 Main St", "--city", "Seattle", "--state", "WA")
        self.assertEqual(code, 0)
        self.assertEqual(RecordingClient.instances[0].calls, [zip4.Address("1 Main St", "Seattle", "WA", "", "")])
        self.assertNotIn("zip_cluster", json.loads(out))


class MakeClientTests(unittest.TestCase):
    def args(self, **kw):
        return zip4.argparse.Namespace(**{"base_url": "https://usps.test", "rate_per_hour": 30.0, **kw})

    def test_needs_both_credentials(self):
        for env in ({}, {"USPS_CLIENT_ID": "id"}, {"USPS_CLIENT_SECRET": "s"}):
            with mock.patch.dict(zip4.os.environ, env, clear=True), self.assertRaises(SystemExit) as cm:
                zip4.make_client(self.args())
            self.assertEqual(str(cm.exception),
                             "zip4: set USPS_CLIENT_ID and USPS_CLIENT_SECRET (see tools/zip4/README.md)")

    def test_lookup_command_uses_real_client_settings(self):
        env = {"USPS_CLIENT_ID": "id", "USPS_CLIENT_SECRET": "s"}
        with mock.patch.dict(zip4.os.environ, env, clear=True), \
                mock.patch.object(zip4.USPSClient, "lookup", autospec=True,
                                  return_value=zip4.Result("not_found")) as lookup, \
                contextlib.redirect_stdout(io.StringIO()):
            code = zip4.main(["--base-url", "https://apis-tem.usps.com", "--rate-per-hour", "10", "lookup",
                              "--street", "1 Main St", "--city", "Seattle", "--state", "WA"])
        self.assertEqual(code, 1)
        client = lookup.call_args.args[0]
        self.assertEqual((client.base_url, client.min_interval), ("https://apis-tem.usps.com", 360.0))

    def test_builds_client_from_env_and_args(self):
        with mock.patch.dict(zip4.os.environ, {"USPS_CLIENT_ID": "id", "USPS_CLIENT_SECRET": "s"}, clear=True):
            c = zip4.make_client(self.args())
        self.assertEqual((c.client_id, c.client_secret, c.base_url, c.min_interval),
                         ("id", "s", "https://usps.test", 120.0))


if __name__ == "__main__":
    unittest.main()
