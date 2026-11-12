/* NEXUS Studio — the application.
 *
 * Plain JavaScript, no framework and no build step. Three rules hold the file
 * together:
 *
 * 1. Every control calls a real endpoint. Nothing is faked on the client: a
 *    reply, a file or a task state is shown only after the server returned it.
 * 2. Everything dynamic that reaches innerHTML goes through esc(); model output
 *    goes through the Markdown renderer, which escapes by construction.
 * 3. Events are delegated from the document through `data-action`, so a missing
 *    element can never stop the rest of the interface from being wired.
 */
(function () {
  'use strict';

  var MD = window.NexusMarkdown;
  var HL = window.NexusHighlight;
  var esc = MD.escape;

  function $(selector, scope) { return (scope || document).querySelector(selector); }
  function $$(selector, scope) { return Array.prototype.slice.call((scope || document).querySelectorAll(selector)); }
  function icon(name, extra) {
    return '<svg class="icon' + (extra ? ' ' + extra : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
  }
  function enc(value) { return encodeURIComponent(value); }

  /* ------------------------------------------------------------ vocabulary */
  var MODES = {
    auto: {
      icon: 'sparkles', label: 'Авто',
      desc: 'Сам решает по сообщению: ответ, разбор или автономный прогон.',
      placeholder: 'Спросите, обсудите или поручите задачу — NEXUS поймёт сам',
      thinking: 'Определяю, что нужно', provider: true
    },
    ask: {
      icon: 'message', label: 'Вопрос',
      desc: 'Ответ ассистента по контексту проекта. Ничего не запускает.',
      placeholder: 'Спросите о коде или проекте',
      thinking: 'Думаю над ответом', provider: true
    },
    analysis: {
      icon: 'eye', label: 'Разбор',
      desc: 'Читает реальное состояние проекта и прогонов. Ничего не меняет.',
      placeholder: 'Что разобрать? Например: почему прогон остановился',
      thinking: 'Изучаю состояние проекта', provider: true
    },
    plan: {
      icon: 'plan', label: 'План',
      desc: 'План работ по шагам с зависимостями. Ничего не запускает.',
      placeholder: 'Что нужно спланировать?',
      thinking: 'Составляю план', provider: true
    },
    code: {
      icon: 'code', label: 'Код',
      desc: 'Запускает задачу OpenHands в репозитории проекта.',
      placeholder: 'Опишите изменение в коде',
      thinking: 'Запускаю задачу в OpenHands', repo: true
    },
    autonomous: {
      icon: 'zap', label: 'Автономно',
      desc: 'Планирует цель и запускает прогон по задачам с проверкой.',
      placeholder: 'Опишите цель — агент спланирует и выполнит',
      thinking: 'Планирую и запускаю прогон', repo: true, provider: true
    }
  };

  // state -> [label, tone]; tone picks the colour: run | ok | bad | warn | idle
  var STATES = {
    queued: ['в очереди', 'idle'], assigned: ['назначена', 'run'],
    running: ['выполняется', 'run'], verifying: ['проверяется', 'run'],
    recovering: ['восстанавливается', 'warn'], completed: ['готово', 'ok'],
    blocked: ['заблокировано', 'warn'], failed: ['ошибка', 'bad'],
    cancelled: ['отменено', 'idle'], dead_letter: ['остановлено после ошибок', 'bad'],
    paused: ['на паузе', 'warn'], waiting_for_approval: ['ждёт подтверждения', 'warn'],
    retrying: ['повтор после ошибки', 'warn'],
    pending: ['ожидает', 'idle'], dispatched: ['отправлена', 'run'],
    completed_verified: ['готово, проверено', 'ok'],
    completed_unverified: ['готово, без проверки', 'warn'],
    partial: ['выполнено частично', 'warn'],
    active: ['активен', 'ok'], invalid: ['отклонён', 'bad'], revoked: ['отключён', 'idle']
  };
  var TOOLS = { start_workflow: 'Запуск задачи OpenHands', start_run: 'Запуск автономного прогона' };
  // States that change on their own, so they are worth polling.
  var MOVING = { queued: 1, assigned: 1, running: 1, verifying: 1, recovering: 1, pending: 1, dispatched: 1, retrying: 1 };

  function stateLabel(state) { return (STATES[state] || [state || 'неизвестно'])[0]; }
  function stateTone(state) { return (STATES[state] || ['', 'idle'])[1]; }
  function pill(state) {
    return '<span class="pill is-' + stateTone(state) + '">' + esc(stateLabel(state)) + '</span>';
  }

  /* ----------------------------------------------------------------- state */
  var S = {
    csrf: '', user: '', status: null,
    projects: [], projectId: null, binding: null,
    chats: [], chatQuery: '', showArchived: false,
    convId: null, conv: null, messages: [],
    mode: 'auto', sending: false, pending: null,
    ws: { open: false, tab: 'files', treeKey: '', tree: null, treeError: null, treeLoading: false,
          expanded: {}, filter: '', file: null, files: {}, pane: 'tree', docView: true },
    work: { items: [], data: {}, timer: null, hidden: {} },
    accounts: { github: null, openhands: null },
    projectSettings: null
  };
  var routeSeq = 0;

  var store = {
    get: function (key) { try { return window.localStorage.getItem('nexus.' + key); } catch (e) { return null; } },
    set: function (key, value) {
      try {
        if (value === null) window.localStorage.removeItem('nexus.' + key);
        else window.localStorage.setItem('nexus.' + key, String(value));
      } catch (e) { /* storage may be unavailable; preferences are optional */ }
    }
  };

  var mqPhone = window.matchMedia('(max-width: 860px)');
  var mqNarrow = window.matchMedia('(max-width: 1180px)');
  var mqTouch = window.matchMedia('(pointer: coarse)');

  /* ------------------------------------------------------------------- api */
  function ApiError(message, status, data) {
    this.name = 'ApiError';
    this.message = message;
    this.status = status;
    this.data = data || null;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function api(path, options) {
    var opts = options || {};
    var method = opts.method || 'GET';
    var headers = { Accept: 'application/json' };
    var body;
    if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
    if (method !== 'GET') headers['X-CSRF-Token'] = S.csrf;
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, opts.timeout || 30000);
    return fetch(path, { method: method, headers: headers, body: body,
                         credentials: 'same-origin', signal: controller.signal })
      .then(function (response) {
        return response.text().then(function (text) {
          var data = null;
          try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
          if (response.status === 401) {
            // The session ended. Nothing on this page works without one.
            window.location.href = '/login';
            throw new ApiError('Сессия завершена. Войдите снова.', 401, data);
          }
          if (!response.ok) {
            throw new ApiError((data && data.error) || ('Сервер ответил ' + response.status),
                               response.status, data);
          }
          return data;
        });
      }, function (error) {
        if (error && error.name === 'AbortError') {
          throw new ApiError('Сервер не ответил вовремя.', 0, null);
        }
        throw new ApiError('Нет связи с сервером.', 0, null);
      })
      .finally(function () { clearTimeout(timer); });
  }

  function projectUrl(tail) { return '/api/v1/projects/' + enc(S.projectId) + (tail || ''); }
  function repoUrl(fullName, tail) {
    return projectUrl('/github/repositories/' + fullName.split('/').map(enc).join('/') + (tail || ''));
  }

  /* ----------------------------------------------------------------- utils */
  function parseTime(value) {
    if (!value) return null;
    // "2026-11-11 10:00:00.123456+00:00" -> an ISO string every browser parses
    var date = new Date(String(value).replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1'));
    return isNaN(date.getTime()) ? null : date;
  }
  function clock(value) {
    var date = parseTime(value);
    return date ? date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '';
  }
  function fullTime(value) {
    var date = parseTime(value);
    return date ? date.toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' }) : '';
  }
  function dayGroup(value) {
    var date = parseTime(value);
    if (!date) return 'Ранее';
    var today = new Date();
    today.setHours(0, 0, 0, 0);
    var days = Math.floor((today.getTime() - new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()) / 86400000);
    if (days <= 0) return 'Сегодня';
    if (days === 1) return 'Вчера';
    if (days < 7) return 'На этой неделе';
    if (days < 30) return 'В этом месяце';
    return 'Ранее';
  }
  function shortId(id) { return String(id || '').slice(0, 8); }
  function debounce(fn, wait) {
    var timer = null;
    return function () {
      var args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait);
    };
  }
  function plural(n, one, few, many) {
    var mod10 = n % 10, mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return one;
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
    return many;
  }
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.className = 'sr-only';
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      area.remove();
      if (ok) resolve(); else reject(new Error('copy failed'));
    });
  }

  function toast(text, kind) {
    var box = $('#toasts');
    var el = document.createElement('div');
    el.className = 'toast' + (kind === 'bad' ? ' is-bad' : '');
    el.innerHTML = icon(kind === 'bad' ? 'alert' : 'check') + '<span>' + esc(text) + '</span>';
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, kind === 'bad' ? 7000 : 3500);
  }

  // Server errors are short English strings written for a log. The interface
  // owes the reader the cause and the next step, so the known ones are
  // translated here; anything unknown is shown as it came.
  function explain(raw) {
    var text = String(raw || '');
    if (/no providers configured|no orchestrator provider configured/i.test(text)) {
      return { text: 'На сервере не настроен ни один LLM-провайдер. Режимам «Вопрос», «План» и «Автономно» нужен ключ: GEMINI_API_KEY, GROQ_API_KEY или OPENROUTER_API_KEY.' };
    }
    if (/all providers failed/i.test(text)) {
      return { text: 'Ни один LLM-провайдер не ответил. Причины по каждому — в технических деталях.' };
    }
    if (/bind a repository to the project/i.test(text)) {
      return { text: 'Этому режиму нужен репозиторий. Подключите его к проекту и повторите.', action: 'open-repo', actionLabel: 'Подключить репозиторий' };
    }
    if (/NEXUS_SECRET_KEY/.test(text)) {
      return { text: 'На сервере не задан NEXUS_SECRET_KEY. Без него аккаунты не сохраняются: ключи хранятся только в зашифрованном виде.' };
    }
    if (/no GitHub token configured/i.test(text)) {
      return { text: 'Нет доступа к GitHub: к проекту не привязан аккаунт, а на сервере не задан GITHUB_TOKEN.', action: 'open-repo', actionLabel: 'Подключить GitHub' };
    }
    if (/github auth/i.test(text)) {
      return { text: 'GitHub отклонил токен. Проверьте аккаунт в настройках.', action: 'open-settings', actionLabel: 'Открыть настройки' };
    }
    if (/could not start the OpenHands workflow/i.test(text)) {
      return { text: 'OpenHands не принял задачу: ' + text.replace(/^.*could not start the OpenHands workflow:\s*/i, '') };
    }
    if (/could not start the autonomous run/i.test(text)) {
      return { text: 'Автономный прогон не создан: ' + text.replace(/^.*could not start the autonomous run:\s*/i, '') };
    }
    return { text: text.replace(/^Не удалось получить ответ:\s*/, '') };
  }

  /* --------------------------------------------------------- menus, dialogs */
  var openMenuState = null;

  function closeMenu() {
    if (!openMenuState) return;
    var state = openMenuState;
    openMenuState = null;
    state.el.remove();
    if (state.anchor) {
      state.anchor.setAttribute('aria-expanded', 'false');
      if (state.refocus) state.anchor.focus();
    }
  }

  function openMenu(anchor, html, options) {
    var wasSame = openMenuState && openMenuState.anchor === anchor;
    closeMenu();
    if (wasSame) return null;
    var opts = options || {};
    var el = document.createElement('div');
    el.className = 'menu';
    el.setAttribute('role', 'menu');
    el.innerHTML = html;
    $('#layer').appendChild(el);
    var rect = anchor.getBoundingClientRect();
    var width = el.offsetWidth, height = el.offsetHeight;
    var left = opts.alignRight ? rect.right - width : rect.left;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    var top = rect.bottom + 6;
    if (top + height > window.innerHeight - 8) top = Math.max(8, rect.top - height - 6);
    el.style.left = left + 'px';
    el.style.top = top + 'px';
    anchor.setAttribute('aria-expanded', 'true');
    openMenuState = { el: el, anchor: anchor, refocus: false };
    var first = $('.menu-item, button', el);
    if (first && opts.focus !== false) first.focus();
    return el;
  }

  var dialogs = [];

  function closeDialog() {
    var dialog = dialogs.pop();
    if (!dialog) return;
    dialog.wrap.remove();
    if (dialog.onClose) dialog.onClose();
    if (dialog.restore && document.contains(dialog.restore)) dialog.restore.focus();
  }

  function openDialog(options) {
    closeMenu();
    var wrap = document.createElement('div');
    wrap.className = 'dialog-wrap';
    wrap.innerHTML =
      '<div class="dialog' + (options.className ? ' ' + options.className : '') + '" role="dialog" aria-modal="true" aria-labelledby="dialog-title">' +
        '<div class="dialog-head"><h2 id="dialog-title">' + esc(options.title) + '</h2>' +
        '<button class="icon-btn" type="button" data-action="close-dialog" aria-label="Закрыть">' + icon('x') + '</button></div>' +
        '<div class="dialog-body">' + (options.body || '') + '</div>' +
        (options.foot ? '<div class="dialog-foot">' + options.foot + '</div>' : '') +
      '</div>';
    $('#layer').appendChild(wrap);
    var dialog = { wrap: wrap, el: $('.dialog', wrap), restore: document.activeElement, onClose: options.onClose };
    dialogs.push(dialog);
    wrap.addEventListener('mousedown', function (event) {
      if (event.target === wrap) closeDialog();
    });
    var focusable = $('[autofocus], input:not([type=hidden]), select, textarea', dialog.el) || $('button', dialog.el);
    if (focusable) focusable.focus();
    return dialog;
  }

  function trapFocus(event) {
    var dialog = dialogs[dialogs.length - 1];
    if (!dialog || event.key !== 'Tab') return;
    var items = $$('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])', dialog.el)
      .filter(function (el) { return el.offsetParent !== null; });
    if (!items.length) return;
    var first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }

  function confirmDialog(title, text, okLabel, onOk, danger) {
    openDialog({
      title: title,
      body: '<p class="lead">' + esc(text) + '</p>',
      foot: '<button class="btn btn-ghost" type="button" data-action="close-dialog">Отмена</button>' +
            '<button class="btn ' + (danger ? 'btn-danger' : 'btn-primary') + '" type="button" id="confirm-ok">' + esc(okLabel) + '</button>'
    });
    $('#confirm-ok').addEventListener('click', function () { closeDialog(); onOk(); });
    $('#confirm-ok').focus();
  }

  function setBusy(button, busy) {
    if (!button) return;
    button.classList.toggle('is-busy', !!busy);
    button.disabled = !!busy;
  }

  /* ---------------------------------------------------------------- layout */
  var appEl = $('#app');

  function setSidebar(open, remember) {
    appEl.setAttribute('data-sidebar', open ? 'open' : 'closed');
    if (remember && !mqPhone.matches) store.set('sidebar', open ? 'open' : 'closed');
    syncScrim();
  }
  function sidebarOpen() { return appEl.getAttribute('data-sidebar') === 'open'; }

  function setWorkspace(open, tab) {
    S.ws.open = !!open;
    if (tab) S.ws.tab = tab;
    $('#workspace').hidden = !S.ws.open;
    $('#ws-toggle').setAttribute('aria-pressed', String(S.ws.open));
    store.set('workspace', S.ws.open ? 'open' : 'closed');
    if (S.ws.open && mqPhone.matches) setSidebar(false);
    syncScrim();
    if (S.ws.open) renderWorkspace();
    scheduleWorkPoll();
  }

  function syncScrim() {
    var needed = (mqPhone.matches && sidebarOpen()) ||
      (mqNarrow.matches && !mqPhone.matches && S.ws.open);
    $('#scrim').hidden = !needed;
  }

  function applyLayoutDefaults() {
    setSidebar(mqPhone.matches ? false : store.get('sidebar') !== 'closed');
    var width = parseInt(store.get('ws-width'), 10);
    if (width && !mqNarrow.matches) $('#workspace').style.width = width + 'px';
    else $('#workspace').style.removeProperty('width');
    syncScrim();
  }

  function setTheme(choice) {
    var resolved = choice === 'light' || choice === 'dark' ? choice
      : (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    document.documentElement.setAttribute('data-theme', resolved);
    document.documentElement.setAttribute('data-theme-choice', choice);
    store.set('theme', choice === 'system' ? null : choice);
    var meta = $('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', resolved === 'light' ? '#ffffff' : '#0b0d12');
  }
  function themeChoice() { return document.documentElement.getAttribute('data-theme-choice') || 'system'; }

  /* -------------------------------------------------------------- projects */
  function project() {
    for (var i = 0; i < S.projects.length; i++) if (S.projects[i].id === S.projectId) return S.projects[i];
    return null;
  }
  function repoName() { return (S.binding && S.binding.full_name) || ''; }
  function branchName() {
    return (S.binding && (S.binding.selected_branch || S.binding.default_branch)) || '';
  }

  function renderProject() {
    var current = project();
    $('#project-name').textContent = current ? current.name : 'Проект не создан';
    $('#project-repo').textContent = repoName() ? repoName() + ' · ' + branchName() : 'Репозиторий не подключён';
    var chip = $('#repo-chip');
    chip.classList.toggle('is-set', !!repoName());
    chip.classList.toggle('is-missing', !repoName());
    $('#repo-chip-text').textContent = repoName() ? repoName() + ' · ' + branchName() : 'Подключить репозиторий';
  }

  function loadProjects() {
    return api('/api/v1/projects').then(function (data) { S.projects = data.projects || []; });
  }

  function selectProject(projectId) {
    S.projectId = projectId;
    S.binding = null;
    S.chats = [];
    S.ws.treeKey = ''; S.ws.tree = null; S.ws.treeError = null; S.ws.file = null; S.ws.files = {};
    S.ws.expanded = {}; S.ws.filter = ''; S.ws.pane = 'tree';
    store.set('project', projectId);
    if (!projectId) return Promise.resolve();
    return Promise.all([
      api(projectUrl('/binding')).then(function (data) { S.binding = data.binding; }, function () { S.binding = null; }),
      loadChats()
    ]);
  }

  function ensureProject() {
    if (S.projectId) return Promise.resolve();
    // The first message should not require a setup step. A project is the
    // container a conversation needs, so one is created and named plainly; it
    // can be renamed from the project menu.
    return api('/api/v1/projects', { method: 'POST', body: { name: 'Мой проект' } }).then(function (data) {
      S.projects.unshift(data.project);
      return selectProject(data.project.id);
    });
  }

  // Navigation routes at once instead of waiting for the browser's hashchange
  // event, which arrives a task later. Until a route has finished, the page
  // still shows the previous conversation, so the composer is held back for
  // that moment: a message can never land in the chat that is being left.
  var routedHash = null, routing = 0;

  function navigate(hash) {
    if (window.location.hash !== hash) window.location.hash = hash;
    return route();
  }
  function go(projectId, convId) {
    return navigate(projectId ? '#/p/' + projectId + (convId ? '/c/' + convId : '') : '#/');
  }

  function route() {
    var seq = ++routeSeq;
    routedHash = window.location.hash;
    routing++;
    syncSend();
    var match = /^#\/p\/([0-9a-fA-F-]{36})(?:\/c\/([0-9a-fA-F-]{36}))?/.exec(window.location.hash);
    var projectId = match ? match[1] : null;
    var convId = match ? (match[2] || null) : null;
    var known = function (id) { return S.projects.some(function (p) { return p.id === id; }); };
    if (!projectId || !known(projectId)) {
      var remembered = store.get('project');
      projectId = remembered && known(remembered) ? remembered : (S.projects[0] ? S.projects[0].id : null);
      convId = null;
    }
    var step = projectId !== S.projectId ? selectProject(projectId) : Promise.resolve();
    return step.then(function () {
      if (seq !== routeSeq) return null;
      return openConversation(convId);
    }).then(function () {
      if (seq !== routeSeq) return;
      renderAll();
      if (mqPhone.matches) setSidebar(false);
    }).catch(function (error) {
      if (seq !== routeSeq) return;
      toast(error.message, 'bad');
      renderAll();
    }).then(function () {
      routing--;
      syncSend();
    });
  }

  /* ----------------------------------------------------------------- chats */
  function loadChats() {
    if (!S.projectId) { S.chats = []; return Promise.resolve(); }
    // Archived conversations come in the same answer. They stay folded under
    // one line, and that line exists only when there is something behind it.
    var query = ['archived=1'];
    if (S.chatQuery) query.push('q=' + enc(S.chatQuery));
    return api(projectUrl('/conversations?' + query.join('&')))
      .then(function (data) { S.chats = data.conversations || []; },
            function () { S.chats = []; });
  }

  function chatItemHtml(chat) {
    var title = chat.title || 'Новый чат';
    var current = chat.id === S.convId;
    var stateDot = chat.state === 'thinking' ? '<span class="sb-state is-thinking" title="Ассистент отвечает"></span>'
      : chat.state === 'error' ? '<span class="sb-state is-error" title="Последний ответ не получен"></span>' : '';
    return '<div class="sb-item' + (current ? ' is-active' : '') + (chat.archived ? ' is-archived' : '') + '">' +
      '<a class="sb-link" href="#/p/' + esc(S.projectId) + '/c/' + esc(chat.id) + '" title="' + esc(title) + '"' +
      (current ? ' aria-current="page"' : '') + '>' + stateDot + '<span>' + esc(title) + '</span></a>' +
      '<button class="icon-btn sm" type="button" data-action="chat-menu" data-id="' + esc(chat.id) + '" aria-haspopup="menu" aria-expanded="false" aria-label="Действия с чатом «' + esc(title) + '»">' + icon('more', 'icon-sm') + '</button>' +
      '</div>';
  }

  function renderChats() {
    var active = S.chats.filter(function (chat) { return !chat.archived; });
    var archived = S.chats.filter(function (chat) { return chat.archived; });
    var html = '', group = '';
    active.forEach(function (chat) {
      var label = dayGroup(chat.updated_at);
      if (label !== group) { group = label; html += '<div class="sb-group">' + esc(label) + '</div>'; }
      html += chatItemHtml(chat);
    });
    if (!active.length) {
      html = '<p class="sb-empty">' + (S.chatQuery
        ? 'По запросу «' + esc(S.chatQuery) + '» ничего не найдено.'
        : 'Здесь появится история чатов. Начните с сообщения — чат создастся сам.') + '</p>';
    }
    if (archived.length) {
      html += '<button class="sb-more" type="button" data-action="toggle-archived" aria-expanded="' + S.showArchived + '">' +
        icon('archive', 'icon-sm') + '<span>Архив · ' + archived.length + '</span>' + icon('chevron-right', 'icon-xs') + '</button>';
      if (S.showArchived) html += archived.map(chatItemHtml).join('');
    }
    $('#chat-list').innerHTML = html;
  }

  function workFromMessages() {
    var seen = {}, items = [];
    S.messages.forEach(function (message) {
      var meta = message.meta || {};
      var entry = meta.workflow_id ? { kind: 'workflow', id: String(meta.workflow_id) }
        : meta.run_id ? { kind: 'run', id: String(meta.run_id) } : null;
      if (!entry) return;
      entry.key = entry.kind + ':' + entry.id;
      if (!seen[entry.key] && !S.work.hidden[entry.key]) {
        seen[entry.key] = true;
        items.push(entry);
      }
    });
    return items.reverse();   // newest first
  }

  // A cancelled run is gone from the interface. The card is dropped from the
  // in-memory list and the record is marked hidden, so a later refresh that
  // re-reads the run (a reload, a poll) does not bring it back. The row is not
  // deleted anywhere: the history, tasks and report stay on the server.
  function dropWork(key) {
    S.work.hidden[key] = true;
    S.work.items = S.work.items.filter(function (item) { return item.key !== key; });
    delete S.work.data[key];
    renderWorkBadges();
    if (S.ws.open && S.ws.tab === 'agent') renderAgent();
  }

  function openConversation(convId) {
    S.convId = convId;
    S.conv = null;
    S.messages = [];
    S.pending = null;
    S.work.items = [];
    if (!convId || !S.projectId) return Promise.resolve();
    return Promise.all([
      api(projectUrl('/conversations/' + enc(convId))),
      api(projectUrl('/conversations/' + enc(convId) + '/messages'))
    ]).then(function (results) {
      S.conv = results[0].conversation;
      S.messages = results[1].messages || [];
      S.mode = MODES[S.conv.mode] ? S.conv.mode : 'ask';
      S.work.items = workFromMessages();
      refreshWork();
    }, function (error) {
      S.convId = null;
      if (error.status === 404) toast('Этот чат не найден.', 'bad');
      else throw error;
    });
  }

  /* ---------------------------------------------------------------- thread */
  function modelLabel(meta) {
    if (!meta) return '';
    if (meta.provider && meta.model) return meta.provider + ' · ' + meta.model;
    if (meta.provider) return meta.provider;
    if (meta.mode && MODES[meta.mode]) return MODES[meta.mode].label;
    return '';
  }

  function workChip(entry) {
    var record = S.work.data[entry.key];
    var state = record && record.state;
    var title = entry.kind === 'workflow' ? 'Задача OpenHands' : 'Автономный прогон';
    return '<button class="work-chip" type="button" data-action="open-work" data-key="' + esc(entry.key) + '">' +
      '<span class="dot is-' + (state ? stateTone(state) : 'idle') + '"></span>' +
      '<span class="work-chip-title">' + title + '</span>' +
      '<span class="work-chip-state">' + esc(state ? stateLabel(state) : 'открыть ход работы') + '</span>' +
      icon('chevron-right', 'icon-sm') + '</button>';
  }

  // One copy button, used by every message that has one. The same handler
  // (``copy-message``) and the same ``copyText`` fallback serve the assistant's
  // reply and the user's own message; the report uses ``copy-report``.
  function copyButton(id, label) {
    return '<button class="icon-btn sm" type="button" data-action="copy-message" data-id="' + id +
      '" aria-label="' + esc(label) + '" title="Скопировать">' + icon('copy', 'icon-sm') + '</button>';
  }

  function messageHtml(message) {
    var meta = message.meta || {};
    var id = esc(message.id || '');
    if (message.role === 'user') {
      // A copy button on the user's own message: the task text is often long
      // and worth reusing. The full text is copied, never the rendered bubble.
      return '<article class="msg user" data-id="' + id + '"><div class="bubble">' + esc(message.content) + '</div>' +
        '<div class="msg-actions">' + copyButton(id, 'Скопировать сообщение') + '</div></article>';
    }
    if (message.role === 'tool') {
      var ref = meta.workflow_id || meta.run_id;
      return '<article class="msg step" data-id="' + id + '">' + icon('zap', 'icon-sm') +
        '<span>' + esc(TOOLS[meta.tool] || meta.tool || 'инструмент') + '</span>' +
        (ref ? '<code>' + esc(shortId(ref)) + '</code>' : '<span>' + esc(message.content) + '</span>') + '</article>';
    }
    if (message.role === 'system') {
      if (!meta.error) return '<article class="msg note" data-id="' + id + '">' + esc(message.content) + '</article>';
      var info = explain(message.content);
      return '<article class="msg error" data-id="' + id + '" role="alert">' + icon('alert') + '<div class="msg-body">' +
        '<p class="err-title">Ответ не получен</p><p class="err-text">' + esc(info.text) + '</p>' +
        '<details class="err-raw"><summary>Технические детали</summary><code>' + esc(message.content) + '</code></details>' +
        '<div class="msg-actions">' +
        '<button class="btn btn-sm" type="button" data-action="retry" data-id="' + id + '">' + icon('refresh', 'icon-sm') + 'Повторить</button>' +
        (info.action ? '<button class="btn btn-sm" type="button" data-action="' + info.action + '">' + esc(info.actionLabel) + '</button>' : '') +
        '</div></div></article>';
    }
    var entry = meta.workflow_id ? { kind: 'workflow', id: String(meta.workflow_id) }
      : meta.run_id ? { kind: 'run', id: String(meta.run_id) } : null;
    if (entry) entry.key = entry.kind + ':' + entry.id;
    var label = modelLabel(meta);
    return '<article class="msg assistant" data-id="' + id + '">' +
      '<svg class="msg-avatar" aria-hidden="true"><use href="#i-logo"/></svg>' +
      '<div class="msg-body"><div class="md">' + MD.render(message.content) + '</div>' +
      (entry ? workChip(entry) : '') +
      '<div class="msg-meta">' + (label ? '<span class="msg-model">' + esc(label) + '</span>' : '') +
      '<span title="' + esc(fullTime(message.created_at)) + '">' + esc(clock(message.created_at)) + '</span>' +
      copyButton(id, 'Скопировать ответ') +
      '</div></div></article>';
  }

  function pendingHtml() {
    return '<article class="msg user"><div class="bubble">' + esc(S.pending.text) + '</div></article>' +
      '<article class="msg assistant" id="pending"><svg class="msg-avatar" aria-hidden="true"><use href="#i-logo"/></svg>' +
      '<div class="msg-body"><div class="thinking"><span class="thinking-text">' + esc(MODES[S.pending.mode].thinking) + '…</span>' +
      '<span class="thinking-time" id="pending-time"></span></div></div></article>';
  }

  function heroHtml() {
    var current = project();
    return '<div class="hero"><svg class="hero-mark" aria-hidden="true"><use href="#i-logo"/></svg>' +
      '<h1>Что будем <em>строить</em>?</h1>' +
      '<p>' + (repoName()
        ? 'Проект «' + esc(current ? current.name : '') + '» · ' + esc(repoName()) + ' · ' + esc(branchName())
        : 'Спросите о коде, составьте план или поручите задачу агенту.') + '</p></div>';
  }

  function renderThread() {
    var thread = $('#thread');
    if (!S.convId && !S.pending) {
      appEl.setAttribute('data-view', 'home');
      thread.innerHTML = heroHtml();
      renderHomeExtras();
      growPrompt();
      return;
    }
    appEl.setAttribute('data-view', 'chat');
    var html = S.messages.map(messageHtml).join('');
    if (S.pending) html += pendingHtml();
    if (!html) html = '<div class="empty"><div class="empty-icon">' + icon('message') + '</div><h3>Пустой чат</h3><p>Напишите первое сообщение.</p></div>';
    thread.innerHTML = '<div class="thread-inner">' + html + '</div>';
    growPrompt();
    scrollToEnd(false);
  }

  // True while the thread sits at its end. Opening the workspace or resizing
  // the window reflows the text; a pinned thread is then put back at its end,
  // and one the reader scrolled up is left where it is.
  var threadPinned = true;

  function scrollToEnd(smooth) {
    var thread = $('#thread');
    threadPinned = true;
    if (!smooth) {
      thread.style.scrollBehavior = 'auto';
      thread.scrollTop = thread.scrollHeight;
      thread.style.removeProperty('scroll-behavior');
    } else {
      thread.scrollTop = thread.scrollHeight;
    }
  }

  function renderHomeExtras() {
    var box = $('#home-extras');
    var notice = '';
    var providers = (S.status && S.status.providers) || [];
    var canAnswer = providers.some(function (name) { return name !== 'openhands'; });
    if (S.status && !canAnswer) {
      notice = '<div class="setup">' + icon('alert') + '<span class="setup-text">На сервере не настроен LLM-провайдер: режимы «Вопрос», «План» и «Автономно» не ответят. Нужен GEMINI_API_KEY, GROQ_API_KEY или OPENROUTER_API_KEY.</span></div>';
    }
    var starts = [
      { mode: 'ask', icon: 'message', title: 'Разобраться в проекте', desc: 'Объясни, как устроен проект и с чего начать чтение кода.', text: 'Объясни, как устроен этот проект: основные модули, точки входа и как они связаны.' },
      { mode: 'plan', icon: 'plan', title: 'Составить план', desc: 'Разбей задачу на шаги с зависимостями — без запуска.', text: 'Составь план: ' },
      { mode: 'code', icon: 'code', title: 'Изменить код', desc: 'Поручи задачу OpenHands в подключённом репозитории.', text: 'Добавь тесты для ' },
      { mode: 'autonomous', icon: 'zap', title: 'Автономный прогон', desc: 'Агент сам планирует, выполняет и проверяет цель.', text: 'Цель: ' }
    ];
    box.innerHTML = notice + '<div class="starts">' + starts.map(function (start, index) {
      return '<button class="start" type="button" data-action="start" data-index="' + index + '">' +
        '<span class="start-icon">' + icon(start.icon, 'icon-sm') + '</span>' +
        '<span class="start-text"><span class="start-title">' + esc(start.title) + '</span>' +
        '<span class="start-desc">' + esc(start.desc) + '</span></span></button>';
    }).join('') + '</div>';
    box.starts = starts;
  }

  function renderTopbar() {
    var title = $('#conv-title');
    if (S.conv) {
      title.hidden = false;
      title.textContent = S.conv.title || 'Новый чат';
      document.title = (S.conv.title || 'Новый чат') + ' — NEXUS';
    } else {
      title.hidden = true;
      document.title = 'NEXUS';
    }
  }

  /* -------------------------------------------------------------- composer */
  function renderComposer() {
    var mode = MODES[S.mode];
    $('#mode-label').textContent = mode.label;
    $('#mode-icon').setAttribute('href', '#i-' + mode.icon);
    var prompt = $('#prompt');
    prompt.placeholder = mode.placeholder;
    var note = $('#composer-note');
    var warn = '';
    if (mode.repo && !repoName()) warn = 'Режиму «' + mode.label + '» нужен репозиторий — подключите его кнопкой вверху.';
    note.textContent = warn || (mqTouch.matches ? '' : 'Enter — отправить, Shift + Enter — новая строка');
    note.classList.toggle('is-warn', !!warn);
    $('#composer-hint').textContent = mode.repo && repoName() ? repoName() + ' · ' + branchName() : '';
    syncSend();
  }

  function syncSend() {
    var button = $('#send');
    button.disabled = S.sending || routing > 0 || !$('#prompt').value.trim();
    button.classList.toggle('is-busy', S.sending);
  }

  function growPrompt() {
    var prompt = $('#prompt');
    prompt.style.height = 'auto';
    prompt.style.height = Math.min(prompt.scrollHeight, 240) + 'px';
  }

  function setMode(mode) {
    if (!MODES[mode]) return;
    S.mode = mode;
    renderComposer();
    if (S.conv && S.conv.mode !== mode) {
      api(projectUrl('/conversations/' + enc(S.convId)), { method: 'PATCH', body: { mode: mode } })
        .then(function (data) { if (S.conv) S.conv.mode = data.conversation.mode; },
              function () { /* the mode is sent with the next message anyway */ });
    }
  }

  var pendingTimer = null;
  function tickPending() {
    var el = $('#pending-time');
    if (!el || !S.pending) return;
    var seconds = Math.floor((Date.now() - S.pending.startedAt) / 1000);
    el.textContent = seconds >= 3 ? seconds + ' с' : '';
  }

  function send(text) {
    text = (text || '').trim();
    if (!text || S.sending || routing > 0) return Promise.resolve();
    var mode = S.mode;
    if (MODES[mode].repo && !repoName()) {
      toast('Сначала подключите репозиторий — он нужен режиму «' + MODES[mode].label + '».', 'bad');
      openRepoDialog();
      return Promise.resolve();
    }
    S.sending = true;
    S.pending = { text: text, mode: mode, startedAt: Date.now() };
    $('#prompt').value = '';
    growPrompt();
    syncSend();
    renderThread();
    pendingTimer = setInterval(tickPending, 1000);

    var createdConversation = false;
    return ensureProject().then(function () {
      if (S.convId) return null;
      createdConversation = true;
      return api(projectUrl('/conversations'), { method: 'POST', body: { title: '', mode: mode } })
        .then(function (data) { S.conv = data.conversation; S.convId = data.conversation.id; });
    }).then(function () {
      // A "Code" turn waits for OpenHands to accept the task, which can take
      // more than a minute; the request is given that long before it gives up.
      return api(projectUrl('/conversations/' + enc(S.convId) + '/messages'),
                 { method: 'POST', body: { text: text, mode: mode }, timeout: 180000 });
    }).then(function (data) {
      S.messages = S.messages.concat(data.messages || []);
    }, function (error) {
      if (error.data && error.data.messages) {
        // The server stored the turn and its failure; show exactly that.
        S.messages = S.messages.concat(error.data.messages);
        return null;
      }
      if (!S.convId) throw error;
      // No usable answer (a timeout, a dropped connection). The user turn is
      // stored before the assistant is asked, so the server is asked what it has.
      return api(projectUrl('/conversations/' + enc(S.convId) + '/messages')).then(function (data) {
        S.messages = data.messages || [];
        var last = S.messages[S.messages.length - 1];
        if (!last || last.role === 'user') {
          S.messages.push({ id: 'local-' + Date.now(), role: 'system', meta: { error: true, local: true },
                            content: error.message + ' Ответ мог не успеть прийти — повторите запрос или обновите страницу позже.' });
        }
      }, function () { throw error; });
    }).catch(function (error) {
      toast(error.message, 'bad');
      $('#prompt').value = text;
      growPrompt();
    }).then(function () {
      clearInterval(pendingTimer);
      S.sending = false;
      S.pending = null;
      S.work.items = workFromMessages();
      renderThread();
      renderTopbar();
      syncSend();
      if (createdConversation && S.convId) {
        // Put the new conversation in the address bar without re-routing.
        window.history.replaceState(null, '', '#/p/' + S.projectId + '/c/' + S.convId);
        routedHash = window.location.hash;
      }
      renderProject();
      var after = [loadChats().then(renderChats)];
      if (S.convId) {
        after.push(api(projectUrl('/conversations/' + enc(S.convId))).then(function (data) {
          S.conv = data.conversation;
          renderTopbar();
        }, function () {}));
      }
      refreshWork();
      if (S.work.items.length && (mode === 'code' || mode === 'autonomous') && !mqPhone.matches) {
        setWorkspace(true, 'agent');
      }
      return Promise.all(after);
    });
  }

  /* ------------------------------------------------------------------ work */
  function loadWork(entry) {
    var request;
    if (entry.kind === 'workflow') {
      request = api(projectUrl('/workflows/' + enc(entry.id))).catch(function (error) {
        // A workflow started before chat turns were project-scoped has no
        // project; it is still readable through the owner-level route.
        if (error.status === 404) return api('/api/workflows/' + enc(entry.id));
        throw error;
      }).then(function (data) { return { state: data.workflow.state, detail: data }; });
    } else {
      request = api(projectUrl('/runs/' + enc(entry.id)))
        .then(function (data) { return { state: data.run.state, detail: data }; });
    }
    return request.then(function (record) {
      // A cancelled run is gone from the interface, even when the chat still
      // carries a chip for it (a run cancelled on another device, or before a
      // reload). Its history stays on the server; only the view drops it.
      if (entry.kind === 'run' && record.state === 'cancelled') {
        dropWork(entry.key);
        return null;
      }
      S.work.data[entry.key] = record;
      return null;
    }, function (error) {
      S.work.data[entry.key] = { state: null, error: error.message };
    });
  }

  function movingWork() {
    return S.work.items.filter(function (entry) {
      var record = S.work.data[entry.key];
      return record && MOVING[record.state];
    }).length;
  }

  function renderWorkBadges() {
    var moving = movingWork();
    var count = $('#agent-count');
    count.hidden = !moving;
    count.textContent = moving;
    $('#ws-dot').hidden = !(moving && !S.ws.open);
  }

  function patchWorkChips() {
    $$('.work-chip').forEach(function (chip) {
      var record = S.work.data[chip.getAttribute('data-key')];
      if (!record) return;
      var dot = $('.dot', chip), state = $('.work-chip-state', chip);
      if (record.state) {
        dot.className = 'dot is-' + stateTone(record.state);
        state.textContent = stateLabel(record.state);
      } else {
        dot.className = 'dot is-bad';
        state.textContent = 'состояние недоступно';
      }
    });
  }

  function scheduleWorkPoll() {
    clearTimeout(S.work.timer);
    S.work.timer = null;
    if (!movingWork() || document.visibilityState !== 'visible') return;
    S.work.timer = setTimeout(refreshWork, 5000);
  }

  function refreshWork() {
    clearTimeout(S.work.timer);
    S.work.timer = null;
    var convId = S.convId;
    if (!S.work.items.length) { renderWorkBadges(); return Promise.resolve(); }
    return Promise.all(S.work.items.map(loadWork)).then(function () {
      if (convId !== S.convId) return;
      patchWorkChips();
      renderWorkBadges();
      if (S.ws.open && S.ws.tab === 'agent') renderAgent();
      scheduleWorkPoll();
    });
  }

  function emptyHtml(iconName, title, text, action) {
    return '<div class="empty"><div class="empty-icon">' + icon(iconName) + '</div><h3>' + esc(title) +
      '</h3><p>' + esc(text) + '</p>' + (action || '') + '</div>';
  }
  var SKELETON = '<div class="skeleton" aria-label="Загрузка"><i></i><i></i><i></i><i></i></div>';

  function resultText(value) {
    if (value == null) return '';
    if (typeof value === 'string') return value;
    if (typeof value === 'object') {
      var picked = value.summary || value.final_response || value.text || value.reason || value.message;
      if (typeof picked === 'string') return picked;
      try { return JSON.stringify(value); } catch (e) { return ''; }
    }
    return String(value);
  }
  function clip(text, limit) {
    text = String(text || '');
    return text.length > limit ? text.slice(0, limit).trimEnd() + '…' : text;
  }

  function workflowCard(entry, detail) {
    var wf = detail.workflow, tasks = detail.tasks || [], events = detail.events || [];
    var link = wf.sr_url && /^https?:\/\//i.test(wf.sr_url)
      ? '<a class="btn btn-sm" href="' + esc(wf.sr_url) + '" target="_blank" rel="noopener noreferrer">' + icon('external', 'icon-sm') + 'Открыть в OpenHands</a>' : '';
    var steps = tasks.length ? '<ul class="steps">' + tasks.map(function (task) {
      var evidence = Array.isArray(task.evidence) && task.evidence.length ? resultText(task.evidence[task.evidence.length - 1]) : '';
      var sub = ['попытка ' + task.attempt + ' из ' + task.max_attempts];
      if (task.repository) sub.push(task.repository + (task.branch ? ' · ' + task.branch : ''));
      var note = resultText(task.result) || evidence;
      var retry = { failed: 1, blocked: 1, dead_letter: 1 }[task.state]
        ? '<button class="btn btn-sm" type="button" data-action="retry-task" data-id="' + esc(task.id) + '">Повторить</button>' : '<span></span>';
      return '<li class="step-row"><span class="dot is-' + stateTone(task.state) + '" title="' + esc(stateLabel(task.state)) + '"></span>' +
        '<div><div class="step-title">' + esc(clip(task.goal, 260)) + '</div>' +
        '<div class="step-sub">' + esc(stateLabel(task.state)) + ' · ' + esc(sub.join(' · ')) + '</div>' +
        (note ? '<div class="step-sub">' + esc(clip(note, 320)) + '</div>' : '') + '</div>' + retry + '</li>';
    }).join('') + '</ul>'
      : '<p class="step-sub">Старший агент ещё не поставил задач исполнителям.</p>';
    var log = events.length ? '<details class="work-section"><summary>События · ' + events.length + '</summary><ul class="events">' +
      events.map(function (event) {
        var detailText = '';
        try { detailText = event.detail && Object.keys(event.detail).length ? JSON.stringify(event.detail) : ''; } catch (e) { detailText = ''; }
        return '<li><time>' + esc(clock(event.at)) + '</time><span>' + esc(event.kind || '') +
          (detailText ? ' <code>' + esc(clip(detailText, 200)) + '</code>' : '') + '</span></li>';
      }).join('') + '</ul></details>' : '';
    var actions = '';
    if ({ queued: 1, running: 1, verifying: 1, blocked: 1 }[wf.state]) {
      actions += '<button class="btn btn-sm btn-danger" type="button" data-action="cancel-workflow" data-id="' + esc(entry.id) + '">' + icon('stop', 'icon-sm') + 'Остановить</button>';
    }
    if ({ blocked: 1, failed: 1 }[wf.state]) {
      actions += '<button class="btn btn-sm" type="button" data-action="resume-workflow" data-id="' + esc(entry.id) + '">' + icon('play', 'icon-sm') + 'Возобновить</button>';
    }
    return '<section class="work"><div class="work-head">' + pill(wf.state) +
      '<span class="work-kind">Задача OpenHands</span><span class="work-id">' + esc(shortId(entry.id)) + '</span>' +
      '<span class="topbar-spacer"></span>' + link + '</div>' +
      '<p class="work-goal">' + esc(clip(wf.goal, 400)) + '</p>' +
      (wf.reason ? '<p class="work-reason">' + esc(clip(resultText(wf.reason), 400)) + '</p>' : '') +
      '<div class="work-section"><p class="work-section-title">Задачи' + (tasks.length ? ' · ' + tasks.length : '') + '</p>' + steps + '</div>' +
      log +
      '<div class="work-foot">' + actions + '<span class="muted">Старший агент: ' + esc(wf.sr_status ? stateLabel(wf.sr_status) : 'не запущен') + '</span></div></section>';
  }

  function runCard(entry, detail) {
    var run = detail.run, tasks = detail.tasks || [], report = detail.report;
    // "Will be taken in a few seconds" is said only of a run nothing has happened to
    // yet. A task that waits out a refused start says why on its own line.
    var waiting = tasks.length && run.state === 'running' &&
      tasks.every(function (task) { return task.state === 'pending' && !task.last_error; });
    var inline = !!(S.status && S.status.lab && S.status.lab.inline);
    var live = { running: 1, paused: 1, blocked: 1, waiting_for_approval: 1, retrying: 1 }[run.state];
    var budget = 'запусков ' + esc(run.iteration) + ' из ' + esc(run.max_iterations) +
      (live && run.deadline_at ? ' · до ' + esc(fullTime(run.deadline_at)) : '');
    if (run.state === 'retrying') {
      budget += ' · циклов ' + esc(run.retry_attempt) + ' из ' + esc(run.max_retry_cycles);
      if (run.retry_at) budget += ' · следующая попытка после ' + esc(fullTime(run.retry_at));
    }
    var steps = tasks.length ? '<ul class="steps">' + tasks.map(function (task) {
      var verdict = task.verification && (task.verification.verdict || task.verification.state);
      var sub = [stateLabel(task.state), 'попытка ' + task.attempt + ' из ' + task.max_attempts];
      if (verdict) sub.push('проверка: ' + verdict);
      // Why it did not verify - without it "не подтверждено" sends a person to the database.
      var reason = task.verification && task.verification.reason;
      var settled = { completed_unverified: 1, partial: 1, failed: 1, blocked: 1 }[task.state];
      return '<li class="step-row"><span class="dot is-' + stateTone(task.state) + '" title="' + esc(stateLabel(task.state)) + '"></span>' +
        '<div><div class="step-title">' + esc(clip(task.title || task.task_key, 260)) + '</div>' +
        '<div class="step-sub">' + esc(sub.join(' · ')) + '</div>' +
        (settled && reason ? '<div class="step-sub">' + esc(clip(reason, 320)) + '</div>' : '') +
        (task.last_error ? '<div class="step-sub">' + esc(clip(task.last_error, 320)) + '</div>' : '') + '</div><span></span></li>';
    }).join('') + '</ul>' : '<p class="step-sub">В плане нет задач.</p>';
    var actions = '';
    if (run.state === 'running') actions += '<button class="btn btn-sm" type="button" data-action="run-state" data-op="pause" data-id="' + esc(entry.id) + '">' + icon('pause', 'icon-sm') + 'Пауза</button>';
    if ({ paused: 1, blocked: 1, retrying: 1 }[run.state]) actions += '<button class="btn btn-sm" type="button" data-action="run-state" data-op="resume" data-id="' + esc(entry.id) + '">' + icon('play', 'icon-sm') + 'Продолжить</button>';
    if ({ running: 1, paused: 1, blocked: 1, waiting_for_approval: 1, retrying: 1 }[run.state]) actions += '<button class="btn btn-sm btn-danger" type="button" data-action="run-state" data-op="cancel" data-id="' + esc(entry.id) + '">' + icon('stop', 'icon-sm') + 'Отменить</button>';
    // The report is the run's own record: the same text delivered to the chat.
    // It is copied in full, never the truncated view.
    var reportSection = report && report.content
      ? '<div class="work-section"><p class="work-section-title">Итоговый отчёт</p>' +
        '<div class="md">' + MD.render(report.content) + '</div>' +
        '<div class="work-foot">' +
        '<button class="btn btn-sm" type="button" data-action="copy-report" data-id="' + esc(entry.id) + '">' +
        icon('copy', 'icon-sm') + 'Скопировать отчёт</button>' +
        '<span class="muted">' + esc(report.status === 'sent' ? 'отправлен в чат'
          : report.status === 'failed' ? 'доставка не удалась, отчёт сохранён'
          : 'ожидает отправки в чат') + '</span></div></div>'
      : '';
    return '<section class="work"><div class="work-head">' + pill(run.state) +
      '<span class="work-kind">Автономный прогон</span><span class="work-id">' + esc(shortId(entry.id)) + '</span>' +
      '<span class="topbar-spacer"></span><span class="work-id" title="Сколько раз прогон запускал задачи и до какого времени он может их запускать">' + budget + '</span></div>' +
      (run.last_error ? '<p class="work-reason">' + esc(clip(run.last_error, 400)) + '</p>' : '') +
      '<div class="work-section"><p class="work-section-title">План · ' + tasks.length + ' ' + plural(tasks.length, 'задача', 'задачи', 'задач') + '</p>' + steps +
      (waiting ? '<p class="step-sub">' + (inline
        ? 'Сервер возьмёт первую задачу в течение нескольких секунд. Если на этой ветке сейчас работает задача другого прогона, сначала дождётся её: на одной ветке работает одна задача.'
        : 'Задачи ждут исполнителя. На этом сервере ведение прогонов выключено (NEXUS_LAB_INLINE=0): их должен вести отдельный процесс python -m orchestration.lab_worker. Если такого процесса нет, уберите эту переменную.') + '</p>' : '') + '</div>' +
      reportSection +
      (actions ? '<div class="work-foot">' + actions + '</div>' : '') + '</section>';
  }

  function renderAgent() {
    var body = $('#ws-body');
    if (!S.work.items.length) {
      body.innerHTML = emptyHtml('bot', 'Агент ещё не запускался',
        'Отправьте задачу в режиме «Код» или «Автономно» — здесь появятся шаги, статусы и ссылка на сессию OpenHands.');
      return;
    }
    body.innerHTML = '<div class="work-list">' + S.work.items.map(function (entry) {
      var record = S.work.data[entry.key];
      if (!record) return '<section class="work">' + SKELETON + '</section>';
      if (record.error || !record.detail) {
        return '<section class="work"><div class="work-head"><span class="pill is-bad">нет данных</span>' +
          '<span class="work-kind">' + (entry.kind === 'workflow' ? 'Задача OpenHands' : 'Автономный прогон') + '</span>' +
          '<span class="work-id">' + esc(shortId(entry.id)) + '</span></div>' +
          '<p class="work-goal">' + esc(record.error || 'Состояние недоступно.') + '</p>' +
          '<div class="work-foot"><button class="btn btn-sm" type="button" data-action="refresh-work">Обновить</button></div></section>';
      }
      return entry.kind === 'workflow' ? workflowCard(entry, record.detail) : runCard(entry, record.detail);
    }).join('') + '</div>';
  }

  function workAction(button, request, done) {
    setBusy(button, true);
    request.then(function () { if (done) toast(done); return refreshWork(); },
                 function (error) { toast(error.message, 'bad'); })
      .then(function () { setBusy(button, false); });
  }

  /* ----------------------------------------------------------------- files */
  function treeKey() { return repoName() ? repoName() + '@' + branchName() : ''; }

  function loadTree(force) {
    var key = treeKey();
    if (!key) return;
    if (!force && S.ws.treeKey === key && (S.ws.tree || S.ws.treeLoading || S.ws.treeError)) return;
    S.ws.treeKey = key;
    S.ws.tree = null; S.ws.treeError = null; S.ws.treeLoading = true;
    S.ws.files = {}; S.ws.file = null; S.ws.pane = 'tree';
    renderFiles();
    api(repoUrl(repoName(), '/tree?ref=' + enc(branchName()))).then(function (data) {
      if (S.ws.treeKey !== key) return;
      S.ws.tree = (data.tree || []).filter(function (item) { return item && typeof item.path === 'string'; });
      S.ws.treeLoading = false;
      if (S.ws.tree.length <= 60) {
        S.ws.tree.forEach(function (item) { if (item.type === 'tree') S.ws.expanded[item.path] = true; });
      }
      renderFiles();
      var readme = S.ws.tree.filter(function (item) { return item.type === 'blob' && /^readme(\.md|\.rst|\.txt)?$/i.test(item.path); })[0];
      if (readme && !mqPhone.matches) openFile(readme.path, true);
    }, function (error) {
      if (S.ws.treeKey !== key) return;
      S.ws.treeLoading = false;
      S.ws.treeError = error.message;
      renderFiles();
    });
  }

  function buildTree(entries) {
    var root = { dirs: {}, files: [] };
    entries.forEach(function (entry) {
      var parts = entry.path.split('/');
      var node = root;
      for (var i = 0; i < parts.length - 1; i++) {
        node = node.dirs[parts[i]] || (node.dirs[parts[i]] = { dirs: {}, files: [] });
      }
      var name = parts[parts.length - 1];
      if (entry.type === 'tree') { if (!node.dirs[name]) node.dirs[name] = { dirs: {}, files: [] }; }
      else if (entry.type === 'blob') node.files.push({ name: name, path: entry.path });
    });
    return root;
  }
  function byName(a, b) { return a.localeCompare(b, undefined, { sensitivity: 'base', numeric: true }); }

  function treeLevel(node, prefix) {
    var html = '<ul>';
    Object.keys(node.dirs).sort(byName).forEach(function (name) {
      var path = prefix ? prefix + '/' + name : name;
      var open = !!S.ws.expanded[path];
      html += '<li><button class="tree-row dir" type="button" data-action="toggle-dir" data-path="' + esc(path) + '" aria-expanded="' + open + '">' +
        icon('chevron-right', 'icon-xs') + icon('folder', 'icon-sm') + '<span>' + esc(name) + '</span></button>' +
        (open ? treeLevel(node.dirs[name], path) : '') + '</li>';
    });
    node.files.sort(function (a, b) { return byName(a.name, b.name); }).forEach(function (file) {
      var active = S.ws.file && S.ws.file.path === file.path;
      html += '<li><button class="tree-row file' + (active ? ' is-active' : '') + '" type="button" data-action="open-file" data-path="' + esc(file.path) + '">' +
        icon('file', 'icon-sm') + '<span>' + esc(file.name) + '</span></button></li>';
    });
    return html + '</ul>';
  }

  function treeBody() {
    var entries = S.ws.tree || [];
    var filter = S.ws.filter.trim().toLowerCase();
    var note = entries.length >= 500
      ? '<p class="tree-note">Показаны первые 500 элементов дерева — в большом репозитории часть файлов сюда не попадает.</p>' : '';
    if (filter) {
      var hits = entries.filter(function (item) { return item.type === 'blob' && item.path.toLowerCase().indexOf(filter) !== -1; }).slice(0, 200);
      if (!hits.length) return '<p class="tree-note">Ничего не найдено.</p>';
      return '<ul>' + hits.map(function (item) {
        var cut = item.path.lastIndexOf('/');
        var active = S.ws.file && S.ws.file.path === item.path;
        return '<li><button class="tree-row file' + (active ? ' is-active' : '') + '" type="button" data-action="open-file" data-path="' + esc(item.path) + '">' +
          icon('file', 'icon-sm') + '<span>' + esc(item.path.slice(cut + 1)) + '</span>' +
          (cut > 0 ? '<span class="tree-path">' + esc(item.path.slice(0, cut)) + '</span>' : '') + '</button></li>';
      }).join('') + '</ul>' + note;
    }
    if (!entries.length) return '<p class="tree-note">В этой ветке нет файлов.</p>';
    return treeLevel(buildTree(entries), '') + note;
  }

  function fileBody() {
    var file = S.ws.file;
    if (!file) {
      return emptyHtml('file', 'Выберите файл',
        'Файлы читаются из ' + repoName() + ' на ветке ' + branchName() + '. Они только для чтения: изменения вносит агент.');
    }
    var parts = file.path.split('/');
    var crumbs = parts.map(function (part) { return '<span>' + esc(part) + '</span>'; }).join('<i>/</i>');
    var lang = HL.languageFor(file.path);
    var isDoc = lang === 'md';
    var github = S.binding && S.binding.provider === 'github'
      ? 'https://github.com/' + repoName().split('/').map(enc).join('/') + '/blob/' + enc(branchName()) + '/' + parts.map(enc).join('/') : '';
    var head = '<div class="file-head">' +
      '<button class="icon-btn sm file-back" type="button" data-action="files-back" aria-label="К списку файлов">' + icon('arrow-left', 'icon-sm') + '</button>' +
      '<div class="crumbs" title="' + esc(file.path) + '">' + crumbs + '</div>' +
      (isDoc && file.content != null ? '<div class="seg" role="group" aria-label="Вид файла">' +
        '<button type="button" data-action="doc-view" data-view="doc" aria-pressed="' + S.ws.docView + '">Просмотр</button>' +
        '<button type="button" data-action="doc-view" data-view="code" aria-pressed="' + !S.ws.docView + '">Код</button></div>' : '') +
      (file.content != null ? '<button class="icon-btn sm" type="button" data-action="copy-file" aria-label="Скопировать содержимое файла" title="Скопировать">' + icon('copy', 'icon-sm') + '</button>' : '') +
      (github ? '<a class="icon-btn sm" href="' + esc(github) + '" target="_blank" rel="noopener noreferrer" aria-label="Открыть на GitHub" title="Открыть на GitHub">' + icon('external', 'icon-sm') + '</a>' : '') +
      '</div>';
    if (file.loading) return head + SKELETON;
    if (file.error) {
      return head + emptyHtml('alert', 'Файл не открыт', file.error,
        '<button class="btn btn-sm" type="button" data-action="open-file" data-path="' + esc(file.path) + '">Повторить</button>');
    }
    var cut = file.truncated
      ? '<p class="tree-note">Файл большой: показаны только первые 200 КБ.</p>' : '';
    if (isDoc && S.ws.docView) return head + '<div class="doc"><div class="md">' + MD.render(file.content) + '</div>' + cut + '</div>';
    var rows = HL.lines(file.content, lang);
    return head + '<pre class="codeview" tabindex="0"><code>' + rows.map(function (row) {
      return '<span class="cl">' + row + '</span>';
    }).join('') + '</code></pre>' + cut;
  }

  function renderFiles() {
    var body = $('#ws-body');
    if (!S.ws.open || S.ws.tab !== 'files') return;
    if (!repoName()) {
      body.innerHTML = emptyHtml('repo', 'Репозиторий не подключён',
        'Подключите репозиторий — здесь появятся его файлы, а агент сможет в нём работать.',
        '<button class="btn btn-primary" type="button" data-action="open-repo">Подключить репозиторий</button>');
      return;
    }
    if (S.ws.treeKey !== treeKey()) { loadTree(false); return; }
    if (S.ws.treeLoading) { body.innerHTML = SKELETON; return; }
    if (S.ws.treeError) {
      var info = explain(S.ws.treeError);
      body.innerHTML = emptyHtml('alert', 'Файлы недоступны', info.text,
        '<div class="msg-actions"><button class="btn btn-sm" type="button" data-action="reload-tree">Повторить</button>' +
        (info.action ? '<button class="btn btn-sm btn-primary" type="button" data-action="' + info.action + '">' + esc(info.actionLabel) + '</button>' : '') + '</div>');
      return;
    }
    body.innerHTML = '<div class="files" data-pane="' + S.ws.pane + '">' +
      '<div class="tree-pane"><div class="tree-head"><label class="tree-search">' + icon('search', 'icon-sm') +
      '<input id="tree-filter" type="search" placeholder="Найти файл" autocomplete="off" aria-label="Найти файл" value="' + esc(S.ws.filter) + '"></label>' +
      '<button class="icon-btn sm" type="button" data-action="reload-tree" aria-label="Обновить список файлов" title="Обновить">' + icon('refresh', 'icon-sm') + '</button></div>' +
      '<div class="tree" id="tree">' + treeBody() + '</div></div>' +
      '<div class="file-pane" id="file-pane">' + fileBody() + '</div></div>';
  }
  function renderTree() { var tree = $('#tree'); if (tree) tree.innerHTML = treeBody(); }
  function renderFile() {
    var pane = $('#file-pane');
    if (pane) pane.innerHTML = fileBody();
    var files = $('.files');
    if (files) files.setAttribute('data-pane', S.ws.pane);
  }

  function openFile(path, quiet) {
    var key = treeKey();
    if (!quiet) S.ws.pane = 'file';
    var cached = S.ws.files[path];
    if (cached) { S.ws.file = cached; renderTree(); renderFile(); return; }
    S.ws.file = { path: path, loading: true, content: null, error: null };
    renderTree();
    renderFile();
    api(repoUrl(repoName(), '/contents/' + path.split('/').map(enc).join('/') + '?ref=' + enc(branchName())))
      .then(function (data) {
        if (S.ws.treeKey !== key) return;
        var file = { path: path, loading: false, error: null, truncated: !!data.truncated,
                     content: data.content == null ? '' : String(data.content) };
        S.ws.files[path] = file;
        if (S.ws.file && S.ws.file.path === path) { S.ws.file = file; renderFile(); }
      }, function (error) {
        if (S.ws.treeKey !== key || !S.ws.file || S.ws.file.path !== path) return;
        S.ws.file = { path: path, loading: false, content: null,
                      error: error.status === 404 ? 'Это двоичный файл, либо его нет в этой ветке.' : explain(error.message).text };
        renderFile();
      });
  }

  function renderWorkspace() {
    if (!S.ws.open) return;
    $('#tab-files').setAttribute('aria-selected', String(S.ws.tab === 'files'));
    $('#tab-agent').setAttribute('aria-selected', String(S.ws.tab === 'agent'));
    if (S.ws.tab === 'agent') renderAgent();
    else renderFiles();
    renderWorkBadges();
  }

  function renderAll() {
    renderProject();
    renderChats();
    renderTopbar();
    renderThread();
    renderComposer();
    renderWorkspace();
    renderWorkBadges();
  }

  /* ---------------------------------------------------------- repo dialog */
  var repoState = null;

  function loadGithubAccounts() {
    return api('/api/github/accounts').then(function (data) {
      S.accounts.github = data.accounts || [];
      return S.accounts.github;
    });
  }

  function openRepoDialog() {
    repoState = { source: '', repos: null, error: '', query: '', selected: null, branches: null, branch: '' };
    openDialog({
      title: 'Репозиторий проекта',
      body:
        '<p class="lead">Выберите репозиторий и ветку. В них будет работать агент, а вкладка «Файлы» покажет их содержимое.</p>' +
        '<div class="field"><span class="field-label">Аккаунт GitHub</span>' +
        '<select class="select" id="repo-source" aria-label="Аккаунт GitHub"></select></div>' +
        '<details class="add" id="repo-add"><summary>' + icon('plus', 'icon-sm') + 'Подключить аккаунт GitHub</summary>' +
        githubFormHtml() + '</details>' +
        '<div class="picker"><label class="picker-search">' + icon('search', 'icon-sm') +
        '<input id="repo-query" type="search" placeholder="Поиск репозитория" autocomplete="off" aria-label="Поиск репозитория"></label>' +
        '<div class="picker-list" id="repo-list" role="listbox" aria-label="Репозитории"></div></div>' +
        '<label class="field"><span class="field-label">Ветка</span>' +
        '<select class="select" id="repo-branch" disabled><option value="">Сначала выберите репозиторий</option></select></label>' +
        '<label class="checkbox"><input type="checkbox" id="repo-analyze" checked>' +
        '<span>Изучить репозиторий после подключения, чтобы ассистент знал структуру проекта</span></label>',
      foot: '<p class="form-msg" id="repo-msg" role="status"></p>' +
        '<button class="btn btn-ghost" type="button" data-action="close-dialog">Отмена</button>' +
        '<button class="btn btn-primary" type="button" id="repo-connect" data-action="repo-connect" disabled>Подключить</button>',
      onClose: function () { repoState = null; }
    });
    $('#repo-list').innerHTML = '<p class="picker-note">Загружаем аккаунты…</p>';
    loadGithubAccounts().then(fillRepoSources, function (error) {
      S.accounts.github = [];
      fillRepoSources();
      toast(error.message, 'bad');
    });
  }

  function fillRepoSources() {
    if (!repoState) return;
    var select = $('#repo-source');
    var active = (S.accounts.github || []).filter(function (account) { return account.state === 'active'; });
    var options = active.map(function (account) {
      return '<option value="' + esc(account.id) + '">' + esc(account.label) + ' — ' + esc(account.login || '') + '</option>';
    });
    if (S.status && S.status.github && S.status.github.server_token) {
      options.push('<option value="server">Токен сервера (GITHUB_TOKEN)</option>');
    }
    if (!options.length) {
      select.innerHTML = '<option value="">Нет подключённых аккаунтов</option>';
      select.disabled = true;
      $('#repo-add').open = true;
      $('#repo-list').innerHTML = '<p class="picker-note">Подключите аккаунт GitHub — список репозиториев загрузится с его токеном.</p>';
      repoState.source = '';
      return;
    }
    select.disabled = false;
    select.innerHTML = options.join('');
    var has = function (value) { return Array.prototype.some.call(select.options, function (o) { return o.value === value; }); };
    var bound = S.binding && S.binding.github_account_id ? String(S.binding.github_account_id) : '';
    var preferred = repoState.source && has(repoState.source) ? repoState.source
      : (bound && has(bound) ? bound : select.options[0].value);
    select.value = preferred;
    repoState.source = preferred;
    loadRepos();
  }

  function loadRepos() {
    if (!repoState || !repoState.source) return;
    var source = repoState.source;
    repoState.repos = null; repoState.error = ''; repoState.selected = null; repoState.branches = null;
    renderRepoList();
    syncRepoBranch();
    var url = source === 'server' ? '/api/github/repositories'
      : '/api/github/accounts/' + enc(source) + '/repositories';
    api(url).then(function (data) {
      if (!repoState || repoState.source !== source) return;
      repoState.repos = data.repositories || [];
      renderRepoList();
    }, function (error) {
      if (!repoState || repoState.source !== source) return;
      repoState.repos = [];
      repoState.error = explain(error.message).text;
      renderRepoList();
    });
  }

  function renderRepoList() {
    var box = $('#repo-list');
    if (!box || !repoState) return;
    if (repoState.repos === null) { box.innerHTML = '<p class="picker-note">Загружаем репозитории…</p>'; return; }
    if (repoState.error) { box.innerHTML = '<p class="picker-note">' + esc(repoState.error) + '</p>'; return; }
    var query = repoState.query.trim().toLowerCase();
    var shown = repoState.repos.filter(function (repo) {
      return !query || String(repo.full_name).toLowerCase().indexOf(query) !== -1;
    });
    if (!shown.length) {
      box.innerHTML = '<p class="picker-note">' + (repoState.repos.length ? 'Ничего не найдено.' : 'У этого аккаунта нет доступных репозиториев.') + '</p>';
      return;
    }
    box.innerHTML = shown.map(function (repo) {
      var parts = String(repo.full_name).split('/');
      var selected = repoState.selected && repoState.selected.full_name === repo.full_name;
      return '<button class="picker-item" type="button" role="option" data-action="repo-pick" data-name="' + esc(repo.full_name) + '" aria-selected="' + !!selected + '">' +
        icon('repo', 'icon-sm') + '<span class="picker-name"><b>' + esc(parts[1] || repo.full_name) + '</b><small>' + esc(parts[0] || '') +
        ' · ' + esc(repo.default_branch || 'main') + '</small></span>' +
        '<span class="tag' + (repo.private ? ' is-private' : '') + '">' + (repo.private ? 'приватный' : 'публичный') + '</span></button>';
    }).join('');
  }

  function syncRepoBranch() {
    var select = $('#repo-branch'), connect = $('#repo-connect');
    if (!select || !repoState) return;
    if (!repoState.selected) {
      select.innerHTML = '<option value="">Сначала выберите репозиторий</option>';
      select.disabled = true;
      connect.disabled = true;
      return;
    }
    var fallback = repoState.selected.default_branch || 'main';
    var branches = repoState.branches || [{ name: fallback }];
    if (!branches.some(function (b) { return b.name === fallback; })) branches = [{ name: fallback }].concat(branches);
    select.innerHTML = branches.map(function (branch) {
      return '<option value="' + esc(branch.name) + '">' + esc(branch.name) +
        (branch.name === fallback ? ' — основная' : '') + (branch.protected ? ' · защищена' : '') + '</option>';
    }).join('');
    select.disabled = false;
    select.value = repoState.branch && branches.some(function (b) { return b.name === repoState.branch; }) ? repoState.branch : fallback;
    repoState.branch = select.value;
    connect.disabled = false;
  }

  function pickRepo(fullName) {
    if (!repoState || !repoState.repos) return;
    var repo = repoState.repos.filter(function (r) { return r.full_name === fullName; })[0];
    if (!repo) return;
    repoState.selected = repo;
    repoState.branches = null;
    repoState.branch = S.binding && S.binding.full_name === fullName ? branchName() : '';
    renderRepoList();
    syncRepoBranch();
    var name = fullName.split('/').map(enc).join('/');
    var url = repoState.source === 'server' ? '/api/github/repositories/' + name + '/branches'
      : '/api/github/accounts/' + enc(repoState.source) + '/repositories/' + name + '/branches';
    api(url).then(function (data) {
      if (!repoState || repoState.selected !== repo) return;
      repoState.branches = data.branches || [];
      syncRepoBranch();
    }, function () {
      // The default branch is still a valid choice; the server checks it again.
    });
  }

  function connectRepo(button) {
    if (!repoState || !repoState.selected) return;
    var message = $('#repo-msg');
    var fullName = repoState.selected.full_name;
    var branch = $('#repo-branch').value || repoState.selected.default_branch;
    // account_id names the identity: a stored account, or null for the
    // server's own GITHUB_TOKEN.
    var body = { full_name: fullName, branch: branch,
                 account_id: repoState.source === 'server' ? null : repoState.source };
    var analyze = $('#repo-analyze').checked;
    message.className = 'form-msg';
    message.textContent = 'Проверяем репозиторий в GitHub…';
    setBusy(button, true);
    // Looking at the list creates nothing; the project appears with the
    // first thing that needs one, which here is the connection itself.
    ensureProject().then(function () {
      return api(projectUrl('/repository'), { method: 'POST', body: body, timeout: 60000 });
    }).then(function (data) {
      S.binding = data.binding;
      S.ws.treeKey = '';
      closeDialog();
      renderAll();
      toast('Подключён ' + repoName() + ' · ' + branchName());
      if (analyze) {
        api(projectUrl('/context/refresh'), { method: 'POST', body: { repository: repoName(), branch: branchName() }, timeout: 90000 })
          .then(function () { toast('Репозиторий изучен: ассистент знает структуру проекта.'); },
                function (error) { toast('Репозиторий подключён, но изучить его не удалось: ' + explain(error.message).text, 'bad'); });
      }
    }, function (error) {
      setBusy(button, false);
      message.className = 'form-msg is-bad';
      message.textContent = explain(error.message).text;
    });
  }

  /* ------------------------------------------------------------- accounts */
  function githubFormHtml() {
    return '<form class="account-form" autocomplete="off">' +
      '<div class="grid-2"><label class="field"><span class="field-label">Название</span>' +
      '<input class="input" name="label" required maxlength="120" placeholder="Например: личный"></label>' +
      '<label class="field"><span class="field-label">Personal access token</span>' +
      '<input class="input" name="token" type="password" required autocomplete="off" placeholder="ghp_… или github_pat_…"></label></div>' +
      '<p class="field-hint">Токену нужен доступ на чтение репозиториев (для приватных — scope repo). Он проверяется в GitHub, хранится зашифрованным и больше нигде не показывается. Чтобы заменить токен, подключите аккаунт заново с тем же названием.</p>' +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">Подключить</button><p class="form-msg" role="status"></p></div></form>';
  }

  function submitAccountForm(form) {
    var message = $('.form-msg', form), button = $('button[type=submit]', form);
    message.className = 'form-msg';
    message.textContent = 'Проверяем токен в GitHub…';
    setBusy(button, true);
    api('/api/github/accounts', { method: 'POST', timeout: 60000,
                                  body: { label: form.elements.label.value.trim(), token: form.elements.token.value } })
      .then(function (data) {
        setBusy(button, false);
        form.reset();
        message.textContent = '';
        toast('Аккаунт GitHub «' + data.account.label + '» подключён.');
        return loadGithubAccounts().then(function () {
          if (repoState) {
            repoState.source = data.account.id;
            $('#repo-add').open = false;
            fillRepoSources();
          }
          return refreshSettings();
        });
      }, function (error) {
        setBusy(button, false);
        message.className = 'form-msg is-bad';
        message.textContent = explain(error.message).text;
      });
  }

  /* -------------------------------------------------------------- settings */
  var settingsTab = null;

  function openSettings(tab) {
    settingsTab = tab === 'project' ? 'project' : 'connections';
    var tabs = [['connections', 'link', 'Подключения'], ['project', 'layers', 'Проект']];
    openDialog({
      title: 'Настройки',
      className: 'is-wide is-settings',
      body: '<div class="settings"><div class="settings-nav" role="tablist" aria-label="Настройки">' +
        tabs.map(function (item) {
          return '<button type="button" role="tab" data-action="settings-tab" data-tab="' + item[0] + '" aria-selected="false">' +
            icon(item[1], 'icon-sm') + '<span>' + item[2] + '</span></button>';
        }).join('') + '</div><div class="settings-pane" id="settings-pane" role="tabpanel"></div></div>',
      onClose: function () { settingsTab = null; }
    });
    refreshSettings();
  }

  function refreshSettings() {
    var pane = $('#settings-pane');
    if (!pane || !settingsTab) return Promise.resolve();
    var tab = settingsTab;
    $$('.settings-nav button').forEach(function (button) {
      button.setAttribute('aria-selected', String(button.getAttribute('data-tab') === tab));
    });
    if (!pane.firstChild || pane.getAttribute('data-tab') !== tab) pane.innerHTML = SKELETON;
    pane.setAttribute('data-tab', tab);
    var load = tab === 'project'
      ? (S.projectId
         ? Promise.all([
             api(projectUrl('/context')).then(function (data) { return data.context; },
                                             function () { return null; }),
             api(projectUrl('/executors')).then(function (data) { return data.executors || []; },
                                               function () { return []; }),
             api(projectUrl('/repositories')).then(function (data) { return data.repositories || []; },
                                                   function () { return []; }),
             loadOpenhandsAccounts().then(function () { return S.accounts.openhands || []; },
                                          function () { return []; }),
           ]).then(function (parts) {
             return { context: parts[0], executors: parts[1], repositories: parts[2],
                      accounts: parts[3] };
           })
         : Promise.resolve(null))
      : Promise.all([
          loadGithubAccounts().catch(function () { S.accounts.github = []; }),
          loadOpenhandsAccounts().catch(function () { S.accounts.openhands = []; }),
          api('/api/studio').then(function (data) { S.status = data; }, function () {})
        ]);
    return load.then(function (result) {
      if (settingsTab !== tab || !document.contains(pane)) return;
      if (tab === 'project') {
        S.projectSettings = result || null;
        pane.innerHTML = projectSettingsHtml(result);
      } else {
        pane.innerHTML = connectionsHtml();
      }
    });
  }

  function loadOpenhandsAccounts() {
    return api('/api/openhands/accounts').then(function (data) {
      S.accounts.openhands = data.accounts || [];
    });
  }

  function submitOpenhandsForm(form) {
    var message = $('.form-msg', form), button = $('button[type=submit]', form);
    message.className = 'form-msg';
    message.textContent = 'Проверяем ключ в OpenHands…';
    setBusy(button, true);
    api('/api/openhands/accounts', { method: 'POST', timeout: 60000,
        body: { label: form.elements.label.value.trim(),
                api_key: form.elements.api_key.value,
                base_url: form.elements.base_url.value.trim() || undefined,
                transport: form.elements.transport.value,
                default_model: form.elements.default_model.value.trim() || undefined } })
      .then(function (data) {
        setBusy(button, false);
        form.reset();
        message.textContent = '';
        toast('OpenHands «' + data.account.label + '» подключён.');
        return loadOpenhandsAccounts().then(refreshSettings);
      }, function (error) {
        setBusy(button, false);
        message.className = 'form-msg is-bad';
        message.textContent = explain(error.message).text;
      });
  }

  function accountCard(account) {
    var id = esc(account.id);
    var actions = account.state === 'revoked' ? ''
      : '<button class="btn btn-sm" type="button" data-action="account-op" data-op="verify" data-id="' + id + '">Проверить</button>' +
        '<button class="btn btn-sm btn-danger" type="button" data-action="account-op" data-op="revoke" data-id="' + id + '">Отключить</button>';
    return '<div class="card"><div class="card-main"><b>' + esc(account.label) + '</b><small>' + esc(account.login || '—') + '</small>' +
      (account.last_error ? '<small class="is-bad">' + esc(account.last_error) + '</small>' : '') + '</div>' +
      pill(account.state) + '<div class="card-actions">' + actions + '</div></div>';
  }

  function openhandsCard(account) {
    var id = esc(account.id);
    var usable = account.state === 'active' && account.enabled !== false;
    var health = { healthy: 'работает', degraded: 'нестабилен', unavailable: 'недоступен' }[account.health] || 'не проверялся';
    var actions = account.state === 'revoked' ? ''
      : '<button class="btn btn-sm" type="button" data-action="openhands-rename" data-id="' + id + '" data-label="' + esc(account.label) + '">Переименовать</button>' +
        '<button class="btn btn-sm" type="button" data-action="openhands-op" data-op="verify" data-id="' + id + '">Проверить</button>' +
        '<button class="btn btn-sm" type="button" data-action="openhands-op" data-op="' + (usable ? 'disable' : 'enable') + '" data-id="' + id + '">' +
        (usable ? 'Выключить' : 'Включить') + '</button>' +
        '<button class="btn btn-sm btn-danger" type="button" data-action="openhands-op" data-op="revoke" data-id="' + id + '">Отключить</button>';
    return '<div class="card"><div class="card-main"><b>' + esc(account.label) + '</b>' +
      '<small>' + esc(account.base_url || '') + ' · ' + esc(account.transport || '') +
      (account.default_model ? ' · ' + esc(account.default_model) : '') + '</small>' +
      '<small>доступность: ' + esc(health) + (account.api_version ? ' · API ' + esc(account.api_version) : '') +
      (account.is_default ? ' · по умолчанию' : '') + '</small>' +
      (account.last_error ? '<small class="is-bad">' + esc(account.last_error) + '</small>' : '') + '</div>' +
      pill(account.state === 'active' ? (account.enabled === false ? 'invalid' : 'active') : account.state) +
      '<div class="card-actions">' + actions + '</div></div>';
  }

  function openhandsFormHtml() {
    return '<form class="account-form openhands-form" autocomplete="off">' +
      '<div class="grid-2"><label class="field"><span class="field-label">Название</span>' +
      '<input class="input" name="label" required maxlength="120" placeholder="Например: основной"></label>' +
      '<label class="field"><span class="field-label">API URL</span>' +
      '<input class="input" name="base_url" type="url" placeholder="https://app.all-hands.dev"></label></div>' +
      '<div class="grid-2"><label class="field"><span class="field-label">API key</span>' +
      '<input class="input" name="api_key" type="password" required autocomplete="off" placeholder="Ключ OpenHands"></label>' +
      '<label class="field"><span class="field-label">Транспорт</span>' +
      '<select class="input" name="transport"><option value="direct">direct — свой sandbox</option>' +
      '<option value="app">app — app-conversation</option></select></label></div>' +
      '<label class="field"><span class="field-label">Модель по умолчанию (необязательно)</span>' +
      '<input class="input" name="default_model" placeholder="openhands/deepseek-v4.1-flash"></label>' +
      '<p class="field-hint">Ключ проверяется в OpenHands, шифруется (NEXUS_SECRET_KEY) и никогда не возвращается в интерфейс или API. Несколько подключений позволяют переключаться между исполнителями.</p>' +
      '<div class="row"><button class="btn btn-primary btn-sm" type="submit">Подключить</button><p class="form-msg" role="status"></p></div></form>';
  }

  function connectionsHtml() {
    var status = S.status || {};
    var accounts = S.accounts.github || [];
    var ohAccounts = S.accounts.openhands || [];
    var providers = (status.providers || []).filter(function (name) { return name !== 'openhands'; });
    var openhands = status.openhands || {}, github = status.github || {}, crypto = status.accounts || {};
    var lab = status.lab || {};
    function line(ok, text) {
      return '<dd><span class="dot is-' + (ok ? 'ok' : 'warn') + '"></span>' + text + '</dd>';
    }
    return '<div class="section"><h3 class="section-title">' + icon('github', 'icon-sm') + 'GitHub</h3>' +
      '<p class="section-note">Аккаунт даёт список репозиториев и чтение файлов проекта. Токен хранится зашифрованным и не возвращается ни в интерфейс, ни в API.</p>' +
      (accounts.length ? '<div class="cards">' + accounts.map(accountCard).join('') + '</div>' : '') +
      '<details class="add"' + (accounts.length ? '' : ' open') + '><summary>' + icon('plus', 'icon-sm') + 'Подключить аккаунт GitHub</summary>' + githubFormHtml() + '</details></div>' +
      '<div class="section"><h3 class="section-title">' + icon('bot', 'icon-sm') + 'OpenHands</h3>' +
      '<p class="section-note">Подключения OpenHands, на которых выполняются задачи. Ключ хранится зашифрованным и не показывается. Несколько подключений дают переключение при недоступности одного из них.</p>' +
      (ohAccounts.length ? '<div class="cards">' + ohAccounts.map(openhandsCard).join('') + '</div>' : '') +
      '<details class="add"' + (ohAccounts.length ? '' : ' open') + '><summary>' + icon('plus', 'icon-sm') + 'Подключить OpenHands</summary>' + openhandsFormHtml() + '</details></div>' +
      '<div class="section"><h3 class="section-title">' + icon('info', 'icon-sm') + 'Сервер</h3>' +
      '<p class="section-note">Задаётся переменными окружения сервера. Здесь видно только, что настроено: сами значения в браузер не передаются.</p>' +
      '<dl class="kv is-status">' +
      '<dt>Модели для ответов</dt>' + line(providers.length, providers.length ? esc(providers.join(', '))
        : 'не настроены — «Вопрос», «План» и «Автономно» не ответят') +
      '<dt>OpenHands</dt>' + line(openhands.server_key, openhands.server_key
        ? 'ключ задан · транспорт ' + esc(openhands.transport || '') : 'OPENHANDS_API_KEY не задан — задачи не запустятся') +
      '<dt>GitHub</dt>' + line(github.server_token, github.server_token
        ? 'токен сервера задан' : 'токена сервера нет — нужен аккаунт GitHub выше') +
      '<dt>Шифрование</dt>' + line(crypto.encryption_key, crypto.encryption_key
        ? 'включено' : 'NEXUS_SECRET_KEY не задан — аккаунты не сохранятся') +
      '<dt>Автономные прогоны</dt>' + line(lab.inline, lab.inline
        ? 'ведёт сам сервер' : 'выключено (NEXUS_LAB_INLINE=0) — нужен процесс lab_worker') +
      '<dt>Версия</dt><dd>' + esc(status.version || '—') + '</dd></dl></div>';
  }

  function projectSettingsHtml(context) {
    var current = project();
    if (!current) {
      return emptyHtml('layers', 'Проекта ещё нет', 'Он создастся с первым сообщением. Можно создать и сейчас.',
        '<button class="btn btn-primary" type="button" data-action="new-project">Новый проект</button>');
    }
    var revision = context && context.revision ? String(context.revision).slice(0, 7) : '';
    return '<div class="section"><h3 class="section-title">' + icon('layers', 'icon-sm') + 'Название</h3>' +
      '<form class="row row-fill" id="project-rename"><input class="input" name="name" required maxlength="120" value="' + esc(current.name) + '" aria-label="Название проекта">' +
      '<button class="btn btn-primary" type="submit">Сохранить</button></form><p class="form-msg" id="project-msg" role="status"></p></div>' +
      '<div class="section"><h3 class="section-title">' + icon('branch', 'icon-sm') + 'Репозиторий</h3>' +
      '<p class="section-note">Репозиторий и ветка, в которых работает агент. Меняются только здесь, явным действием.</p>' +
      (repoName()
        ? '<dl class="kv"><dt>Репозиторий</dt><dd class="mono">' + esc(repoName()) + '</dd><dt>Ветка</dt><dd class="mono">' + esc(branchName()) + '</dd>' +
          '<dt>Доступ</dt><dd>' + (S.binding.github_account_id ? 'аккаунт GitHub, привязанный к проекту' : 'токен сервера') + '</dd></dl>'
        : '') +
      '<button class="btn" type="button" data-action="open-repo">' + (repoName() ? 'Сменить репозиторий или ветку' : 'Подключить репозиторий') + '</button></div>' +
      '<div class="section"><h3 class="section-title">' + icon('eye', 'icon-sm') + 'Контекст для ассистента</h3>' +
      '<p class="section-note">Сводка структуры репозитория, которую ассистент получает вместе с вопросом.</p>' +
      (context
        ? '<dl class="kv"><dt>Репозиторий</dt><dd class="mono">' + esc(context.repository || '—') + '</dd>' +
          '<dt>Ревизия</dt><dd class="mono">' + esc(revision || '—') + '</dd><dt>Собран</dt><dd>' + esc(fullTime(context.created_at)) + '</dd></dl>'
        : '<p class="section-note">Контекст ещё не собран.</p>') +
      '<button class="btn" type="button" data-action="refresh-context"' + (repoName() ? '' : ' disabled') + '>' + icon('refresh', 'icon-sm') +
      (context ? 'Изучить заново' : 'Изучить репозиторий') + '</button></div>' +
      projectRepositoriesHtml(context && context.repositories) +
      projectExecutorsHtml(context && context.executors, context && context.accounts);
  }

  // The project's repositories (several, possibly from different accounts).
  function projectRepositoriesHtml(repositories) {
    var rows = repositories || [];
    var body = rows.length
      ? '<div class="cards">' + rows.map(function (r) {
          var actions = '<button class="btn btn-sm" type="button" data-action="repo-primary" data-name="' + esc(r.full_name) + '"' +
            (r.is_primary ? ' disabled' : '') + '>Основной</button>';
          return '<div class="card"><div class="card-main"><b>' + esc(r.full_name) + '</b>' +
            '<small>' + (r.github_account_id ? 'аккаунт: ' + esc(r.account_login || '') : 'токен сервера') +
            (r.is_primary ? ' · основной' : '') + '</small></div>' +
            (r.is_primary ? pill('active') : '') +
            '<div class="card-actions">' + actions + '</div></div>';
        }).join('') + '</div>'
      : '<p class="section-note">Репозитории ещё не подключены.</p>';
    return '<div class="section"><h3 class="section-title">' + icon('repo', 'icon-sm') + 'Репозитории проекта</h3>' +
      '<p class="section-note">Проект может работать с несколькими репозиториями, в том числе из разных аккаунтов GitHub. Один из них — основной: на него идут задачи без явного репозитория.</p>' +
      body +
      '<button class="btn" type="button" data-action="open-repo">Подключить ещё репозиторий</button></div>';
  }

  // The project's OpenHands executors (primary + fallbacks, in order).
  function projectExecutorsHtml(executors, accounts) {
    var assigned = executors || [];
    var all = accounts || [];
    var assignedIds = assigned.map(function (e) { return String(e.account_id); });
    var body = assigned.length
      ? '<div class="cards">' + assigned.map(function (e) {
          var role = e.role === 'primary' ? 'основной' : 'резервный ' + (e.position || 0);
          var actions = '<button class="btn btn-sm" type="button" data-action="executor-role" data-id="' + esc(e.account_id) + '"' +
            (e.role === 'primary' ? ' disabled' : '') + '>Сделать основным</button>' +
            '<button class="btn btn-sm" type="button" data-action="executor-enabled" data-id="' + esc(e.account_id) + '" data-enabled="' + (e.enabled ? '0' : '1') + '">' +
            (e.enabled ? 'Выключить' : 'Включить') + '</button>' +
            '<button class="btn btn-sm btn-danger" type="button" data-action="executor-remove" data-id="' + esc(e.account_id) + '">Убрать</button>';
          return '<div class="card"><div class="card-main"><b>' + esc(e.label) + '</b>' +
            '<small>' + esc(role) + ' · ' + esc(e.state || '') + (e.enabled ? '' : ' · выключен') + '</small></div>' +
            (e.enabled ? pill('active') : pill('invalid')) +
            '<div class="card-actions">' + actions + '</div></div>';
        }).join('') + '</div>'
      : '<p class="section-note">Исполнители не назначены: проект использует аккаунт по умолчанию.</p>';
    // Accounts that could still be added.
    var available = all.filter(function (a) { return assignedIds.indexOf(String(a.id)) === -1; });
    var add = available.length
      ? '<form class="row row-fill" id="executor-add"><select class="input" name="account_id" aria-label="Добавить исполнителя">' +
        available.map(function (a) { return '<option value="' + esc(a.id) + '">' + esc(a.label) + '</option>'; }).join('') +
        '</select><button class="btn" type="submit">Добавить</button></form>'
      : (all.length ? '' : '<p class="section-note">Сначала подключите OpenHands во вкладке «Подключения».</p>');
    return '<div class="section"><h3 class="section-title">' + icon('bot', 'icon-sm') + 'Исполнители OpenHands</h3>' +
      '<p class="section-note">Какие подключения OpenHands могут выполнять задачи проекта. Основной используется первым, резервные — по порядку при недоступности основного.</p>' +
      body + add + '</div>';
  }

  /* ----------------------------------------------------------------- menus */
  var THEMES = [['system', 'monitor', 'Системная'], ['dark', 'moon', 'Тёмная'], ['light', 'sun', 'Светлая']];

  function projectMenu(anchor) {
    var html = '<div class="menu-title">Проекты</div>' + S.projects.map(function (item) {
      var current = item.id === S.projectId;
      return '<button class="menu-item" type="button" role="menuitemradio" aria-checked="' + current + '" data-action="pick-project" data-id="' + esc(item.id) + '">' +
        icon('layers', 'icon-sm') + '<span class="menu-item-text"><span>' + esc(item.name) + '</span>' +
        (current && repoName() ? '<small>' + esc(repoName()) + ' · ' + esc(branchName()) + '</small>' : '') + '</span>' +
        icon('check', 'icon-sm check') + '</button>';
    }).join('');
    if (!S.projects.length) html += '<p class="sb-empty">Проектов пока нет.</p>';
    html += '<div class="menu-sep"></div>' +
      '<button class="menu-item" type="button" role="menuitem" data-action="new-project">' + icon('plus', 'icon-sm') + '<span>Новый проект</span></button>' +
      (S.projectId ? '<button class="menu-item" type="button" role="menuitem" data-action="open-settings" data-tab="project">' + icon('settings', 'icon-sm') + '<span>Настройки проекта</span></button>' : '');
    openMenu(anchor, html);
  }

  function modeMenu(anchor) {
    openMenu(anchor, '<div class="menu-title">Режим</div>' + Object.keys(MODES).map(function (key) {
      var mode = MODES[key];
      return '<button class="menu-item" type="button" role="menuitemradio" aria-checked="' + (key === S.mode) + '" data-action="pick-mode" data-mode="' + key + '">' +
        icon(mode.icon) + '<span class="menu-item-text wrap"><span>' + mode.label + '</span><small>' + mode.desc + '</small></span>' +
        icon('check', 'icon-sm check') + '</button>';
    }).join(''));
  }

  function userMenu(anchor) {
    var choice = themeChoice();
    openMenu(anchor, '<div class="menu-title">Тема</div><div class="menu-seg">' + THEMES.map(function (option) {
      return '<button type="button" data-action="theme" data-theme="' + option[0] + '" aria-pressed="' + (choice === option[0]) + '">' +
        icon(option[1], 'icon-sm') + '<span>' + option[2] + '</span></button>';
    }).join('') + '</div><div class="menu-sep"></div>' +
      '<button class="menu-item" type="button" role="menuitem" data-action="open-settings">' + icon('settings', 'icon-sm') + '<span>Настройки</span></button>' +
      '<button class="menu-item is-danger" type="button" role="menuitem" id="logout" data-action="logout">' + icon('logout', 'icon-sm') + '<span>Выйти</span></button>');
  }

  function findChat(id) {
    return S.chats.filter(function (chat) { return chat.id === id; })[0] || (S.conv && S.conv.id === id ? S.conv : null);
  }

  function chatMenu(anchor) {
    var id = anchor.getAttribute('data-id');
    var chat = findChat(id);
    if (!chat) return;
    openMenu(anchor,
      '<button class="menu-item" type="button" role="menuitem" data-action="rename-chat" data-id="' + esc(id) + '">' + icon('pencil', 'icon-sm') + '<span>Переименовать</span></button>' +
      (chat.archived
        ? '<button class="menu-item" type="button" role="menuitem" data-action="archive-chat" data-id="' + esc(id) + '" data-archived="0">' + icon('refresh', 'icon-sm') + '<span>Вернуть из архива</span></button>'
        : '<button class="menu-item" type="button" role="menuitem" data-action="archive-chat" data-id="' + esc(id) + '" data-archived="1">' + icon('archive', 'icon-sm') + '<span>В архив</span></button>'),
      { alignRight: true });
  }

  function formDialog(options) {
    // A one-field dialog: rename a chat, name a project.
    openDialog({
      title: options.title,
      body: (options.lead ? '<p class="lead">' + esc(options.lead) + '</p>' : '') +
        '<form id="one-form"><label class="field"><span class="field-label">Название</span>' +
        '<input class="input" id="one-input" name="value" required maxlength="' + options.max + '" value="' + esc(options.value || '') + '"' +
        (options.placeholder ? ' placeholder="' + esc(options.placeholder) + '"' : '') + '></label></form>',
      foot: '<p class="form-msg" id="one-msg" role="status"></p><button class="btn btn-ghost" type="button" data-action="close-dialog">Отмена</button>' +
        '<button class="btn btn-primary" type="submit" form="one-form" id="one-submit">' + esc(options.ok) + '</button>'
    });
    var form = $('#one-form'), input = $('#one-input');
    input.focus();
    input.select();
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var value = input.value.trim();
      if (!value) return;
      var button = $('#one-submit');
      setBusy(button, true);
      options.submit(value).catch(function (error) {
        setBusy(button, false);
        var message = $('#one-msg');
        if (!message) return;
        message.className = 'form-msg is-bad';
        message.textContent = error.message;
      });
    });
  }

  function renameChat(id) {
    var chat = findChat(id);
    if (!chat) return;
    formDialog({
      title: 'Название чата', ok: 'Сохранить', max: 200, value: chat.title || '',
      submit: function (title) {
        return api(projectUrl('/conversations/' + enc(id)), { method: 'PATCH', body: { title: title } }).then(function (data) {
          if (S.conv && S.conv.id === id) S.conv.title = data.conversation.title;
          closeDialog();
          renderTopbar();
          return loadChats().then(renderChats);
        });
      }
    });
  }

  function newProjectDialog() {
    formDialog({
      title: 'Новый проект', ok: 'Создать', max: 120, placeholder: 'Например: Магазин',
      lead: 'Проект объединяет чаты и один репозиторий с веткой.',
      submit: function (name) {
        return api('/api/v1/projects', { method: 'POST', body: { name: name } }).then(function (data) {
          S.projects.unshift(data.project);
          while (dialogs.length) closeDialog();
          go(data.project.id, null);
        });
      }
    });
  }

  /* --------------------------------------------------------------- actions */
  function copied(promise, done) {
    promise.then(function () { toast(done); }, function () { toast('Не удалось скопировать.', 'bad'); });
  }

  var ACTIONS = {
    'toggle-sidebar': function () { setSidebar(!sidebarOpen(), true); },
    'close-overlays': function () {
      if (mqPhone.matches && sidebarOpen()) setSidebar(false);
      else if (mqNarrow.matches && S.ws.open) setWorkspace(false);
    },
    'toggle-workspace': function () { setWorkspace(!S.ws.open); },
    'ws-tab': function (el) {
      S.ws.tab = el.getAttribute('data-tab') === 'agent' ? 'agent' : 'files';
      renderWorkspace();
      if (S.ws.tab === 'agent') refreshWork();
    },
    'new-chat': function () {
      if (S.projectId) go(S.projectId, null); else route();
      if (mqPhone.matches) setSidebar(false);
      else $('#prompt').focus();
    },
    'project-menu': projectMenu,
    'mode-menu': modeMenu,
    'user-menu': userMenu,
    'chat-menu': chatMenu,
    'pick-project': function (el) { closeMenu(); go(el.getAttribute('data-id'), null); },
    'pick-mode': function (el) { closeMenu(); setMode(el.getAttribute('data-mode')); $('#prompt').focus(); },
    'new-project': function () { closeMenu(); newProjectDialog(); },
    'open-settings': function (el) {
      var tab = el.getAttribute('data-tab');
      closeMenu();
      if (!settingsTab) openSettings(tab);
      else if (tab) { settingsTab = tab; refreshSettings(); }
    },
    'settings-tab': function (el) { settingsTab = el.getAttribute('data-tab'); refreshSettings(); },
    'open-repo': function () {
      closeMenu();
      if (repoState) return;
      if (settingsTab) closeDialog();
      openRepoDialog();
    },
    'close-dialog': function () { closeDialog(); },
    'theme': function (el) {
      setTheme(el.getAttribute('data-theme'));
      $$('[data-action="theme"]').forEach(function (button) {
        button.setAttribute('aria-pressed', String(button.getAttribute('data-theme') === themeChoice()));
      });
    },
    'logout': function () {
      closeMenu();
      var leave = function () { window.location.href = '/login'; };
      api('/logout', { method: 'POST' }).then(leave, leave);
    },
    'toggle-archived': function () { S.showArchived = !S.showArchived; renderChats(); },
    'rename-chat': function (el) { closeMenu(); renameChat(el.getAttribute('data-id') || S.convId); },
    'archive-chat': function (el) {
      var id = el.getAttribute('data-id'), archived = el.getAttribute('data-archived') === '1';
      closeMenu();
      api(projectUrl('/conversations/' + enc(id)), { method: 'PATCH', body: { archived: archived } }).then(function () {
        toast(archived ? 'Чат перенесён в архив.' : 'Чат возвращён из архива.');
        return loadChats().then(function () {
          // Archiving the chat that is open leaves nothing to show: back to
          // the start screen. Either way the history is redrawn from the server.
          if (archived && id === S.convId) return go(S.projectId, null);
          renderChats();
          return null;
        });
      }, function (error) { toast(error.message, 'bad'); });
    },
    'start': function (el) {
      var start = ($('#home-extras').starts || [])[Number(el.getAttribute('data-index'))];
      if (!start) return;
      setMode(start.mode);
      var prompt = $('#prompt');
      prompt.value = start.text;
      growPrompt();
      syncSend();
      prompt.focus();
      prompt.setSelectionRange(prompt.value.length, prompt.value.length);
    },
    'retry': function (el) {
      var id = el.getAttribute('data-id');
      var index = S.messages.map(function (message) { return String(message.id); }).indexOf(id);
      for (var i = index - 1; i >= 0; i--) {
        if (S.messages[i].role === 'user') { send(S.messages[i].content); return; }
      }
    },
    'copy-message': function (el) {
      var id = el.getAttribute('data-id');
      var message = S.messages.filter(function (m) { return String(m.id) === id; })[0];
      if (!message) return;
      // The full stored text, not the rendered bubble: the same button copies an
      // assistant reply and the user's own message.
      copied(copyText(message.content),
             message.role === 'user' ? 'Сообщение скопировано.' : 'Ответ скопирован.');
    },
    'copy-report': function (el) {
      var id = el.getAttribute('data-id');
      var record = S.work.data['run:' + id];
      var content = record && record.detail && record.detail.report
        ? record.detail.report.content : '';
      if (!content) { toast('Отчёт ещё не готов.', 'bad'); return; }
      copied(copyText(content), 'Отчёт скопирован.');
    },
    'copy-code': function (el) {
      var code = $('code', el.closest('.code'));
      copyText(code ? code.textContent : '').then(function () {
        var label = el.textContent;
        el.textContent = 'Скопировано';
        setTimeout(function () { el.textContent = label; }, 1400);
      }, function () { toast('Не удалось скопировать.', 'bad'); });
    },
    'open-work': function () { setWorkspace(true, 'agent'); refreshWork(); },
    'refresh-work': function () { refreshWork(); },
    'cancel-workflow': function (el) {
      var id = el.getAttribute('data-id');
      confirmDialog('Остановить задачу?', 'Диспетчер перестанет её вести. Сессия OpenHands сохранится, чтобы было видно, что уже сделано.', 'Остановить', function () {
        workAction(null, api('/api/workflows/' + enc(id) + '/cancel', { method: 'POST', body: {} }), 'Задача остановлена.');
      }, true);
    },
    'resume-workflow': function (el) {
      workAction(el, api('/api/workflows/' + enc(el.getAttribute('data-id')) + '/resume', { method: 'POST', body: {} }), 'Задача возобновлена.');
    },
    'retry-task': function (el) {
      workAction(el, api('/api/tasks/' + enc(el.getAttribute('data-id')) + '/retry', { method: 'POST', body: {} }), 'Задача снова в очереди.');
    },
    'run-state': function (el) {
      var id = el.getAttribute('data-id'), op = el.getAttribute('data-op');
      var done = { pause: 'Прогон на паузе.', resume: 'Прогон продолжен.', cancel: 'Прогон отменён.' }[op];
      if (!done) return;
      var act = function (button) {
        if (op === 'cancel') {
          // A cancelled run leaves the interface at once: the server hides it
          // from the active list, and the card is dropped here without waiting
          // for the next poll. The history stays on the server.
          dropWork('run:' + id);
        }
        workAction(button, api(projectUrl('/runs/' + enc(id) + '/' + op), { method: 'POST', body: {} }), done);
      };
      if (op === 'cancel') confirmDialog('Отменить прогон?', 'Незавершённые задачи больше не будут выполняться. Записи о прогоне останутся в истории.', 'Отменить прогон', function () { act(null); }, true);
      else act(el);
    },
    'toggle-dir': function (el) {
      var path = el.getAttribute('data-path');
      S.ws.expanded[path] = !S.ws.expanded[path];
      renderTree();
    },
    'open-file': function (el) { openFile(el.getAttribute('data-path')); },
    'files-back': function () { S.ws.pane = 'tree'; renderFile(); },
    'reload-tree': function () { loadTree(true); },
    'doc-view': function (el) { S.ws.docView = el.getAttribute('data-view') === 'doc'; renderFile(); },
    'copy-file': function () {
      if (S.ws.file && S.ws.file.content != null) copied(copyText(S.ws.file.content), 'Файл скопирован.');
    },
    'repo-pick': function (el) { pickRepo(el.getAttribute('data-name')); },
    'repo-connect': connectRepo,
    'account-op': function (el) {
      var op = el.getAttribute('data-op') === 'revoke' ? 'revoke' : 'verify', id = el.getAttribute('data-id');
      var run = function (button) {
        setBusy(button, true);
        api('/api/github/accounts/' + enc(id) + '/' + op, { method: 'POST', body: {}, timeout: 60000 }).then(function (data) {
          var account = data.account || {};
          if (op === 'revoke') toast('Аккаунт отключён.');
          else if (account.state === 'active') toast('Аккаунт работает.');
          else toast('GitHub отклонил токен: ' + (account.last_error || 'причина не указана'), 'bad');
          return refreshSettings();
        }, function (error) { setBusy(button, false); toast(explain(error.message).text, 'bad'); });
      };
      if (op === 'revoke') {
        confirmDialog('Отключить аккаунт?', 'Сохранённый токен перестанет использоваться. Проекты, привязанные к этому аккаунту, потеряют доступ к его репозиториям.', 'Отключить', function () { run(null); }, true);
      } else run(el);
    },
    'openhands-op': function (el) {
      var op = el.getAttribute('data-op'), id = el.getAttribute('data-id');
      var run = function (button) {
        setBusy(button, true);
        var body = op === 'enable' ? { enabled: true } : op === 'disable' ? { enabled: false } : {};
        api('/api/openhands/accounts/' + enc(id) + '/' + op,
            { method: 'POST', body: body, timeout: 60000 }).then(function (data) {
          if (op === 'revoke') toast('Подключение отключено.');
          else if (op === 'verify') {
            var account = data.account || {};
            if (account.state === 'active' && !account.last_error) toast('Подключение работает.');
            else toast('OpenHands: ' + (account.last_error || 'недоступно'), 'bad');
          } else toast(op === 'enable' ? 'Исполнитель включён.' : 'Исполнитель выключен.');
          return loadOpenhandsAccounts().then(refreshSettings);
        }, function (error) { setBusy(button, false); toast(explain(error.message).text, 'bad'); });
      };
      if (op === 'revoke') {
        confirmDialog('Отключить OpenHands?', 'Ключ перестанет использоваться. Проекты, назначившие это подключение, потеряют исполнителя.', 'Отключить', function () { run(null); }, true);
      } else run(el);
    },
    'openhands-rename': function (el) {
      var id = el.getAttribute('data-id'), label = el.getAttribute('data-label');
      formDialog({
        title: 'Переименовать подключение', ok: 'Сохранить', max: 120, value: label,
        submit: function (name) {
          return api('/api/openhands/accounts/' + enc(id) + '/rename',
                     { method: 'POST', body: { label: name } }).then(function () {
            closeDialog();
            toast('Подключение переименовано.');
            return loadOpenhandsAccounts().then(refreshSettings);
          });
        }
      });
    },
    'refresh-context': function (el) {
      setBusy(el, true);
      api(projectUrl('/context/refresh'), { method: 'POST', body: { repository: repoName(), branch: branchName() }, timeout: 90000 })
        .then(function () { toast('Контекст обновлён.'); return refreshSettings(); },
              function (error) { setBusy(el, false); toast(explain(error.message).text, 'bad'); });
    },
    // Project executors: make primary / enable / remove.
    'executor-role': function (el) {
      executorAction(el, { primary: el.getAttribute('data-id'), fallbacks: [] },
                     'Исполнитель назначен основным.');
    },
    'executor-enabled': function (el) {
      var id = el.getAttribute('data-id'), enable = el.getAttribute('data-enabled') === '1';
      executorAction(el, null, enable ? 'Исполнитель включён.' : 'Исполнитель выключен.',
                     id, enable ? 'enable' : 'disable');
    },
    'executor-remove': function (el) {
      executorAction(el, null, 'Исполнитель убран.', el.getAttribute('data-id'), 'remove');
    },
    // Project repositories: make one primary.
    'repo-primary': function (el) {
      var name = el.getAttribute('data-name');
      setBusy(el, true);
      api(projectUrl('/repositories/' + name.split('/').map(enc).join('/') + '/primary'),
          { method: 'POST', body: {} }).then(function () {
        toast('Основной репозиторий: ' + name + '.');
        return refreshSettings();
      }, function (error) { setBusy(el, false); toast(explain(error.message).text, 'bad'); });
    }
  };

  // One helper for the executor buttons: set the whole assignment (make
  // primary) or act on one account (enable/disable/remove).
  function executorAction(button, replaceBody, done, accountId, op) {
    setBusy(button, true);
    var request = replaceBody
      ? api(projectUrl('/executors'), { method: 'POST', body: replaceBody })
      : api(projectUrl('/executors/' + enc(accountId) + '/' + op), { method: 'POST', body: {} });
    request.then(function () { toast(done); return refreshSettings(); },
                 function (error) { setBusy(button, false); toast(explain(error.message).text, 'bad'); });
  }

  /* ---------------------------------------------------------------- events */
  function bindEvents() {
    document.addEventListener('click', function (event) {
      var target = event.target.closest('[data-action]');
      if (openMenuState && !openMenuState.el.contains(event.target) &&
          !(target && target === openMenuState.anchor)) closeMenu();
      // A plain click on a chat or on the logo routes here and now. A click
      // with a modifier is left to the browser, so "open in a new tab" works.
      var link = event.target.closest('a.sb-link, a.brand');
      if (link && event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
        event.preventDefault();
        navigate(link.getAttribute('href'));
        // On a phone the drawer goes away, also when the chat was already open.
        if (mqPhone.matches) setSidebar(false);
      }
      if (!target) return;
      var handler = ACTIONS[target.getAttribute('data-action')];
      if (handler) handler(target, event);
    });

    document.addEventListener('submit', function (event) {
      var form = event.target;
      if (form.id === 'composer') {
        event.preventDefault();
        send($('#prompt').value);
      } else if (form.classList.contains('openhands-form')) {
        event.preventDefault();
        submitOpenhandsForm(form);
      } else if (form.id === 'executor-add') {
        event.preventDefault();
        var accountId = form.elements.account_id.value;
        if (!accountId) return;
        // Add to the current assignment: keep the primary, append the new one
        // as a fallback. The list is applied whole by the server.
        var execs = (S.projectSettings && S.projectSettings.executors) || [];
        var primary = (execs.filter(function (e) { return e.role === 'primary'; })[0] || {}).account_id || null;
        var fallbacks = execs.filter(function (e) { return e.role !== 'primary'; })
                             .map(function (e) { return String(e.account_id); });
        fallbacks.push(accountId);
        setBusy($('button[type=submit]', form), true);
        api(projectUrl('/executors'), { method: 'POST', body: { primary: primary, fallbacks: fallbacks } })
          .then(function () { toast('Исполнитель добавлен.'); return refreshSettings(); },
                function (error) { toast(explain(error.message).text, 'bad'); });
      } else if (form.classList.contains('account-form')) {
        event.preventDefault();
        submitAccountForm(form);
      } else if (form.id === 'project-rename') {
        event.preventDefault();
        var message = $('#project-msg');
        api(projectUrl(''), { method: 'PATCH', body: { name: form.elements.name.value.trim() } }).then(function (data) {
          S.projects = S.projects.map(function (item) { return item.id === data.project.id ? data.project : item; });
          renderProject();
          if (!S.convId) renderThread();
          message.className = 'form-msg is-ok';
          message.textContent = 'Сохранено.';
        }, function (error) {
          message.className = 'form-msg is-bad';
          message.textContent = error.message;
        });
      }
    });

    document.addEventListener('input', function (event) {
      var el = event.target;
      if (el.id === 'prompt') { growPrompt(); syncSend(); }
      else if (el.id === 'tree-filter') { S.ws.filter = el.value; renderTree(); }
      else if (el.id === 'repo-query' && repoState) { repoState.query = el.value; renderRepoList(); }
    });
    document.addEventListener('change', function (event) {
      var el = event.target;
      if (el.id === 'repo-source' && repoState) { repoState.source = el.value; loadRepos(); }
      else if (el.id === 'repo-branch' && repoState) repoState.branch = el.value;
    });

    var thread = $('#thread');
    thread.addEventListener('scroll', function () {
      threadPinned = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
    }, { passive: true });
    if (window.ResizeObserver) {
      new ResizeObserver(function () {
        if (threadPinned && S.convId) scrollToEnd(false);
      }).observe(thread);
    }

    var searchChats = debounce(function () { loadChats().then(renderChats); }, 220);
    $('#chat-search').addEventListener('input', function (event) {
      S.chatQuery = event.target.value.trim();
      searchChats();
    });

    $('#prompt').addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
      // On a touch keyboard Enter is the only way to start a new line.
      if (mqTouch.matches) return;
      event.preventDefault();
      send(event.target.value);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') {
        if (openMenuState) { openMenuState.refocus = true; closeMenu(); }
        else if (dialogs.length) closeDialog();
        else if (mqPhone.matches && sidebarOpen()) setSidebar(false);
        else if (mqNarrow.matches && S.ws.open) setWorkspace(false);
        return;
      }
      trapFocus(event);
      if (openMenuState && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        var items = $$('.menu-item, .menu-seg button', openMenuState.el);
        if (!items.length) return;
        var at = items.indexOf(document.activeElement);
        var next = event.key === 'ArrowDown' ? (at + 1) % items.length : (at <= 0 ? items.length - 1 : at - 1);
        event.preventDefault();
        items[next].focus();
      }
    });

    // navigate() has already routed its own changes; this is for the address
    // bar and the Back button.
    window.addEventListener('hashchange', function () {
      if (window.location.hash !== routedHash) route();
    });
    window.addEventListener('resize', debounce(function () { closeMenu(); syncScrim(); }, 120));
    mqPhone.addEventListener('change', applyLayoutDefaults);
    mqNarrow.addEventListener('change', applyLayoutDefaults);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') refreshWork();
      else scheduleWorkPoll();
    });
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', function () {
      if (themeChoice() === 'system') setTheme('system');
    });

    bindResizer();
  }

  function bindResizer() {
    var handle = $('#ws-resizer'), panel = $('#workspace');
    function apply(width) {
      width = Math.max(400, Math.min(Math.round(width), Math.round(window.innerWidth * 0.72)));
      panel.style.width = width + 'px';
      store.set('ws-width', width);
    }
    handle.addEventListener('pointerdown', function (event) {
      if (mqNarrow.matches) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add('is-resizing');
      function move(e) { apply(window.innerWidth - e.clientX); }
      function stop() {
        document.body.classList.remove('is-resizing');
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', stop);
        handle.removeEventListener('pointercancel', stop);
      }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', stop);
      handle.addEventListener('pointercancel', stop);
    });
    handle.addEventListener('keydown', function (event) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      apply(panel.getBoundingClientRect().width + (event.key === 'ArrowLeft' ? 24 : -24));
    });
  }

  /* ------------------------------------------------------------------ boot */
  function boot() {
    bindEvents();
    applyLayoutDefaults();
    renderComposer();
    // A link straight to a conversation should not flash the start screen.
    if (/\/c\//.test(window.location.hash)) appEl.setAttribute('data-view', 'chat');
    else renderThread();
    api('/api/session').then(function (session) {
      S.csrf = session.csrf_token || '';
      S.user = session.username || '';
      $('#whoami').textContent = S.user;
      $('#user-avatar').textContent = S.user.charAt(0) || '?';
      return Promise.all([
        api('/api/studio').then(function (data) { S.status = data; }, function () { S.status = null; }),
        loadProjects()
      ]);
    }).then(function () {
      return route();
    }).then(function () {
      if (store.get('workspace') === 'open' && !mqNarrow.matches) setWorkspace(true);
      appEl.setAttribute('data-ready', 'true');
      if (!mqTouch.matches) $('#prompt').focus();
    }).catch(function (error) {
      appEl.setAttribute('data-ready', 'error');
      if (error.status !== 401) toast(error.message || 'Не удалось загрузить студию.', 'bad');
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
