// No authentication data is stored by the service worker. PWA installation is
// optional and failure to register must never prevent the login or dashboard.
if ('serviceWorker' in navigator && window.isSecureContext) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js', {updateViaCache: 'none'})
      .catch(() => {});
  });
}
