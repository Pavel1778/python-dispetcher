// Deliberately no Cache Storage, background sync or queued writes: the server
// is the source of truth. Offline fallback contains no user or project data.
const OFFLINE = `<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Нет соединения — NEXUS</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;font:15px/1.5 system-ui,sans-serif;background:#0b0d12;color:#e9ecf2">
<main style="max-width:380px;padding:24px;text-align:center">
<h1 style="margin:0 0 8px;font-size:22px">Нет соединения</h1>
<p style="margin:0 0 6px;color:#a5adbe">Не удалось связаться с сервером. Проверьте интернет и повторите попытку.</p>
<p style="margin:0 0 18px;color:#a5adbe">Данные и действия не сохраняются для отправки офлайн.</p>
<button onclick="location.reload()" style="height:38px;padding:0 16px;border:0;border-radius:10px;background:#6a57ea;color:#fff;font:inherit;font-weight:500;cursor:pointer">Повторить</button>
</main></body></html>`;

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET' || request.mode !== 'navigate' ||
      new URL(request.url).origin !== self.location.origin) return;
  event.respondWith(fetch(request).catch(() => new Response(OFFLINE, {
    status: 503,
    headers: {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'}
  })));
});
