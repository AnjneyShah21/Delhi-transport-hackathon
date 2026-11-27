# Data needed to finish delay and accessibility features

The planner now exposes comfort, family, older-adult, wheelchair, and women-travelling-alone preferences. The first three are transparent schedule/crowd heuristics. Wheelchair journeys are only shown when GTFS explicitly verifies the trip and boarding/alighting stops; unknown values stay unknown. The women-safety choice does not label routes safe without verified evidence. Check `/feature-readiness` and `/accessibility/status` for the current connected-data state.

## Request from Delhi Transport Stack

At [Transport Stack Data Services](https://delhi.transportstack.in/data-services), ask the data team for:

1. **Current bus GTFS with accessibility data**: `stops.txt` with `wheelchair_boarding`; `trips.txt` with `wheelchair_accessible`; `pathways.txt` and `levels.txt` including `pathway_mode`, `is_bidirectional`, `traversal_time`, `stair_count`, `max_slope`, and `signposted_as`; and `transfers.txt` with transfer type and minimum transfer time. Also request the update date, coverage, and definitions of `0`/unknown values.
2. **Bus-specific accessibility inventory**: wheelchair boarding/ramp or low-floor confirmation by vehicle or trip, plus verified accessible stops, tactile guidance, lifts/ramps, and step-free transfer links. The portal's Delhi Accessibility Static data is described as metro data; it does not fill these bus fields.
3. **ETA Calculator for Buses API (on hold)**: request approval for the stop-ETA and vehicle-ETA endpoints. The portal guide documents `GET /api/serviceset/eta-calculator/stop/etas?stop_id=...` and `GET /api/serviceset/eta-calculator/bus/etas?vehicle_id=...`, authenticated with an `x-api-key` emailed after approval. The app has an optional live stop-arrival integration for `DTS_ETA_API_KEY`; it is currently on hold. The portal also lists a GTFS-RT positions subscription; its guide specifies `GET /api/dataset/otd/get-file?agency=delhi-buses&category=realtime_gtfs&filename=VehiclePositions.pb` with an `x-api-key` header. The app can use that feed through `DTS_GTFSRT_API_KEY` for map markers. This provides stop-wide ETAs, not route-specific schedule delay or disruption alerts.
4. **GTFS-Realtime TripUpdates and ServiceAlerts**: feed URLs, authentication, refresh interval, coverage, and an example protobuf or JSON response. Ask whether the ETA API includes schedule-vs-predicted arrivals and trip/route identifiers. TripUpdates/ServiceAlerts are needed for trip-level delay, cancellation, disruption alerts, and reliable alternative recommendations.
5. **Pedestrian network/routing**: a supported walking-routing API or network extract, including step-free/wheelchair routing if available, service limits, and a key. Ask for stop access points and pedestrian crossings/footpaths where maintained.
6. **Verified public-realm safety data** (only if they maintain it): coverage and last-update date for lit paths/stops, staffed locations, CCTV, emergency points, and any defined safety scoring. Avoid collecting or inferring a passenger's gender; the preference should only surface user-selected route factors.

The current official portal lists GTFS, GTFS-RT positions, an approval-based bus ETA API, and a metro accessibility dataset. Confirm bus accessibility and TripUpdates/ServiceAlerts availability with the data team rather than assuming the metro feed applies to buses.

## Current service configuration

Secrets belong in the ignored local `.env` file (never commit or paste them into chat):

```dotenv
OTD_API_KEY=your_existing_key
OTD_TRIP_UPDATES_URL=https://provider.example/TripUpdates.pb
OTD_SERVICE_ALERTS_URL=https://provider.example/ServiceAlerts.pb
DTS_ETA_API_KEY=your_approved_transport_stack_key
ORS_API_KEY=your_optional_openrouteservice_key
```

The feed URLs and API keys shown are examples, not working provider URLs or keys. Do not set them until the provider confirms the feeds/API and grants access. `/feature-readiness` reports what is configured; it does not claim a feed is valid merely because a URL is present. After receiving the bus GTFS, replace the local archive and rebuild with `make gtfs`.

## What is currently safe to claim

- Static bus schedules and routes are usable for journey options.
- Authorized Transport Stack vehicle positions can be shown on the map. Fresh route-matched speed and position samples now flag a possible traffic hold after a bus remains nearly stationary for 90 seconds away from a mapped GTFS stop. This is a heuristic, not a confirmed schedule delay; the app suggests comparing its other scheduled options.
- October ticket estimates measure relative activity in the available ticket sample, not bus occupancy or seat availability.
- Comfort/family/older-adult preferences favor fewer transfers and lower sampled crowd. They do not yet minimize actual walking or assess safety.
- Wheelchair routes are withheld until trip and stop accessibility is explicitly known. The currently supplied GTFS has no usable wheelchair flags or pathways.
- Schedule-based significant-delay and disruption alerts are not available until trip-matched ETAs or GTFS-Realtime TripUpdates and ServiceAlerts are supplied and validated. No safety ranking is claimed without verified safety data.
