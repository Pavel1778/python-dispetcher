"""The single-page dashboard served to the logged-in owner.

Kept separate from :mod:`orchestration.web_server` so the HTTP routing stays
readable. Everything here is a static string; all data is fetched from the JSON
API with the session cookie. No secret is ever embedded in the page.

Visual language: dark neutral surfaces, one accent colour, card-based layout,
generous spacing, one accent per state. Responsive from 360px up. Labels are in
Russian; product and technical names (OpenHands, MCP, Sr/Jr) stay as-is.
"""

from __future__ import annotations

#: Stylesheet served at /static/app.css. Plain CSS, no build step.
APP_CSS = r"""
:root {
  --bg: #0b0d12;
  --surface: #12151c;
  --surface-2: #171b24;
  --line: #232a36;
  --line-soft: #1b212b;
  --fg: #e8ebf0;
  --fg-dim: #9aa4b4;
  --fg-faint: #6b7484;
  --accent: #5b8cff;
  --accent-dim: #3d6fe0;
  --ok: #3fb950;
  --warn: #d9a441;
  --bad: #f0554d;
  --radius: 12px;
  --radius-sm: 8px;
  --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.25);
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 var(--sans);
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1, h2, h3, h4 { margin: 0; font-weight: 600; letter-spacing: -0.01em; }
h1 { font-size: 17px; }
h2 { font-size: 20px; }
h3 { font-size: 15px; }
h4 { font-size: 13px; text-transform: uppercase; letter-spacing: .06em; color: var(--fg-dim); }
p { margin: 0 0 8px; }
.muted { color: var(--fg-dim); }
.faint { color: var(--fg-faint); }
.small { font-size: 12.5px; }
.mono { font-family: var(--mono); font-size: 12.5px; }
.err { color: var(--bad); }
.ok { color: var(--ok); }
.warn { color: var(--warn); }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

/* ---------------------------------------------------------------- layout */
.topbar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; gap: 12px;
  padding: 12px 20px; background: rgba(11,13,18,.9);
  border-bottom: 1px solid var(--line); backdrop-filter: blur(8px);
}
.brand { display: flex; align-items: center; gap: 10px; font-weight: 600; }
.brand .dot {
  width: 9px; height: 9px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 0 3px rgba(91,140,255,.18);
}
.topbar .spacer { flex: 1; }
.wrap { max-width: 1120px; margin: 0 auto; padding: 24px 20px 64px; }
.grid { display: grid; gap: 16px; }
.cols-4 { grid-template-columns: repeat(4, minmax(0,1fr)); }
.cols-2 { grid-template-columns: repeat(2, minmax(0,1fr)); }
.card {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 16px;
}
.card + .card { margin-top: 16px; }
.card-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }

/* --------------------------------------------------------------- buttons */
button, .btn {
  font: inherit; cursor: pointer; border-radius: var(--radius-sm);
  border: 1px solid transparent; padding: 9px 14px;
  background: var(--accent); color: #fff; transition: background .15s, border-color .15s;
}
button:hover:not(:disabled), .btn:hover { background: var(--accent-dim); text-decoration: none; }
button:disabled { opacity: .5; cursor: not-allowed; }
button.ghost, .btn.ghost {
  background: transparent; border-color: var(--line); color: var(--fg);
}
button.ghost:hover:not(:disabled) { background: var(--surface-2); border-color: #2e3746; }
button.link {
  background: none; border: none; color: var(--accent); padding: 0;
}
button.link:hover { text-decoration: underline; background: none; }

/* ---------------------------------------------------------------- forms */
label { display: block; font-size: 12.5px; color: var(--fg-dim); margin: 12px 0 5px; }
input, textarea, select {
  width: 100%; background: #0d1016; color: var(--fg);
  border: 1px solid var(--line); border-radius: var(--radius-sm);
  padding: 10px 11px; font: inherit;
}
textarea { min-height: 96px; resize: vertical; }
input:focus, textarea:focus, select:focus { border-color: var(--accent); outline: none; }
input[aria-invalid="true"], textarea[aria-invalid="true"] { border-color: var(--bad); }
.field-error { color: var(--bad); font-size: 12.5px; margin-top: 4px; min-height: 1em; }
.row { display: flex; gap: 12px; flex-wrap: wrap; }
.row > * { flex: 1 1 220px; min-width: 0; }
.actions { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }

/* --------------------------------------------------------------- tables */
.tablewrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 10px 10px; border-bottom: 1px solid var(--line-soft); }
th { color: var(--fg-dim); font-weight: 500; font-size: 12.5px; white-space: nowrap; }
tbody tr:hover { background: var(--surface-2); }
td.nowrap { white-space: nowrap; }
tr.clickable { cursor: pointer; }

/* ------------------------------------------------------------- metrics */
.metric { display: flex; flex-direction: column; gap: 4px; }
.metric .value { font-size: 26px; font-weight: 650; letter-spacing: -0.02em; }
.metric .name { font-size: 12.5px; color: var(--fg-dim); }
.metric .value.zero { color: var(--fg-faint); }

/* ----------------------------------------------------------------- pills */
.pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 2px 9px; border-radius: 999px; font-size: 12px;
  background: var(--surface-2); color: var(--fg-dim); border: 1px solid var(--line);
  white-space: nowrap;
}
.pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill.completed { color: var(--ok); border-color: rgba(63,185,80,.35); background: rgba(63,185,80,.08); }
.pill.failed, .pill.dead_letter { color: var(--bad); border-color: rgba(240,85,77,.35); background: rgba(240,85,77,.08); }
.pill.running, .pill.verifying, .pill.assigned, .pill.recovering { color: var(--accent); border-color: rgba(91,140,255,.35); background: rgba(91,140,255,.08); }
.pill.blocked, .pill.paused { color: var(--warn); border-color: rgba(217,164,65,.35); background: rgba(217,164,65,.08); }
.pill.queued, .pill.cancelled { color: var(--fg-dim); }

/* ---------------------------------------------------------------- misc */
pre {
  background: #0d1016; border: 1px solid var(--line); border-radius: var(--radius-sm);
  padding: 10px 12px; overflow: auto; max-height: 340px; font: 12.5px/1.5 var(--mono);
  margin: 0 0 10px; white-space: pre-wrap; word-break: break-word;
}
.empty { padding: 28px 12px; text-align: center; color: var(--fg-dim); }
.skeleton {
  height: 14px; border-radius: 6px; background: linear-gradient(90deg,#171b24,#1f2530,#171b24);
  background-size: 200% 100%; animation: sk 1.3s infinite linear; margin: 8px 0;
}
@keyframes sk { from { background-position: 200% 0; } to { background-position: -200% 0; } }
@media (prefers-reduced-motion: reduce) { .skeleton { animation: none; } }
.banner {
  display: flex; gap: 10px; align-items: flex-start;
  border: 1px solid var(--line); border-left: 3px solid var(--warn);
  background: var(--surface-2); border-radius: var(--radius-sm);
  padding: 10px 12px; margin-bottom: 16px; font-size: 13.5px;
}
.banner.bad { border-left-color: var(--bad); }
.banner.info { border-left-color: var(--accent); }
.kv { display: grid; grid-template-columns: 200px 1fr; gap: 6px 14px; font-size: 13.5px; }
.kv dt { color: var(--fg-dim); }
.kv dd { margin: 0; }
.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin-bottom: 16px; flex-wrap: wrap; }
.tab {
  background: none; border: none; color: var(--fg-dim); border-radius: 0;
  padding: 9px 12px; border-bottom: 2px solid transparent;
}
.tab:hover { background: none; color: var(--fg); }
.tab[aria-selected="true"] { color: var(--fg); border-bottom-color: var(--accent); }
.toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; }
.toolbar .grow { flex: 1 1 200px; }
.hidden { display: none !important; }

/* ---------------------------------------------------------------- login */
.auth-body { display: grid; place-items: center; min-height: 100vh; padding: 20px; }
.auth-card {
  width: 100%; max-width: 380px; background: var(--surface);
  border: 1px solid var(--line); border-radius: var(--radius);
  padding: 28px 24px; box-shadow: var(--shadow);
}
.auth-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.auth-brand .dot {
  width: 10px; height: 10px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 0 3px rgba(91,140,255,.18);
}
.auth-card h1 { font-size: 18px; }
.auth-card button { width: 100%; margin-top: 18px; }
.auth-card .err { min-height: 1.2em; margin-top: 10px; }

/* --------------------------------------------------------- responsive */
@media (max-width: 860px) { .cols-4 { grid-template-columns: repeat(2, minmax(0,1fr)); } }
@media (max-width: 720px) {
  .cols-2 { grid-template-columns: 1fr; }
  .kv { grid-template-columns: 1fr; gap: 2px 0; }
  .kv dt { margin-top: 8px; }
  .wrap { padding: 18px 14px 56px; }
}
@media (max-width: 520px) {
  .cols-4 { grid-template-columns: repeat(2, minmax(0,1fr)); }
  .metric .value { font-size: 22px; }
  .topbar { padding: 10px 14px; gap: 8px; }
  h2 { font-size: 18px; }
  .row > * { flex: 1 1 100%; }
}
"""


