# beestat-bridge

A local-first **ecobee API emulator** that lets a self-hosted
[beestat](https://github.com/beestat/app) fork run without an ecobee developer
API key — and keep running even if ecobee cloud access disappears entirely.

The bridge impersonates `api.ecobee.com` for beestat (auth, `/1/thermostat`,
`/1/runtimeReport`) and serves data from one of two sources:

| Mode | Source | Notes |
|---|---|---|
| `cloud` | Real ecobee API, via consumer-grade auth (no dev key) | Full fidelity incl. stages/aux and historical backfill. Every response is archived locally as it passes through. |
| `local` | Home Assistant (HomeKit integration + optional ESPHome wire sensors) | Works forever, no cloud. Serves the last archived thermostat snapshot overlaid with live HA state. Equipment runtime columns come from wire sensors when configured, otherwise from unambiguous `hvac_action` mapping, otherwise blank — never guessed. |

Design principles (agreed upfront):

- **Beestat is never modified beyond one setting.** The companion fork carries an
  ~11-line patch adding `ecobee_api_base_url`; everything else lives here.
- **Beestat only ever holds bridge-minted tokens**, so the identity beestat sees
  is stable across mode switches and across the death of ecobee's auth.
- **The local recorder runs in *both* modes.** The SQLite store is always warm,
  so flipping to `local` loses nothing.
- **The cloud path tees everything** — full `/1/thermostat` objects and
  `runtimeReport` responses are archived while access exists. Local mode serves
  the archive + live overlay rather than fabricating data.
- **No classifiers.** Equipment columns are real measurements or null.

## Installation on Home Assistant (the intended path — no CLI)

1. Settings → Add-ons → Add-on Store → ⋮ → Repositories → add this repo's URL.
   Two add-ons appear: **Beestat Bridge** and **Beestat**.
2. Install **Beestat Bridge** and start it. Open it in the HA sidebar, log in
   with your ecobee account (MFA supported), and add your thermostats (entity
   pickers are filled from HA). It is now connected, archiving, and recording.
3. Install **Beestat**. In its Configuration tab set `bridge_url` and `app_url`
   to your host (see the reachability note below), then start it and click
   **Open Web UI**. beestat loads, talks to the bridge instead of ecobee's
   cloud, and your thermostats appear.

The bridge's own sidebar page handles ecobee login, thermostat/entity
configuration, mode switching, and status — changes apply immediately, no
restarts. The bridge's page and admin endpoints are reachable only through the
authenticated HA sidebar (Ingress). The beestat app is served on its own port
(not Ingress: its frontend assumes root-path URLs), with an "Open Web UI"
button.

### Reachability note (important)

beestat uses a single base URL both for your browser's ecobee-login redirect
and for its own server-to-server calls, so that URL must resolve from **both**
your browser and the add-on container. The host's **LAN IP** works for both
(e.g. `http://192.168.1.50:8127` / `:8128`); `homeassistant.local` may not
resolve inside add-on containers. If login or sync stalls, use the IP.

## Status

Working: facade endpoints, bridge token minting, interactive ecobee consumer
login and refresh — primary implementation is
[python-ecobee-api](https://github.com/nkgilley/python-ecobee-api) (the same
library Home Assistant core uses for keyless auth: when ecobee changes the
flow, the fix ships upstream and is adopted here by bumping one pin), with an
in-tree Auth0 PKCE flow as automatic fallback (ported from Apache-2.0
[ha-ecobee](https://github.com/pjordanandrsn/ha-ecobee) — see NOTICE; wrong
credentials never trigger the fallback, only breakage does). Plus: cloud
passthrough with archive tee, HA polling recorder, local serving with
snapshot overlay, mode switching and full configuration via the Ingress
setup page, applied live.

TODO (tracked in code with `TODO(bridge)` markers):

- Validate the login flow against the real Auth0 tenant (it is faithful to
  ha-ecobee's implementation but has only been exercised against mocks).
- `runtimeReport` value scaling verification against archived real responses.
- Equipment-seconds integration from ESPHome binary sensor history.
- Beestat HA app (second add-on folder) and published multi-arch images.

## Layout

    src/beestat_bridge/   Python package (FastAPI service — the bridge)
    beestat_bridge/       Home Assistant app manifest for the bridge
    beestat/              Home Assistant app for the beestat web app itself
                          (PHP + MariaDB + nginx; builds from the fork)
    repository.yaml       Makes this repo installable as an HA app repository
    docker-compose.yml    Non-HA deployment (both services)
    bridge.example.yaml   All bridge settings, documented (NOT named config.*
                          so the HA Supervisor doesn't parse it as an add-on)

The **beestat** app image is built from the companion fork
(`curious-ninja/beestat-app`, branch `claude/beestat-home-assistant-m4qbve`),
which carries only two small self-host patches over upstream: a configurable
`ecobee_api_base_url`, and a session-cookie fix for port/IP hosting. It runs
beestat in `dev` mode so no JavaScript build step is needed. Override the
`BEESTAT_REPO` / `BEESTAT_REF` build args to build a different fork.

## For developers only (not needed on Home Assistant)

    pip install -r requirements.txt
    cp bridge.example.yaml config.yaml   # edit
    python -m beestat_bridge
    # facade + setup page on http://localhost:8127

Point the beestat fork's `ecobee_api_base_url` setting at the bridge, e.g.
`http://localhost:8127`. Outside HA there is no Ingress, so the setup page is
open on the port — treat the LAN accordingly.

