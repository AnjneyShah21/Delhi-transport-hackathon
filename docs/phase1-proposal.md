# Delhi Ki Public — Phase 1 proposal

## Purpose

Build an open, mobile-first Delhi journey planner that helps commuters compare travel time with relative bus crowdedness. It uses October 2026 ONDC e-ticketing records, static GTFS and a bounded one-week sample of vehicle GPS history. Since e-ticketing captures only about 3–4% of trips, crowd scores express relative percentiles within route, stop segment, hour and day type. They do not estimate absolute passenger loads.

## Method

First inspect files and schema, then normalize timestamps to Asia/Kolkata, validate stop and route keys, and cache compact aggregates to Parquet. Spread each observed ticket between start and end stop after matching to ordered route stops. Calculate median/mean profiles by hour and weekday/weekend/holiday type. Convert distributions to 0–100 route-relative scores and Low/Medium/High labels. Keep sample count and confidence alongside every estimate. Flag unusual daily totals against a robust baseline; do not attribute a cause without weather/event data.

## Evaluation and delivery

Use a chronological held-out week. Report MAE for scores and Spearman rank correlation; compare morning/evening peaks, day types and trunk routes. Add OD examples comparing fastest and least-crowded routes. An initial demo UI/API uses synthetic placeholders only; it must not present those as measured performance. No actual data files are currently present, so model metrics await data placement.

## Risks and constraints

Ticket sample bias and missing vehicle identifiers limit absolute load inference. Stop-name matching can be ambiguous. Historical GPS databases are large and unindexed, so extract and stream one day at a time for at most one week. GTFS-RT access needs an uncommitted API key. Metro crowding requires AFC records. The project will document unavailable data and confidence rather than invent measurements.
