#!/usr/bin/env python3
"""Geocode CSV addresses with the free US Census Bureau batch geocoder.

Adds latitude/longitude and Census geography (state, county, tract, block group, block) to each
row, for distance-based or area-based geographic clustering.

    geocode.py csv addresses.csv --street-col "Address" --city-col "City" \\
        --state-col "State" --zip-col "Zip" [-o out.csv] [--cluster-level block_group]

Single address:

    geocode.py lookup --street "1600 Pennsylvania Ave NW" --city Washington --state DC

Addresses are sent in batches (default 2,500, Census maximum 10,000). No API key needed.
Results are cached next to the input, so an interrupted run resumes where it stopped.
Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

BATCH_URL = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
DEFAULT_BENCHMARK = "Public_AR_Current"
DEFAULT_VINTAGE = "Current_Current"
MAX_BATCH = 10_000  # Census limit per request
USER_AGENT = "TeneoGroupLLC-DataAnalysisTools-geocode/1.0"

# Columns appended to the output CSV.
OUT_COLUMNS = (
    "latitude", "longitude", "geo_status", "geo_match_type", "geo_matched_address",
    "state_fips", "county_fips", "tract", "block_group", "block",
    "county_geoid", "tract_geoid", "block_group_geoid", "block_geoid",
)
CLUSTER_COLUMN = "geo_cluster"  # added only with --cluster-level

# Census areas from coarsest to finest; --cluster-level picks one, named by its GEOID.
CLUSTER_LEVELS = ("county", "tract", "block_group", "block")

# Status values written to geo_status.
MATCHED = "matched"
NO_MATCH = "no_match"
TIE = "tie"  # several equally good candidates; Census returns no location
INVALID = "invalid_input"
ERROR = "error"  # the batch request failed; not cached, so a rerun retries it


@dataclass(frozen=True)
class Address:
    street: str
    city: str = ""
    state: str = ""
    zip5: str = ""

    def key(self) -> str:
        """Normalized key so the same address is only geocoded once."""
        return "|".join(" ".join(p.upper().split()) for p in (self.street, self.city, self.state, self.zip5))


@dataclass
class Result:
    status: str
    match_type: str = ""
    matched_address: str = ""
    latitude: str = ""
    longitude: str = ""
    state_fips: str = ""
    county_fips: str = ""
    tract: str = ""
    block: str = ""
    message: str = ""

    @property
    def block_group(self) -> str:
        return self.block[:1]

    def geoid(self, level: str) -> str:
        """GEOID for a Census level: county 5 digits, tract 11, block group 12, block 15."""
        if self.status != MATCHED:
            return ""
        parts = {
            "county": (self.state_fips, self.county_fips),
            "tract": (self.state_fips, self.county_fips, self.tract),
            "block_group": (self.state_fips, self.county_fips, self.tract, self.block_group),
            "block": (self.state_fips, self.county_fips, self.tract, self.block),
        }[level]
        return "".join(parts) if all(parts) else ""

    def columns(self) -> dict[str, str]:
        return {
            "latitude": self.latitude, "longitude": self.longitude, "geo_status": self.status,
            "geo_match_type": self.match_type,
            "geo_matched_address": self.matched_address or self.message,
            "state_fips": self.state_fips, "county_fips": self.county_fips, "tract": self.tract,
            "block_group": self.block_group, "block": self.block,
            "county_geoid": self.geoid("county"), "tract_geoid": self.geoid("tract"),
            "block_group_geoid": self.geoid("block_group"), "block_geoid": self.geoid("block"),
        }


def clean_zip5(raw: str) -> str:
    """Return a 5-digit ZIP from '98101', '98101-1234', '981011234' or a spreadsheet-mangled '2134'."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) in (3, 4):  # leading zeros dropped by Excel
        digits = digits.zfill(5)
    return digits[:5] if len(digits) >= 5 else ""


def validate(addr: Address) -> str | None:
    """Return a reason the address cannot be geocoded, or None if it can."""
    if not addr.street.strip():
        return "street is empty"
    if not addr.zip5 and not (addr.city.strip() and addr.state.strip()):
        return "need a 5-digit ZIP, or a city and state"
    return None


def parse_response(text: str) -> dict[str, Result]:
    """Parse the Census batch response CSV (no header) into results by row ID.

    Matched rows: ID, input, "Match", Exact|Non_Exact, matched address, "lon,lat", TIGER line ID,
    side, state FIPS, county FIPS, tract, block. Other rows stop after the match column.
    """
    results: dict[str, Result] = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            continue
        rid, match = row[0], row[2].strip().lower()
        if match == "match" and len(row) >= 6:
            lon, _, lat = row[5].partition(",")
            fips = [*row[8:12], "", "", "", ""][:4]
            results[rid] = Result(
                MATCHED, match_type=row[3].strip().lower(), matched_address=row[4].strip(),
                latitude=lat.strip(), longitude=lon.strip(), state_fips=fips[0].strip(),
                county_fips=fips[1].strip(), tract=fips[2].strip(), block=fips[3].strip(),
            )
        elif match == "tie":
            results[rid] = Result(TIE, message="several equally good matches; no location returned")
        else:
            results[rid] = Result(NO_MATCH, message="Census found no match")
    return results


