# Changelog

## 0.5.2

- Fix the build so source changes actually ship. The Dockerfile clones the repo
  at build time, but that layer wasn't tied to the version, so Docker cached it
  forever — every bump reused the original clone and no src change landed
  (including the sensor auto-discovery and the UI restyle). Reference
  BUILD_VERSION in the clone step to bust the cache on each version bump. This
  build finally includes 0.5.0 (sensors) and 0.5.1 (UI restyle).

## 0.5.1

- Restyle the setup/config page to match beestat's dark, card-based look:
  elevated cards, status pills, a segmented Cloud/Local/Auto control, and styled
  form controls. Same functionality; no behavior change.

## 0.5.0

- Auto-discover ecobee remote sensors for local mode — no manual mapping. Uses
  the Home Assistant device registry (each remote sensor's `via_device_id` points
  at the thermostat, i.e. the "connected to Upstairs" relationship) via the
  template API to find each thermostat's sensors, classifies their
  temperature/occupancy entities, and serves them to beestat as ecobee
  remoteSensors (the header Sensors section) in both current and stored form.
  Historical sensor graphs (runtimeReport sensorList) will follow.

## 0.4.0

- Bridge service with its own config web UI: cloud (ecobee passthrough) and
  local (Home Assistant) modes with at-will switching, interactive ecobee
  consumer login (Auth0 + PKCE) plus a refresh-token escape hatch, and an
  always-on recorder for local run data.
