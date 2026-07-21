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

## Status

Scaffolding. Working: config loading, SQLite store, bridge token minting,
facade endpoints, cloud passthrough (given a valid ecobee refresh token),
HA polling recorder, local thermostat serving with snapshot overlay.

TODO (tracked in code with `TODO(bridge)` markers):

- Interactive ecobee consumer login (Auth0 PKCE / pyecobee) — for now bootstrap
  by POSTing a refresh token to `/admin/ecobee/tokens`.
- `runtimeReport` value scaling verification against archived real responses.
- Equipment-seconds integration from ESPHome binary sensor history.
- Beestat HA app (second add-on folder) and published multi-arch images.

## Layout

    src/beestat_bridge/   Python package (FastAPI service)
    beestat_bridge/       Home Assistant app (add-on) manifest for the bridge
    repository.yaml       Makes this repo installable as an HA app repository
    docker-compose.yml    Non-HA deployment method
    config.example.yaml   All settings, documented

## Development quickstart

    pip install -r requirements.txt
    cp config.example.yaml config.yaml   # edit
    python -m beestat_bridge
    # facade now on http://localhost:8127

Point the beestat fork's `ecobee_api_base_url` setting at the bridge, e.g.
`http://localhost:8127`.

To bootstrap cloud mode before interactive login exists:

    curl -X POST localhost:8127/admin/ecobee/tokens \
      -H 'Content-Type: application/json' \
      -d '{"refresh_token": "<token obtained elsewhere>"}'

