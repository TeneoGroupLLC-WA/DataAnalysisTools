#!/usr/bin/env python3
"""Add ZIP+4 codes to addresses using the USPS Addresses API (v3).

Batch: read a CSV, look up each row's address, write a copy with ZIP+4 columns added.

    zip4.py csv addresses.csv --street-col "Address" --city-col "City" \\
        --state-col "State" --zip-col "Zip" [-o out.csv] [--cluster-digits 2] [--rate-per-hour 60]

--cluster-digits N (0-4) adds a zip_cluster column holding the ZIP plus only the first N digits
of the +4 (e.g. 98101-12 for N=2), for grouping addresses at a coarser level. The full ZIP+4 is
always kept as well, so the grouping can be changed later without new USPS lookups.

USPS limits each app to a number of requests per hour. Lookups are paced to --rate-per-hour
(default 60), results are cached next to the input, and if the quota still runs out the run
stops cleanly: rows not yet looked up are marked "pending" and rerunning the same command
resumes from the cache.

Single address:

    zip4.py lookup --street "475 L'Enfant Plaza SW" --city Washington --state DC [--zip 20260]

Credentials come from USPS_CLIENT_ID and USPS_CLIENT_SECRET (see .envrc). Get them by
registering an app at https://developers.usps.com. Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://apis.usps.com"
DEFAULT_RATE_PER_HOUR = 60.0  # USPS's default quota for a new app; raise it if USPS grants more
MAX_RETRY_WAIT = 3600.0  # quotas are hourly, so never wait longer than that for one retry
USER_AGENT = "TeneoGroupLLC-DataAnalysisTools-zip4/1.0"

# Columns appended to the output CSV.
OUT_COLUMNS = ("zip4", "zip_plus4", "zip4_status", "zip4_message")
CLUSTER_COLUMN = "zip_cluster"  # added only with --cluster-digits

# Status values written to zip4_status.
MATCHED = "matched"
NOT_FOUND = "not_found"
INVALID = "invalid_input"
ERROR = "error"
PENDING = "pending"  # not looked up because the USPS quota ran out; rerun to finish


class QuotaExceeded(Exception):
    """USPS kept answering 429 after waiting and retrying."""


@dataclass(frozen=True)
class Address:
    street: str
    city: str = ""
    state: str = ""
    zip5: str = ""
    secondary: str = ""

    def key(self) -> str:
        """Normalized key so the same address is only looked up once."""
        parts = (self.street, self.secondary, self.city, self.state, self.zip5)
        return "|".join(" ".join(p.upper().split()) for p in parts)


@dataclass
class Result:
    status: str
    zip5: str = ""
    zip4: str = ""
    message: str = ""

    @property
    def zip_plus4(self) -> str:
        return f"{self.zip5}-{self.zip4}" if self.zip5 and self.zip4 else ""

    def cluster(self, digits: int) -> str:
        """ZIP plus the first `digits` digits of the +4 (0 = ZIP only); empty unless matched.

        The first two +4 digits are the USPS sector (a few blocks), the last two the segment
        (about one side of a block or one building).
        """
        if self.status != MATCHED or not self.zip5:
            return ""
        return f"{self.zip5}-{self.zip4[:digits]}" if digits > 0 else self.zip5

    def to_json(self) -> dict:
        return {"status": self.status, "zip5": self.zip5, "zip4": self.zip4, "message": self.message}

    @classmethod
    def from_json(cls, d: dict) -> Result:
        return cls(d.get("status", ERROR), d.get("zip5", ""), d.get("zip4", ""), d.get("message", ""))


def clean_zip5(raw: str) -> str:
    """Return a 5-digit ZIP from '98101', '98101-1234', '981011234' or a spreadsheet-mangled '2134'."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) in (3, 4):  # leading zeros dropped by Excel
        digits = digits.zfill(5)
    return digits[:5] if len(digits) >= 5 else ""


