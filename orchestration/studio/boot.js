// Applied before the stylesheet paints, so a saved theme never flashes the
// other one. Kept in its own file because the page allows no inline script.
(function () {
  var root = document.documentElement;
  var saved = null;
  try { saved = window.localStorage.getItem('nexus.theme'); } catch (e) { /* private mode */ }
  var system = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches
    ? 'light' : 'dark';
  root.setAttribute('data-theme', saved === 'light' || saved === 'dark' ? saved : system);
  root.setAttribute('data-theme-choice', saved === 'light' || saved === 'dark' ? saved : 'system');
})();