#: The dashboard shell. Data is loaded by /static/app.js from the JSON API.
APP_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Диспетчер OpenHands</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header class="topbar">
  <div class="brand"><span class="dot" aria-hidden="true"></span><h1>Диспетчер OpenHands</h1></div>
  <div class="spacer"></div>
  <span class="muted small" id="whoami"></span>
  <button class="ghost" id="logout" type="button">Выйти</button>
</header>

<div class="wrap">
  <div id="banners"></div>

  <section class="grid cols-4" id="metrics" aria-label="Сводка">
    <div class="card metric"><span class="value skeleton" style="width:60%"></span><span class="name">Загрузка…</span></div>
    <div class="card metric"><span class="value skeleton" style="width:60%"></span><span class="name">Загрузка…</span></div>
    <div class="card metric"><span class="value skeleton" style="width:60%"></span><span class="name">Загрузка…</span></div>
    <div class="card metric"><span class="value skeleton" style="width:60%"></span><span class="name">Загрузка…</span></div>
  </section>

  <nav class="tabs" role="tablist" aria-label="Разделы" style="margin-top:24px">
    <button class="tab" role="tab" id="tab-new" aria-selected="true" aria-controls="panel-new">Новая задача</button>
    <button class="tab" role="tab" id="tab-queue" aria-selected="false" aria-controls="panel-queue">Очередь и задачи</button>
    <button class="tab" role="tab" id="tab-system" aria-selected="false" aria-controls="panel-system">Система</button>
  </nav>

  <section id="panel-new" role="tabpanel" aria-labelledby="tab-new">
    <div class="card">
      <div class="card-head"><h3>Поставить задачу</h3></div>
      <p class="muted small" style="margin-bottom:14px">
        Sr-агент планирует работу и делегирует её Jr-агентам в OpenHands Cloud.
      </p>
      <form id="newform" novalidate>
        <label for="goal">Название / суть задачи <span class="faint">(обязательно)</span></label>
        <textarea id="goal" name="goal" required maxlength="4000"
                  placeholder="Например: добавить экспорт отчётов в CSV и покрыть тестами"></textarea>
        <div class="field-error" id="goal-err"></div>

        <div class="row">
          <div>
            <label for="repo">Репозиторий <span class="faint">(если нужен)</span></label>
            <select id="repo"><option value="">— без репозитория (только анализ) —</option></select>
          </div>
          <div>
            <label for="branch">Ветка <span class="faint">(необязательно)</span></label>
            <input id="branch" name="branch" placeholder="main" maxlength="200" autocapitalize="none">
          </div>
        </div>

        <div class="actions">
          <button type="submit" id="go">Отправить</button>
          <span class="muted small" id="newmsg" role="status" aria-live="polite"></span>
        </div>
      </form>
    </div>
  </section>

  <section id="panel-queue" role="tabpanel" aria-labelledby="tab-queue" class="hidden">
    <div class="card">
      <div class="toolbar" style="margin-bottom:12px">
        <div class="grow">
          <label for="search">Поиск</label>
          <input id="search" type="search" placeholder="по названию задачи или цели" maxlength="200">
        </div>
        <div>
          <label for="statefilter">Статус</label>
          <select id="statefilter"><option value="">все</option></select>
        </div>
        <div>
          <label for="sortby">Сортировка</label>
          <select id="sortby">
            <option value="updated_desc">Обновлено (сначала новые)</option>
            <option value="updated_asc">Обновлено (сначала старые)</option>
          </select>
        </div>
      </div>
      <div class="tablewrap">
        <table>
          <thead><tr>
            <th>Задача</th><th>Статус</th><th>Исполнитель</th>
            <th>Обновлено</th><th>Попытка</th><th></th>
          </tr></thead>
          <tbody id="tasks"><tr><td colspan="6" class="empty">Загрузка…</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>

  <section id="panel-system" role="tabpanel" aria-labelledby="tab-system" class="hidden">
    <div class="card">
      <div class="card-head"><h3>Состояние системы</h3></div>
      <dl class="kv" id="sysinfo"><dt>Загрузка…</dt><dd></dd></dl>
    </div>
    <div class="card">
      <div class="card-head"><h3>Возможности аккаунта OpenHands</h3></div>
      <p class="muted small">Только чтение. Секреты не отображаются.</p>
      <div id="caps"><span class="muted">Загрузка…</span></div>
    </div>
  </section>

  <section id="detail" class="hidden" style="margin-top:16px">
    <div class="card">
      <div class="card-head">
        <h3 id="dtitle">Задача</h3>
        <div class="spacer" style="flex:1"></div>
        <button class="ghost" id="dclose" type="button">Закрыть</button>
      </div>
      <div id="dbody"></div>
    </div>
  </section>
