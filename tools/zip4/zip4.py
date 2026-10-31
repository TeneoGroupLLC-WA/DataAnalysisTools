#!/usr/bin/env python3
"""Add ZIP+4 codes to addresses using the USPS Addresses API (v3).

Batch: read a CSV, look up each row's address, write a copy with ZIP+4 columns added.

    zip4.py csv addresses.csv --street-col "Address" --city-col "City" \\
        --state-col "State" --zip-col "Zip" [-o out.csv] [--cache zip4-cache.json]

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
USER_AGENT = "TeneoGroupLLC-DataAnalysisTools-zip4/1.0"

# Columns appended to the output CSV.
OUT_COLUMNS = ("zip4", "zip_plus4", "zip4_status", "zip4_message")

# Status values written to zip4_status.
MATCHED = "matched"
NOT_FOUND = "not_found"
INVALID = "invalid_input"
ERROR = "error"


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
                 max_retries: int = 5, timeout: float = 30.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.timeout = timeout
        self._token = ""
        self._token_expires = 0.0

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
            if status in (429, 500, 502, 503, 504) and attempt < self.max_retries:
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
                return max(1.0, float(v))
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
                cache: Cache, save_every: int = 25) -> dict[str, int]:
    with in_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    missing = [name for name in cols.values() if name and name not in fieldnames]
    if missing:
        raise SystemExit(f"zip4: column(s) not in {in_path.name}: {', '.join(missing)}\n"
                         f"zip4: available columns: {', '.join(fieldnames)}")
    clash = [c for c in OUT_COLUMNS if c in fieldnames]
    if clash:
        raise SystemExit(f"zip4: input already has output column(s): {', '.join(clash)}")

    def val(row: dict, which: str) -> str:
        name = cols.get(which)
        return (row.get(name) or "") if name else ""

    counts: dict[str, int] = {}
    seen: dict[str, Result] = {}
    lookups = 0
    for i, row in enumerate(rows, 1):
        addr = Address(street=val(row, "street"), city=val(row, "city"), state=val(row, "state"),
                       zip5=clean_zip5(val(row, "zip")), secondary=val(row, "secondary"))
        key = addr.key()
        result = seen.get(key) or cache.get(key)
        if result is None:
            result = client.lookup(addr)
            cache.put(key, result)
            lookups += 1
            if lookups % save_every == 0:
                cache.save()
        seen[key] = result
        row.update(zip4=result.zip4, zip_plus4=result.zip_plus4, zip4_status=result.status,
                   zip4_message=result.message)
        counts[result.status] = counts.get(result.status, 0) + 1
        if i % 50 == 0:
            print(f"zip4: {i}/{len(rows)} rows", file=sys.stderr)
    cache.save()

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames + list(OUT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    counts["usps_lookups"] = lookups
    return counts


def make_client(args: argparse.Namespace) -> USPSClient:
    cid, secret = os.environ.get("USPS_CLIENT_ID"), os.environ.get("USPS_CLIENT_SECRET")
    if not cid or not secret:
        raise SystemExit("zip4: set USPS_CLIENT_ID and USPS_CLIENT_SECRET (see tools/zip4/README.md)")
    return USPSClient(cid, secret, base_url=args.base_url)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Add ZIP+4 codes to addresses using the USPS Addresses API.")
    p.add_argument("--base-url", default=os.environ.get("USPS_BASE_URL", DEFAULT_BASE_URL),
                   help="USPS API base URL (default: %(default)s; testing: https://apis-tem.usps.com)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("csv", help="add ZIP+4 columns to a CSV")
    c.add_argument("input", type=Path, help="input CSV")
    c.add_argument("-o", "--output", type=Path, help="output CSV (default: <input>_zip4.csv)")
    c.add_argument("--street-col", required=True, help="column with the street address")
    c.add_argument("--city-col", help="column with the city")
    c.add_argument("--state-col", required=True, help="column with the state")
    c.add_argument("--zip-col", help="column with the ZIP code")
    c.add_argument("--secondary-col", help="column with apartment/suite/unit, if separate")
    c.add_argument("--cache", type=Path, help="JSON cache file; reruns skip addresses already looked up")

    s = sub.add_parser("lookup", help="look up a single address")
    s.add_argument("--street", required=True)
    s.add_argument("--secondary", default="")
    s.add_argument("--city", default="")
    s.add_argument("--state", required=True)
    s.add_argument("--zip", default="")

    args = p.parse_args(argv)

    if args.cmd == "lookup":
        addr = Address(args.street, args.city, args.state, clean_zip5(args.zip), args.secondary)
        r = make_client(args).lookup(addr)
        print(json.dumps({**r.to_json(), "zip_plus4": r.zip_plus4}, indent=2))
        return 0 if r.status == MATCHED else 1

    if not args.city_col and not args.zip_col:
        p.error("csv needs --city-col or --zip-col (USPS requires a city or ZIP)")
    out = args.output or args.input.with_name(f"{args.input.stem}_zip4.csv")
    if out.resolve() == args.input.resolve():
        p.error("output would overwrite the input")
    cols = {"street": args.street_col, "city": args.city_col, "state": args.state_col,
            "zip": args.zip_col, "secondary": args.secondary_col}
    counts = process_csv(args.input, out, cols, make_client(args), Cache(args.cache))
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"zip4: wrote {out} ({summary})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
