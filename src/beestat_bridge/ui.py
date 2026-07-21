"""The bridge's setup/status page.

Served at / and designed for Home Assistant Ingress (it appears in the HA
sidebar): every URL in the page is RELATIVE, because under Ingress the app
lives beneath /api/hassio_ingress/<token>/.

Plain HTML + a little inline JS; no build step, no external assets (works
offline and under Ingress's proxy).
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Beestat Bridge</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto;
         padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem; }
  .card { border: 1px solid color-mix(in srgb, currentColor 25%, transparent);
          border-radius: 8px; padding: 1rem 1.25rem; margin: 1rem 0; }
  .ok { color: #2e7d32; } .bad { color: #c62828; } .muted { opacity: .65; }
  label { display: block; margin-top: .75rem; font-size: .9rem; }
  input { width: 100%; box-sizing: border-box; padding: .5rem; margin-top: .25rem; }
  button { margin-top: 1rem; margin-right: .5rem; padding: .5rem 1rem; cursor: pointer; }
  button.active { font-weight: bold; outline: 2px solid currentColor; }
  #message { margin-top: 1rem; min-height: 1.5em; }
  details { margin-top: 1rem; }
  code { font-size: .85em; }
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
  local = Home Assistant data only. "Auto" follows the configured default.</p>
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
  <div id="message"></div>
</div>

<script>
async function api(path, options) {
  const response = await fetch(path, options);
  return response.json();
}
function show(text, ok) {
  const el = document.getElementById('message');
  el.textContent = text;
  el.className = ok ? 'ok' : 'bad';
}
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
    document.getElementById('btn-auto')
      .classList.toggle('active', !s.mode_override);
  } catch (e) { /* transient */ }
}
async function setMode(mode) {
  await api('admin/mode', {method: 'POST', headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify({mode})});
  refreshStatus();
}
async function login(event) {
  event.preventDefault();
  show('Logging in…', true);
  const body = {email: document.getElementById('email').value,
                password: document.getElementById('password').value};
  const result = await api('admin/ecobee/login',
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify(body)});
  if (result.mfa_required) {
    document.getElementById('mfa-type').textContent = result.challenge_type;
    document.getElementById('mfa-form').style.display = '';
    show('Enter your verification code.', true);
  } else if (result.connected) {
    document.getElementById('login-form').reset();
    show('Connected to ecobee.', true);
  } else {
    show(result.error || 'Login failed.', false);
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
    show('Connected to ecobee.', true);
  } else {
    show(result.error || 'Verification failed.', false);
  }
  refreshStatus();
  return false;
}
async function pasteToken(event) {
  event.preventDefault();
  const result = await api('admin/ecobee/tokens',
    {method: 'POST', headers: {'Content-Type': 'application/json'},
     body: JSON.stringify({refresh_token: document.getElementById('refresh-token').value})});
  show(result.stored ? 'Token saved.' : (result.error || 'Failed.'), !!result.stored);
  refreshStatus();
}
refreshStatus();
setInterval(refreshStatus, 10000);
</script>
</body>
</html>
"""
