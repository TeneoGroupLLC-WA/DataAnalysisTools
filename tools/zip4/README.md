# zip4: add ZIP+4 codes to addresses

Looks up addresses with the official **USPS Addresses API (v3)** and adds the ZIP+4. Give it a
CSV and the names of its address columns; it writes a copy of the CSV with ZIP+4 columns added.
Python 3.10+, standard library only (nothing to install).

## Setup

1. Register at <https://developers.usps.com>, create an app, and copy its **Consumer Key** and
   **Consumer Secret**.
2. Put them in the repo's `.envrc` (gitignored), then run `direnv allow`:

   ```bash
   export USPS_CLIENT_ID="..."      # Consumer Key
   export USPS_CLIENT_SECRET="..."  # Consumer Secret
   ```

## Batch: CSV in, CSV out

```bash
python3 tools/zip4/zip4.py csv customers.csv \
  --street-col "Address" --city-col "City" --state-col "State" --zip-col "Zip" \
  --cluster-digits 2
```

| Option | Required | Meaning |
|---|---|---|
| `--street-col` | yes | column with the street address (e.g. `123 Main St`) |
| `--state-col` | yes | column with the state |
| `--city-col` | one of city/zip | column with the city |
| `--zip-col` | one of city/zip | column with the ZIP (`98101`, `98101-1234` and Excel-mangled `2134` all work) |
| `--secondary-col` | no | apartment/suite/unit, if it is in its own column |
| `-o`, `--output` | no | output file (default: `<input>_zip4.csv`; never overwrites the input) |
| `--cluster-digits N` | no | also add `zip_cluster`: the ZIP plus the first N (0-4) digits of the +4 (see below) |
| `--cache` | no | cache file of past results (default: `<input>_zip4-cache.json`); a rerun skips addresses already looked up |
| `--no-cache` | no | do not read or write a cache file |
| `--rate-per-hour N` | no | goes **before** `csv`; max lookups per hour, spaced evenly (default 60, or `USPS_RATE_PER_HOUR`; `0` = no pacing) |

Columns added to every row:

| Column | Example | Meaning |
|---|---|---|
| `zip4` | `1234` | the 4-digit add-on |
| `zip_plus4` | `98101-1234` | full ZIP+4 (USPS's ZIP, which may correct the input ZIP) |
| `zip4_status` | `matched` | `matched`, `not_found`, `invalid_input` (missing street/state/city-or-zip), `error`, or `pending` (USPS quota ran out; rerun to finish) |
| `zip4_message` | | USPS match description or error text |
| `zip_cluster` | `98101-12` | only with `--cluster-digits`; empty unless matched |

All original columns and rows are kept in order. Exit code: 0 done, 3 some rows still `pending`.

## Clustering granularity (`--cluster-digits`)

A ZIP+4 is very fine: often one side of one block, or one building. `zip_cluster` truncates the
+4 so addresses group at a coarser level:

| N | `zip_cluster` | Roughly |
|---|---|---|
| 0 | `98101` | the whole ZIP code |
| 2 | `98101-12` | USPS *sector*: a few blocks or a group of streets |
| 4 | `98101-1234` | USPS *segment*: one side of a block, one building, or one PO box range |

1 and 3 are allowed as in-between levels. The full `zip4` and `zip_plus4` are always written too,
and results are cached, so you can rerun with a different N without new USPS lookups.

ZIP+4 codes describe mail delivery routes, not geometry: codes that are numerically close are
usually close on the ground within a ZIP, but this is not guaranteed and there is no distance
between codes. For distance-based clustering, add latitude/longitude from a geocoder.

## Single address

```bash
python3 tools/zip4/zip4.py lookup --street "1600 Pennsylvania Ave NW" --city Washington --state DC
```

Prints JSON; exit code 0 when matched, 1 otherwise.

## Rate limits

The USPS API has an hourly request quota per app. New apps have historically had about 60 per hour;
check yours in the developer portal and ask USPS for more if needed. The tool:

1. **Paces requests** to `--rate-per-hour` (default 60, i.e. one a minute), so it stays under the
   quota instead of hitting it. Set it to your quota, e.g. `export USPS_RATE_PER_HOUR=600` in `.envrc`.
   At the start it prints how many new addresses it will look up and about how long that takes.
2. **Looks each address up once.** Duplicate rows (ignoring case and spacing) share one lookup, rows
   missing a street, state, or city/ZIP are never sent, and results are cached across runs.
3. **Stops cleanly if the quota runs out anyway.** On HTTP 429 it waits (honoring `Retry-After`, up
   to an hour) and retries. If USPS keeps refusing, it stops looking up, writes the output with the
   remaining rows marked `pending`, and exits with code 3. Rerun the same command later; it resumes
   from the cache and only looks up what is left.

Server errors (HTTP 5xx) are retried with backoff and then recorded as `error`. Errors are not cached,
so a rerun tries them again.

The cache file holds real addresses: it is gitignored, and should be deleted when no longer needed.

## Tests

```bash
python3 -m unittest discover -s tools/zip4/tests -v
```