def validate(addr: Address) -> str | None:
    """Return a reason the address cannot be looked up, or None if it can."""
    if not addr.street.strip():
        return "street is empty"
    if not addr.state.strip():
        return "state is empty"
    if not addr.city.strip() and not addr.zip5:
        return "need a city or a 5-digit ZIP"
    return None


class USPSClient:
    """Minimal client for the USPS Addresses API v3 (OAuth client credentials)."""

    def __init__(self, client_id: str, client_secret: str, base_url: str = DEFAULT_BASE_URL,
                 max_retries: int = 5, timeout: float = 30.0, rate_per_hour: float = DEFAULT_RATE_PER_HOUR):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self.min_interval = 3600.0 / rate_per_hour if rate_per_hour > 0 else 0.0
        self._last_call: float | None = None
        self._token = ""
        self._token_expires = 0.0

    def _throttle(self) -> None:
        """Space address requests at least min_interval seconds apart."""
        if self.min_interval and self._last_call is not None:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last_call = time.monotonic()

    def _request(self, req: urllib.request.Request) -> tuple[int, dict, dict]:
        """Send a request; return (status, json body, headers). HTTP errors are returned, not raised."""
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}"), dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {"error": {"message": raw.decode("utf-8", "replace")[:200]}}
            return e.code, body, dict(e.headers or {})

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        data = json.dumps({
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/oauth2/v3/token", data=data, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        status, body, _ = self._request(req)
        if status != 200 or "access_token" not in body:
            raise RuntimeError(f"USPS token request failed (HTTP {status}): {error_message(body)}")
        self._token = body["access_token"]
        self._token_expires = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def lookup(self, addr: Address) -> Result:
        reason = validate(addr)
        if reason:
            return Result(INVALID, message=reason)

        params = {"streetAddress": addr.street.strip(), "state": addr.state.strip().upper()}
        if addr.secondary.strip():
            params["secondaryAddress"] = addr.secondary.strip()
        if addr.city.strip():
            params["city"] = addr.city.strip()
        if addr.zip5:
            params["ZIPCode"] = addr.zip5
        url = f"{self.base_url}/addresses/v3/address?{urllib.parse.urlencode(params)}"

        for attempt in range(self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._get_token()}",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
            status, body, headers = self._request(req)

            if status == 200:
                a = body.get("address") or {}
                zip5, zip4 = a.get("ZIPCode") or "", a.get("ZIPPlus4") or ""
                if not zip4:
                    return Result(NOT_FOUND, zip5=zip5, message="USPS returned no ZIP+4 for this address")
                return Result(MATCHED, zip5=zip5, zip4=zip4, message=match_text(body))
            if status == 401 and attempt < self.max_retries:
                self._token = ""  # expired token: fetch a new one and retry
                continue
            if status == 429:
                if attempt == self.max_retries:
                    raise QuotaExceeded(error_message(body))
                wait = retry_after(headers, attempt)
                print(f"zip4: USPS rate limit (HTTP 429), waiting {wait:.0f}s before retrying", file=sys.stderr)
                time.sleep(wait)
                continue
            if status in (500, 502, 503, 504) and attempt < self.max_retries:
                wait = retry_after(headers, attempt)
                print(f"zip4: HTTP {status} from USPS, waiting {wait:.0f}s before retrying", file=sys.stderr)
                time.sleep(wait)
                continue
            if status in (400, 404):
                return Result(NOT_FOUND, message=error_message(body))
            return Result(ERROR, message=f"HTTP {status}: {error_message(body)}")
        return Result(ERROR, message="gave up after retries")


def retry_after(headers: dict, attempt: int) -> float:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return min(MAX_RETRY_WAIT, max(1.0, float(v)))
            except ValueError:
                break
    return min(300.0, 5.0 * 2 ** attempt)


def error_message(body: dict) -> str:
    err = body.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or ""
        details = [d.get("detail") or d.get("title") or "" for d in err.get("errors") or [] if isinstance(d, dict)]
        return "; ".join(x for x in [msg, *details] if x) or "unknown error"
    return str(err or body.get("message") or "unknown error")


def match_text(body: dict) -> str:
    matches = body.get("matches") or []
    return "; ".join(m.get("text", "") for m in matches if isinstance(m, dict) and m.get("text"))


class Cache:
    """Optional JSON cache of results by address key, so reruns skip addresses already looked up."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, dict] = {}
        if path and path.exists():
            self.data = json.loads(path.read_text())

    def get(self, key: str) -> Result | None:
        d = self.data.get(key)
        return Result.from_json(d) if d else None

    def put(self, key: str, result: Result) -> None:
        if result.status == ERROR:
            return  # transient; try again next run
        self.data[key] = result.to_json()

    def save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
            tmp.replace(self.path)


def process_csv(in_path: Path, out_path: Path, cols: dict[str, str | None], client: USPSClient,
                cache: Cache, save_every: int = 25, cluster_digits: int | None = None) -> dict[str, int]:
    with in_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    missing = [name for name in cols.values() if name and name not in fieldnames]
    if missing:
        raise SystemExit(f"zip4: column(s) not in {in_path.name}: {', '.join(missing)}\n"
                         f"zip4: available columns: {', '.join(fieldnames)}")
    out_columns = list(OUT_COLUMNS) + ([CLUSTER_COLUMN] if cluster_digits is not None else [])
    clash = [c for c in out_columns if c in fieldnames]
    if clash:
        raise SystemExit(f"zip4: input already has output column(s): {', '.join(clash)}")

    def val(row: dict, which: str) -> str:
        name = cols.get(which)
        return (row.get(name) or "") if name else ""

    addrs = [Address(street=val(row, "street"), city=val(row, "city"), state=val(row, "state"),
                     zip5=clean_zip5(val(row, "zip")), secondary=val(row, "secondary")) for row in rows]
    todo = {a.key() for a in addrs if validate(a) is None and cache.get(a.key()) is None}
    interval = getattr(client, "min_interval", 0.0)
    if todo:
        eta = f", about {len(todo) * interval / 3600:.1f} h at the current rate" if interval else ""
        print(f"zip4: {len(rows)} rows, {len(todo)} new addresses to look up{eta}", file=sys.stderr)

    counts: dict[str, int] = {}
    seen: dict[str, Result] = {}
    lookups = 0
    quota_hit = ""
    for i, (row, addr) in enumerate(zip(rows, addrs), 1):
        key = addr.key()
        result = seen.get(key) or cache.get(key)
        if result is None and (reason := validate(addr)):
            result = Result(INVALID, message=reason)  # needs no USPS call, so never pending
        if result is None and quota_hit:
            result = Result(PENDING, message="not looked up yet (USPS quota); rerun to finish")
        if result is None:
            try:
                result = client.lookup(addr)
            except QuotaExceeded as e:
                quota_hit = str(e) or "rate limit"
                print(f"zip4: USPS quota exhausted ({quota_hit}); stopping lookups", file=sys.stderr)
                result = Result(PENDING, message="not looked up yet (USPS quota); rerun to finish")
            else:
                cache.put(key, result)
                lookups += 1
                if lookups % save_every == 0:
                    cache.save()
        if result.status != PENDING:
            seen[key] = result
        row.update(zip4=result.zip4, zip_plus4=result.zip_plus4, zip4_status=result.status,
                   zip4_message=result.message)
        if cluster_digits is not None:
            row[CLUSTER_COLUMN] = result.cluster(cluster_digits)
        counts[result.status] = counts.get(result.status, 0) + 1
        if i % 50 == 0:
            print(f"zip4: {i}/{len(rows)} rows", file=sys.stderr)
    cache.save()

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames + out_columns)
        writer.writeheader()
        writer.writerows(rows)
    counts["usps_lookups"] = lookups
    if quota_hit:
        print("zip4: rerun the same command later to look up the pending rows", file=sys.stderr)
    return counts


def cluster_digits_arg(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        n = -1
    if not 0 <= n <= 4:
        raise argparse.ArgumentTypeError(f"must be 0-4, got {value!r}")
    return n


def make_client(args: argparse.Namespace) -> USPSClient:
    cid, secret = os.environ.get("USPS_CLIENT_ID"), os.environ.get("USPS_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("zip4: set USPS_CLIENT_ID and USPS_CLIENT_SECRET (see tools/zip4/README.md)")
    return USPSClient(cid, secret, base_url=args.base_url, rate_per_hour=args.rate_per_hour)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Add ZIP+4 codes to addresses using the USPS Addresses API.")
    p.add_argument("--base-url", default=os.environ.get("USPS_BASE_URL", DEFAULT_BASE_URL),
                   help="USPS API base URL (default: %(default)s; testing: https://apis-tem.usps.com)")
    p.add_argument("--rate-per-hour", type=float,
                   default=float(os.environ.get("USPS_RATE_PER_HOUR", DEFAULT_RATE_PER_HOUR)),
                   help="max USPS address lookups per hour, spaced evenly; 0 = no pacing (default: %(default)g)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("csv", help="add ZIP+4 columns to a CSV")
    c.add_argument("input", type=Path, help="input CSV")
    c.add_argument("-o", "--output", type=Path, help="output CSV (default: <input>_zip4.csv)")
    c.add_argument("--street-col", required=True, help="column with the street address")
    c.add_argument("--city-col", help="column with the city")
    c.add_argument("--state-col", required=True, help="column with the state")
    c.add_argument("--zip-col", help="column with the ZIP code")
    c.add_argument("--secondary-col", help="column with apartment/suite/unit, if separate")
    c.add_argument("--cache", type=Path,
                   help="JSON cache of results; reruns skip addresses already looked up "
                        "(default: <input>_zip4-cache.json)")
    c.add_argument("--no-cache", action="store_true", help="do not read or write a cache file")
    c.add_argument("--cluster-digits", type=cluster_digits_arg, metavar="N",
                   help="also add zip_cluster: ZIP plus the first N (0-4) digits of the +4")

    s = sub.add_parser("lookup", help="look up a single address")
    s.add_argument("--street", required=True)
    s.add_argument("--secondary", default="")
    s.add_argument("--city", default="")
    s.add_argument("--state", required=True)
    s.add_argument("--zip", default="")
    s.add_argument("--cluster-digits", type=cluster_digits_arg, metavar="N",
                   help="also print zip_cluster: ZIP plus the first N (0-4) digits of the +4")

    args = p.parse_args(argv)

    if args.cmd == "lookup":
        addr = Address(args.street, args.city, args.state, clean_zip5(args.zip), args.secondary)
        r = make_client(args).lookup(addr)
        out = {**r.to_json(), "zip_plus4": r.zip_plus4}
        if args.cluster_digits is not None:
            out[CLUSTER_COLUMN] = r.cluster(args.cluster_digits)
        print(json.dumps(out, indent=2))
        return 0 if r.status == MATCHED else 1

    if args.rate_per_hour < 0:
        p.error("--rate-per-hour must be 0 or more")
    if not args.city_col and not args.zip_col:
        p.error("csv needs --city-col or --zip-col (USPS requires a city or ZIP)")
    out = args.output or args.input.with_name(f"{args.input.stem}_zip4.csv")
    if out.resolve() == args.input.resolve():
        p.error("output would overwrite the input")
    cols = {"street": args.street_col, "city": args.city_col, "state": args.state_col,
            "zip": args.zip_col, "secondary": args.secondary_col}
    cache_path = None if args.no_cache else (
        args.cache or args.input.with_name(f"{args.input.stem}_zip4-cache.json"))
    counts = process_csv(args.input, out, cols, make_client(args), Cache(cache_path),
                         cluster_digits=args.cluster_digits)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"zip4: wrote {out} ({summary})", file=sys.stderr)
    return 3 if counts.get(PENDING) else 0


if __name__ == "__main__":
    sys.exit(main())
