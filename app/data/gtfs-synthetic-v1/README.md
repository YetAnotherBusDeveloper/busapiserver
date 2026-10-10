# Fictional GTFS import fixture

Every agency, route, stop, and schedule in this directory is invented for tests.
This is not an official Taiwan feed and does not establish real-world coverage.
Coordinates are illustrative only. The `example.invalid` publisher URL is not a
data source. Always import this fixture with `--synthetic`.

For snapshot 2026-10-10, `local-0800` plus the explicit B -> C walking link and
`express-0814` reaches D at 08:25 with one transfer; the through local arrives
08:45 with zero transfers. The 2026-10-09 night trip's B/D events spill into the
snapshot, and 2026-10-11 trips are included inside the 48-hour horizon.

`HOLIDAY` is removed on 2026-10-10; `SPECIAL` is added only on that date.
The rail trip is out of scope. Frequency, approximate, phone-arranged, and
incomplete bus templates intentionally demonstrate explicit import exclusions.
The report must show these losses, so this fixture produces a partial import.

Walking configuration: `../gtfs-synthetic-walking-v1.json`. Complete commands,
output IDs, and integration instructions are in the repository's JOURNEYS.md.
