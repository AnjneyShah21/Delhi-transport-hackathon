# Demo script (3 minutes)

1. Plan a bus journey and explain that stops, schedules, and route geometry come from the static bus GTFS.
2. Compare two scheduled options side by side and use the comfort slider to change their ranking. Explain that sampled tickets are not onboard passenger counts.
3. Open a route detail and inspect the sampled ticket origin–destination pairs. Point out that these are route samples, not occupancy or seat availability.
4. If a route has GPS coverage, compare its historical median speed and sample count. It is an October baseline, not live traffic or a schedule-delay forecast.
5. Change departure hour and weekday/weekend, inspect the data-backed quieter-hour tip, toggle the crowd layer, and save a usual journey. The saved trip stays in that browser's local storage. Special-day context only flags unusual ticket sample volumes; it does not claim an event cause.
6. Try the live-bus control only when an authorized OTD API key is configured. Without the key, the app reports that live positions are unavailable.
7. Show the validation page and its October 22–28 holdout, DTC Insights, CSV export, `/healthz`, `/data/status`, and `/docs`. The holdout measures ticket-rank stability, not actual onboard crowd accuracy. Explain that metro/AFC data still needs authorized access.
