// The studio's Markdown renderer and syntax highlighter, run under Node.
//
// Both files turn untrusted text (model output, repository files) into HTML that
// is assigned to innerHTML, so what is tested here is the property the page
// depends on: nothing from the source can become markup.
const assert = require('node:assert/strict');
const path = require('node:path');
const dir = path.join(__dirname, '../orchestration/studio');
const MD = require(path.join(dir, 'markdown.js'));
const HL = require(path.join(dir, 'highlight.js'));

const md = text => MD.render(text, { highlight: HL.html });
const tags = html => [...html.matchAll(/<\/?([a-zA-Z][\w-]*)/g)].map(m => m[1].toLowerCase());
const unescape = html => html.replace(/<[^>]*>/g, '')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
function within(ms, label, fn) {
  const started = Date.now();
  const result = fn();
  const took = Date.now() - started;
  assert.ok(took < ms, `${label} took ${took} ms (budget ${ms} ms)`);
  return result;
}

// ------------------------------------------------------------------ markdown
// 1. The only tags in any output are the ones the renderer writes itself.
const ALLOWED = new Set(['p', 'h1', 'h2', 'h3', 'h4', 'ul', 'ol', 'li', 'a', 'code', 'pre', 'strong',
  'em', 'del', 'blockquote', 'hr', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'div', 'span', 'button', 'br']);