</div>

<script src="/static/app.js"></script>
</body>
</html>
"""


#: Client script served at /static/app.js. No framework, no build step.
APP_JS = r"""
'use strict';
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));

let CSRF = '';
let WORKFLOWS = [];
let TASKS = [];
let REPOS = [];
let STATE_LABELS = {
  queued: 'В очереди', assigned: 'Назначена', running: 'Выполняется',
  verifying: 'Проверяется', recovering: 'Восстанавливается',
  completed: 'Завершена', failed: 'Ошибка', blocked: 'Заблокирована',
  cancelled: 'Отменена', dead_letter: 'Отклонена',
  // workflow states
  paused: 'Пауза'
};

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function label(state) { return STATE_LABELS[state] || state || '—'; }
function pill(state) {
  return '<span class="pill ' + esc(state) + '">' + esc(label(state)) + '</span>';
}
function when(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  if (isNaN(d)) return '—';
  return d.toLocaleString('ru-RU', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'});
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({'Accept': 'application/json'}, opts.headers || {});
  if (opts.method && opts.method !== 'GET') {
    opts.headers['X-CSRF-Token'] = CSRF;
    if (opts.body) opts.headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(path, opts);
  if (r.status === 401) { window.location.href = '/login'; throw new Error('unauthorized'); }
  const t = await r.text();
  let d = {};
  try { d = t ? JSON.parse(t) : {}; } catch (e) { d = {error: t}; }
  if (!r.ok) throw new Error(d.error || ('HTTP ' + r.status));
  return d;
}

/* ------------------------------------------------------------- banners */
function banner(kind, text) {
  const b = document.createElement('div');
  b.className = 'banner ' + (kind || '');
  b.innerHTML = esc(text);
  $('#banners').appendChild(b);
}
function clearBanners() { $('#banners').innerHTML = ''; }

/* ------------------------------------------------------------- metrics */
function renderMetrics(d, health) {
  const items = [
    {n:'Всего задач', v:d.tasks},
    {n:'В очереди', v:d.queued},
    {n:'В работе', v:d.running},
    {n:'Завершено успешно', v:d.completed}
  ];
  $('#metrics').innerHTML = items.map(i =>
    '<div class="card metric"><span class="value' + (i.v ? '' : ' zero') + '">' + esc(i.v) +
    '</span><span class="name">' + esc(i.n) + '</span></div>').join('') +
    '<div class="card metric"><span class="value' + (d.blocked ? '' : ' zero') + '">' + esc(d.blocked) +
    '</span><span class="name">Заблокировано</span></div>' +
    '<div class="card metric"><span class="value' + (d.failed ? '' : ' zero') + '">' + esc(d.failed) +
    '</span><span class="name">С ошибкой</span></div>' +
    '<div class="card metric"><span class="value">' + esc(d.workflows) +
    '</span><span class="name">Рабочих процессов</span></div>' +
    '<div class="card metric"><span class="value">' +
      '<span class="pill ' + (health.cloud ? 'completed' : 'failed') + '">' +
      (health.cloud ? 'на связи' : 'нет связи') + '</span></span>' +
      '<span class="name">OpenHands Cloud</span></div>';
}

/* --------------------------------------------------------------- tasks */
function stateOptions() {
  const sel = $('#statefilter');
  const cur = sel.value;
  const states = Array.from(new Set(TASKS.map(t => t.state))).sort();
  sel.innerHTML = '<option value="">все</option>' +
    states.map(s => '<option value="' + esc(s) + '">' + esc(label(s)) + '</option>').join('');
  sel.value = cur;
}
function visibleTasks() {
  const q = $('#search').value.trim().toLowerCase();
  const st = $('#statefilter').value;
  const sort = $('#sortby').value;
  let out = TASKS.filter(t => {
    if (st && t.state !== st) return false;
    if (!q) return true;
    return (t.goal || '').toLowerCase().includes(q) ||
           (t.workflow_goal || '').toLowerCase().includes(q);
  });
  out.sort((a, b) => sort === 'updated_asc'
    ? (a.updated_at || 0) - (b.updated_at || 0)
    : (b.updated_at || 0) - (a.updated_at || 0));
  return out;
}
function renderTasks() {
  const tb = $('#tasks');
  const rows = visibleTasks();
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="6" class="empty">' +
      (TASKS.length ? 'Ничего не найдено по фильтру.' : 'Задач пока нет. Создайте первую во вкладке «Новая задача».') +
      '</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(t =>
    '<tr class="clickable" data-wf="' + esc(t.workflow_id || '') + '">' +
      '<td>' + esc((t.goal || '').slice(0, 90)) +
        (t.selected_repository ? '<div class="faint small mono">' + esc(t.selected_repository) +
          (t.selected_branch ? ' @ ' + esc(t.selected_branch) : '') + '</div>' : '') + '</td>' +
      '<td>' + pill(t.state) + '</td>' +
      '<td class="mono faint">' + esc(t.owner || '—') + '</td>' +
      '<td class="nowrap faint">' + when(t.updated_at) + '</td>' +
      '<td class="faint">' + esc(t.attempt) + '/' + esc(t.max_attempts) + '</td>' +
      '<td><button class="link" data-wf="' + esc(t.workflow_id || '') + '">открыть</button></td>' +
    '</tr>').join('');
  $$('#tasks .clickable').forEach(tr => tr.onclick = (e) => {
    if (e.target.tagName === 'BUTTON') return;
    openDetail(tr.dataset.wf);
  });
  $$('#tasks button.link').forEach(b => b.onclick = (e) => { e.stopPropagation(); openDetail(b.dataset.wf); });
}

/* ------------------------------------------------------------ workflow */
async function openDetail(wfId) {
  if (!wfId) return;
  $('#detail').classList.remove('hidden');
  $('#dtitle').textContent = 'Загрузка…';
  $('#dbody').innerHTML = '<div class="skeleton" style="width:40%"></div><div class="skeleton"></div>';
  try {
    const d = await api('/api/workflows/' + encodeURIComponent(wfId));
    $('#dtitle').textContent = d.workflow.goal || 'Задача';
    const w = d.workflow;
    let h = '<dl class="kv">' +
      '<dt>Идентификатор</dt><dd class="mono">' + esc(w.id) + '</dd>' +
      '<dt>Состояние</dt><dd>' + pill(w.state) + '</dd>' +
      '<dt>Sr-беседа</dt><dd class="mono">' +
        (w.sr_conversation_id
          ? esc(w.sr_conversation_id) + (w.sr_url ? ' — <a href="' + esc(w.sr_url) + '" target="_blank" rel="noopener">открыть в Cloud</a>' : '')
          : '<span class="faint">ещё не создана</span>') + '</dd>' +
      '<dt>Статус Sr</dt><dd>' + esc(w.sr_status || '—') + '</dd>' +
      '</dl>';
    if (w.reason) h += '<div class="banner">Причина остановки: ' + esc(w.reason) + '</div>';

    h += '<h4 style="margin-top:18px">Задачи Jr</h4>';
    if (!d.tasks.length) h += '<p class="muted small">Пока нет дочерних задач.</p>';
    else {
      h += '<div class="tablewrap"><table><thead><tr><th>Задача</th><th>Статус</th>' +
           '<th>Попытка</th><th>Репозиторий</th><th>Результат</th></tr></thead><tbody>';
      for (const t of d.tasks) {
        h += '<tr><td>' + esc(t.goal) + '</td><td>' + pill(t.state) + '</td>' +
          '<td class="faint">' + esc(t.attempt) + '/' + esc(t.max_attempts) + '</td>' +
          '<td class="mono faint">' + esc(t.repository || '—') +
            (t.branch ? ' @ ' + esc(t.branch) : '') + '</td>' +
          '<td class="faint">' + esc((t.result || '—').slice(0, 160)) + '</td></tr>';
        if (t.evidence && t.evidence.length) {
          h += '<tr><td colspan="5"><pre>' + esc(JSON.stringify(t.evidence, null, 1)) + '</pre></td></tr>';
        }
      }
      h += '</tbody></table></div>';
    }

    h += '<h4 style="margin-top:18px">Вызовы инструментов Sr</h4>';
    if (!d.tool_calls.length) h += '<p class="muted small">Пока нет.</p>';
    for (const c of d.tool_calls) {
      h += '<pre>' + esc(c.name) + ' ' + esc(JSON.stringify(c.args)) +
        '\n→ ' + (c.is_error ? 'ОШИБКА' : 'ок') + ' ' + esc(JSON.stringify(c.result)) + '</pre>';
    }

    h += '<h4 style="margin-top:18px">История событий</h4>';
    if (!d.events || !d.events.length) h += '<p class="muted small">Пока нет.</p>';
    else {
      h += '<div class="tablewrap"><table><thead><tr><th>Время</th><th>Событие</th><th>Детали</th></tr></thead><tbody>';
      for (const e of d.events) {
        h += '<tr><td class="faint nowrap">' + esc(e.at || '') + '</td><td>' + esc(e.kind) +
          '</td><td class="mono faint">' + esc(JSON.stringify(e.detail || {}).slice(0, 200)) + '</td></tr>';
      }
      h += '</tbody></table></div>';
    }

    h += '<div class="actions"><input id="msgto" placeholder="Сообщение Sr-агенту…" maxlength="4000">' +
         '<button id="sendmsg" style="flex:0 0 auto">Отправить</button></div>' +
         '<div class="field-error" id="msg-err"></div>';
    $('#dbody').innerHTML = h;
    const send = $('#sendmsg');
    if (send) send.onclick = async () => {
      const text = $('#msgto').value.trim();
      if (!text) { $('#msg-err').textContent = 'Введите сообщение.'; return; }
      send.disabled = true;
      try {
        await api('/api/workflows/' + encodeURIComponent(wfId) + '/message',
                  {method:'POST', body: JSON.stringify({text})});
        $('#msgto').value = ''; $('#msg-err').textContent = 'Отправлено.';
      } catch (e) { $('#msg-err').textContent = e.message; }
      finally { send.disabled = false; }
    };
    $('#detail').scrollIntoView({behavior:'smooth', block:'nearest'});
  } catch (e) {
    $('#dbody').innerHTML = '<p class="err">Не удалось загрузить задачу: ' + esc(e.message) + '</p>';
  }
}

/* -------------------------------------------------------------- system */
async function loadSystem() {
  try {
    const s = await api('/api/system');
    const rows = [
      ['База данных', s.store.ok ? 'доступна' : ('недоступна: ' + (s.store.error || ''))],
      ['OpenHands Cloud', s.cloud.ok ? 'доступен' : ('недоступен: ' + (s.cloud.error || ''))],
      ['URL Cloud', s.cloud.url || '—'],
      ['Ключ Cloud настроен', s.config.cloud_key ? 'да' : 'нет'],
      ['Запись требует токен', s.config.writes_require_auth ? 'да' : 'нет'],
      ['Версия приложения', s.build.version || 'неизвестно'],
      ['Сборка', s.build.commit || 'неизвестно'],
      ['Интервал опроса, с', s.runtime.poll_interval],
      ['Последний цикл', s.runtime.last_tick_at ? new Date(s.runtime.last_tick_at * 1000).toLocaleString('ru-RU') : '—'],
      ['Ошибка последнего цикла', s.runtime.last_error || '—']
    ];
    $('#sysinfo').innerHTML = rows.map(r =>
      '<dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd>').join('');
  } catch (e) {
    $('#sysinfo').innerHTML = '<dt>Ошибка</dt><dd class="err">' + esc(e.message) + '</dd>';
  }
  try {
    const c = await api('/api/capabilities');
    let h = '<dl class="kv">' +
      '<dt>LLM-ключ аккаунта</dt><dd>' + (c.llm_api_key_set ? 'настроен' : 'нет') + '</dd>' +
      '<dt>Активный профиль</dt><dd class="mono">' + esc(c.active_agent_profile_id || 'наследуется из аккаунта') + '</dd>' +
      '<dt>MCP-серверы</dt><dd class="mono">' + (c.mcp_servers.length ? esc(c.mcp_servers.join(', ')) : 'нет') + '</dd>' +
      '<dt>Marketplaces</dt><dd>' + esc(c.registered_marketplaces) + '</dd>' +
      '<dt>GitHub-провайдер</dt><dd>' +
        (c.provider_tokens_set && c.provider_tokens_set.github ? 'подключён' : '<span class="warn">не подключён</span>') + '</dd>' +
      '<dt>Выбор диспетчера</dt><dd class="mono">профиль: ' +
        esc(c.dispatcher.agent_profile_id || 'аккаунт') + ', плагинов: ' + esc((c.dispatcher.plugins || []).length) + '</dd>' +
      '</dl>';
    if (!(c.provider_tokens_set && c.provider_tokens_set.github)) {
      h += '<div class="banner">GitHub-провайдер не подключён к аккаунту. Задачи с репозиторием ' +
           'могут не запуститься, пока провайдер не подключён в настройках OpenHands Cloud.</div>';
    }
    $('#caps').innerHTML = h;
  } catch (e) {
    $('#caps').innerHTML = '<p class="err">Не удалось получить возможности: ' + esc(e.message) + '</p>';
  }
}

/* --------------------------------------------------------------- refresh */
async function refresh() {
  clearBanners();
  try {
    const [dash, health] = await Promise.all([api('/api/dashboard'), api('/api/health')]);
    renderMetrics(dash.counts, health);
    if (!health.cloud) banner('bad', 'Нет связи с OpenHands Cloud. Новые задачи не запустятся до восстановления связи.');
  } catch (e) {
    banner('bad', 'Не удалось получить сводку: ' + e.message);
  }
  try {
    const r = await api('/api/repositories');
    REPOS = r.repositories || [];
    const sel = $('#repo');
    const cur = sel.value;
    sel.innerHTML = '<option value="">— без репозитория (только анализ) —</option>' +
      REPOS.map(x => '<option value="' + esc(x.full_name) + '">' + esc(x.full_name) + '</option>').join('');
    sel.value = cur;
  } catch (e) { /* the banner already explains a Cloud problem */ }
  try {
    const t = await api('/api/tasks');
    TASKS = t.tasks || [];
    stateOptions();
    renderTasks();
  } catch (e) { /* ignore */ }
}

/* ----------------------------------------------------------------- boot */
async function boot() {
  try {
    const s = await api('/api/session');
    CSRF = s.csrf_token;
    $('#whoami').textContent = s.username;
  } catch (e) { return; }  // api() already redirected to /login
  $('#logout').onclick = async () => {
    try { await api('/logout', {method:'POST'}); } catch (e) { /* ignore */ }
    window.location.href = '/login';
  };
  $$('.tab').forEach(tab => tab.onclick = () => {
    $$('.tab').forEach(t => t.setAttribute('aria-selected', String(t === tab)));
    $$('#panel-new, #panel-queue, #panel-system').forEach(p => p.classList.add('hidden'));
    $('#' + tab.getAttribute('aria-controls')).classList.remove('hidden');
    if (tab.id === 'tab-system') loadSystem();
  });
  $('#dclose').onclick = () => $('#detail').classList.add('hidden');
  $('#search').oninput = renderTasks;
  $('#statefilter').onchange = renderTasks;
  $('#sortby').onchange = renderTasks;

  $('#newform').onsubmit = async (e) => {
    e.preventDefault();
    const goal = $('#goal').value.trim();
    $('#goal-err').textContent = '';
    if (!goal) { $('#goal-err').textContent = 'Введите описание задачи.'; $('#goal').focus(); return; }
    const btn = $('#go');
    btn.disabled = true;
    $('#newmsg').textContent = 'Создаём Sr-беседу…';
    try {
      const body = {goal};
      if ($('#repo').value) body.repository = $('#repo').value;
      const branch = $('#branch').value.trim();
      if (branch) body.branch = branch;
      const d = await api('/api/workflows', {method:'POST', body: JSON.stringify(body)});
      $('#newmsg').innerHTML = 'Создано: <span class="mono">' + esc(d.workflow_id) + '</span>';
      $('#goal').value = ''; $('#branch').value = '';
      await refresh();
      openDetail(d.workflow_id);
    } catch (err) {
      $('#newmsg').innerHTML = '<span class="err">' + esc(err.message) + '</span>';
    } finally { btn.disabled = false; }
  };

  await refresh();
  setInterval(refresh, 5000);
}
document.addEventListener('DOMContentLoaded', boot);
"""
