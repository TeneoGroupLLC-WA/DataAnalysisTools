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
  --cache zip4-cache.json
```

| Option | Required | Meaning |
|---|---|---|
| `--street-col` | yes | column with the street address (e.g. `123 Main St`) |
| `--state-col` | yes | column with the state |
| `--city-col` | one of city/zip | column with the city |
| `--zip-col` | one of city/zip | column with the ZIP (`98101`, `98101-1234` and Excel-mangled `2134` all work) |
| `--secondary-col` | no | apartment/suite/unit, if it is in its own column |
| `-o`, `--output` | no | output file (default: `<input>_zip4.csv`; never overwrites the input) |
| `--cache` | no | JSON file of past results; a rerun skips addresses already looked up |

Columns added to every row:

| Column | Example | Meaning |
|---|---|---|
| `zip4` | `1234` | the 4-digit add-on |
| `zip_plus4` | `98101-1234` | full ZIP+4 (USPS's ZIP, which may correct the input ZIP) |
| `zip4_status` | `matched` | `matched`, `not_found`, `invalid_input` (missing street/state/city-or-zip) or `error` |
| `zip4_message` | | USPS match description or error text |

All original columns and rows are kept in order.

## Single address

```bash
python3 tools/zip4/zip4.py lookup --street "1600 Pennsylvania Ave NW" --city Washington --state DC
```

Prints JSON; exit code 0 when matched, 1 otherwise.

## Rate limits

The USPS API has an hourly request quota per app (low by default; you can ask USPS for more).
The tool looks up each distinct address once per run, waits and retries when USPS returns
HTTP 429, and with `--cache` saves progress as it goes, so a large file can be stopped and rerun
without repeating work. Lookups that failed with `error` are not cached and are retried next run.

## Tests

```bash
python3 -m unittest discover -s tools/zip4/tests -v
```
