# Changelog

## 0.5.9

- Sensor names now match beestat/ecobee. When a cloud snapshot exists, local
  remoteSensors (and the runtimeReport sensorList) borrow the ecobee-official
  sensor name instead of the HomeKit device name, which was prefixed with the
  thermostat name ("Upstairs Bedroom") and overflowed beestat's Sensors section.
  The HomeKit prefix is stripped and matched to the official name; only when the
  install never connected to the cloud do we keep the Home Assistant name.
- Fix the sensor "in use" flag in local mode. It was hard-coded true for every
  sensor; it now reflects the ecobee-official inUse value from the last cloud
  snapshot (falling back to true only when we have no cloud data to go on). The
  bridge's own config UI shows "not in use" for idle sensors too.

## 0.5.8

- Fix local-mode runtime sync dying with `Undefined array key "HVACmode"`.
  beestat requests the `hvacMode` column but reads the response column back as
  `HVACmode` (ecobee capitalizes it in runtimeReport output); the local source
  now emits the ecobee response-column name, unblocking thermostat sync.
- Make local runtimeReport buckets honor the thermostat's own timezone. Interval
  timestamps are now formatted in the thermostat's `location.timeZone` (from the
  cloud snapshot) instead of the container's clock, so self-hosted sensor/runtime
  graphs line up with the live beestat site. Bundles `tzdata` so zone lookups
  work on minimal images.

## 0.5.7

- Historical sensor graphs in local mode: the runtimeReport now includes a
  sensorList built from recorded remote-sensor samples (per-sensor temperature
  and occupancy, 5-minute buckets), so beestat's sensor history charts populate
  from Home Assistant data. Capability ids are now deterministic so the
  thermostat object's remoteSensors and the sensorList agree.

## 0.5.6

- Fix sensor auto-discovery: HA's /api/template does not expose the `devices()`
  enumerator ("'devices' is undefined"), which made every discovery 400. Rewrite
  it to iterate sensor/binary_sensor states and resolve each to its device via
  `device_id`/`device_attr` (both available), keeping the same via_device
  relationship logic.

## 0.5.5

- Surface HA's actual error when the discovery template is rejected (a 400 from
  /api/template carries the Jinja error in its body, which was being dropped).
  The Diagnose output now shows it.

## 0.5.4

- Sensor discovery: broaden entity classification (fall back to unit /
  entity-id naming when a HomeKit sensor doesn't set a device_class), and add a
  "Diagnose sensor discovery" button + /admin/discover endpoint that dumps the
  raw device/entity topology HA reports, so an empty result is explainable
  instead of silent.

## 0.5.3

- Collapsible thermostat cards in the config UI (default collapsed). The
  collapsed summary shows the climate entity name, current temperature/humidity/
  action, and sensor count so you can confirm the right thermostat at a glance.
  Expanding reveals the config plus a "Discovered remote sensors" list with each
  sensor's live temperature and occupancy. Backed by a new /admin/thermostats
  endpoint sourced from the recorder store.

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
