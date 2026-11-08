// Run the real worker in a small service-worker-like environment.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const worker = fs.readFileSync(path.join(__dirname, '../orchestration/pwa/service-worker.js'), 'utf8');
let listener;
let online = true;
let calls = 0;
const response = new Response('live response', {status: 200});
const context = {
  self: {location: {origin: 'https://example.test'}, addEventListener: (type, fn) => {
    assert.equal(type, 'fetch'); listener = fn;
  }},
  URL, Response,
  fetch: async () => {calls++; if (!online) throw new Error('offline'); return response;}
};
vm.runInNewContext(worker, context);
async function dispatch(request) {
  let result;
  listener({request, respondWith: value => {result = value;}});
  return result === undefined ? undefined : await result;
}
(async () => {
  const navigation = {method:'GET', mode:'navigate', url:'https://example.test/'};
  assert.equal(await dispatch(navigation), response);
  online = false;
  const offline = await dispatch(navigation);
  assert.equal(offline.status, 503);
  assert.equal(offline.headers.get('Cache-Control'), 'no-store');
  assert.match(await offline.text(), /Нет соединения/);
  for (const req of [
    {method:'POST', mode:'navigate', url:'https://example.test/login'},
    {method:'GET', mode:'cors', url:'https://example.test/api/session'},
    {method:'GET', mode:'navigate', url:'https://other.test/'}
  ]) assert.equal(await dispatch(req), undefined);
  assert.equal(calls, 2);
})().catch(error => {console.error(error); process.exitCode = 1;});
