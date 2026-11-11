/* NEXUS Studio — a small, safe Markdown renderer.
 *
 * The input is untrusted: it is model output, and a model repeats whatever a
 * repository or a web page told it. So the rule is structural, not a filter:
 * every character of the source reaches the page through escape(), and the only
 * markup in the result is the fixed set of tags this file writes itself. There
 * is no raw-HTML passthrough to sanitise, because there is no raw-HTML path.
 *
 * Supported: fenced code, headings, lists (nested, ordered, task items),
 * blockquotes, tables, rules, paragraphs; inline code, bold, italic, strike,
 * links and bare URLs (http, https and mailto only).
 *
 * No DOM access, so the same file runs under Node for its tests.
 */
(function (root, factory) {
  'use strict';
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.NexusMarkdown = api;
})(typeof self !== 'undefined' ? self : this, function (root) {
  'use strict';

  var ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  function escape(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) { return ESC[c]; });
  }

  // Nested quotes and lists recurse; a hostile input must not be able to turn
  // that into a stack overflow.
  var MAX_DEPTH = 8;

  var FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]*)[^`]*$/;
  var HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
  var QUOTE = /^ {0,3}>\s?/;
  var LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
  var TABLE_SEP = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

  function isBlank(line) { return /^\s*$/.test(line); }
  function indentOf(line) { return /^\s*/.exec(line)[0].replace(/\t/g, '    ').length; }
  function isTableStart(lines, i) {
    return lines[i].indexOf('|') !== -1 && i + 1 < lines.length &&
      TABLE_SEP.test(lines[i + 1]) && lines[i + 1].indexOf('-') !== -1 &&
      lines[i + 1].indexOf('|') !== -1;
  }
  function startsBlock(lines, i) {
    var line = lines[i];
    return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) ||
      LIST.test(line) || isTableStart(lines, i);
  }

  /* --------------------------------------------------------------- inline */
  function safeUrl(escapedUrl) {
    // The URL has already been through escape(); only the scheme is judged.
    return /^(https?:\/\/|mailto:)[^\s]+$/i.test(escapedUrl) ? escapedUrl : null;
  }

  function inline(raw) {
    var stash = [];
    function keep(html) { stash.push(html); return '\u0000' + (stash.length - 1) + '\u0000'; }

    // Code spans first: nothing inside them is markup.
    var text = raw.replace(/(`+)(?!`)([\s\S]*?[^`])\1(?!`)/g, function (_m, _ticks, code) {
      return keep('<code>' + escape(code.replace(/^ (.*) $/, '$1')) + '</code>');
    });
    text = escape(text);

    // [label](url) — the tags are stashed so that emphasis cannot rewrite the
    // href, while the label stays in the text and may carry emphasis.
    // An image, ![alt](url), becomes a link to it: the page loads nothing from
    // another origin (see the Content-Security-Policy), so there is no <img>.
    text = text.replace(/(!?)\[([^\]\n]*)\]\(([^)\s]+)\)/g, function (match, bang, label, url) {
      var href = safeUrl(url);
      if (!href) return match;
      return keep('<a href="' + href + '" target="_blank" rel="noopener noreferrer">') +
        (label || (bang ? 'изображение' : href)) + keep('</a>');
    });
    // Bare URLs.
    text = text.replace(/(^|[\s(])(https?:\/\/[^\s<>()]*[^\s<>().,;:!?&])/g, function (_m, pre, url) {
      return pre + keep('<a href="' + url + '" target="_blank" rel="noopener noreferrer">' +
        url + '</a>');
    });

    text = text
      .replace(/\*\*([^\s*](?:[^*]*[^\s*])?)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^\s_](?:[^_]*[^\s_])?)__/g, '<strong>$1</strong>')
      .replace(/(^|[^\w*])\*([^\s*](?:[^*]*[^\s*])?)\*(?![\w*])/g, '$1<em>$2</em>')
      .replace(/(^|[^\w_])_([^\s_](?:[^_]*[^\s_])?)_(?![\w_])/g, '$1<em>$2</em>')
      .replace(/~~([^~\s](?:[^~]*[^~\s])?)~~/g, '<del>$1</del>');

    return text.replace(/\u0000(\d+)\u0000/g, function (_m, n) { return stash[Number(n)]; });
  }

  /* --------------------------------------------------------------- blocks */
  function codeBlock(code, lang, options) {
    var highlight = options.highlight ||
      (root && root.NexusHighlight && root.NexusHighlight.html);
    var body;
    try {
      body = highlight ? highlight(code, lang) : escape(code);
    } catch (e) {
      body = escape(code);
    }
    return '<div class="code"><div class="code-head"><span class="code-lang">' +
      escape(lang || 'текст') + '</span>' +
      '<button class="code-copy" type="button" data-action="copy-code">Копировать</button></div>' +
      '<pre><code>' + body + '</code></pre></div>';
  }

  function table(lines, i) {
    // Split on unescaped pipes by hand: a lookbehind regex would be shorter,
    // but one browser generation still fails to parse it, and a parse error
    // takes the whole script down.
    function cells(line) {
      var inner = line.trim().replace(/^\|/, '').replace(/\|$/, '');
      var out = [], current = '';
      for (var k = 0; k < inner.length; k++) {
        var ch = inner.charAt(k);
        if (ch === '\\' && inner.charAt(k + 1) === '|') { current += '|'; k++; }
        else if (ch === '|') { out.push(current.trim()); current = ''; }
        else current += ch;
      }
      out.push(current.trim());
      return out;
    }
    var head = cells(lines[i]);
    var align = cells(lines[i + 1]).map(function (c) {
      var left = c.charAt(0) === ':', right = c.charAt(c.length - 1) === ':';
      return left && right ? ' class="al-c"' : right ? ' class="al-r"' : '';
    });
    var html = '<div class="md-table"><table><thead><tr>';
    head.forEach(function (c, n) { html += '<th' + (align[n] || '') + '>' + inline(c) + '</th>'; });
    html += '</tr></thead><tbody>';
    i += 2;
    while (i < lines.length && !isBlank(lines[i]) && lines[i].indexOf('|') !== -1) {
      var row = cells(lines[i]);
      html += '<tr>';
      for (var n = 0; n < head.length; n++) {
        html += '<td' + (align[n] || '') + '>' + inline(row[n] || '') + '</td>';
      }
      html += '</tr>';
      i++;
    }
    return [html + '</tbody></table></div>', i];
  }

  function list(lines, i, depth) {
    var first = LIST.exec(lines[i]);
    var base = indentOf(lines[i]);
    var ordered = /\d/.test(first[2]);
    var items = [];
    while (i < lines.length) {
      var m = LIST.exec(lines[i]);
      if (!m) break;
      var indent = indentOf(lines[i]);
      if (indent < base) break;
      if (indent >= base + 2 && items.length) {
        if (depth >= MAX_DEPTH) { items[items.length - 1] += '<br>' + inline(lines[i].trim()); i++; continue; }
        var nested = list(lines, i, depth + 1);
        items[items.length - 1] += nested[0];
        i = nested[1];
        continue;
      }
      if (/\d/.test(m[2]) !== ordered) break;
      var text = m[3];
      i++;
      // Continuation lines: indented text that is not a new item.
      while (i < lines.length && !isBlank(lines[i]) && !LIST.test(lines[i]) &&
             indentOf(lines[i]) > base && !FENCE.test(lines[i])) {
        text += '\n' + lines[i].trim();
        i++;
      }
      var task = /^\[( |x|X)\]\s+/.exec(text);
      var prefix = '';
      if (task) {
        prefix = '<span class="md-task' + (task[1] === ' ' ? '' : ' is-done') + '"></span>';
        text = text.slice(task[0].length);
      }
      items.push(prefix + text.split('\n').map(inline).join('<br>'));
      // One blank line between items does not end the list.
      if (i + 1 < lines.length && isBlank(lines[i]) && LIST.test(lines[i + 1]) &&
          indentOf(lines[i + 1]) >= base) i++;
    }
    var tag = ordered ? 'ol' : 'ul';
    var start = ordered ? parseInt(first[2], 10) : 1;
    var open = '<' + tag + (ordered && start !== 1 && start >= 0 ? ' start="' + start + '"' : '') + '>';
    return [open + items.map(function (item) { return '<li>' + item + '</li>'; }).join('') +
            '</' + tag + '>', i];
  }

  function blocks(lines, depth, options) {
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (isBlank(line)) { i++; continue; }

      var fence = FENCE.exec(line);
      if (fence) {
        var marker = fence[1].charAt(0), length = fence[1].length;
        var close = new RegExp('^ {0,3}\\' + marker + '{' + length + ',}\\s*$');
        var code = [];
        i++;
        while (i < lines.length && !close.test(lines[i])) code.push(lines[i++]);
        i++;  // the closing fence, or the end of an unterminated block
        out.push(codeBlock(code.join('\n'), fence[2], options));
        continue;
      }

      var heading = HEADING.exec(line);
      if (heading) {
        var level = Math.min(heading[1].length, 4);
        out.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
        i++;
        continue;
      }

      if (RULE.test(line)) { out.push('<hr>'); i++; continue; }

      if (QUOTE.test(line)) {
        var quoted = [];
        while (i < lines.length && QUOTE.test(lines[i])) quoted.push(lines[i++].replace(QUOTE, ''));
        out.push('<blockquote>' + (depth >= MAX_DEPTH
          ? '<p>' + quoted.map(inline).join('<br>') + '</p>'
          : blocks(quoted, depth + 1, options)) + '</blockquote>');
        continue;
      }

      if (isTableStart(lines, i)) {
        var t = table(lines, i);
        out.push(t[0]);
        i = t[1];
        continue;
      }

      if (LIST.test(line)) {
        var l = list(lines, i, depth);
        out.push(l[0]);
        i = l[1];
        continue;
      }

      var paragraph = [line];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !startsBlock(lines, i)) paragraph.push(lines[i++]);
      out.push('<p>' + paragraph.map(function (p) { return inline(p.trim()); }).join('<br>') + '</p>');
    }
    return out.join('');
  }

  /**
   * Render Markdown to HTML. `options.highlight(code, lang)` may supply
   * already-escaped HTML for a code block; without it the code is escaped.
   */
  function render(source, options) {
    var text = String(source == null ? '' : source)
      .replace(/\u0000/g, '')            // reserved for the inline stash
      .replace(/\r\n?/g, '\n');
    return blocks(text.split('\n'), 0, options || {});
  }

  return { render: render, escape: escape };
});
