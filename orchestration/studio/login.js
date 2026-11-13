/* The login page's own script: a signed-in owner is not asked to sign in.

   The session cookie is SameSite=Strict, so the browser leaves it out of a
   navigation that starts on another site - a link in a chat, in a mail, on
   GitHub. The server then sees a stranger and answers with this page, although
   the session is there. A request this page makes itself is same-site and does
   carry the cookie: it asks whether there is a session and, if so, goes to the
   studio. That navigation starts here, so the cookie goes with it.

   The address the link named is kept: the studio's routes live in the fragment
   (#/p/<project>/c/<chat>), and a fragment survives the server's redirect.

   One try, and only when it can be remembered: if the studio sent the visitor
   back here all the same, the form stays - a page that reloads itself for ever
   is worse than a form. The studio forgets the mark when it opens. */
(function () {
  'use strict';

  var KEY = 'nexus.login.bounce';
  var FRESH_MS = 10000;

  function tried() {
    try {
      var at = Number(window.sessionStorage.getItem(KEY) || 0);
      return at > 0 && Date.now() - at < FRESH_MS;
    } catch (e) { return false; }
  }

  function remember() {
    try {
      window.sessionStorage.setItem(KEY, String(Date.now()));
      return Number(window.sessionStorage.getItem(KEY)) > 0;
    } catch (e) { return false; }
  }

  if (!window.fetch || tried()) return;
  window.fetch('/login/state', { credentials: 'same-origin', cache: 'no-store',
                                 headers: { 'Accept': 'application/json' } })
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (state) {
      if (!state || state.signed_in !== true || !remember()) return;
      window.location.replace('/' + window.location.hash);
    })
    .catch(function () { /* the form is on the page; there is nothing to undo */ });
}());
