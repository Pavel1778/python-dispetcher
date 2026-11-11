/* NEXUS Studio — a compact syntax highlighter.
 *
 * It is a tokenizer, not a parser: comments, strings, numbers, keywords and
 * call sites are enough to make code readable, and that much can be done with
 * one regular expression per language family. Output is built only from
 * escape()d source text wrapped in <span class="t-…">, so highlighted code is as
 * safe to insert as plain escaped text.
 *
 * No DOM access; the same file runs under Node for its tests.
 */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NexusHighlight = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escape(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }
  function set(words) {
    var out = Object.create(null);
    words.split(/\s+/).forEach(function (w) { if (w) out[w] = true; });
    return out;
  }

  // Past this size a file is shown as plain text: highlighting must never be
  // the reason a click feels slow.
  var MAX_CHARS = 300000;

  var NUMBER = '\\b(?:0[xXbBoO][\\da-fA-F_]+|\\d[\\d_]*(?:\\.[\\d_]+)?(?:[eE][+-]?\\d+)?)\\b';
  var DQ = '"(?:\\\\[\\s\\S]|[^"\\\\\\n])*"?';
  var SQ = "'(?:\\\\[\\s\\S]|[^'\\\\\\n])*'?";
  var WORD = '[A-Za-z_$][\\w$]*';

  /* Each language: a global regex whose capture groups are, in order, the
     token types named in `groups`; plus optional keyword and literal sets
     applied to the group called "w" (a bare word). */
  function family(pattern, groups, options) {
    var lang = options || {};
    lang.re = new RegExp(pattern, 'g' + (lang.flags || ''));
    lang.groups = groups;
    return lang;
  }
  function cLike(keywords, extra) {
    var o = extra || {};
    o.kw = set(keywords);
    o.lit = set('true false null undefined nil None NULL nullptr this self super');
    return family(
      '(\\/\\/[^\\n]*|\\/\\*[\\s\\S]*?(?:\\*\\/|$))|(' + DQ + '|' + SQ +
      '|`(?:\\\\[\\s\\S]|[^`\\\\])*`?)|(' + NUMBER + ')|(' + WORD + ')',
      ['c', 's', 'n', 'w'], o);
  }

  var JS = 'async await break case catch class const continue debugger default delete do else ' +
    'export extends finally for from function get if import in instanceof let new of return set ' +
    'static switch throw try typeof var void while with yield as enum implements interface ' +
    'namespace private protected public readonly type declare abstract keyof satisfies';

  var LANGS = {
    js: cLike(JS),
    go: cLike('break case chan const continue default defer else fallthrough for func go goto if ' +
      'import interface map package range return select struct switch type var'),
    rust: cLike('as async await break const continue crate dyn else enum extern fn for if impl in ' +
      'let loop match mod move mut pub ref return static struct trait type unsafe use where while'),
    java: cLike('abstract assert boolean break byte case catch char class const continue default ' +
      'do double else enum extends final finally float for if implements import instanceof int ' +
      'interface long native new package private protected public return short static switch ' +
      'synchronized throw throws transient try var void volatile while record sealed permits ' +
      'fun val when object companion data override suspend internal open lateinit'),
    c: cLike('auto break case char class const constexpr continue default delete do double else ' +
      'enum explicit extern float for friend goto if inline int long namespace new operator ' +
      'private protected public register return short signed sizeof static struct switch ' +
      'template typedef typename union unsigned using virtual void volatile while bool ' +
      'include define ifdef ifndef endif pragma'),
    cs: cLike('abstract as async await base bool break byte case catch char class const continue ' +
      'decimal default delegate do double else enum event explicit extern finally fixed float ' +
      'for foreach get if implicit in int interface internal is lock long namespace new object ' +
      'operator out override params private protected public readonly record ref return sealed ' +
      'set short static string struct switch throw try typeof uint using var virtual void while'),
    php: cLike('abstract and array as break callable case catch class clone const continue declare ' +
      'default do echo else elseif empty enum extends final finally fn for foreach function ' +
      'global if implements include instanceof interface isset list match namespace new or ' +
      'print private protected public readonly require return static switch throw trait try ' +
      'unset use var while yield'),
    swift: cLike('as associatedtype break case catch class continue default defer deinit do else ' +
      'enum extension fallthrough fileprivate for func guard if import in init inout internal ' +
      'is let mutating open operator private protocol public repeat return static struct ' +
      'subscript switch throw throws try typealias var where while actor async await'),
    py: family(
      '(#[^\\n]*)|((?:[rRbBfFuU]{0,2})(?:"""[\\s\\S]*?(?:"""|$)|\'\'\'[\\s\\S]*?(?:\'\'\'|$)|' +
      DQ + '|' + SQ + '))|(' + NUMBER + ')|(@[\\w.]+)|(' + WORD + ')',
      ['c', 's', 'n', 'f', 'w'],
      { kw: set('and as assert async await break case class continue def del elif else except ' +
          'finally for from global if import in is lambda match nonlocal not or pass raise ' +
          'return try while with yield'),
        lit: set('True False None self cls') }),
    rb: family(
      '(#[^\\n]*)|(' + DQ + '|' + SQ + ')|(' + NUMBER + ')|(:[A-Za-z_]\\w*|@{1,2}\\w+)|(' + WORD + ')',
      ['c', 's', 'n', 'v', 'w'],
      { spaceBeforeComment: true,
        kw: set('alias and begin break case class def defined do else elsif end ensure for if in ' +
          'module next not or redo rescue retry return then unless until when while yield ' +
          'require require_relative include extend attr_reader attr_writer attr_accessor'),
        lit: set('true false nil self') }),
    sh: family(
      '(#[^\\n]*)|("(?:\\\\[\\s\\S]|[^"\\\\])*"?|\'[^\']*\'?)|(\\$\\{[^}\\n]*\\}|\\$[\\w@#?*!$-]+)|' +
      '(' + NUMBER + ')|([A-Za-z_][\\w-]*)',
      ['c', 's', 'v', 'n', 'w'],
      { spaceBeforeComment: true, noCalls: true,
        kw: set('if then else elif fi for while until do done case esac in function select ' +
          'return exit export local readonly set unset source alias echo cd sudo') }),
    json: family(
      '("(?:\\\\[\\s\\S]|[^"\\\\\\n])*"(?=\\s*:))|("(?:\\\\[\\s\\S]|[^"\\\\\\n])*"?)|' +
      '(-?\\d+(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)|\\b(true|false|null)\\b',
      ['a', 's', 'n', 'k']),
    yaml: family(
      '(#[^\\n]*)|(' + DQ + '|' + SQ + ')|(^[ \\t]*(?:- +)?[\\w.\\-\\/]+(?=:(?:[ \\t]|$)))|' +
      '\\b(true|false|null|yes|no|on|off)\\b|(' + NUMBER + ')',
      ['c', 's', 'a', 'k', 'n'], { flags: 'm', spaceBeforeComment: true }),
    toml: family(
      '([#;][^\\n]*)|("""[\\s\\S]*?(?:"""|$)|' + DQ + '|' + SQ + ')|(^[ \\t]*\\[[^\\]\\n]*\\]+)|' +
      '(^[ \\t]*[\\w.\\-]+(?=[ \\t]*=))|\\b(true|false)\\b|(' + NUMBER + ')',
      ['c', 's', 't', 'a', 'k', 'n'], { flags: 'm', spaceBeforeComment: true }),
    css: family(
      '(\\/\\*[\\s\\S]*?(?:\\*\\/|$))|(' + DQ + '|' + SQ + ')|(@[\\w-]+)|(--[\\w-]+)|' +
      '(#[\\da-fA-F]{3,8}\\b|-?\\d*\\.?\\d+(?:%|[a-zA-Z]+)?)|([\\w-]+(?=\\s*:))',
      ['c', 's', 'k', 'v', 'n', 'a']),
    sql: family(
      '(--[^\\n]*|\\/\\*[\\s\\S]*?(?:\\*\\/|$))|(\'(?:\'\'|[^\'])*\'?)|(' + NUMBER + ')|(' + WORD + ')',
      ['c', 's', 'n', 'w'],
      { caseless: true,
        kw: set('add all alter and as asc begin between by cascade case check column commit ' +
          'constraint create cross default delete desc distinct do drop else end exists foreign ' +
          'from full function group having if in index inner insert into is join key left like ' +
          'limit not null offset on or order outer primary references returning returns right ' +
          'rollback select set table then to transaction trigger union unique update using ' +
          'values view when where with grant revoke enable security policy level row') }),
    md: family(
      '(^#{1,6} [^\\n]*)|(^ {0,3}(?:```|~~~)[^\\n]*|`[^`\\n]+`)|(^[ \\t]*(?:[-*+]|\\d+\\.)(?= ))|' +
      '(\\[[^\\]\\n]+\\]\\([^)\\n]+\\))|(\\*\\*[^*\\n]+\\*\\*)',
      ['k', 's', 'n', 'f', 'a'], { flags: 'm' }),
    diff: family(
      '(^(?:diff |index |--- |\\+\\+\\+ )[^\\n]*)|(^@@[^\\n]*)|(^\\+[^\\n]*)|(^-[^\\n]*)',
      ['c', 'k', 'add', 'del'], { flags: 'm' }),
    docker: family(
      '(#[^\\n]*)|(' + DQ + '|' + SQ + ')|(^[ \\t]*(?:FROM|RUN|CMD|LABEL|EXPOSE|ENV|ADD|COPY|' +
      'ENTRYPOINT|VOLUME|USER|WORKDIR|ARG|ONBUILD|STOPSIGNAL|HEALTHCHECK|SHELL|AS)\\b)|' +
      '(\\$\\{[^}\\n]*\\}|\\$\\w+)',
      ['c', 's', 'k', 'v'], { flags: 'm', spaceBeforeComment: true })
  };

  var ALIAS = {
    javascript: 'js', jsx: 'js', mjs: 'js', cjs: 'js', ts: 'js', tsx: 'js', typescript: 'js',
    golang: 'go', rs: 'rust', kt: 'java', kotlin: 'java', scala: 'java', groovy: 'java',
    h: 'c', cpp: 'c', cc: 'c', cxx: 'c', hpp: 'c', 'c++': 'c', objc: 'c', m: 'c',
    csharp: 'cs', python: 'py', python3: 'py', pyi: 'py', ruby: 'rb',
    bash: 'sh', shell: 'sh', zsh: 'sh', console: 'sh', sh: 'sh', env: 'sh', makefile: 'sh',
    jsonc: 'json', json5: 'json', yml: 'yaml', ini: 'toml', cfg: 'toml', conf: 'toml',
    scss: 'css', less: 'css', sass: 'css', postgres: 'sql', postgresql: 'sql', psql: 'sql',
    mysql: 'sql', markdown: 'md', patch: 'diff', dockerfile: 'docker',
    html: 'html', htm: 'html', xml: 'html', svg: 'html', vue: 'html', xhtml: 'html'
  };
  function resolve(lang) {
    var key = String(lang || '').toLowerCase();
    if (LANGS[key] || key === 'html') return key;
    return ALIAS[key] || '';
  }

  /* ------------------------------------------------------------ tokenizers */
  function scan(code, lang, out) {
    var re = lang.re;
    re.lastIndex = 0;
    var last = 0, m;
    while ((m = re.exec(code)) !== null) {
      if (m[0] === '') { re.lastIndex++; continue; }
      var type = '';
      for (var g = 0; g < lang.groups.length; g++) {
        if (m[g + 1] !== undefined) { type = lang.groups[g]; break; }
      }
      if (type === 'c' && lang.spaceBeforeComment && m.index > 0 &&
          !/\s/.test(code.charAt(m.index - 1)) && /^[#;]/.test(m[0])) {
        // "#" glued to a word (a URL fragment, $#) is not a comment.
        re.lastIndex = m.index + 1;
        continue;
      }
      if (type === 'w') {
        var word = lang.caseless ? m[0].toLowerCase() : m[0];
        if (lang.kw && lang.kw[word]) type = 'k';
        else if (lang.lit && lang.lit[word]) type = 'n';
        else if (!lang.noCalls && code.charAt(re.lastIndex) === '(') type = 'f';
        else type = '';
      }
      if (type) {
        if (m.index > last) out.push(['', code.slice(last, m.index)]);
        out.push([type, m[0]]);
        last = re.lastIndex;
      }
    }
    if (last < code.length) out.push(['', code.slice(last)]);
    return out;
  }

  // Markup needs to know whether it is inside a tag, so it is a small state
  // machine instead of one regex. <script> and <style> bodies are handed to
  // the JavaScript and CSS tokenizers.
  function scanMarkup(code, out) {
    var i = 0, n = code.length;
    function push(type, text) { if (text) out.push([type, text]); }
    while (i < n) {
      var lt = code.indexOf('<', i);
      if (lt === -1) { push('', code.slice(i)); break; }
      push('', code.slice(i, lt));
      if (code.startsWith('<!--', lt)) {
        var endComment = code.indexOf('-->', lt + 4);
        endComment = endComment === -1 ? n : endComment + 3;
        push('c', code.slice(lt, endComment));
        i = endComment;
        continue;
      }
      var open = /^<[\/!?]?[A-Za-z][\w:.-]*/.exec(code.slice(lt, lt + 80));
      if (!open) { push('', '<'); i = lt + 1; continue; }
      push('t', open[0]);
      i = lt + open[0].length;
      var name = open[0].replace(/^<[\/!?]?/, '').toLowerCase();
      var closing = open[0].charAt(1) === '/';
      // attributes, until the tag closes
      while (i < n) {
        var ch = code.charAt(i);
        if (ch === '>') { push('t', '>'); i++; break; }
        if (ch === '/' && code.charAt(i + 1) === '>') { push('t', '/>'); i += 2; break; }
        if (ch === '"' || ch === "'") {
          var endQuote = code.indexOf(ch, i + 1);
          endQuote = endQuote === -1 ? n : endQuote + 1;
          push('s', code.slice(i, endQuote));
          i = endQuote;
          continue;
        }
        var attr = /^[^\s=>\/"']+/.exec(code.slice(i, i + 120));
        if (attr) { push('a', attr[0]); i += attr[0].length; continue; }
        push('', ch);
        i++;
      }
      if (!closing && (name === 'script' || name === 'style')) {
        var endTag = code.toLowerCase().indexOf('</' + name, i);
        if (endTag === -1) endTag = n;
        scan(code.slice(i, endTag), name === 'script' ? LANGS.js : LANGS.css, out);
        i = endTag;
      }
    }
    return out;
  }

  /** The source as [type, text] pairs; type '' is plain text. */
  function tokens(code, lang) {
    code = String(code == null ? '' : code);
    var key = resolve(lang);
    if (!key || code.length > MAX_CHARS) return [['', code]];
    return key === 'html' ? scanMarkup(code, []) : scan(code, LANGS[key], []);
  }

  function span(type, text) {
    return type ? '<span class="t-' + type + '">' + escape(text) + '</span>' : escape(text);
  }

  /** Highlighted HTML for a whole block. */
  function html(code, lang) {
    var list = tokens(code, lang), out = '';
    for (var i = 0; i < list.length; i++) out += span(list[i][0], list[i][1]);
    return out;
  }

  /** Highlighted HTML per source line; a token that spans lines is split. */
  function lines(code, lang) {
    var list = tokens(code, lang), out = [''];
    for (var i = 0; i < list.length; i++) {
      var parts = list[i][1].split('\n');
      for (var p = 0; p < parts.length; p++) {
        if (p > 0) out.push('');
        if (parts[p]) out[out.length - 1] += span(list[i][0], parts[p]);
      }
    }
    return out;
  }

  var BY_NAME = { dockerfile: 'docker', makefile: 'sh', gemfile: 'rb', rakefile: 'rb',
                  '.gitignore': 'sh', '.dockerignore': 'sh', '.env': 'sh', '.bashrc': 'sh' };
  var BY_EXT = {
    js: 'js', mjs: 'js', cjs: 'js', jsx: 'js', ts: 'js', tsx: 'js', go: 'go', rs: 'rust',
    java: 'java', kt: 'java', kts: 'java', scala: 'java', groovy: 'java', gradle: 'java',
    c: 'c', h: 'c', cpp: 'c', cc: 'c', cxx: 'c', hpp: 'c', m: 'c', mm: 'c', cs: 'cs',
    php: 'php', swift: 'swift', py: 'py', pyi: 'py', rb: 'rb', sh: 'sh', bash: 'sh', zsh: 'sh',
    json: 'json', webmanifest: 'json', map: 'json', yml: 'yaml', yaml: 'yaml', toml: 'toml',
    ini: 'toml', cfg: 'toml', conf: 'toml', lock: 'toml', css: 'css', scss: 'css', less: 'css',
    sql: 'sql', md: 'md', markdown: 'md', diff: 'diff', patch: 'diff', html: 'html', htm: 'html',
    xml: 'html', svg: 'html', vue: 'html'
  };
  var LABELS = { js: 'JavaScript / TypeScript', go: 'Go', rust: 'Rust', java: 'Java / Kotlin',
                 c: 'C / C++', cs: 'C#', php: 'PHP', swift: 'Swift', py: 'Python', rb: 'Ruby',
                 sh: 'Shell', json: 'JSON', yaml: 'YAML', toml: 'TOML / INI', css: 'CSS',
                 sql: 'SQL', md: 'Markdown', diff: 'Diff', docker: 'Dockerfile', html: 'HTML / XML' };

  /** The language id for a file path, or '' when it is not recognised. */
  function languageFor(path) {
    var name = String(path || '').split('/').pop().toLowerCase();
    if (BY_NAME[name]) return BY_NAME[name];
    if (name.indexOf('dockerfile') === 0) return 'docker';
    var dot = name.lastIndexOf('.');
    return dot > 0 ? (BY_EXT[name.slice(dot + 1)] || '') : '';
  }
  function label(lang) { return LABELS[resolve(lang)] || ''; }

  return { tokens: tokens, html: html, lines: lines, languageFor: languageFor,
           label: label, escape: escape };
});
