# Delhi Ki Public — Phase 1 proposal

## Purpose

Build an open, mobile-first Delhi journey planner that helps commuters compare scheduled bus travel with relative ticket activity. The current build uses the October 2026 ONDC e-ticket sample, static bus GTFS and the first week of October DTC GPS history. Ticket records cover only a small sample and do not measure absolute passenger loads.

## Method

The current crowd profile groups ticket records into route/hour and weekday/weekend relative activity scores and sampled origin–destination pairs. It preserves sample counts and confidence and does not infer passenger loads, seats or onboard occupancy. GPS history is grouped into route/local-hour/weekend median and mean reported speeds with sample counts. These historical speeds are displayed as context; GPS trip IDs do not consistently map to scheduled GTFS trips, so they are not presented as exact schedule delays. Static GTFS supplies stops, scheduled trips and route shapes.

## Evaluation and delivery

The app and grouped statistical profiles are implemented. A chronological ticket-rank check trains on October 1–21 and tests on October 22–28: 44 route-hour bands (22,283 supported route-hour-day cells) yield mean Spearman rank correlation 0.769 and mean percentile-rank MAE 13.7 points. This measures stability of sampled ticket rankings, not accuracy against actual onboard crowding. No supervised machine-learning model has been trained; a Google Maps comparison has not been performed.

## Risks and constraints

Ticket sample bias and missing vehicle identifiers limit crowd inference. Stop-name matching can be ambiguous. The 16.1 GB compressed GPS archive expands to about 124 GB, so process one daily SQLite file at a time and retain only compact profiles. Live GTFS-RT needs an authorized API key. Metro routing and crowd estimates need current authorized schedule and AFC feeds. The app reports these data gaps rather than inventing results.
