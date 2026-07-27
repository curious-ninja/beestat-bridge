"""The bridge's setup/status/config page.

Served at / and designed for Home Assistant Ingress (it appears in the HA
sidebar): every URL in the page is RELATIVE, because under Ingress the app
lives beneath /api/hassio_ingress/<token>/.

Plain HTML + a little inline JS; no build step, no external assets (works
offline and under Ingress's proxy). Configuration edited here is persisted
by the bridge and applied immediately — no restarts.
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beestat Bridge</title>
<style>
  :root {
    --bg: #202a30;
    --surface: #2b3a43;
    --surface-2: #22303880;
    --field: #1c262c;
    --border: rgba(255,255,255,.09);
    --border-strong: rgba(255,255,255,.16);
    --text: #eceff1;
    --muted: #8fa3ad;
    --accent: #2196f3;
    --accent-hover: #42a5f5;
    --ok: #66bb6a;
    --bad: #ef5350;
    --amber: #f5a623;
    --radius: 12px;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 4px 16px rgba(0,0,0,.22);
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  body {
    font-family: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    background: var(--bg); color: var(--text);
    margin: 0; padding: 24px 16px 64px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .app { max-width: 780px; margin: 0 auto; }

  .topbar {
    display: flex; align-items: center; gap: 12px;
    padding: 4px 4px 20px; margin-bottom: 8px;
  }
  .topbar .logo {
    width: 38px; height: 38px; display: grid; place-items: center;
    font-size: 20px; border-radius: 10px;
    background: linear-gradient(150deg, #ffd15c, var(--amber));
    box-shadow: var(--shadow);
  }
  .topbar h1 { font-size: 1.3rem; font-weight: 700; letter-spacing: -.01em; margin: 0; }
  .topbar .sub { font-size: .8rem; color: var(--muted); margin-top: 1px; }

  .card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px; margin: 16px 0;
    box-shadow: var(--shadow);
  }
  h2 {
    font-size: .72rem; text-transform: uppercase; letter-spacing: .09em;
    color: var(--muted); font-weight: 700; margin: 0 0 14px;
  }
  p.muted { color: var(--muted); font-size: .86rem; margin: 0 0 16px; }
  .muted { color: var(--muted); }

  /* status */
  .status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px 20px; }
  .stat { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .stat-label { font-size: .7rem; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); }
  .stat-value { font-size: 1.02rem; font-weight: 600; overflow-wrap: anywhere; }
  .stat-value .muted { font-weight: 400; font-size: .84rem; }
  .pill {
    align-self: flex-start; display: inline-flex; align-items: center; gap: 7px;
    padding: 3px 11px; border-radius: 999px; font-size: .82rem; font-weight: 600;
    background: var(--surface-2); color: var(--muted);
  }
  .pill::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  .pill.ok  { color: var(--ok);  background: color-mix(in srgb, var(--ok) 16%, transparent); }
  .pill.bad { color: var(--bad); background: color-mix(in srgb, var(--bad) 16%, transparent); }

  /* segmented control */
  .segmented {
    display: inline-flex; padding: 4px; gap: 4px; border-radius: 10px;
    background: var(--field); border: 1px solid var(--border);
  }
  .segmented button {
    margin: 0; border: 0; background: transparent; color: var(--muted);
    padding: 8px 18px; border-radius: 7px; font-weight: 600; font-size: .9rem;
    cursor: pointer; transition: all .15s;
  }
  .segmented button:hover { color: var(--text); }
  .segmented button.active { background: var(--accent); color: #fff; box-shadow: var(--shadow); }

  /* forms */
  label { display: block; margin-top: 14px; font-size: .82rem; color: var(--muted); font-weight: 500; }
  label.inline { display: inline-flex; align-items: center; gap: .5rem; margin-right: 1.25rem; color: var(--text); }
  input:not([type=checkbox]), select {
    width: 100%; margin-top: 6px; padding: 10px 12px; color: var(--text);
    background: var(--field); border: 1px solid var(--border-strong);
    border-radius: 8px; font: inherit; font-size: .9rem; transition: border-color .15s, box-shadow .15s;
  }
  input:not([type=checkbox]):focus, select:focus {
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 28%, transparent);
  }
  input::placeholder { color: color-mix(in srgb, var(--muted) 75%, transparent); }
  input[type=checkbox] { width: 18px; height: 18px; accent-color: var(--accent); margin: 0; }
  select { appearance: none;
    background-image: linear-gradient(45deg, transparent 50%, var(--muted) 50%),
                      linear-gradient(135deg, var(--muted) 50%, transparent 50%);
    background-position: right 15px center, right 10px center;
    background-size: 5px 5px, 5px 5px; background-repeat: no-repeat; padding-right: 32px; }

  button {
    margin-top: 18px; margin-right: 8px; padding: 9px 18px; cursor: pointer;
    font: inherit; font-size: .88rem; font-weight: 600; border-radius: 8px;
    background: var(--field); color: var(--text); border: 1px solid var(--border-strong);
    transition: all .15s;
  }
  button:hover { border-color: var(--muted); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
  button.danger { background: transparent; color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
  button.danger:hover { background: color-mix(in srgb, var(--bad) 12%, transparent); }

  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 18px; }
  @media (max-width: 560px) { .grid2, .status-grid { grid-template-columns: 1fr; } }

  fieldset {
    border: 1px solid var(--border); border-radius: 10px;
    margin-top: 16px; padding: 4px 18px 18px; background: var(--surface-2);
  }
  legend { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em;
           color: var(--muted); font-weight: 700; padding: 0 8px; }
  details { margin-top: 16px; }
  summary { cursor: pointer; font-size: .84rem; }
  .msg { margin-top: 14px; min-height: 1.4em; font-size: .86rem; font-weight: 600; }
  .msg.ok { color: var(--ok); } .msg.bad { color: var(--bad); }
  a { color: var(--accent); }

  /* collapsible thermostat cards */
  details.thermostat {
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--surface-2); margin-top: 14px; overflow: hidden;
  }
  details.thermostat > summary {
    list-style: none; cursor: pointer; padding: 13px 16px;
    display: flex; align-items: center; gap: 10px;
  }
  details.thermostat > summary::-webkit-details-marker { display: none; }
  details.thermostat > summary::before {
    content: "\\25B8"; color: var(--muted); transition: transform .15s; flex: none;
  }
  details.thermostat[open] > summary::before { transform: rotate(90deg); }
  .t-head { display: flex; align-items: center; justify-content: space-between;
            flex: 1; gap: 12px; min-width: 0; }
  .t-head-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .t-title { font-weight: 600; font-size: .94rem; white-space: nowrap;
             overflow: hidden; text-overflow: ellipsis; }
  .t-live { font-size: .82rem; color: var(--muted); }
  .t-live .live-temp { color: var(--text); font-weight: 600; }
  .t-count { font-size: .76rem; color: var(--muted); white-space: nowrap; flex: none; }
  .t-body { padding: 2px 16px 18px; }
  .t-sensors { margin-top: 16px; }
  .sensors-title { font-size: .7rem; text-transform: uppercase; letter-spacing: .07em;
                   color: var(--muted); font-weight: 700; margin-bottom: 10px; }
  .sensor-list { display: flex; flex-wrap: wrap; gap: 8px; }
  .sensor-chip {
    display: flex; flex-direction: column; gap: 3px; padding: 9px 13px;
    background: var(--field); border: 1px solid var(--border); border-radius: 9px;
    min-width: 130px;
  }
  .sensor-name { font-size: .84rem; font-weight: 600; display: flex; align-items: center; gap: 6px; }
  .sensor-name .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); flex: none; }
  .sensor-name .dot.occupied { background: var(--ok); }
  .sensor-stat { font-size: .82rem; color: var(--muted); }
  .sensor-stat .live-temp { color: var(--text); font-weight: 600; }
  details.equip { margin-top: 16px; }
  .diag { margin-top: 14px; padding: 12px 14px; background: var(--field);
          border: 1px solid var(--border); border-radius: 8px; font-size: .76rem;
          white-space: pre-wrap; word-break: break-word; max-height: 420px;
          overflow: auto; color: var(--muted); }
</style>
</head>
<body>
<div class="app">

<header class="topbar">
  <div class="logo">🐝</div>
  <div>
    <h1>Beestat Bridge</h1>
    <div class="sub">ecobee API emulator for self-hosted beestat</div>
  </div>
</header>

<div class="card" id="status-card">
  <div class="status-grid">
    <div class="stat">
      <span class="stat-label">Serving beestat from</span>
      <span class="stat-value"><span id="mode">…</span> <span class="muted" id="mode-detail"></span></span>
    </div>
    <div class="stat">
      <span class="stat-label">ecobee cloud</span>
      <span class="pill" id="cloud-status">…</span>
    </div>
    <div class="stat">
      <span class="stat-label">Local recorder</span>
      <span class="pill" id="recorder-status">…</span>
    </div>
    <div class="stat">
      <span class="stat-label">Thermostats</span>
      <span class="stat-value muted" id="thermostats">…</span>
    </div>
  </div>
</div>

<div class="card">
  <h2>Data source</h2>
  <p class="muted">Cloud = real ecobee API (archived locally as it flows through).
  Local = Home Assistant data only. Auto follows the configured default.
  Takes effect immediately.</p>
  <div class="segmented">
    <button id="btn-cloud" onclick="setMode('cloud')">Cloud</button>
    <button id="btn-local" onclick="setMode('local')">Local</button>
    <button id="btn-auto" onclick="setMode(null)">Auto</button>
  </div>
</div>

<div class="card">
  <h2>Connect to ecobee</h2>
  <p class="muted">Your ecobee account login (the same one the ecobee app uses).
  Credentials are used once to obtain tokens and are not stored.</p>
  <form id="login-form" onsubmit="return login(event)">
    <label>Email <input type="email" id="email" required autocomplete="username"></label>
    <label>Password <input type="password" id="password" required autocomplete="current-password"></label>
    <button type="submit" class="primary">Log in</button>
  </form>
  <form id="mfa-form" onsubmit="return submitMfa(event)" style="display:none">
    <label>Verification code (<span id="mfa-type"></span>)
      <input id="mfa-code" inputmode="numeric" autocomplete="one-time-code" required></label>
    <button type="submit" class="primary">Verify</button>
  </form>
  <details>
    <summary class="muted">Advanced: paste a refresh token instead</summary>
    <form onsubmit="return pasteToken(event)">
      <label>Refresh token <input id="refresh-token" required></label>
      <button type="submit">Save token</button>
    </form>
  </details>
  <div class="msg" id="login-message"></div>
</div>

<div class="card">
  <h2>Configuration</h2>
  <p class="muted">Saved by the bridge and applied immediately — no restart.
  Entity pickers are filled from Home Assistant.</p>

  <div id="thermostat-list"></div>
  <button onclick="addThermostat()">＋ Add thermostat</button>
  <button onclick="diagnoseDiscovery()">Diagnose sensor discovery</button>
  <pre id="discover-output" class="diag" hidden></pre>

  <div class="grid2">
    <label>Outdoor temperature entity
      <input id="cfg-outdoor" list="outdoor-entities" placeholder="weather.home"></label>
    <label>Recorder poll interval (seconds)
      <input id="cfg-poll" type="number" min="15" step="5"></label>
  </div>
  <label>Mode input_select entity <span class="muted">(optional, for switching from an HA dashboard)</span>
    <input id="cfg-mode-entity" placeholder="input_select.beestat_data_source"></label>
  <label class="inline"><input type="checkbox" id="cfg-failover">
    Auto-failover to local data if ecobee cloud auth dies</label>

  <div><button class="primary" onclick="saveConfig()">Save configuration</button></div>
  <div class="msg" id="config-message"></div>
</div>

<datalist id="climate-entities"></datalist>
<datalist id="binary-sensor-entities"></datalist>
<datalist id="outdoor-entities"></datalist>

</div>

<script>
const EQUIPMENT_LABELS = {
  comp_stage_1: 'Compressor stage 1 (Y1)',
  comp_stage_2: 'Compressor stage 2 (Y2)',
  aux_commanded: 'Aux heat, thermostat call (W)',
  aux_defrost: 'Aux heat, defrost board (local-only)',
  fan: 'Fan (G)',
};
let SYSTEM_TYPES = [];

async function api(path, options) {
  const response = await fetch(path, options);
  return response.json();
}
function show(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'bad');
}

/* ---- status + mode ---- */
async function refreshStatus() {
  try {
    const s = await api('admin/status');
    document.getElementById('mode').textContent = s.effective_mode;
    document.getElementById('mode-detail').textContent =
      s.mode_override ? '(manual override)' : '(configured default: ' + s.configured_mode + ')';
    const cloud = document.getElementById('cloud-status');
    if (s.cloud_failed_over) { cloud.textContent = 'auth failed'; cloud.className = 'pill bad'; }
    else if (s.ecobee_tokens_present) { cloud.textContent = 'connected'; cloud.className = 'pill ok'; }
    else { cloud.textContent = 'not connected'; cloud.className = 'pill bad'; }
    const recorder = document.getElementById('recorder-status');
    recorder.textContent = s.recorder_running ? 'running' : 'not running';
    recorder.className = s.recorder_running ? 'pill ok' : 'pill bad';
    document.getElementById('thermostats').textContent =
      s.thermostats.length ? s.thermostats.join(', ') : 'none configured';
    for (const value of ['cloud', 'local']) {
      document.getElementById('btn-' + value)
        .classList.toggle('active', s.mode_override === value);
    }
    document.getElementById('btn-auto').classList.toggle('active', !s.mode_override);
  } catch (e) { /* transient */ }
}
async function setMode(mode) {
  await api('admin/mode', {method: 'POST', headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify({mode})});
  refreshStatus();
}

/* ---- ecobee login ---- */
async function login(event) {
  event.preventDefault();
  show('login-message', 'Logging in…', true);
  const result = await api('admin/ecobee/login',
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({email: document.getElementById('email').value,
                           password: document.getElementById('password').value})});
  if (result.mfa_required) {
    document.getElementById('mfa-type').textContent = result.challenge_type;
    document.getElementById('mfa-form').style.display = '';
    show('login-message', 'Enter your verification code.', true);
  } else if (result.connected) {
    document.getElementById('login-form').reset();
    show('login-message', 'Connected to ecobee.', true);
  } else {
    show('login-message', result.error || 'Login failed.', false);
  }
  refreshStatus();
  return false;
}
async function submitMfa(event) {
  event.preventDefault();
  const result = await api('admin/ecobee/mfa',
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({code: document.getElementById('mfa-code').value})});
  if (result.connected) {
    document.getElementById('mfa-form').style.display = 'none';
    document.getElementById('login-form').reset();
    show('login-message', 'Connected to ecobee.', true);
  } else {
    show('login-message', result.error || 'Verification failed.', false);
  }
  refreshStatus();
  return false;
}
async function pasteToken(event) {
  event.preventDefault();
  const result = await api('admin/ecobee/tokens',
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({refresh_token: document.getElementById('refresh-token').value})});
  show('login-message', result.stored ? 'Token saved.' : (result.error || 'Failed.'),
       !!result.stored);
  refreshStatus();
}

/* ---- configuration ---- */
function fmtTemp(value) {
  return (Math.round(value * 10) / 10) + '°';
}
function thermostatCard(t) {
  const card = document.createElement('details');
  card.className = 'thermostat';
  card.dataset.serial = t.serial || '';
  const sources = t.equipment_sources || {};
  card.innerHTML = `
    <summary>
      <div class="t-head">
        <div class="t-head-main">
          <span class="t-title">${t.homekit_entity || 'New thermostat'}</span>
          <span class="t-live">…</span>
        </div>
        <span class="t-count"></span>
      </div>
    </summary>
    <div class="t-body">
      <div class="grid2">
        <label>Serial number <input class="t-serial" required></label>
        <label>Climate entity (HomeKit) <input class="t-entity" list="climate-entities" required></label>
        <label>System type <select class="t-system"></select></label>
        <label class="inline" style="margin-top:2.1rem">
          <input type="checkbox" class="t-mapping"> Derive unambiguous runtime from hvac_action</label>
      </div>
      <div class="t-sensors"></div>
      <details class="equip">
        <summary class="muted">Equipment wire sensors (optional — future ESPHome 24VAC monitor)</summary>
        <div class="grid2 t-sources"></div>
      </details>
      <button type="button" class="danger" onclick="this.closest('details.thermostat').remove()">Remove</button>
    </div>`;
  card.querySelector('.t-serial').value = t.serial || '';
  const entity = card.querySelector('.t-entity');
  entity.value = t.homekit_entity || '';
  // Keep the collapsed title in sync while editing.
  entity.addEventListener('input', () => {
    card.querySelector('.t-title').textContent = entity.value || 'New thermostat';
  });
  const select = card.querySelector('.t-system');
  for (const type of SYSTEM_TYPES) {
    const option = new Option(type.replaceAll('_', ' '), type, false, type === t.system_type);
    select.add(option);
  }
  card.querySelector('.t-mapping').checked = t.hvac_action_mapping !== false;
  const sourcesDiv = card.querySelector('.t-sources');
  for (const [key, label] of Object.entries(EQUIPMENT_LABELS)) {
    const wrap = document.createElement('label');
    wrap.textContent = label + ' ';
    const input = document.createElement('input');
    input.className = 't-source';
    input.dataset.key = key;
    input.setAttribute('list', 'binary-sensor-entities');
    input.placeholder = 'binary_sensor.…';
    input.value = sources[key] || '';
    wrap.appendChild(input);
    sourcesDiv.appendChild(wrap);
  }
  return card;
}
function addThermostat() {
  const card = thermostatCard({});
  card.open = true; // new one starts expanded so you can fill it in
  document.getElementById('thermostat-list').appendChild(card);
}
/* Fill each thermostat card's collapsed summary + discovered-sensor list from
   the recorder store. Read-only; runs on a timer. */
async function loadThermostatStatus() {
  let data;
  try { data = await api('admin/thermostats'); } catch (e) { return; }
  const bySerial = {};
  for (const t of (data.thermostats || [])) { bySerial[t.serial] = t; }
  for (const card of document.querySelectorAll('details.thermostat')) {
    const status = bySerial[card.dataset.serial];
    const live = card.querySelector('.t-live');
    const count = card.querySelector('.t-count');
    const sensorsDiv = card.querySelector('.t-sensors');
    if (!status) { live.textContent = ''; count.textContent = ''; sensorsDiv.innerHTML = ''; continue; }
    const c = status.current;
    if (c && c.temperature != null) {
      const parts = ['<span class="live-temp">' + fmtTemp(c.temperature) + '</span>'];
      if (c.humidity != null) { parts.push(Math.round(c.humidity) + '%'); }
      if (c.hvac_action && c.hvac_action !== 'off' && c.hvac_action !== 'idle') { parts.push(c.hvac_action); }
      live.innerHTML = parts.join(' · ');
    } else {
      live.textContent = 'no recent data';
    }
    count.textContent = status.sensors.length
      ? status.sensors.length + ' sensor' + (status.sensors.length === 1 ? '' : 's')
      : '';
    sensorsDiv.innerHTML = '';
    if (status.sensors.length) {
      const title = document.createElement('div');
      title.className = 'sensors-title';
      title.textContent = 'Discovered remote sensors';
      sensorsDiv.appendChild(title);
      const list = document.createElement('div');
      list.className = 'sensor-list';
      for (const s of status.sensors) {
        const chip = document.createElement('div');
        chip.className = 'sensor-chip';
        const occ = s.occupancy === true ? 'occupied' : (s.occupancy === false ? 'vacant' : '');
        const dot = s.occupancy === true ? ' occupied' : '';
        const temp = s.temperature != null
          ? '<span class="live-temp">' + fmtTemp(s.temperature) + '</span>' : '—';
        const idle = s.in_use === false ? ' · not in use' : '';
        chip.innerHTML =
          '<span class="sensor-name"><span class="dot' + dot + '"></span>' + s.name + '</span>' +
          '<span class="sensor-stat">' + temp + (occ ? ' · ' + occ : '') + idle + '</span>';
        list.appendChild(chip);
      }
      sensorsDiv.appendChild(list);
    }
  }
}
async function diagnoseDiscovery() {
  const out = document.getElementById('discover-output');
  out.hidden = false;
  out.textContent = 'Running discovery…';
  try {
    out.textContent = JSON.stringify(await api('admin/discover'), null, 2);
  } catch (e) {
    out.textContent = 'Failed: ' + e;
  }
}
function collectConfig() {
  const thermostats = [];
  for (const card of document.querySelectorAll('details.thermostat')) {
    const sources = {};
    for (const input of card.querySelectorAll('.t-source')) {
      sources[input.dataset.key] = input.value.trim() || null;
    }
    thermostats.push({
      serial: card.querySelector('.t-serial').value.trim(),
      homekit_entity: card.querySelector('.t-entity').value.trim(),
      system_type: card.querySelector('.t-system').value,
      hvac_action_mapping: card.querySelector('.t-mapping').checked,
      equipment_sources: sources,
    });
  }
  return {
    thermostats,
    outdoor_temperature: document.getElementById('cfg-outdoor').value.trim() || null,
    poll_interval: parseInt(document.getElementById('cfg-poll').value, 10) || 60,
    mode_entity: document.getElementById('cfg-mode-entity').value.trim() || null,
    auto_failover: document.getElementById('cfg-failover').checked,
  };
}
async function saveConfig() {
  const result = await api('admin/config',
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify(collectConfig())});
  show('config-message',
       result.saved ? 'Saved and applied.' : (result.error || 'Save failed.'),
       !!result.saved);
  refreshStatus();
}
async function loadConfig() {
  const result = await api('admin/config');
  SYSTEM_TYPES = result.system_types;
  const config = result.config;
  const list = document.getElementById('thermostat-list');
  list.textContent = '';
  for (const thermostat of config.thermostats) {
    list.appendChild(thermostatCard(thermostat));
  }
  document.getElementById('cfg-outdoor').value = config.outdoor_temperature || '';
  document.getElementById('cfg-poll').value = config.poll_interval;
  document.getElementById('cfg-mode-entity').value = config.mode_entity || '';
  document.getElementById('cfg-failover').checked = config.auto_failover;
}
async function loadEntities() {
  const groups = await api('admin/ha/entities');
  const fill = (id, entities) => {
    const datalist = document.getElementById(id);
    datalist.textContent = '';
    for (const entity of entities) {
      datalist.appendChild(new Option('', entity));
    }
  };
  fill('climate-entities', groups.climate || []);
  fill('binary-sensor-entities', groups.binary_sensor || []);
  fill('outdoor-entities', groups.outdoor || []);
}

refreshStatus();
loadEntities();
loadConfig().then(loadThermostatStatus);
setInterval(refreshStatus, 10000);
setInterval(loadThermostatStatus, 15000);
</script>
</body>
</html>
"""
