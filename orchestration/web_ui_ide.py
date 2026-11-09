"""NEXUS AI IDE shell: stylesheet and client script for the new workspace.

Kept in its own module so :mod:`orchestration.web_ui` stays a readable set of
static strings. Everything here is static text served to the browser; no secret
is ever embedded, and every control calls a real endpoint.

The visual language: a left sidebar (the single navigation system), a main
workspace (chat / IDE / agent views) and a collapsible right panel for agent
activity. Dark-first with a light theme via ``prefers-color-scheme``. Responsive
from 320px: the sidebar becomes a drawer and secondary panels stack.
"""

#: Additional stylesheet for the IDE shell. Concatenated after the base CSS.
IDE_CSS = r"""
/* =================================================================== shell */
:root {
  --side: #0e1117;
  --side-2: #12161e;
  --chat-user: #1b2230;
  --chat-assistant: #12161e;
  --radius-lg: 14px;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg: #f6f7f9; --surface: #ffffff; --surface-2: #f1f3f6; --side: #ffffff;
    --side-2: #f6f7f9; --line: #e2e5ea; --line-soft: #eceef2;
    --fg: #12161e; --fg-dim: #566072; --fg-faint: #7b8494;
    --chat-user: #eef2ff; --chat-assistant: #ffffff;
  }
}
html, body { height: 100%; }
body { overflow-x: hidden; }

.app-shell { display: grid; grid-template-columns: 264px minmax(0, 1fr) 0; min-height: calc(100vh - 57px); }
.app-shell.right-open { grid-template-columns: 264px minmax(0, 1fr) 360px; }
.main { min-width: 0; display: flex; flex-direction: column; }
.main > .wrap { flex: 1; }

/* ---------------------------------------------------------------- sidebar */
#sidebar {
  background: var(--side); border-right: 1px solid var(--line);
  padding: 14px 12px; display: flex; flex-direction: column; gap: 12px;
  min-height: calc(100vh - 57px); position: sticky; top: 57px; overflow-y: auto;
}
.side-brand { display: flex; align-items: center; gap: 9px; padding: 2px 6px 6px; }
.side-brand .logo {
  width: 26px; height: 26px; border-radius: 8px; background: var(--accent);
  color: #fff; display: grid; place-items: center; font-weight: 700; font-size: 14px;
}
.side-brand .side-title { font-weight: 650; letter-spacing: .04em; }
.side-label { font-size: 11px; text-transform: uppercase; letter-spacing: .07em;
              color: var(--fg-faint); margin: 4px 6px 4px; display: block; }
.side-pickers { display: flex; flex-direction: column; gap: 4px; }
.side-binding { margin: 2px 6px 0; line-height: 1.4; }
.side-recent { margin-top: 6px; }
.side-recent h4 { margin: 8px 6px 6px; }
.recent-item {
  display: block; width: 100%; text-align: left; background: none; border: none;
  color: var(--fg-dim); padding: 7px 8px; border-radius: 8px; font: inherit;
  font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.recent-item:hover { background: var(--surface-2); color: var(--fg); }
.recent-item.active { background: var(--surface-2); color: var(--fg); }

/* The tab row becomes the vertical navigation inside the sidebar. */
.tabs { flex-direction: column; gap: 2px; border-bottom: none; margin: 6px 0 0; }
.tab {
  text-align: left; padding: 8px 10px; border-radius: 8px;
  border-bottom: none; border-left: 2px solid transparent; color: var(--fg-dim);
}
.tab:hover { background: var(--surface-2); }
.tab[aria-selected="true"] {
  background: var(--surface-2); color: var(--fg); border-bottom-color: transparent;
  border-left-color: var(--accent);
}
.block { width: 100%; }

/* --------------------------------------------------------- workspace head */
.ws-header {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 10px 20px; border-bottom: 1px solid var(--line);
  background: var(--surface); position: sticky; top: 57px; z-index: 15;
}
.ws-header .crumbs { display: flex; align-items: center; gap: 8px; min-width: 0; }
.ws-header .repo { font-family: var(--mono); font-size: 12.5px; color: var(--fg-dim); }
.ws-header .grow { flex: 1; }

/* -------------------------------------------------------------------- chat */
.chat { display: flex; flex-direction: column; height: calc(100vh - 57px - 54px); }
.chat-modes { display: flex; gap: 6px; padding: 12px 20px 0; flex-wrap: wrap; }
.mode-chip {
  background: var(--surface); border: 1px solid var(--line); color: var(--fg-dim);
  border-radius: 999px; padding: 5px 12px; font-size: 12.5px;
}
.mode-chip[aria-pressed="true"] { color: var(--fg); border-color: var(--accent); background: var(--surface-2); }
.chat-log { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex; flex-direction: column; gap: 14px; }
.msg { display: flex; gap: 12px; max-width: 900px; }
.msg .avatar {
  flex: 0 0 30px; width: 30px; height: 30px; border-radius: 8px;
  display: grid; place-items: center; font-size: 13px; font-weight: 600;
  background: var(--surface-2); color: var(--fg-dim); border: 1px solid var(--line);
}
.msg .body { min-width: 0; flex: 1; }
.msg .who { font-size: 12px; color: var(--fg-faint); margin-bottom: 4px; }
.msg .content {
  background: var(--chat-assistant); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 11px 13px; font-size: 14px; line-height: 1.6;
  overflow-wrap: anywhere;
}
.msg.user .content { background: var(--chat-user); }
.msg.user .avatar { background: var(--accent); color: #fff; border-color: transparent; }
.msg.tool .content {
  background: transparent; border: 1px dashed var(--line); color: var(--fg-dim);
  font-family: var(--mono); font-size: 12.5px; padding: 7px 10px;
}
.msg.system .content { border-color: rgba(240,85,77,.5); color: var(--bad); }
.msg .content p:first-child { margin-top: 0; }
.msg .content p:last-child { margin-bottom: 0; }
.msg .content pre { margin: 8px 0; }
.msg .content code { font-family: var(--mono); font-size: 12.5px; background: var(--surface-2);
                     padding: 1px 5px; border-radius: 5px; }
.msg .content pre code { background: none; padding: 0; }
.code-block { position: relative; }
.code-block .copy {
  position: absolute; top: 6px; right: 6px; font-size: 11px; padding: 3px 8px;
  background: var(--surface-2); border: 1px solid var(--line); color: var(--fg-dim);
  border-radius: 6px; opacity: 0; transition: opacity .15s;
}
.code-block:hover .copy { opacity: 1; }

.composer { border-top: 1px solid var(--line); padding: 10px 20px 14px; background: var(--surface); }
.composer .attach-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.attachment-chip {
  display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
  background: var(--surface-2); border: 1px solid var(--line); border-radius: 999px;
  padding: 3px 10px; color: var(--fg-dim);
}
.composer textarea { min-height: 56px; max-height: 220px; }
.composer .row2 { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
.typing { display: inline-flex; gap: 4px; align-items: center; color: var(--fg-dim); font-size: 12.5px; }
.typing i { width: 5px; height: 5px; border-radius: 50%; background: currentColor;
            animation: blink 1.2s infinite ease-in-out; }
.typing i:nth-child(2) { animation-delay: .2s; }
.typing i:nth-child(3) { animation-delay: .4s; }
@keyframes blink { 0%, 60%, 100% { opacity: .25; } 30% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) { .typing i { animation: none; } }

/* --------------------------------------------------------------------- IDE */
.ide { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 0;
       height: calc(100vh - 57px - 54px); }
.ide-tree { border-right: 1px solid var(--line); overflow-y: auto; padding: 10px; }
.ide-tree .tree-item {
  display: block; width: 100%; text-align: left; background: none; border: none;
  color: var(--fg-dim); font: inherit; font-size: 12.5px; padding: 4px 8px;
  border-radius: 6px; font-family: var(--mono); white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis;
}
.ide-tree .tree-item:hover { background: var(--surface-2); color: var(--fg); }
.ide-tree .tree-item.active { background: var(--surface-2); color: var(--fg); }
.ide-main { display: flex; flex-direction: column; min-width: 0; }
.ide-tabs { display: flex; gap: 4px; padding: 8px 12px 0; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.ide-editor { flex: 1; overflow: auto; padding: 12px 16px; }
.ide-editor pre { max-height: none; }
.ide-bottom { border-top: 1px solid var(--line); max-height: 220px; overflow: auto; padding: 10px 14px; }
.ide-bottom h4 { margin-bottom: 6px; }
.diff-line { font-family: var(--mono); font-size: 12.5px; white-space: pre-wrap; word-break: break-word; }
.diff-line.add { color: var(--ok); }
.diff-line.del { color: var(--bad); }

/* ---------------------------------------------------------- agent / right */
.rightpanel {
  background: var(--side-2); border-left: 1px solid var(--line);
  padding: 14px 14px; overflow-y: auto; min-height: calc(100vh - 57px);
  position: sticky; top: 57px;
}
.app-shell:not(.right-open) .rightpanel { display: none; }
.rightpanel h4 { margin: 14px 0 6px; }
.rightpanel h4:first-child { margin-top: 0; }
.agent-card { background: var(--surface); border: 1px solid var(--line);
              border-radius: var(--radius); padding: 10px 12px; margin-bottom: 10px; }
.agent-card .kv { grid-template-columns: 96px 1fr; font-size: 12.5px; gap: 3px 10px; }
.activity { font-size: 12.5px; color: var(--fg-dim); white-space: pre-wrap; word-break: break-word; }

/* ----------------------------------------------------------------- pickers */
.picker { display: flex; flex-direction: column; gap: 8px; }
.picker-list { max-height: 300px; overflow-y: auto; border: 1px solid var(--line);
               border-radius: var(--radius-sm); }
.picker-item {
  display: flex; align-items: center; gap: 10px; width: 100%; text-align: left;
  background: none; border: none; border-bottom: 1px solid var(--line-soft);
  color: var(--fg); font: inherit; padding: 9px 12px; cursor: pointer;
}
.picker-item:last-child { border-bottom: none; }
.picker-item:hover { background: var(--surface-2); }
.picker-item .name { font-weight: 550; }
.picker-item .meta { color: var(--fg-dim); font-size: 12px; }
.picker-item .grow { flex: 1; min-width: 0; }
.vis { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line); color: var(--fg-dim); }
.vis.private { color: var(--warn); border-color: rgba(217,164,65,.4); }
.vis.public { color: var(--ok); border-color: rgba(63,185,80,.4); }

/* --------------------------------------------------------------- responsive */
.menu-btn { display: none; }
@media (max-width: 1024px) {
  .app-shell, .app-shell.right-open { grid-template-columns: minmax(0, 1fr); }
  #sidebar {
    position: fixed; inset: 57px auto 0 0; width: 280px; z-index: 40;
    transform: translateX(-100%); transition: transform .18s ease; box-shadow: var(--shadow);
  }
  body.sidebar-open #sidebar { transform: none; }
  .menu-btn { display: inline-block; }
  .ide { grid-template-columns: 1fr; }
  .ide-tree { border-right: none; border-bottom: 1px solid var(--line); max-height: 200px; }
}
@media (max-width: 720px) {
  .chat { height: calc(100vh - 57px - 54px); }
  .msg { gap: 8px; }
  .chat-log, .composer, .ws-header { padding-left: 12px; padding-right: 12px; }
}
@media (max-width: 560px) {
  .topbar { gap: 6px; padding: 10px 12px; }
  #whoami { max-width: 84px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* The right-panel toggle keeps its function but not its wide label. */
  #rp-toggle { font-size: 0; padding: 8px 10px; }
  #rp-toggle::before { content: "▤"; font-size: 15px; }
  .ws-header { padding: 8px 12px; }
}
@media (max-width: 520px) {
  .chat-modes { padding-left: 12px; padding-right: 12px; }
  .msg .avatar { display: none; }
}
"""


