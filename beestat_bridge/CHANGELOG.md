# Changelog

## 0.6.9

- Fix the recorder stopping on a single failed read. 0.6.8 added a read of the
  thermostat's own temperature sensor BEFORE the sample insert; because the whole
  poll shared one try/except, any error there (or in a later per-sensor read)
  aborted the poll and skipped the thermostat sample — so the runtime graph could
  stop recording. Each thermostat is now isolated, the finer-temperature lookup
  and per-sensor reads are best-effort, and the thermostat sample insert can no
  longer be blocked by optional enrichment. One flaky entity no longer stalls the
  recorder.

## 0.6.8

- Finer indoor temperature in local mode. HomeKit reports the climate entity's
  current_temperature at coarse (~1°) resolution, so the thermostat graph looked
  stepped. The recorder now prefers the thermostat's own built-in temperature
  sensor (and humidity) when discovered, which reads to a decimal — matching the
  granularity you get from the remote sensors.
- The "Compare sensor identifiers" diagnostic now also shows how each recorded
  sensor is currently emitted (`resolved`: stored id + serial -> the ecobee id
  it's sent as), so it's clear at a glance whether the cloud/local identity match
  is firing.

## 0.6.7

- Match sensors by a stable id instead of just their name. Discovery now records
  each Home Assistant device's serial number, and sensor reconciliation tries the
  ecobee id and the HA serial (against ecobee's per-sensor pairing `code` / id)
  before falling back to name matching. Renaming a sensor no longer risks
  breaking the cloud/local identity link.
- Add a "Compare sensor identifiers" diagnostic (config page + /admin/sensors/
  identity) that lists ecobee's id/code/name next to Home Assistant's
  serial/model/name per thermostat, so it's visible which stable id actually
  correlates (and whether the serial match is firing).

## 0.6.6

- Give local sensors the same identity as their cloud counterparts. Previously
  local mode invented its own sensor ids (rs:<ha_device_id>), so beestat saw them
  as different sensors from the ecobee-cloud ones (rs2:100, …) — registering
  duplicates and, worse, deactivating the cloud sensors that held all the
  historical graph data. The local source now maps each discovered sensor to its
  ecobee-official id from the snapshot (by name) and emits that id everywhere —
  the thermostat remoteSensors and the runtimeReport sensorList (columns and
  sensorId). Local readings now attach to the same beestat sensor as the cloud
  history, so remote-sensor graphs stay continuous across cloud/local and the
  cloud history that was hidden comes back (after beestat's next sensor sync
  reactivates those sensors). Sensors with no cloud match keep their local id.

## 0.6.5

- Add a "Check cloud sensor history" diagnostic (config page + /admin/archive/
  sensors). It reads the last archived cloud responses and reports exactly what
  ecobee returned — the /1/thermostat remoteSensors and the runtimeReport
  sensorList (per-sensor columns and row count) — so it's clear whether missing
  remote-sensor history is because ecobee didn't return it or because beestat
  never synced it.

## 0.6.4

- Fix the thermostat runtime graph (indoor temp, setpoints, equipment) being
  empty in local mode. beestat discards any runtime row that has a null value in
  any equipment column, and its CSV parser treats a blank cell as null — so our
  rows, which left idle equipment columns blank, were thrown away wholesale
  (real ecobee reports 0, not blank). Every equipment column on a sampled row is
  now 0 by default, and HVACmode always resolves to a valid mode, so the rows are
  accepted and the graph fills. (The sensor graph was unaffected because sensor
  rows have no such null check — which is why only it kept working.)
- Make runtimeReport bucket timestamps robust. Buckets are labeled in the
  thermostat's local time, taken from the cloud snapshot's location; if that's
  ever missing (as it was during the 0.6.0 partial-snapshot window), the bridge
  now falls back to the home's Home Assistant time zone instead of the container
  clock (UTC), which was shifting the sensor graph's time axis.

## 0.6.3

- Fix the config page hanging at "..." with no data. A stale-badge tooltip added
  in 0.6.0 contained an apostrophe ("hasn't") that, once emitted, landed inside a
  single-quoted JS string and broke the entire inline script at parse time — so
  the status/thermostat bootstrap never ran and every field stayed "...". (The
  bridge itself was fine the whole time; only the page's JavaScript was dead.)
  Reworded the tooltip and added a test that syntax-checks the emitted inline JS
  so this can't recur silently.

## 0.6.2

- Refresh the cloud snapshot on each grab, not on a 6-hour timer. When beestat
  grabs `/1/thermostat` in local mode, the bridge now refreshes the cloud
  snapshot from ecobee first, so the cloud-only fields it carries (current
  comfort mode, sensor inUse) are current for that grab. The refresh is debounced
  (beestat's back-to-back thermostat + sensor grabs collapse into one cloud call)
  and best-effort (a slow or dead cloud never blocks or breaks the local serve —
  it just serves the last snapshot). The 6-hour background refresh remains as a
  startup self-heal and long-gap safety net.

## 0.6.1

- Fix a 0.6.0 regression that wiped local thermostat data. The new snapshot
  refresher requested only a handful of ecobee objects, but beestat's sync
  dereferences the full set (runtime, extendedRuntime, notificationSettings, …)
  with no guards — so the partial snapshot it wrote overwrote the good one and
  killed beestat's sync on the first missing key. The refresher now requests the
  exact same full object set as beestat, so snapshots stay complete and existing
  partial ones self-heal on the next refresh. If you saw a thermostat go blank
  after updating to 0.6.0, this restores it (a running ecobee cloud login heals
  it within a refresh cycle).
- Keep cloud-only thermostats visible in local mode. A thermostat you haven't
  added to the bridge but that still has a cloud snapshot (e.g. your Main Floor
  stat) is now served from that snapshot as old data, instead of vanishing — so
  it stays in beestat's thermostat-swap list. Its runtimeReport returns a benign
  "no new data" signal beestat swallows, preserving its existing history.
- Harden against missing runtime keys: the served thermostat always includes the
  runtime fields beestat reads unconditionally, using out-of-range sentinels
  (mapped to null by beestat) when there's no real value.

## 0.6.0

- Keep cloud-only data fresh while serving local. A new background task refreshes
  each thermostat's cloud snapshot every 6 hours (sensor inUse, current comfort
  setting, and location timeZone — none of which HomeKit exposes), using whatever
  ecobee tokens exist. As long as cloud access lasts, these fields stop being
  frozen at your last manual sync.
- Mark cloud data stale after 24h without a successful refresh. The bridge's
  config page now hides each sensor's "in use" flag and the thermostat's current
  comfort mode once the snapshot ages out — instead of showing frozen,
  possibly-false values — and shows a "cloud stale" badge. beestat itself keeps
  displaying the last-known values (its UI has no "unknown" state); the bridge
  page is the honest view of whether cloud data is still current.
- Config page: thermostat cards now show the current comfort setting (when known)
  alongside the live temperature/humidity/action.

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
