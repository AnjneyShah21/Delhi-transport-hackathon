# Data needed to finish delay and accessibility features

The planner now exposes comfort, family, older-adult, wheelchair, and women-travelling-alone preferences. The first three are transparent schedule/crowd heuristics. Wheelchair journeys are only shown when GTFS explicitly verifies the trip and boarding/alighting stops; unknown values stay unknown. The women-safety choice does not label routes safe without verified evidence. Check `/feature-readiness` and `/accessibility/status` for the current connected-data state.

## Request from Delhi Transport Stack

At [Transport Stack Data Services](https://delhi.transportstack.in/data-services), ask the data team for:

1. **Current bus GTFS with accessibility data**: `stops.txt` with `wheelchair_boarding`; `trips.txt` with `wheelchair_accessible`; `pathways.txt` and `levels.txt` including `pathway_mode`, `is_bidirectional`, `traversal_time`, `stair_count`, `max_slope`, and `signposted_as`; and `transfers.txt` with transfer type and minimum transfer time. Also request the update date, coverage, and definitions of `0`/unknown values.
2. **Bus-specific accessibility inventory**: wheelchair boarding/ramp or low-floor confirmation by vehicle or trip, plus verified accessible stops, tactile guidance, lifts/ramps, and step-free transfer links. The portal's Delhi Accessibility Static data is described as metro data; it does not fill these bus fields.
3. **GTFS-Realtime TripUpdates and ServiceAlerts**: feed URLs, authentication, refresh interval, coverage, and an example protobuf or JSON response. Ask whether the ETA Calculator API includes prediction timestamps and scheduled-vs-predicted stop arrivals. These fields are needed to detect a late bus and recommend an alternate trip.
4. **Pedestrian network/routing**: a supported walking-routing API or network extract, including step-free/wheelchair routing if available, service limits, and a key. Ask for stop access points and pedestrian crossings/footpaths where maintained.
5. **Verified public-realm safety data** (only if they maintain it): coverage and last-update date for lit paths/stops, staffed locations, CCTV, emergency points, and any defined safety scoring. Avoid collecting or inferring a passenger's gender; the preference should only surface user-selected route factors.

The current official portal lists GTFS, GTFS-RT positions, an approval-based bus ETA API, and a metro accessibility dataset. Confirm bus accessibility and TripUpdates/ServiceAlerts availability with the data team rather than assuming the metro feed applies to buses.

## Current service configuration

Secrets belong in the ignored local `.env` file (never commit or paste them into chat):

```dotenv
OTD_API_KEY=your_existing_key
OTD_TRIP_UPDATES_URL=https://provider.example/TripUpdates.pb
OTD_SERVICE_ALERTS_URL=https://provider.example/ServiceAlerts.pb
ORS_API_KEY=your_optional_openrouteservice_key
```

The last three values are examples, not working provider URLs or keys. Do not set them until the provider confirms the feeds/API and grants access. `/feature-readiness` reports what is configured; it does not claim a feed is valid merely because a URL is present. After receiving the bus GTFS, replace the local archive and rebuild with `make gtfs`.

## What is currently safe to claim

- Static bus schedules and routes are usable for journey options.
- Authorized OTD vehicle positions can be shown on the map. A position feed alone is not a reliable delay signal; the currently observed positions do not consistently match the supplied static trips and do not provide usable movement speed.
- October ticket estimates measure relative activity in the available ticket sample, not bus occupancy or seat availability.
- Comfort/family/older-adult preferences favor fewer transfers and lower sampled crowd. They do not yet minimize actual walking or assess safety.
- Wheelchair routes are withheld until trip and stop accessibility is explicitly known. The currently supplied GTFS has no usable wheelchair flags or pathways.
- No live delay/disruption alerts or safety ranking are claimed until their source feeds are supplied and validated.