const hostile = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  'text <iframe src="https://evil.example"></iframe> text',
  '# <svg onload=alert(1)>',
  '- <a href="javascript:alert(1)">x</a>',
  '> <style>*{display:none}</style>',
  '| <b>a</b> | <i>b</i> |\n|---|---|\n| <u>1</u> | <s>2</s> |',
  '```\n</code></pre><script>alert(1)</script>\n```',
  '```"><img src=x onerror=alert(1)>\ncode\n```',
  '`</code><script>alert(1)</script>`',
  '**<b onclick=alert(1)>bold</b>**',
  '[<img src=x onerror=alert(1)>](https://example.com)',
  '[x](https://example.com/"onmouseover="alert(1))',
  "[x](https://example.com/'onmouseover='alert(1))",
  '<<script>script>alert(1)<</script>/script>',
  '&lt;script&gt; already escaped &amp; stays text',
  '\u0000' + '0' + '\u0000 stash marker typed by hand',
];
for (const source of hostile) {
  const html = md(source);
  for (const tag of tags(html)) assert.ok(ALLOWED.has(tag), `unexpected <${tag}> from ${JSON.stringify(source)}: ${html}`);
  assert.ok(!/<script|<img|<iframe|<svg|<style|onerror=|onload=|onclick=/i.test(html.replace(/&lt;[\s\S]*?&gt;/g, '')),
    `markup leaked from ${JSON.stringify(source)}: ${html}`);
  // No attribute of a written tag can be broken out of: inside every tag the
  // quotes are balanced and there is no event handler.
  for (const tag of html.match(/<[a-zA-Z][^>]*>/g) || []) {
    assert.equal((tag.match(/"/g) || []).length % 2, 0, `unbalanced quotes in ${tag}`);
    assert.ok(!/\son\w+\s*=/i.test(tag), `event handler in ${tag}`);
  }
}

// 2. Links: only http, https and mailto become links, and they open safely.
assert.equal(md('[ok](https://example.com/a?b=1&c=2)'),
  '<p><a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">ok</a></p>');
assert.ok(md('[m](mailto:a@b.c)').includes('href="mailto:a@b.c"'));
for (const url of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,<script>1</script>',
                   'vbscript:x', 'file:///etc/passwd', '//evil.example/x', ' javascript:alert(1)']) {
  const html = md(`[x](${url})`);
  assert.ok(!html.includes('<a '), `${url} became a link: ${html}`);
}
assert.ok(md('see https://bare.example/x.').includes('<a href="https://bare.example/x" '));
// An image is a link to it, never an <img>: the page loads nothing cross-origin.
assert.equal(md('![схема](https://example.com/a.png)'),
  '<p><a href="https://example.com/a.png" target="_blank" rel="noopener noreferrer">схема</a></p>');
assert.ok(md('![](https://example.com/a.png)').includes('>изображение</a>'));

// 3. The structure a model actually writes.
assert.equal(md('# H1\n#### H4'), '<h1>H1</h1><h4>H4</h4>');
assert.equal(md('**b** *i* ~~s~~ `c`'), '<p><strong>b</strong> <em>i</em> <del>s</del> <code>c</code></p>');
assert.equal(md('- [ ] todo\n- [x] done\n  - nested\n\n1. one\n2. two'),
  '<ul><li><span class="md-task"></span>todo</li><li><span class="md-task is-done"></span>done' +
  '<ul><li>nested</li></ul></li></ul><ol><li>one</li><li>two</li></ol>');
assert.equal(md('> quote\n\n---'), '<blockquote><p>quote</p></blockquote><hr>');
assert.equal(md('| a | b |\n|---|:-:|\n| 1 | 2 |'),
  '<div class="md-table"><table><thead><tr><th>a</th><th class="al-c">b</th></tr></thead>' +
  '<tbody><tr><td>1</td><td class="al-c">2</td></tr></tbody></table></div>');
assert.equal(md('snake_case_name and 2*3*4 stay as typed'), '<p>snake_case_name and 2*3*4 stay as typed</p>');
assert.equal(md(''), '');
assert.equal(md(null), '');

// 4. A code block keeps every character and carries its copy control.
const fenced = md('```python\ndef f(x):\n    return "<b>" + x  # 1 < 2\n```');
assert.ok(fenced.includes('<span class="code-lang">python</span>'));
assert.ok(fenced.includes('data-action="copy-code"'));
assert.equal(unescape(fenced.slice(fenced.indexOf('<pre>'))), 'def f(x):\n    return "<b>" + x  # 1 < 2');
// An unterminated fence still renders as code instead of swallowing the reply.
assert.ok(md('```js\nlet a = 1;').includes('<pre><code>'));

// 5. Hostile sizes and shapes finish quickly and do not overflow the stack.
within(2000, 'deep quotes', () => md('>'.repeat(5000) + ' x'));
within(2000, 'deep list', () => md(Array.from({ length: 400 }, (_, i) => ' '.repeat(i * 2) + '- x').join('\n')));
for (const [label, source] of [
  ['stars', '*'.repeat(20000)], ['underscores', '_'.repeat(20000)], ['backticks', '`'.repeat(20000)],
  ['brackets', '['.repeat(20000)], ['open links', '[a]('.repeat(8000)], ['pipes', 'a|'.repeat(10000)],
  ['tildes', '~'.repeat(20000)], ['spaced stars', '* '.repeat(10000)], ['urls', 'https://a.b/'.repeat(4000)],
  ['mixed', '**_`[~'.repeat(6000)], ['long line', 'word '.repeat(60000)],
]) {
  const html = within(2000, label, () => md(source));
  for (const tag of tags(html)) assert.ok(ALLOWED.has(tag), `${label}: <${tag}>`);
}

// ---------------------------------------------------------------- highlight
const samples = {
  py: 'import os\n\n@decorator\nclass A(B):\n    """doc "quoted" </span>"""\n    def f(self, x: int = 0x1F) -> str:\n        return f"{x!r}" + \'<b>\'  # c < d & e\n',
  js: 'const a = `t ${x} </script>`;\n// </span><script>alert(1)</script>\nfunction f(a, b) { return a < b && a > 0 ? /re[/]+/g.test("q\\"") : null; }\n',
  json: '{"key": "<b>v</b>", "n": -1.5e3, "t": true, "z": null}',
  html: '<!doctype html>\n<a href="x" onclick=\'y\'>&amp; text</a><!-- c -->',
  css: 'a:hover > b { color: #fff; content: "</style>"; } /* c */',
  sh: 'echo "$HOME" | grep -c \'<x>\' # c\n',
  sql: "SELECT a, 'b<c' FROM t WHERE x >= 1 -- c\n",
  yaml: 'key: "<v>"\n# comment\nlist:\n  - 1\n',
  go: 'package main\n\nfunc main() { fmt.Println("<b>", 0x10) } // c\n',
  diff: '--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old <b>\n+new & "q"\n',
  '': 'plain <b>text</b> & more',
  nope: 'unknown language <i>stays</i> text',
};
for (const [lang, code] of Object.entries(samples)) {
  const html = HL.html(code, lang);
  // Nothing is lost or invented: removing the spans gives the source back.
  assert.equal(unescape(html), code, `round trip (${lang})`);
  // The only markup is <span class="t-…">, so the result is safe as innerHTML.
  for (const tag of html.match(/<[^>]*>/g) || []) {
    assert.ok(/^<span class="t-[a-z]+">$|^<\/span>$/.test(tag), `unexpected markup ${tag} (${lang})`);
  }
  const rows = HL.lines(code, lang);
  assert.equal(rows.length, code.split('\n').length, `line count (${lang})`);
  assert.equal(rows.map(unescape).join('\n'), code, `lines round trip (${lang})`);
  for (const row of rows) {
    // A token that spans lines is closed and reopened: every row is balanced.
    assert.equal((row.match(/<span /g) || []).length, (row.match(/<\/span>/g) || []).length, `balanced row (${lang})`);
  }
}
assert.ok(HL.html('def f(): return 1', 'py').includes('<span class="t-k">def</span>'));
assert.ok(HL.html('x = "s"  # c', 'python').includes('<span class="t-c"># c</span>'));
assert.equal(HL.languageFor('a/b/Dockerfile'), 'docker');
assert.equal(HL.languageFor('src/App.TSX'), 'js');
assert.equal(HL.languageFor('README.md'), 'md');
assert.equal(HL.languageFor('LICENSE'), '');
assert.equal(HL.languageFor(''), '');
within(3000, 'highlight 400 KB', () => HL.lines('x = "a" + 1  # c\n'.repeat(25000), 'py'));
within(3000, 'highlight one long line', () => HL.html('"'.repeat(100000), 'js'));
within(3000, 'highlight unterminated comment', () => HL.html('/*' + ' *'.repeat(50000), 'js'));

console.log('studio render tests passed');
