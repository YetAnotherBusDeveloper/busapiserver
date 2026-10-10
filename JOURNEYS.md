# Fixed-date journey routing (v1)

`POST /api/v1/journeys` returns the nondominated earliest-arrival / transfer-count
frontier, sorted by arrival then transfers. A later direct bus is retained when
the fastest journey requires a transfer. Walking-only and a direct bus both have
zero transfers, so only the earlier survives; equal arrival/transfer objectives
prefer fewer boardings. Other equal-objective itineraries are represented by one
journey; this is not a k-shortest-path API.

## Offline demo

With `uv` installed, run from this directory:

```powershell
uv run --python 3.13 --with-requirements requirements.txt python -m uvicorn app.journey_demo:app --host 127.0.0.1 --port 8000
```

This app has no credentials, database initialization, sync jobs, or network data
dependencies. Open `http://127.0.0.1:8000/docs` for interactive requests.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/journeys -Method Post -ContentType application/json -Body '{"origin":"A","destination":"D","service_date":"2026-10-10","departure_seconds":28800,"max_transfers":2}'
```

Expected frontier: arrive **08:25 with one transfer** (A–B bus, B–C walk,
C–D bus), or **08:45 with zero transfers** on the through bus. The walk takes
180 seconds and the next boarding requires another 60 seconds. The response
includes source, dataset ID, `is_demo: true`, ISO timestamps with `+08:00`, and
legs with trip/route IDs. `Journey.departure` is the requested search time;
the first leg's departure is the actual start of movement. Gaps between legs
represent waiting and required transfer margins.

## Integration and input contract

The main FastAPI app includes the same router. Set `JOURNEY_TIMETABLE_PATH` to
an absolute JSON path before starting it. With no file configured it returns
503; demo coverage is never silently enabled in the main app. Relative paths are
rejected rather than resolved against a worker's current directory. Loading is
lazy, validated, and serialized per worker, including concurrent cold requests.
The first successful snapshot is cached; restart workers to change the path or
load a replacement file. Each worker has its own cache; publish a complete file
atomically before starting workers to give them the same snapshot. Failed loads
are not cached and can be retried after the configuration/file is repaired.
Missing, unreadable, invalid-encoding or invalid-schema files return 503 without
exposing filesystem details. Unknown stops, unsupported service dates, invalid
numbers and extra fields return 422.
Valid requests with no journey return 200 with `status: "unreachable"` and an
empty `journeys` array. Origin equal to destination returns an empty-leg journey.

`app/data/journey-demo-v1.json` is a complete example. Models in
`app/journeys.py` are the authoritative version-1 schema. To export JSON Schema:

```powershell
uv run --python 3.13 --with-requirements requirements.txt python -c "import json; from app.journeys import Timetable; print(json.dumps(Timetable.model_json_schema(), indent=2))"
```

* One explicit `service_date`, timezone `Asia/Taipei`, source description,
  dataset ID and demo flag per immutable input snapshot. Trip IDs must uniquely
  identify a vehicle run, not just a route. Stop IDs must be unique.
* All arrival/departure/query times are integer elapsed seconds from **that
  date's local midnight**, from 0 through 172799. Times never implicitly wrap.
  23:50 is 85800; next-day 00:20 is 87600. The demo night bus illustrates this.
  To search next-day 00:05 in this snapshot use the original service date and
  86700. A different `service_date` is rejected, not silently remapped. Dates
  must be at most 9999-12-30 so the full horizon has representable timestamps.
* Trip stop times are ordered and nondecreasing, including dwell. Overnight
  times must already be normalized. No interpolation of missing stops/times.
  `pickup`/`dropoff` default true and can prohibit boarding/alighting; passing
  such stops on the same vehicle remains possible.
* Footpaths are **directed**, strictly positive duration, explicit stop-to-stop
  links. Add both directions when appropriate. Chained walking, access walking,
  egress walking and walking-only journeys are supported. No inferred links
  based on coordinates or station names. Walking cannot extend beyond the
  snapshot's exclusive 48-hour horizon.
* Initial boarding has no extra margin. Every subsequent boarding requires the
  **boarding stop's** `min_transfer_seconds` (default 120), in addition to any
  walking since alighting. Equality catches a departure. Remaining aboard the
  same trip requires no margin or additional boarding, even across dwell stops.
  Transfers are `max(boardings - 1, 0)`; walking is not a boarding. Reboarding
  is a new boarding. `max_transfers` is 0–4; walking-only is always considered.

## Algorithm and correctness boundary

The router uses RAPTOR-style exact-boarding rounds with Dijkstra walking closure.
For every reached stop it checks each scheduled boarding occurrence and relaxes
all downstream permitted alightings. This simpler first version does not rely on
route grouping or a non-overtaking assumption: later express departures, repeated
stops and identical event times are valid. Through-rides are relaxed in one step,
so stop-label dominance cannot lose an onboard continuation.

Within a round, an earlier arrival at the same stop dominates a later arrival:
waiting is unrestricted, walking times are fixed, and boarding feasibility depends
only on arrival, boarding count and the boarding stop's margin. Walking closure
finds the minimum under these same conditions. Induction over rounds therefore
finds the earliest feasible arrival for each boarding count. The output groups
zero and one boarding into the same zero-transfer objective, then removes
non-improving higher transfer counts. Path tuples are immutable to preserve valid
reconstruction when labels improve. Walking is a feasibility component, not a
third optimization objective; no maximum cumulative walking budget is imposed.

For K boarding rounds, total downstream relaxations are worst-case
O(K × sum(trip_length²)), plus walking closure O(K × (stops + footpaths) log stops).
This is deliberately correctness-first, suitable for bounded snapshots; it is
not yet an optimized nationwide route-pattern RAPTOR implementation. The sync
endpoint executes in FastAPI's worker threadpool; it does not block the event loop,
but large inputs still consume CPU and need deployment-specific capacity planning.

## Existing dataset boundary

Inspection of `app/db.py` and `_sync_route_schedules` in `app/sync_static.py`
found storage/ingestion for `route_schedules` containing frequency windows or
timetable JSON with weekly/holiday flags and possibly incomplete stop times. `stop_travel_times`
contains averages, including ETA-derived estimates. These are not a guaranteed
complete fixed-date operating calendar. Alias/subroute stop identities also need
resolution before interchange links can be trusted.

V1 consequently does **not** adapt those rows automatically. The GTFS importer
below accepts a separately supplied feed; a database-to-GTFS exporter would still
need to resolve route/stop identities and select actual operating trips for the date
(including holiday exceptions), normalize overnight offsets, require complete
stop-level schedules, and supply audited walking links and transfer margins.
Include prior-service-day spillover runs when relevant, translated to this
snapshot's time origin and given unique run IDs. Trips active at midnight can
be represented starting at their first nonnegative scheduled event. Frequency
windows and average travel times must not be passed off as exact trips. The
fixture is fictional and provides no live Taipei coverage. This review checked
the schema and ingestion code, not a populated production database or an actual
service-date export. Passing synthetic routing tests does not establish real-data
completeness, operating-day accuracy, or the usability of interchange links.

Not modeled: realtime disruption/delays, fares, accessibility, geographic access,
trip-pair-specific forbidden/guaranteed transfers, vehicle block interlining,
multiple timezones/DST, recurring service calendars, or cumulative walk limits.
Adding history-dependent transfer rules or resource limits requires expanding
label state; changing only the boarding predicate can invalidate dominance.

## Offline GTFS import

`app.journey_gtfs` converts a **user-supplied GTFS directory or ZIP** into the
existing validated JSON schema plus a separate provenance/loss report. It uses
the standard library and existing Pydantic dependency. It does not download
feeds, read application configuration or `.env`, access production databases,
start the API, or require credentials.

```powershell
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" python -m app.journey_gtfs --help
```

### Runnable fictional example

The files in `app/data/gtfs-synthetic-v1/` are **invented test data**, including
the illustrative coordinates. No genuine Taiwan feed was imported or verified.
Run from the repository directory. The two output paths must be new files;
choose new names if repeating the command. Their parent directory must exist.

```powershell
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" python -m app.journey_gtfs app/data/gtfs-synthetic-v1 --date 2026-10-10 --feed-id synthetic-v1 --source "Fictional GTFS fixture; no official coverage" --synthetic --walking-config app/data/gtfs-synthetic-walking-v1.json --output journey-20261010.json --report journey-20261010.report.json
```

Expected: **8 imported runs**, **4 excluded bus templates**, **1 omitted run**,
and **1 clipped run**; the rail template is separately reported as non-bus.
This is deliberately a `partial` import: the fixture contains frequency,
approximate, phone-arranged, and incomplete schedules to demonstrate visible loss.
At 08:00 from original stop A to D, the imported timetable returns 08:25 with one
transfer and 08:45 with zero transfers. The previous service day's night bus and
the following day's in-horizon service are also represented.

For a supplied feed, replace the input directory with its directory or ZIP path,
use a persistent `--feed-id` namespace and an accurate `--source` description,
and omit `--synthetic` only for non-fictional input. `is_demo: false` is a caller
declaration, not evidence that the publisher or coverage has been verified.

### Calendar, time, and ID contract

* Required routing tables: `agency.txt`, `stops.txt`, `routes.txt`, `trips.txt`,
  `stop_times.txt`, and at least one of `calendar.txt` / `calendar_dates.txt`.
  UTF-8 BOMs are accepted. ZIP tables must be at the root, as the spec requires;
  duplicate members, nested GTFS tables, unsafe member paths and table symlinks
  are rejected. ZIPs are read directly, never extracted.
* Weekly weekday flags apply within inclusive start/end dates. Date exceptions
  override weekly service: `1` adds, `2` removes. Exceptions-only feeds and
  additions outside weekly ranges are supported. Conflicting/duplicate exception
  keys and unresolved service IDs reject the feed. There is no inferred holiday
  calendar: publisher-supplied exceptions determine service.
* Every agency must explicitly use `Asia/Taipei`. GTFS stop times use the agency
  timezone, **not** `stop_timezone`. The fixed-UTC+08:00 router does not implement
  historical DST; snapshot and included original service dates must be on/after
  1980-01-01, with snapshot dates no later than 9999-12-30.
* Times accept `H:MM:SS` / `HH:MM:SS` with hours greater than 24 (up to six hour
  digits). They never wrap implicitly. Events are ordered by numeric
  `stop_sequence` and must be monotonic, including dwell. The importer considers
  **every potentially overlapping original service date**, including D-1,
  earlier dates for values such as 49:00:00, D, and D+1 inside `[0, 172800)`.
  Each run uses its own service date's calendar/exception decision.
* Input trips require complete exact arrival **and** departure values at every
  stop. Only complete events inside the output horizon are retained; no time
  is clamped or interpolated. Prefix/suffix removals are listed per run. A stop
  whose dwell straddles a boundary is removed and explicitly marked, even when
  that loses an otherwise feasible boundary boarding/alighting. Fewer than two
  retained events omit the run. This is conservative undercoverage, not invented
  schedule accuracy.
* Standard bus/trolleybus types `3`, `11`, and extended coach `200–209`, bus
  `700–716`, trolleybus `800` are recognized. Restricted/demand-responsive types
  `707–709`, `712`, `715` are excluded. Non-bus trips are listed separately.
* Stable snapshot IDs hash the original GTFS ID within `--feed-id` and entity
  type. Trip-run IDs also include the **original service date**, so overlapping
  snapshots identify the same run consistently. Long/Unicode IDs fit the schema.
  `report.ids.stops` and `report.ids.routes` map original IDs to API IDs;
  `report.imported_runs` maps original trip/date pairs to run IDs. Dataset IDs
  fingerprint the imported routing-file bytes, options, and walking configuration.

### Explicit exclusions and transfer policy

The report lists trip IDs and reason codes for missing/inexact/nonmonotonic times,
`timepoint=0`, conditional pickup/dropoff types `2`/`3`, continuous or flexible
service, and all `frequencies.txt` templates. **Even `exact_times=1` is excluded**:
frequency expansion is not implemented, and its template is never emitted as
one purportedly exact departure. Pickup/dropoff `0` or blank means allowed;
`1` means forbidden, while through-riding remains possible.

Generic GTFS transfer rules cannot be faithfully represented by this router's
stop-only margin. For **every nonempty `transfers.txt` row**, the importer
conservatively excludes both sides' trip scopes and records the rule, CSV row,
and affected trip IDs. A trip selector excludes that trip; a route selector
excludes the entire route; otherwise a stop/station selector excludes trips
visiting that stop or its descendants. This deliberately over-excludes, even
for recommended transfers. Missing required selectors, unknown IDs, or malformed
rules reject the feed because their scope cannot be trusted. Walking config does
**not** override a forbidden, guaranteed, in-seat, or otherwise unsupported rule.
`block_id` does not join trips; affected templates receive report warnings.

Walking links come only from `--walking-config`, using **original GTFS stop IDs**:

```json
{
  "schema_version": 1,
  "default_min_transfer_seconds": 120,
  "min_transfer_seconds": {"C": 60},
  "footpaths": [{"from_stop": "B", "to_stop": "C", "seconds": 180}]
}
```

Links are directed and strictly positive. Margins are boarding-stop margins,
additional to walking; the first boarding has none. No links are inferred from
coordinates, shared names, parent stations, `transfers.txt`, or `pathways.txt`.
Without a config there are no walking links and same-stop transfers use 120
seconds. Surveyed links/margins are still an external data-quality responsibility.

### Provenance, publication, and integration

The JSON report contains the importer version/specification reference, agency/publisher metadata,
SHA-256/byte/row counts for each read table, a routing-input fingerprint, ignored
entries, assumptions, exclusions, omitted/clipped runs, ID maps, summary counts,
and the exact serialized timetable's SHA-256. Unused tables/unknown columns are
not a full GTFS validation target. This is a bounded routing importer, not a
complete GTFS conformance validator. Limits are 128 MiB per read table, 512 MiB
total, and seven days of event span per trip; exceeding them is reported.

Exit `0` means an `ok` or explicitly `partial` snapshot was published. Check the
summary and report before relying on coverage. Rejected feed imports publish a
`rejected` report, omit the timetable, and exit `2`; argument/path/publication
failures exit `2` with a diagnostic. A valid no-service date may yield an empty
timetable. No exclusion or sparse dataset is presented as full live coverage.

Outputs must be new paths outside a directory input and must not alias the ZIP,
walking config, each other, or any existing file (including hardlinks/symlinks).
There is no overwrite switch. Each complete file is fsynced then published by
atomic, no-clobber hard-link creation on the same filesystem; unsupported
filesystems fail closed. **The pair is not transactional:** the report publishes
first, the timetable last. If the second publication fails, the report can remain;
the command exits `2` and preserves any competing writer's file. Match
`timetable_sha256` when associating outputs. Use stable input files while importing.

After a successful import, obtain the API stop IDs from the report. Configure the
existing main app before startup, using an absolute path:

```powershell
$env:JOURNEY_TIMETABLE_PATH = (Resolve-Path ./journey-20261010.json).Path
```

Send `POST /api/v1/journeys` with those mapped origin/destination IDs, the snapshot
date, and elapsed departure seconds. Restart workers when replacing their
configured snapshot. Importing itself does not launch the main application's
database/sync lifecycle.

Specification references: [GTFS Schedule reference](https://gtfs.org/documentation/schedule/reference/)
(calendars, stop times, frequencies, transfers, timezone and ZIP conventions),
and [extended route types](https://developers.google.com/transit/gtfs/reference/extended-route-types).

## Verification and benchmark

```powershell
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" --with pytest --with httpx python -m pytest -q test_journeys.py test_main.py
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" --with pytest --with httpx python benchmark_journeys.py --trips 1000 --queries 20
```

Tests include explicit dominance counterexamples (including walk vs. direct bus),
exact/missed transfers, onboard continuation despite better off-vehicle labels,
chained access/transfer/egress walking, destination arrival vs. departure,
pickup/dropoff restrictions, repeated stops, overtaking, origin = destination,
zero-time bus cycles, the five-boarding limit, midnight, and the exclusive
48-hour horizon. Zero-duration footpaths are intentionally rejected by the schema.
There are also 400 seeded comparisons against a separate exact-state oracle:
it traverses individual onboard stop events, enumerates off-vehicle states without
earliest-arrival pruning, and uses pairwise Pareto comparison. Every returned leg
is checked for continuity, timing, permissions, and transfer feasibility.
Loader tests cover real files, retries, error redaction, absolute paths, an
eight-thread cold-load race, and shared-router query isolation. Main-app tests
exercise the registered endpoint through its middleware without starting sync.

The test environment uses the README's Python 3.13 / FastAPI 0.115 baseline;
the FastAPI constraint above applies only to the isolated process, and no project
requirements are changed. `uvicorn[standard]` in the complete requirements brings
in `python-dotenv`; tests also require `httpx` and a test runner.
The repository's `test_tdx_metro.py` and `test_tdx_tra_station_of_line.py` are
import-time live TDX probes, not isolated unit tests. Unrestricted collection
executes them and requires working TDX credentials. To run all automated tests:

```powershell
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" --with pytest --with httpx python -m pytest -q --ignore=test_tdx_metro.py --ignore=test_tdx_tra_station_of_line.py
```

Review evidence (2026-10-10, Windows, isolated Python 3.13.14):

* Before the fixes, the new tests reproduced the walk/direct dominance error
  (also found independently in seeded case 80), the concurrent cold-load race,
  relative-path acceptance, and acceptance of an unrepresentable snapshot date.
* The targeted command above passed **34 tests / 444 subtests**.
* Earlier full discovery, using the command below, passed **319 tests / 461 subtests**
  with **two collection errors**: the two live probes received HTTP 401 and
  attempted to parse the non-JSON responses. No FastAPI-version or missing-dotenv
  errors remained. That unrestricted run was not all-green.

```powershell
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" --with pytest --with httpx python -m pytest -q --continue-on-collection-errors
```

**Pre-importer routing-review offline verification:** the documented command with exactly
`--ignore=test_tdx_metro.py --ignore=test_tdx_tra_station_of_line.py` passed on
the then-current worktree: **326 tests / 464 subtests passed, zero failures or errors**,
in **70.82 seconds**. The command was repeated after additional source/test files
appeared during the initial offline verification; their content hashes were
stable across this final run. There was **one dependency deprecation warning** from
Starlette's use of `anyio.abc.BlockingPortal`. Both live probes were excluded
from collection and execution. No `.env` contents were read: the repository file
was absent (existence check only), and the configuration unit test used its
existing `app.config.load_dotenv` stub. This rerun used the complete requirements
and the isolated FastAPI 0.115.14 environment; only this evidence document was
edited by that verification task.

The benchmark generates 20-stop trips, reports median/p95 milliseconds and
excludes loading/indexing. Its synthetic output is a reproducible baseline,
not a production throughput claim. The review run with 1,000 trips / 20,000 stop
times / 20 queries reported median **151.86 ms**, p95 **195.28 ms**.

### GTFS importer verification (2026-10-10)

The importer adds 29 focused tests covering weekday/range selection, holiday
additions/removals, exception-only calendars, prior-day and >48-hour service
offsets, next-day service, boundary dwell losses, exact/conditional stop times,
transfer-rule exclusions, malformed input, ZIP/directory equivalence, BOMs,
stable/Unicode IDs, explicit walking, output aliases and competing writers.
There are **105 routing comparisons against a separately hand-authored schedule**
using the independent event-state oracle; no importer calendar/time/ID helpers
construct that expected schedule.

Exact test commands:

```powershell
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" --with pytest --with httpx python -m pytest -q test_journey_gtfs.py test_journeys.py test_main.py --durations=5
uv run --isolated --python 3.13 --with-requirements requirements.txt --with "fastapi==0.115.14" --with pytest --with httpx python -m pytest -q --ignore=test_tdx_metro.py --ignore=test_tdx_tra_station_of_line.py
```

* Focused journey/importer/integration run: **63 tests / 626 subtests passed**
  in **85.09 seconds**.
* Final full offline run: **366 tests / 661 subtests passed**, zero failures or
  errors, in **193.93 seconds**. Exactly the two live TDX probes above were
  excluded. One existing Starlette/AnyIO deprecation warning remained.
* An earlier full run encountered three stop-search subtest failures while
  unrelated stop-search work was in progress. The isolated stop-search run then
  passed (6 tests / 13 subtests), followed by the all-green full run above. The
  importer and stop-search file hashes were stable across that final run; this
  journey task made no stop-search edits.
* The real `python -m app.journey_gtfs --help` process launched successfully.
  The fictional directory CLI import published validated JSON and a report in a
  temporary directory, with the expected **8 imported / 4 excluded templates /
  1 omitted run / 1 clipped run** counts. Its snapshot is marked `is_demo: true`.
  CLI output loading, report SHA-256 matching, and routed imported results are
  also tested. Directory/ZIP equivalence is exercised with a generated ZIP.

These runs used complete project requirements in isolated `uv` Python 3.13 /
FastAPI 0.115.14 processes. Requirements were not changed. No production data,
local `.env` contents, or live probe endpoints were accessed by this verification.
