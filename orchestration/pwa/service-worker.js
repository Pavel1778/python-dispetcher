// Deliberately no Cache Storage, background sync or queued writes: the server
// is the source of truth. Offline fallback contains no user or project data.
const OFFLINE = `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Нет соединения — Диспетчер</title></head>
<body style="font:16px system-ui;background:#0b0d12;color:#e8ebf0;padding:2rem">
<h1>Нет соединения</h1><p>Не удалось связаться с сервером. Проверьте интернет и повторите попытку.</p>
<p>Данные и действия не сохраняются для отправки офлайн.</p>
<button onclick="location.reload()">Повторить</button></body></html>`;

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET' || request.mode !== 'navigate' ||
      new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(fetch(request).catch(() => new Response(OFFLINE, {
    status: 503,
    headers: {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'}
  })));
});
