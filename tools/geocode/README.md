# geocode: add location and Census areas to addresses

Geocodes addresses with the free **US Census Bureau batch geocoder** and adds latitude/longitude
plus the Census areas each address falls in (state, county, tract, block group, block). Built for
geographic clustering of large files: no API key and no hourly quota, and addresses go up 2,500 at a
time. In a test, 1,000 addresses took about 7 seconds. Python 3.10+, standard library only.

## Batch: CSV in, CSV out

```bash
python3 tools/geocode/geocode.py csv customers.csv \
  --street-col "Address" --city-col "City" --state-col "State" --zip-col "Zip" \
  --cluster-level block_group
```

| Option | Required | Meaning |
|---|---|---|
| `--street-col` | yes | column with the street address |
| `--zip-col` | ZIP, or city + state | column with the ZIP (`98101`, `98101-1234` and Excel-mangled `2134` all work) |
| `--city-col`, `--state-col` | ZIP, or city + state | columns with the city and state |
| `--secondary-col` | no | apartment/suite/unit, if in its own column (appended to the street) |
| `--cluster-level` | no | also add `geo_cluster`: the GEOID of the `county`, `tract`, `block_group` or `block` |
| `-o`, `--output` | no | output file (default: `<input>_geocoded.csv`; never overwrites the input) |
| `--batch-size` | no | addresses per Census request (default 2,500, max 10,000) |
| `--workers` | no | batches sent at once (default 2, max 4) |
| `--cache` / `--no-cache` | no | results cache (default: `<input>_geocode-cache.json`); a rerun skips addresses already done |
| `--benchmark`, `--vintage` | no | go **before** `csv`; Census data versions (defaults `Public_AR_Current`, `Current_Current`) |

Columns added to every row (original columns and row order are kept):

| Column | Example | Meaning |
|---|---|---|
| `latitude`, `longitude` | `38.8987`, `-77.0352` | location of the address (interpolated along the street) |
| `geo_status` | `matched` | `matched`, `no_match`, `tie` (several equally good candidates, no location), `invalid_input`, or `error` (request failed; rerun to retry) |
| `geo_match_type` | `exact` | `exact` or `non_exact` (Census corrected or fuzzily matched the input) |
| `geo_matched_address` | `1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500` | the address Census matched, or why it did not |
| `state_fips`, `county_fips`, `tract`, `block_group`, `block` | `11`, `001`, `980000`, `1`, `1034` | Census area codes |
| `county_geoid` … `block_geoid` | `11001`, `11001980000`, `110019800001`, `110019800001034` | full IDs for joining to Census data |
| `geo_cluster` | `110019800001` | only with `--cluster-level` |

Exit code: 0 done, 3 some rows are `error` (rerun the same command to retry just those).

## Clustering

Two ways to use the output:

- **By distance:** cluster on `latitude`/`longitude` (k-means, DBSCAN, hierarchical, ...). Use a
  haversine or projected distance; degrees of longitude shrink away from the equator.
- **By area:** group on a Census area. `--cluster-level` picks one, coarsest to finest:

  | Level | GEOID digits | Typical size |
  |---|---|---|
  | `county` | 5 | a county |
  | `tract` | 11 | ~4,000 people (1,200 to 8,000) |
  | `block_group` | 12 | ~600 to 3,000 people |
  | `block` | 15 | often one city block |

  All four GEOIDs are always written, and results are cached, so you can switch levels later
  without geocoding again. Tracts and block groups have Census demographic data (ACS) you can join
  on the GEOID.

## Match rate and limits

- Census matches against its own street database, so expect some `no_match` and `tie` rows, more for
  rural routes, PO boxes, new construction and misspelled streets. Clean, complete addresses match best.
- No ZIP+4: use `tools/zip4` for that.
- Requests that fail (timeouts, server errors) are retried 3 times with backoff, then recorded as
  `error`. Errors are not cached, so rerunning the command retries only those rows.
- The cache and output hold real addresses; both are gitignored. Delete them when no longer needed.

## Single address

```bash
python3 tools/geocode/geocode.py lookup --street "1600 Pennsylvania Ave NW" --city Washington --state DC
```

Prints JSON; exit code 0 when matched, 1 otherwise.

## Tests

```bash
python3 -m unittest discover -s tools/geocode/tests -v                  # offline
GEOCODE_LIVE=1 python3 -m unittest discover -s tools/geocode/tests -v   # also calls the real API
```
