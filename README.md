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
2. Install **Beestat Bridge** and start it.
3. Open **Beestat Bridge** in the HA sidebar. Log in with your ecobee account
   (MFA supported), add your thermostats (entity pickers filled from HA), and
   you're done — connected, archiving, recording.

Everything happens in the bridge's own sidebar page: ecobee login, thermostat
and entity configuration, mode switching, status. Changes save to the
bridge's storage and apply immediately — no restarts. The app's Configuration
tab holds only the default data-source mode. The page and admin endpoints
refuse direct network access when running as an HA app — they are only
reachable through the authenticated HA sidebar (Ingress).

The **Beestat** app itself (PHP + MySQL + sync cron, pre-pointed at the
bridge) will be the second app in this repository — not built yet.

## Status

Working: facade endpoints, bridge token minting, interactive ecobee consumer
login (Auth0 universal login + PKCE, ported from Apache-2.0
[ha-ecobee](https://github.com/pjordanandrsn/ha-ecobee) — see NOTICE), cloud
passthrough with archive tee, HA polling recorder, local serving with
snapshot overlay, mode switching via UI / config / HA `input_select`,
Ingress setup page.

TODO (tracked in code with `TODO(bridge)` markers):

- Validate the login flow against the real Auth0 tenant (it is faithful to
  ha-ecobee's implementation but has only been exercised against mocks).
- `runtimeReport` value scaling verification against archived real responses.
- Equipment-seconds integration from ESPHome binary sensor history.
- Beestat HA app (second add-on folder) and published multi-arch images.

## Layout

    src/beestat_bridge/   Python package (FastAPI service)
    beestat_bridge/       Home Assistant app (add-on) manifest for the bridge
    repository.yaml       Makes this repo installable as an HA app repository
    docker-compose.yml    Non-HA deployment method
    config.example.yaml   All settings, documented

## For developers only (not needed on Home Assistant)

    pip install -r requirements.txt
    cp config.example.yaml config.yaml   # edit
    python -m beestat_bridge
    # facade + setup page on http://localhost:8127

Point the beestat fork's `ecobee_api_base_url` setting at the bridge, e.g.
`http://localhost:8127`. Outside HA there is no Ingress, so the setup page is
open on the port — treat the LAN accordingly.

