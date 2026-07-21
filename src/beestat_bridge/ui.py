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
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto;
         padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  .card { border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
          border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }
  .ok { color: #2e7d32; } .bad { color: #c62828; } .muted { opacity: .65; }
  label { display: block; margin-top: .75rem; font-size: .9rem; }
  label.inline { display: inline-flex; align-items: center; gap: .4rem; margin-right: 1.25rem; }
  input:not([type=checkbox]), select { width: 100%; box-sizing: border-box;
    padding: .5rem; margin-top: .25rem; }
  button { margin-top: 1rem; margin-right: .5rem; padding: .5rem 1rem; cursor: pointer; }
  button.active { font-weight: bold; outline: 2px solid currentColor; }
  button.danger { color: #c62828; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 1rem; }
  fieldset { border: 1px dashed color-mix(in srgb, currentColor 30%, transparent);
             border-radius: 8px; margin-top: 1rem; }
  legend { font-size: .9rem; padding: 0 .4rem; }
  .msg { margin-top: 1rem; min-height: 1.5em; }
  details { margin-top: 1rem; }
</style>
</head>
<body>
<h1>🐝 Beestat Bridge</h1>

<div class="card" id="status-card">
  <div>Serving beestat from: <strong id="mode">…</strong>
       <span class="muted" id="mode-detail"></span></div>
  <div>ecobee cloud: <strong id="cloud-status">…</strong></div>
  <div>Local recorder: <strong id="recorder-status">…</strong></div>
  <div>Thermostats: <span id="thermostats" class="muted">…</span></div>
</div>

<h2>Data source</h2>
<div class="card">
  <p class="muted">cloud = real ecobee API (archived locally as it flows through).
  local = Home Assistant data only. "Auto" follows the configured default.
  Takes effect immediately.</p>
  <button id="btn-cloud" onclick="setMode('cloud')">cloud</button>
  <button id="btn-local" onclick="setMode('local')">local</button>
  <button id="btn-auto" onclick="setMode(null)">auto (default)</button>
</div>

<h2>Connect to ecobee</h2>
<div class="card">
  <p class="muted">Your ecobee account login (the same one the ecobee app uses).
  Credentials are used once to obtain tokens and are not stored.</p>
  <form id="login-form" onsubmit="return login(event)">
    <label>Email <input type="email" id="email" required autocomplete="username"></label>
    <label>Password <input type="password" id="password" required autocomplete="current-password"></label>
    <button type="submit">Log in</button>
  </form>
  <form id="mfa-form" onsubmit="return submitMfa(event)" style="display:none">
    <label>Verification code (<span id="mfa-type"></span>)
      <input id="mfa-code" inputmode="numeric" autocomplete="one-time-code" required></label>
    <button type="submit">Verify</button>
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

<h2>Configuration</h2>
<div class="card">
  <p class="muted">Saved by the bridge and applied immediately — no restart.
  Entity pickers are filled from Home Assistant.</p>

  <div id="thermostat-list"></div>
  <button onclick="addThermostat()">＋ Add thermostat</button>

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

  <div><button onclick="saveConfig()">Save configuration</button></div>
  <div class="msg" id="config-message"></div>
</div>

<datalist id="climate-entities"></datalist>
<datalist id="binary-sensor-entities"></datalist>
<datalist id="outdoor-entities"></datalist>

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
    if (s.cloud_failed_over) { cloud.textContent = 'auth failed'; cloud.className = 'bad'; }
    else if (s.ecobee_tokens_present) { cloud.textContent = 'connected'; cloud.className = 'ok'; }
    else { cloud.textContent = 'not connected'; cloud.className = 'bad'; }
    const recorder = document.getElementById('recorder-status');
    recorder.textContent = s.recorder_running ? 'running' : 'not running';
    recorder.className = s.recorder_running ? 'ok' : 'bad';
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
function thermostatCard(t) {
  const card = document.createElement('fieldset');
  card.className = 'thermostat';
  const sources = t.equipment_sources || {};
  card.innerHTML = `
    <legend>Thermostat</legend>
    <div class="grid2">
      <label>Serial number <input class="t-serial" required></label>
      <label>Climate entity (HomeKit) <input class="t-entity" list="climate-entities" required></label>
      <label>System type <select class="t-system"></select></label>
      <label class="inline" style="margin-top:2.1rem">
        <input type="checkbox" class="t-mapping"> Derive unambiguous runtime from hvac_action</label>
    </div>
    <details>
      <summary class="muted">Equipment wire sensors (optional — future ESPHome 24VAC monitor)</summary>
      <div class="grid2 t-sources"></div>
    </details>
    <button type="button" class="danger" onclick="this.closest('fieldset').remove()">Remove</button>`;
  card.querySelector('.t-serial').value = t.serial || '';
  card.querySelector('.t-entity').value = t.homekit_entity || '';
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
  document.getElementById('thermostat-list').appendChild(thermostatCard({}));
}
function collectConfig() {
  const thermostats = [];
  for (const card of document.querySelectorAll('fieldset.thermostat')) {
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
loadConfig();
loadEntities();
setInterval(refreshStatus, 10000);
</script>
</body>
</html>
"""