#: The client script for the IDE shell. Concatenated after the base script, so
#: the shared helpers (``api``, ``esc``, ``$``) are already defined.
IDE_JS = r"""

/* ==================================================================== shell
   The NEXUS AI IDE shell. Every control calls a real endpoint; nothing here
   fabricates state. The existing Dispatcher tabs remain reachable from the
   sidebar so no capability was removed.
   ------------------------------------------------------------------------ */
let WS_PROJECT = null;          // the project the workspace is focused on
let WS_BINDING = null;          // its repository/branch/account binding
let WS_CONVERSATIONS = [];      // conversations of the focused project
let WS_CONVERSATION = null;     // the open conversation
let WS_MESSAGES = [];           // its messages
let WS_MODE = 'ask';
let WS_ATTACHMENTS = [];        // {file_id, filename, kind, content_type, size}
let WS_SENDING = false;
let WS_ACCOUNTS = [];           // GitHub accounts (token-free views)
let WS_OH_ACCOUNTS = [];        // OpenHands accounts (key-free views)
let WS_LAST_REPOS = null;       // the last repository list loaded (for search)

function wsProjectId() { return WS_PROJECT || (SELECTED_PROJECT || null); }

function toggleSidebar(open) {
  const next = open === undefined ? !document.body.classList.contains('sidebar-open') : open;
  document.body.classList.toggle('sidebar-open', next);
  const btn = $('#menu-toggle');
  if (btn) btn.setAttribute('aria-expanded', String(next));
}
function toggleRightPanel(open) {
  const shell = $('.app-shell');
  const next = open === undefined ? !shell.classList.contains('right-open') : open;
  shell.classList.toggle('right-open', next);
  const btn = $('#rp-toggle');
  if (btn) btn.setAttribute('aria-pressed', String(next));
  if (next) refreshAgentPanel();
}

/* ------------------------------------------------------------- markdown */
function renderMarkdown(text) {
  // A small, safe renderer: escape everything first, then apply fenced code,
  // inline code, bold, italics, links and lists. No raw HTML ever passes.
  const src = String(text == null ? '' : text);
  const blocks = [];
  let body = src.replace(/```([\s\S]*?)```/g, (m, code) => {
    const lang = (code.match(/^([\w+-]*)\n/) || [])[1] || '';
    const clean = code.replace(/^[\w+-]*\n/, '').replace(/\n$/, '');
    blocks.push('<div class="code-block"><button class="copy" type="button" data-copy="' +
      esc(clean) + '">Копировать</button><pre><code data-lang="' + esc(lang) + '">' +
      esc(clean) + '</code></pre></div>');
    return '\u0000' + (blocks.length - 1) + '\u0000';
  });
  body = esc(body)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\s)\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/^\s*[-*]\s+(.*)$/gm, '<li>$1</li>')
    .replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>')
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br>');
  body = '<p>' + body + '</p>';
  return body.replace(/\u0000(\d+)\u0000/g, (m, i) => blocks[Number(i)]);
}
document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.copy');
  if (!btn) return;
  const text = btn.getAttribute('data-copy') || '';
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(
    () => { btn.textContent = 'Скопировано'; setTimeout(() => { btn.textContent = 'Копировать'; }, 1200); },
    () => {});
});

/* ------------------------------------------------------- workspace loader */
async function loadWorkspace() {
  try {
    const d = await api('/api/v1/projects');
    PROJECTS = d.projects || [];
  } catch (e) { PROJECTS = []; }
  const sel = $('#ws-project');
  if (sel) {
    sel.innerHTML = '<option value="">— выберите проект —</option>' +
      PROJECTS.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>').join('');
    if (WS_PROJECT) sel.value = WS_PROJECT;
  }
  const alSel = $('#al-project');
  if (alSel) alSel.innerHTML = '<option value="">— выберите проект —</option>' +
    PROJECTS.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>').join('');
  const labSel = $('#lab-project');
  if (labSel) labSel.innerHTML = '<option value="">— выберите проект —</option>' +
    PROJECTS.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>').join('');
  const ghSel = $('#gh-project');
  if (ghSel) ghSel.innerHTML = '<option value="">— выберите проект —</option>' +
    PROJECTS.map(p => '<option value="' + esc(p.id) + '">' + esc(p.name) + '</option>').join('');
}

async function openWorkspaceProject(projectId) {
  WS_PROJECT = projectId || null;
  SELECTED_PROJECT = WS_PROJECT;
  WS_BINDING = null;
  WS_CONVERSATION = null; WS_MESSAGES = [];
  const sel = $('#ws-project');
  if (sel && WS_PROJECT) sel.value = WS_PROJECT;
  if (!WS_PROJECT) {
    $('#ws-binding').textContent = 'Проект не выбран.';
    renderConversations(); renderChat();
    return;
  }
  try { WS_BINDING = (await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) + '/binding')).binding; }
  catch (e) { WS_BINDING = null; }
  renderBinding();
  await loadConversations();
  await refreshAgentPanel();
  const p = PROJECTS.find(x => x.id === WS_PROJECT);
  const crumb = $('#ws-project-name');
  if (crumb) crumb.textContent = p ? p.name : 'Проект';
}

function renderBinding() {
  const box = $('#ws-binding');
  if (!box) return;
  if (!WS_BINDING) { box.textContent = 'Репозиторий не привязан.'; return; }
  const repo = WS_BINDING.full_name || '—';
  const br = WS_BINDING.selected_branch || WS_BINDING.default_branch || '—';
  box.innerHTML = '<span class="mono">' + esc(repo) + '</span> @ <span class="mono">' + esc(br) + '</span>';
  const h = $('#ws-repo');
  if (h) h.textContent = repo;
  const hb = $('#ws-branch');
  if (hb) hb.textContent = br;
}

/* ----------------------------------------------------------- conversations */
async function loadConversations() {
  if (!WS_PROJECT) { WS_CONVERSATIONS = []; renderConversations(); renderChat(); return; }
  try {
    const d = await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) + '/conversations');
    WS_CONVERSATIONS = d.conversations || [];
  } catch (e) { WS_CONVERSATIONS = []; }
  renderConversations();
  if (!WS_CONVERSATION) renderChat();
}
function renderConversations() {
  const box = $('#ws-recent');
  if (!box) return;
  if (!WS_CONVERSATIONS.length) { box.innerHTML = '<p class="muted small" style="margin:6px 8px">Чатов пока нет.</p>'; return; }
  box.innerHTML = WS_CONVERSATIONS.map(c =>
    '<button class="recent-item' + (WS_CONVERSATION && c.id === WS_CONVERSATION.id ? ' active' : '') +
    '" data-conv="' + esc(c.id) + '" title="' + esc(c.title || 'Без названия') + '">' +
    esc(c.title || 'Без названия') + '</button>').join('');
}
async function newConversation() {
  if (!WS_PROJECT) { alert('Сначала выберите проект в левой панели.'); return; }
  try {
    const d = await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) + '/conversations',
      {method:'POST', body: JSON.stringify({title:'', mode: WS_MODE})});
    WS_CONVERSATION = d.conversation; WS_MESSAGES = [];
    await loadConversations();
    renderChat();
  } catch (e) { toast(e.message, 'bad'); }
}
async function openConversation(id) {
  try {
    const d = await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) +
      '/conversations/' + encodeURIComponent(id));
    WS_CONVERSATION = d.conversation;
    const m = await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) +
      '/conversations/' + encodeURIComponent(id) + '/messages');
    WS_MESSAGES = m.messages || [];
    WS_MODE = WS_CONVERSATION.mode || 'ask';
    renderConversations(); renderChat(); renderModeChips();
    showView('chat');
  } catch (e) { toast(e.message, 'bad'); }
}

function renderModeChips() {
  $$('.mode-chip').forEach(chip => chip.setAttribute('aria-pressed',
    String(chip.getAttribute('data-mode') === WS_MODE)));
}
function renderChat() {
  const log = $('#chat-log');
  if (!log) return;
  if (!WS_PROJECT) {
    log.innerHTML = '<div class="empty">Выберите проект слева, затем откройте чат.<br>' +
      'Или создайте проект в разделе «Проекты».</div>';
    return;
  }
  if (!WS_CONVERSATION) {
    log.innerHTML = '<div class="empty">Начните новый чат: опишите цель на естественном языке.</div>';
    return;
  }
  if (!WS_MESSAGES.length) {
    log.innerHTML = '<div class="empty">Пустой чат. Напишите, что нужно сделать.</div>';
    return;
  }
  log.innerHTML = WS_MESSAGES.map(m => messageHtml(m)).join('');
  log.scrollTop = log.scrollHeight;
}
function messageHtml(m) {
  const who = {user:'Вы', assistant:'NEXUS', tool:'Инструмент', system:'Система'}[m.role] || m.role;
  const initial = {user:'Я', assistant:'N', tool:'⚙', system:'!'}[m.role] || '·';
  const attachments = (m.attachments || []).map(a =>
    '<span class="attachment-chip">📎 ' + esc(a.filename || a.kind) + '</span>').join('');
  const content = m.role === 'assistant' ? renderMarkdown(m.content) : esc(m.content);
  return '<div class="msg ' + esc(m.role) + '">' +
    '<div class="avatar" aria-hidden="true">' + esc(initial) + '</div>' +
    '<div class="body"><div class="who">' + esc(who) + '</div>' +
    '<div class="content">' + content + attachments + '</div></div></div>';
}
function appendMessage(m) {
  WS_MESSAGES.push(m);
  const log = $('#chat-log');
  if (!log) return;
  const empty = log.querySelector('.empty');
  if (empty) empty.remove();
  log.insertAdjacentHTML('beforeend', messageHtml(m));
  log.scrollTop = log.scrollHeight;
}

async function sendMessage() {
  const ta = $('#chat-input');
  const text = (ta.value || '').trim();
  if (!text || WS_SENDING) return;
  if (!WS_CONVERSATION) { await newConversation(); if (!WS_CONVERSATION) return; }
  WS_SENDING = true;
  ta.value = ''; ta.style.height = '';
  const sendBtn = $('#chat-send'); if (sendBtn) sendBtn.disabled = true;
  $('#chat-status').innerHTML = '<span class="typing"><i></i><i></i><i></i> NEXUS работает…</span>';
  try {
    const d = await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) +
      '/conversations/' + encodeURIComponent(WS_CONVERSATION.id) + '/messages',
      {method:'POST', body: JSON.stringify({text, mode: WS_MODE, attachments: WS_ATTACHMENTS})});
    // The user turn is echoed by the server; render the whole appended set once.
    (d.messages || []).forEach(appendMessage);
    WS_ATTACHMENTS = []; renderAttachments();
    if (WS_MODE === 'code' || WS_MODE === 'autonomous') { toggleRightPanel(true); refreshAgentPanel(); }
    loadConversations();
  } catch (e) {
    appendMessage({role:'system', content:'Не удалось получить ответ: ' + e.message, meta:{error:true}});
  } finally {
    WS_SENDING = false; if (sendBtn) sendBtn.disabled = false;
    $('#chat-status').textContent = '';
    if (WS_CONVERSATION) {
      try {
        const c = await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) +
          '/conversations/' + encodeURIComponent(WS_CONVERSATION.id));
        WS_CONVERSATION = c.conversation;
      } catch (e) { /* ignore */ }
    }
  }
}
function renderAttachments() {
  const box = $('#chat-attachments');
  if (!box) return;
  box.innerHTML = WS_ATTACHMENTS.map(a =>
    '<span class="attachment-chip">📎 ' + esc(a.filename) +
    ' <button class="link" type="button" data-rm="' + esc(a.file_id) + '">×</button></span>').join('');
}

/* ---------------------------------------------------------------- pickers */
async function loadAccounts() {
  try { WS_ACCOUNTS = (await api('/api/github/accounts')).accounts || []; } catch (e) { WS_ACCOUNTS = []; }
  try { WS_OH_ACCOUNTS = (await api('/api/openhands/accounts')).accounts || []; } catch (e) { WS_OH_ACCOUNTS = []; }
  renderAccountSelects();
}
function renderAccountSelects() {
  const gh = $('#repo-account');
  if (gh) {
    gh.innerHTML = '<option value="">— аккаунт GitHub —</option>' +
      WS_ACCOUNTS.map(a => '<option value="' + esc(a.id) + '">' + esc(a.label) +
        ' (' + esc(a.login) + ')</option>').join('');
  }
  const oh = $('#project-oh-account');
  if (oh) {
    oh.innerHTML = '<option value="">— наследовать аккаунт —</option>' +
      WS_OH_ACCOUNTS.map(a => '<option value="' + esc(a.id) + '">' + esc(a.label) +
        (a.is_default ? ' · по умолчанию' : '') + '</option>').join('');
  }
}

async function loadAccountRepositories(accountId) {
  const list = $('#repo-list');
  list.innerHTML = '<p class="muted small">Загружаем репозитории…</p>';
  if (!accountId) {
    // No account bound: fall back to the process-wide discovery, clearly labelled.
    try {
      const d = await api('/api/github/repositories');
      renderRepoList(d.repositories || [], true);
    } catch (e) { list.innerHTML = '<div class="empty err">' + esc(e.message) + '</div>'; }
    return;
  }
  try {
    const d = await api('/api/github/accounts/' + encodeURIComponent(accountId) + '/repositories');
    renderRepoList(d.repositories || [], false);
  } catch (e) { list.innerHTML = '<div class="empty err">' + esc(e.message) + '</div>'; }
}
function renderRepoList(repos, legacy) {
  WS_LAST_REPOS = repos;
  const list = $('#repo-list');
  const q = ($('#repo-search') && $('#repo-search').value || '').trim().toLowerCase();
  const filtered = repos.filter(r => !q || r.full_name.toLowerCase().includes(q));
  if (!filtered.length) {
    list.innerHTML = '<div class="empty">' + (repos.length ? 'Ничего не найдено.' :
      (legacy ? 'Нет доступных репозиториев (используется токен по умолчанию).' : 'У аккаунта нет репозиториев.')) + '</div>';
    return;
  }
  list.innerHTML = filtered.map(r => {
    const [owner, name] = (r.full_name || '/').split('/');
    return '<button class="picker-item" type="button" data-repo="' + esc(r.full_name) +
      '" data-branch="' + esc(r.default_branch || 'main') + '">' +
      '<span class="grow"><span class="name">' + esc(name) + '</span> ' +
      '<span class="meta">' + esc(owner) + '</span>' +
      '<div class="meta">по умолчанию: ' + esc(r.default_branch || 'main') + '</div></span>' +
      '<span class="vis ' + (r.private ? 'private' : 'public') + '">' +
      (r.private ? 'Private' : 'Public') + '</span></button>';
  }).join('');
}

async function loadBranches(fullName) {
  const box = $('#branch-list');
  box.innerHTML = '<p class="muted small">Загружаем ветки…</p>';
  const pid = wsProjectId();
  try {
    const parts = fullName.split('/').map(encodeURIComponent);
    const d = await api('/api/v1/projects/' +
      encodeURIComponent(pid || '00000000-0000-0000-0000-000000000000') +
      '/github/repositories/' + parts.join('/') + '/branches');
    const branches = d.branches || [];
    if (!branches.length) { box.innerHTML = '<p class="muted small">Ветки не найдены.</p>'; return; }
    box.innerHTML = '<label for="branch-pick">Ветка</label>' +
      '<select id="branch-pick">' + branches.map(b =>
        '<option value="' + esc(b.name) + '">' + esc(b.name) +
        (b.protected ? ' · защищённая' : '') + '</option>').join('') + '</select>';
  } catch (e) { box.innerHTML = '<p class="err small">Ветки не загружены: ' + esc(e.message) + '</p>'; }
}

/* --------------------------------------------------------------- agent panel */
async function refreshAgentPanel() {
  const box = $('#rp-body');
  if (!box) return;
  const pid = wsProjectId();
  if (!pid) { box.innerHTML = '<p class="muted small">Выберите проект.</p>'; return; }
  let sessions = [], binding = null, tasks = [];
  try { sessions = (await api('/api/v1/projects/' + encodeURIComponent(pid) + '/agents')).sessions || []; }
  catch (e) { sessions = []; }
  try { binding = (await api('/api/v1/projects/' + encodeURIComponent(pid) + '/binding')).binding; }
  catch (e) { binding = null; }
  let html = '';
  if (binding) {
    html += '<h4>Контекст проекта</h4><div class="agent-card"><dl class="kv">' +
      '<dt>Репозиторий</dt><dd class="mono">' + esc(binding.full_name || '—') + '</dd>' +
      '<dt>Ветка</dt><dd class="mono">' + esc(binding.selected_branch || binding.default_branch || '—') + '</dd>' +
      '<dt>Аккаунт GH</dt><dd>' + esc(binding.github_account_id ? 'привязан' : 'не привязан') + '</dd>' +
      '<dt>Аккаунт OH</dt><dd>' + esc(binding.openhands_account_id ? 'привязан' : 'наследуется') + '</dd>' +
      '</dl></div>';
  }
  html += '<h4>Сессии агента</h4>';
  if (!sessions.length) html += '<p class="muted small">Сессий нет.</p>';
  else html += sessions.map(s =>
    '<div class="agent-card"><div>' + pill(s.state) + '</div>' +
    '<dl class="kv" style="margin-top:6px">' +
    '<dt>Проект</dt><dd>—</dd>' +
    '<dt>Сессия OH</dt><dd class="mono">' + esc(s.external_conversation_id || '—') + '</dd>' +
    '<dt>Аккаунт OH</dt><dd>' + esc(s.account_label || '—') + '</dd>' +
    '<dt>Репозиторий</dt><dd class="mono">' + esc(s.repository || '—') + '</dd>' +
    '<dt>Ветка</dt><dd class="mono">' + esc(s.branch || '—') + '</dd>' +
    '</dl><div class="activity">' + esc(s.current_activity || '') + '</div>' +
    (s.last_error ? '<div class="err small">' + esc(s.last_error) + '</div>' : '') +
    '</div>').join('');
  box.innerHTML = html;
  const agents = $('#agents-body');
  if (agents) agents.innerHTML = html;
}

/* -------------------------------------------------------------------- views */
const VIEW_TABS = {chat:'tab-chat', ide:'tab-ide', agents:'tab-agents',
                   projects:'tab-projects', autolab:'tab-autolab', lab:'tab-lab',
                   github:'tab-github', new:'tab-new', queue:'tab-queue',
                   integrations:'tab-integrations', system:'tab-system'};
function showView(name) {
  const tabId = VIEW_TABS[name];
  const tab = tabId && document.getElementById(tabId);
  if (tab) { tab.click(); return; }
  $$('.tab').forEach(t => t.setAttribute('aria-selected', 'false'));
  $$('#panel-projects, #panel-autolab, #panel-lab, #panel-github, #panel-new, #panel-queue, #panel-integrations, #panel-system, #panel-chat, #panel-ide, #panel-agents')
    .forEach(p => p.classList.add('hidden'));
  const panel = $('#panel-' + name);
  if (panel) panel.classList.remove('hidden');
}

/* ----------------------------------------------------------------- IDE view */
async function loadIdeTree() {
  const box = $('#ide-tree');
  const pid = wsProjectId();
  if (!pid) { box.innerHTML = '<p class="muted small">Выберите проект.</p>'; return; }
  if (!WS_BINDING || !WS_BINDING.full_name) {
    box.innerHTML = '<p class="muted small">Репозиторий не привязан. Привяжите его, чтобы видеть файлы.</p>';
    return;
  }
  box.innerHTML = '<p class="muted small">Загружаем файлы…</p>';
  const parts = WS_BINDING.full_name.split('/').map(encodeURIComponent);
  const branch = WS_BINDING.selected_branch || WS_BINDING.default_branch || 'main';
  try {
    const d = await api('/api/v1/projects/' + encodeURIComponent(pid) + '/github/repositories/' +
      parts.join('/') + '/tree?ref=' + encodeURIComponent(branch));
    const tree = d.tree || [];
    box.innerHTML = tree.map(t =>
      '<button class="tree-item" type="button" data-path="' + esc(t.path) + '">' +
      (t.type === 'tree' ? '📁 ' : '📄 ') + esc(t.path) + '</button>').join('') ||
      '<p class="muted small">Файлы не найдены.</p>';
  } catch (e) {
    box.innerHTML = '<p class="muted small">Файлы недоступны: ' + esc(e.message) +
      '</p><p class="muted small">Ветки и файлы читаются через GitHub API привязанного аккаунта.</p>';
  }
}
async function openIdeFile(path) {
  const pid = wsProjectId();
  const parts = WS_BINDING.full_name.split('/').map(encodeURIComponent);
  const branch = WS_BINDING.selected_branch || WS_BINDING.default_branch || 'main';
  $('#ide-editor').innerHTML = '<p class="muted small">Открываем ' + esc(path) + '…</p>';
  try {
    const d = await api('/api/v1/projects/' + encodeURIComponent(pid) + '/github/repositories/' +
      parts.join('/') + '/contents/' + path.split('/').map(encodeURIComponent).join('/') +
      '?ref=' + encodeURIComponent(branch));
    const content = d.content != null ? d.content : '';
    $('#ide-editor').innerHTML = '<div class="card-head"><h3 class="mono">' + esc(path) + '</h3>' +
      '<div class="spacer" style="flex:1"></div><span class="muted small">только чтение · ' +
      esc(branch) + '</span></div><div class="code-block"><pre><code>' + esc(content) + '</code></pre></div>';
    $$('#ide-tree .tree-item').forEach(b => b.classList.toggle('active', b.getAttribute('data-path') === path));
  } catch (e) {
    $('#ide-editor').innerHTML = '<div class="empty err">Не удалось открыть файл: ' + esc(e.message) + '</div>';
  }
}

/* -------------------------------------------------------------------- toast */
function toast(text, kind) {
  let box = $('#toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts';
    box.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:80;display:flex;flex-direction:column;gap:8px';
    document.body.appendChild(box);
  }
  const el = document.createElement('div');
  el.className = 'banner ' + (kind === 'bad' ? 'bad' : 'info');
  el.style.margin = '0';
  el.textContent = text;
  box.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

/* ----------------------------------------------------------------- accounts */
async function renderAccountLists() {
  await loadAccounts();
  const gh = $('#gh-accounts-list');
  if (gh) {
    gh.innerHTML = WS_ACCOUNTS.length ? WS_ACCOUNTS.map(a =>
      '<div class="agent-card"><dl class="kv">' +
      '<dt>Название</dt><dd>' + esc(a.label) + '</dd>' +
      '<dt>GitHub</dt><dd class="mono">' + esc(a.login) + '</dd>' +
      '<dt>Статус</dt><dd>' + pill(a.state === 'active' ? 'running' : 'failed') + '</dd>' +
      '<dt>Токен</dt><dd>скрыт (зашифрован)</dd>' +
      (a.last_error ? '<dt>Ошибка</dt><dd class="err">' + esc(a.last_error) + '</dd>' : '') +
      '</dl><div class="actions">' +
      '<button class="ghost" data-gh-act="verify" data-id="' + esc(a.id) + '">Проверить</button>' +
      '<button class="ghost" data-gh-act="revoke" data-id="' + esc(a.id) + '">Отключить</button>' +
      '</div></div>').join('') : '<p class="muted small">Аккаунтов нет.</p>';
  }
  const oh = $('#oh-accounts-list');
  if (oh) {
    oh.innerHTML = WS_OH_ACCOUNTS.length ? WS_OH_ACCOUNTS.map(a =>
      '<div class="agent-card"><dl class="kv">' +
      '<dt>Название</dt><dd>' + esc(a.label) + (a.is_default ? ' · по умолчанию' : '') + '</dd>' +
      '<dt>Base URL</dt><dd class="mono">' + esc(a.base_url) + '</dd>' +
      '<dt>Транспорт</dt><dd class="mono">' + esc(a.transport) + '</dd>' +
      '<dt>Статус</dt><dd>' + pill(a.state === 'active' ? 'running' : 'failed') + '</dd>' +
      '<dt>Ключ</dt><dd>скрыт (зашифрован)</dd>' +
      '</dl><div class="actions">' +
      '<button class="ghost" data-oh-act="verify" data-id="' + esc(a.id) + '">Проверить</button>' +
      '<button class="ghost" data-oh-act="default" data-id="' + esc(a.id) + '">Сделать по умолчанию</button>' +
      '<button class="ghost" data-oh-act="revoke" data-id="' + esc(a.id) + '">Отключить</button>' +
      '</div></div>').join('') : '<p class="muted small">Аккаунтов нет.</p>';
  }
}
document.addEventListener('click', async (e) => {
  const gh = e.target.closest && e.target.closest('[data-gh-act]');
  if (gh) {
    const act = gh.getAttribute('data-gh-act'), id = gh.getAttribute('data-id');
    try { await api('/api/github/accounts/' + encodeURIComponent(id) + '/' + act, {method:'POST', body:'{}'});
      toast('Готово.'); await renderAccountLists(); }
    catch (err) { toast(err.message, 'bad'); }
    return;
  }
  const oh = e.target.closest && e.target.closest('[data-oh-act]');
  if (oh) {
    const act = oh.getAttribute('data-oh-act'), id = oh.getAttribute('data-id');
    try { await api('/api/openhands/accounts/' + encodeURIComponent(id) + '/' + act, {method:'POST', body:'{}'});
      toast('Готово.'); await renderAccountLists(); }
    catch (err) { toast(err.message, 'bad'); }
  }
});

async function initAccounts() {
  on('#gh-account-form', 'onsubmit', async (e) => {
    e.preventDefault();
    const msg = $('#gh-account-msg'); msg.textContent = 'Проверяем токен в GitHub…';
    try {
      await api('/api/github/accounts', {method:'POST', body: JSON.stringify({
        label: $('#gh-account-label').value.trim(), token: $('#gh-account-token').value})});
      $('#gh-account-label').value = ''; $('#gh-account-token').value = '';
      msg.textContent = 'Аккаунт добавлен.';
      await renderAccountLists();
    } catch (err) { msg.textContent = 'Не добавлено: ' + err.message; }
  });
  on('#oh-account-form', 'onsubmit', async (e) => {
    e.preventDefault();
    const msg = $('#oh-account-msg'); msg.textContent = 'Проверяем ключ в OpenHands…';
    try {
      await api('/api/openhands/accounts', {method:'POST', body: JSON.stringify({
        label: $('#oh-account-label').value.trim(), api_key: $('#oh-account-key').value,
        base_url: $('#oh-account-url').value.trim() || null,
        transport: $('#oh-account-transport').value,
        make_default: $('#oh-account-default').checked})});
      $('#oh-account-label').value = ''; $('#oh-account-key').value = '';
      msg.textContent = 'Аккаунт добавлен.';
      await renderAccountLists();
    } catch (err) { msg.textContent = 'Не добавлено: ' + err.message; }
  });
  await renderAccountLists();
}

/* ------------------------------------------------------------------- wiring */
// Bind an element's event only if it exists, so one missing element can never
// abort the whole shell (the pattern that silently left the workspace unloaded).
function on(sel, event, handler) {
  const el = $(sel);
  if (el) el[event] = handler;
  return el;
}

async function initShell() {
  on('#menu-toggle', 'onclick', () => toggleSidebar());
  on('#rp-toggle', 'onclick', () => toggleRightPanel());
  on('#rp-close', 'onclick', () => toggleRightPanel(false));

  on('#new-chat', 'onclick', async () => { await newConversation(); showView('chat'); toggleSidebar(false); });
  on('#ws-manage', 'onclick', () => { showView('projects'); toggleSidebar(false); });
  on('#ws-project', 'onchange', async (e) => { await openWorkspaceProject(e.target.value); });

  // Chat composer.
  const ta = $('#chat-input');
  if (ta) {
    ta.oninput = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 220) + 'px'; };
    ta.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } };
  }
  on('#chat-send', 'onclick', () => sendMessage());
  $$('.mode-chip').forEach(chip => chip.onclick = async () => {
    WS_MODE = chip.getAttribute('data-mode');
    renderModeChips();
    if (WS_CONVERSATION) {
      try { await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) + '/conversations/' +
        encodeURIComponent(WS_CONVERSATION.id), {method:'PATCH', body: JSON.stringify({mode: WS_MODE})}); }
      catch (e) { /* keep the local mode */ }
    }
  });
  on('#chat-attach', 'onclick', () => $('#chat-file').click());
  on('#chat-file', 'onchange', async (e) => {
    const file = e.target.files[0];
    if (!file || !WS_PROJECT) return;
    try {
      const response = await fetch('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) + '/files', {
        method:'POST', headers:{'X-CSRF-Token':CSRF, 'X-File-Name':encodeURIComponent(file.name),
        'Content-Type':'application/octet-stream'}, body: file});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || ('HTTP ' + response.status));
      const f = result.file || {};
      WS_ATTACHMENTS.push({file_id: f.id, filename: f.name || file.name,
        kind: file.name.toLowerCase().endsWith('.zip') ? 'archive' : 'file',
        content_type: file.type, size: file.size});
      renderAttachments();
    } catch (err) { toast('Вложение не загружено: ' + err.message, 'bad'); }
    finally { e.target.value = ''; }
  });
  document.addEventListener('click', (e) => {
    const rm = e.target.closest && e.target.closest('[data-rm]');
    if (rm) { WS_ATTACHMENTS = WS_ATTACHMENTS.filter(a => a.file_id !== rm.getAttribute('data-rm')); renderAttachments(); }
  });

  // Conversation list.
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('[data-conv]');
    if (b) openConversation(b.getAttribute('data-conv'));
  });

  // IDE file tree: open the clicked file.
  document.addEventListener('click', (e) => {
    const item = e.target.closest && e.target.closest('#ide-tree [data-path]');
    if (item) openIdeFile(item.getAttribute('data-path'));
  });

  // Repository picker.
  on('#repo-refresh', 'onclick', () => loadAccountRepositories($('#repo-account').value));
  on('#repo-account', 'onchange', (e) => loadAccountRepositories(e.target.value));
  on('#repo-search', 'oninput', () => { if (WS_LAST_REPOS) renderRepoList(WS_LAST_REPOS, false); });
  document.addEventListener('click', async (e) => {
    const item = e.target.closest && e.target.closest('[data-repo]');
    if (!item) return;
    const fullName = item.getAttribute('data-repo');
    $('#repo-selected').textContent = fullName;
    $('#repo-bind').dataset.repo = fullName;
    await loadBranches(fullName);
  });
  on('#repo-bind', 'onclick', async () => {
    const fullName = $('#repo-bind').dataset.repo || $('#repo-selected').textContent;
    if (!fullName || !WS_PROJECT) { toast('Выберите проект и репозиторий.', 'bad'); return; }
    const branchSel = $('#branch-pick');
    const branch = branchSel ? branchSel.value : 'main';
    try {
      const d = await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) + '/binding',
        {method:'POST', body: JSON.stringify({full_name: fullName, branch})});
      WS_BINDING = d.binding; renderBinding();
      toast('Репозиторий привязан: ' + d.binding.full_name + ' @ ' + d.binding.selected_branch);
      showView('chat');
    } catch (err) { toast('Не удалось привязать: ' + err.message, 'bad'); }
  });
  on('#open-picker', 'onclick', async () => {
    showView('projects');
    await loadAccounts();
    loadAccountRepositories($('#repo-account').value);
  });
  on('#project-oh-account', 'onchange', async (e) => {
    if (!WS_PROJECT) return;
    try {
      await api('/api/v1/projects/' + encodeURIComponent(WS_PROJECT) + '/openhands-account',
        {method:'POST', body: JSON.stringify({account_id: e.target.value || null})});
      toast('Аккаунт OpenHands для проекта обновлён.');
    } catch (err) { toast(err.message, 'bad'); }
  });

  await loadWorkspace();
  await loadAccounts();
  if (PROJECTS.length) await openWorkspaceProject(PROJECTS[0].id);
  showView('chat');
}
document.addEventListener('DOMContentLoaded', () => { setTimeout(initShell, 0); });
"""