def batch_csv(items: list[tuple[str, Address]]) -> bytes:
    """Census batch input: ID, street, city, state, ZIP (no header)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for rid, a in items:
        w.writerow([rid, a.street.strip(), a.city.strip(), a.state.strip().upper(), a.zip5])
    return buf.getvalue().encode("utf-8")


class CensusClient:
    def __init__(self, url: str = BATCH_URL, benchmark: str = DEFAULT_BENCHMARK, vintage: str = DEFAULT_VINTAGE,
                 timeout: float = 1800.0, max_retries: int = 3):
        if not url.lower().startswith("https://"):
            raise ValueError(f"Census URL must use https://, got {url!r}")
        self.url = url
        self.benchmark = benchmark
        self.vintage = vintage
        self.timeout = timeout
        self.max_retries = max_retries

    def _post(self, payload: bytes) -> str:
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in (("benchmark", self.benchmark), ("vintage", self.vintage)):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="addressFile"; filename="addresses.csv"\r\n'
                     f"Content-Type: text/csv\r\n\r\n".encode() + payload + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.url, data=b"".join(parts), method="POST", headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": USER_AGENT,
        })
        # Safe: __init__ only accepts https:// URLs, so file:// and other schemes never reach urlopen.
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # nosec B310
            return resp.read().decode("utf-8", "replace")

    def geocode_batch(self, addrs: list[Address]) -> list[Result]:
        """Geocode up to MAX_BATCH addresses; returns one Result per input, in order."""
        if len(addrs) > MAX_BATCH:
            raise ValueError(f"batch of {len(addrs)} exceeds Census limit of {MAX_BATCH}")
        items = [(str(i), a) for i, a in enumerate(addrs)]
        payload = batch_csv(items)
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                parsed = parse_response(self._post(payload))
                return [parsed.get(rid) or Result(ERROR, message="missing from Census response") for rid, _ in items]
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = str(getattr(e, "reason", e))
                if attempt < self.max_retries:
                    wait = 30.0 * 2 ** attempt
                    print(f"geocode: batch of {len(addrs)} failed ({last}); retrying in {wait:.0f}s", file=sys.stderr)
                    time.sleep(wait)
        return [Result(ERROR, message=f"Census request failed: {last}") for _ in addrs]


class Cache:
    """Optional JSON cache of results by address key, so reruns skip addresses already geocoded."""

    def __init__(self, path: Path | None):
        self.path = path
        self.data: dict[str, dict] = {}
        if path and path.exists():
            self.data = json.loads(path.read_text())

    def get(self, key: str) -> Result | None:
        d = self.data.get(key)
        return Result(**d) if d else None

    def put(self, key: str, result: Result) -> None:
        if result.status == ERROR:
            return  # transient; try again next run
        self.data[key] = asdict(result)

    def save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, separators=(",", ":"), sort_keys=True))
            tmp.replace(self.path)


def process_csv(in_path: Path, out_path: Path, cols: dict[str, str | None], client: CensusClient, cache: Cache,
                batch_size: int = 2500, workers: int = 2, cluster_level: str | None = None) -> dict[str, int]:
    with in_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    missing = [name for name in cols.values() if name and name not in fieldnames]
    if missing:
        raise SystemExit(f"geocode: column(s) not in {in_path.name}: {', '.join(missing)}\n"
                         f"geocode: available columns: {', '.join(fieldnames)}")
    out_columns = list(OUT_COLUMNS) + ([CLUSTER_COLUMN] if cluster_level else [])
    clash = [c for c in out_columns if c in fieldnames]
    if clash:
        raise SystemExit(f"geocode: input already has output column(s): {', '.join(clash)}")

    def val(row: dict, which: str) -> str:
        name = cols.get(which)
        return (row.get(name) or "") if name else ""

    def street(row: dict) -> str:
        parts = [val(row, "street").strip(), val(row, "secondary").strip()]
        return " ".join(p for p in parts if p)

    addrs = [Address(street(row), val(row, "city"), val(row, "state"), clean_zip5(val(row, "zip"))) for row in rows]

    results: dict[str, Result] = {}
    todo: dict[str, Address] = {}
    for a in addrs:
        key = a.key()
        if key in results or key in todo:
            continue
        reason = validate(a)
        cached = None if reason else cache.get(key)
        if reason:
            results[key] = Result(INVALID, message=reason)
        elif cached:
            results[key] = cached
        else:
            todo[key] = a

    batches = [list(todo.items())[i:i + batch_size] for i in range(0, len(todo), batch_size)]
    print(f"geocode: {len(rows)} rows, {len(results)} cached or invalid, {len(todo)} to geocode "
          f"in {len(batches)} batch(es) of up to {batch_size}", file=sys.stderr)

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(client.geocode_batch, [a for _, a in b]): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            for (key, _), result in zip(batch, fut.result(), strict=True):
                results[key] = result
                cache.put(key, result)
            cache.save()  # after every batch, so an interrupted run loses at most the batches in flight
            done += 1
            matched = sum(1 for k, _ in batch if results[k].status == MATCHED)
            print(f"geocode: batch {done}/{len(batches)} done ({matched}/{len(batch)} matched)", file=sys.stderr)

    counts: dict[str, int] = {}
    for row, a in zip(rows, addrs, strict=True):
        r = results[a.key()]
        row.update(r.columns())
        if cluster_level:
            row[CLUSTER_COLUMN] = r.geoid(cluster_level)
        counts[r.status] = counts.get(r.status, 0) + 1
    cache.save()

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames + out_columns)
        writer.writeheader()
        writer.writerows(rows)
    counts["census_lookups"] = len(todo)
    return counts


def positive_int(limit: int):
    def parse(value: str) -> int:
        try:
            n = int(value)
        except ValueError:
            n = 0
        if not 1 <= n <= limit:
            raise argparse.ArgumentTypeError(f"must be 1-{limit}, got {value!r}")
        return n
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Geocode addresses with the US Census Bureau batch geocoder.")
    p.add_argument("--benchmark", default=DEFAULT_BENCHMARK, help="Census address benchmark (default: %(default)s)")
    p.add_argument("--vintage", default=DEFAULT_VINTAGE, help="Census geography vintage (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("csv", help="add location and Census geography columns to a CSV")
    c.add_argument("input", type=Path, help="input CSV")
    c.add_argument("-o", "--output", type=Path, help="output CSV (default: <input>_geocoded.csv)")
    c.add_argument("--street-col", required=True, help="column with the street address")
    c.add_argument("--city-col", help="column with the city")
    c.add_argument("--state-col", help="column with the state")
    c.add_argument("--zip-col", help="column with the ZIP code")
    c.add_argument("--secondary-col", help="column with apartment/suite/unit, if separate (appended to the street)")
    c.add_argument("--cluster-level", choices=CLUSTER_LEVELS,
                   help="also add geo_cluster: the GEOID of the address's county, tract, block group or block")
    c.add_argument("--batch-size", type=positive_int(MAX_BATCH), default=2500,
                   help=f"addresses per Census request (1-{MAX_BATCH}, default: %(default)s)")
    c.add_argument("--workers", type=positive_int(4), default=2,
                   help="batches sent at the same time (1-4, default: %(default)s)")
    c.add_argument("--cache", type=Path, help="JSON cache of results (default: <input>_geocode-cache.json)")
    c.add_argument("--no-cache", action="store_true", help="do not read or write a cache file")

    s = sub.add_parser("lookup", help="geocode a single address")
    s.add_argument("--street", required=True)
    s.add_argument("--city", default="")
    s.add_argument("--state", default="")
    s.add_argument("--zip", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    client = CensusClient(benchmark=args.benchmark, vintage=args.vintage)

    if args.cmd == "lookup":
        addr = Address(args.street, args.city, args.state, clean_zip5(args.zip))
        reason = validate(addr)
        r = Result(INVALID, message=reason) if reason else client.geocode_batch([addr])[0]
        print(json.dumps(r.columns(), indent=2))
        return 0 if r.status == MATCHED else 1

    if not args.zip_col and not (args.city_col and args.state_col):
        p.error("csv needs --zip-col, or both --city-col and --state-col")
    out = args.output or args.input.with_name(f"{args.input.stem}_geocoded.csv")
    if out.resolve() == args.input.resolve():
        p.error("output would overwrite the input")
    cols = {"street": args.street_col, "city": args.city_col, "state": args.state_col,
            "zip": args.zip_col, "secondary": args.secondary_col}
    cache_path = None if args.no_cache else (
        args.cache or args.input.with_name(f"{args.input.stem}_geocode-cache.json"))
    counts = process_csv(args.input, out, cols, client, Cache(cache_path), batch_size=args.batch_size,
                         workers=args.workers, cluster_level=args.cluster_level)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"geocode: wrote {out} ({summary})", file=sys.stderr)
    return 3 if counts.get(ERROR) else 0


if __name__ == "__main__":
    sys.exit(main())
