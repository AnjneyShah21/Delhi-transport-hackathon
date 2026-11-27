# Network twin, scenario simulator, and Journey Guardian

## Bus Network Digital Twin

The DTC Insights page can draw every route and direction shape in the supplied Delhi static GTFS snapshot (2,574 route IDs, 6,862 stops, and 3,498 shapes in the current index). Shapes are simplified for browser rendering. The lines describe the published schedule network, not live operating status.

Selecting a line opens a scenario panel. Route connections are defined by GTFS routes sharing at least one stop.

## What-if scenarios

- **Congestion:** apply a user-selected speed reduction to the selected route corridor. The in-vehicle estimate is `baseline / (1 - slowdown)`, plus the scheduled average wait. Baseline duration uses route/hour GPS speed when present, otherwise the median full-trip GTFS duration.
- **Route unavailable:** compare the fastest indexed timetable itinerary for the selected stop pair with the selected route included and then excluded. The disruption does not change the real schedule.
- **Demand increase:** add a user-entered dwell penalty per 10% increase to one full route trip. The relative ticket-activity score is also scaled as a demand scenario. Ticket sample volume does not measure passenger load, so the user supplies the dwell assumption.
- **Additional buses:** use the GTFS departure headway and an estimated route fleet size to estimate the average-wait change if extra buses are spread evenly over the same service window. It does not generate a dispatch plan.
- **Shared-stop exposure:** show routes connected through common stops. A configurable share of the scenario delay is displayed as a transfer stress-test value; this is not a predicted delay on those vehicles.

Every result displays its inputs and assumptions. No road-level traffic feed, passenger load, vehicle assignment, or control-room action is connected.

## Conversational Journey Assistant

`/assistant/parse` is a small local, rule-based parser, not a trained or generative AI model. It recognizes common stop phrases, departure or arrival times, direct-only/one-transfer constraints, less-crowded preferences, and wheelchair/family/older-adult/women-travelling-alone profiles. It returns the parsed constraints for the planner and shows the interpretation to the passenger. Walking minimization is treated as a direct-route proxy because no pedestrian-routing network is connected. Unknown or ambiguous phrases are not silently converted into safety or accessibility claims.

## Journey Guardian

Guardian starts only after a passenger chooses a route and explicitly opts into browser geolocation. Location checks run in the browser while the page remains open; no coordinates are sent to the app API. It can:

- notify when the device is within 300 m of the scheduled destination stop;
- flag a possible transfer risk if the live route feed reports a possible hold and the scheduled transfer slack is 10 minutes or less;
- say a connection *may* have been missed if the device is still within 350 m of the transfer stop two minutes after its scheduled departure;
- fetch revised timetable options from the transfer stop and let the passenger replace the current plan.

Vehicle positions are route-wide, not matched to the specific trip or passenger. Notifications are not delivered after the page is closed, and the app does not claim to confirm boarding or a missed connection.
