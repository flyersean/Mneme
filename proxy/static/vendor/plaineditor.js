/* PlainEditor — a dependency-free line-numbered text editor.
 *
 * Replaces CodeMirror for the config editor (dashboard) and the file
 * viewer/editor (swarm monitor). It is a native <textarea> plus a
 * synchronized line-number gutter. Native textareas have no font-metric
 * measurement subsystem, so the class of bugs where the caret "jumps
 * around" or lands at the wrong column (caused by measuring before layout,
 * or by a hidden measure div with stale metrics) simply cannot occur here:
 * the caret is the browser's own, and it always sits exactly where the
 * user clicked or typed.
 *
 * API (mirrors the subset of CodeMirror the app actually used):
 *   var ed = PlainEditor.create(textarea, opts);   // opts: { tabSize }
 *   ed.getValue()  ed.setValue(s)  ed.refresh()  ed.focus()
 *   ed.setSize(w, h)  ed.on('change', fn)  ed.destroy()
 *   ed.setErrorLine(n)  // n = 1-based line number to highlight, or 0 to clear
 *
 * destroy() restores the original <textarea> in place (id/class intact) so
 * the host page can re-create the editor cleanly.
 */
(function () {
  'use strict';

  var STYLE_ID = 'plaineditor-style';

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var css = [
      '.plain-editor{display:flex;flex:1;min-height:0;min-width:0;position:relative;',
      '  border:1px solid var(--line,#30363d);border-radius:7px;overflow:hidden;',
      '  background:var(--code-bg,#0d1117);color:var(--fg,#e6e6e6);',
      '  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;',
      '  font-size:13px;line-height:1.5;}',
      '.pe-gutter{flex:0 0 auto;padding:8px 10px 8px 12px;text-align:right;',
      '  color:var(--muted,#6e7681);background:rgba(0,0,0,0.16);',
      '  overflow:hidden;user-select:none;',
      '  border-right:1px solid rgba(128,128,128,0.15);}',
      '.pe-ln{line-height:1.5;}',
      '.pe-ln-err{color:#fff;background:rgba(255,70,70,0.40);border-radius:3px;}',
      '.pe-text{flex:1;min-width:0;border:none;outline:none;resize:none;',
      '  padding:8px 12px;background:transparent;color:inherit;',
      '  font-family:inherit;font-size:13px;line-height:1.5;',
      '  white-space:pre;overflow:auto;tab-size:2;word-wrap:normal;',
      '  overflow-wrap:normal;}',
      '.pe-errbar{position:absolute;left:0;right:0;display:none;',
      '  pointer-events:none;z-index:2;background:rgba(255,80,80,0.16);}'
    ].join('');
    var st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent = css;
    document.head.appendChild(st);
  }

  function create(ta, opts) {
    opts = opts || {};
    ensureStyle();
    var tabSize = opts.tabSize != null ? opts.tabSize : 2;

    var wrapper = document.createElement('div');
    wrapper.className = 'plain-editor';
    if (ta.className) wrapper.className += ' ' + ta.className;

    var gutter = document.createElement('div');
    gutter.className = 'pe-gutter';

    var text = document.createElement('textarea');
    text.className = 'pe-text';
    text.spellcheck = false;
    text.wrap = 'off';
    text.value = ta.value || '';

    var errbar = document.createElement('div');
    errbar.className = 'pe-errbar';

    wrapper.appendChild(gutter);
    wrapper.appendChild(text);
    wrapper.appendChild(errbar);

    ta.parentNode.insertBefore(wrapper, ta);
    ta.parentNode.removeChild(ta);

    var listeners = { change: [] };
    var errorLine = 0; // 1-based line to highlight, 0 = none

    function lineCount() {
      var v = text.value;
      var n = 1;
      for (var i = 0; i < v.length; i++) if (v.charCodeAt(i) === 10) n++;
      return n;
    }

    function renderGutter() {
      var n = lineCount();
      var digits = String(n).length;
      gutter.style.minWidth = (digits + 2) + 'ch';
      var frag = document.createDocumentFragment();
      var d;
      for (var i = 1; i <= n; i++) {
        d = document.createElement('div');
        d.className = (i === errorLine) ? 'pe-ln pe-ln-err' : 'pe-ln';
        d.textContent = i;
        frag.appendChild(d);
      }
      gutter.textContent = '';
      gutter.appendChild(frag);
    }

    function syncScroll() {
      gutter.scrollTop = text.scrollTop;
      refreshErrorBar();
    }

    function refreshErrorBar() {
      if (errorLine < 1) { errbar.style.display = 'none'; return; }
      var gl = gutter.children[errorLine - 1];
      if (!gl) { errbar.style.display = 'none'; return; }
      var wr = wrapper.getBoundingClientRect();
      var lr = gl.getBoundingClientRect();
      errbar.style.top = (lr.top - wr.top) + 'px';
      errbar.style.height = lr.height + 'px';
      errbar.style.display = 'block';
    }

    function setErrorLine(n) {
      errorLine = n > 0 ? n : 0;
      renderGutter();
      refreshErrorBar();
    }

    function emit() {
      renderGutter();
      for (var i = 0; i < listeners.change.length; i++) listeners.change[i]();
    }

    text.addEventListener('input', emit);
    text.addEventListener('scroll', syncScroll);

    // Tab indents, Shift+Tab outdents, Enter preserves/extends indentation.
    text.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') {
        e.preventDefault();
        var s = text.selectionStart, t = text.selectionEnd;
        var v = text.value;
        if (e.shiftKey) {
          // Unindent: strip up to tabSize leading spaces from each selected line.
          var ls = v.lastIndexOf('\n', s - 1);
          var le = v.indexOf('\n', t);
          if (le === -1) le = v.length;
          var seg = v.slice(ls + 1, le);
          var lines = seg.split('\n');
          var removed = 0;
          for (var i = 0; i < lines.length; i++) {
            var m = lines[i].match(/^ {1,8}/);
            var cut = m ? Math.min(m[0].length, tabSize) : 0;
            lines[i] = lines[i].slice(cut);
            removed += cut;
          }
          var nv = v.slice(0, ls + 1) + lines.join('\n') + v.slice(le);
          text.value = nv;
          var newS = Math.max(ls + 1, s - Math.min(removed, s - (ls + 1)));
          text.setSelectionRange(newS, newS);
        } else {
          // Indent each selected line by tabSize spaces.
          var pad = new Array(tabSize + 1).join(' ');
          var ls2 = v.lastIndexOf('\n', s - 1);
          var le2 = v.indexOf('\n', t);
          if (le2 === -1) le2 = v.length;
          var seg2 = v.slice(ls2 + 1, le2);
          var lines2 = seg2.split('\n');
          for (var j = 0; j < lines2.length; j++) lines2[j] = pad + lines2[j];
          text.value = v.slice(0, ls2 + 1) + lines2.join('\n') + v.slice(le2);
          text.setSelectionRange(s + tabSize, t + tabSize * lines2.length);
        }
        emit();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        var v2 = text.value;
        var pos = text.selectionStart;
        var lineStart = v2.lastIndexOf('\n', pos - 1) + 1;
        var before = v2.slice(lineStart, pos);
        var indent = (before.match(/^[ \t]*/) || [''])[0];
        var trimmed = before.trim();
        if (/(:\s*|\-\s*)$/.test(trimmed)) indent += new Array(tabSize + 1).join(' ');
        var ins = '\n' + indent;
        text.value = v2.slice(0, pos) + ins + v2.slice(pos);
        var np = pos + ins.length;
        text.setSelectionRange(np, np);
        emit();
      }
    });

    renderGutter();
    syncScroll();

    var editor = {
      getValue: function () { return text.value; },
      setValue: function (s) { text.value = String(s == null ? '' : s); emit(); },
      refresh: function () { renderGutter(); syncScroll(); },
      focus: function () { text.focus(); },
      setSize: function (w, h) {
        if (w) wrapper.style.width = w;
        if (h) wrapper.style.height = h;
      },
      on: function (evt, fn) {
        if (evt === 'change' && typeof fn === 'function') listeners.change.push(fn);
      },
      setErrorLine: setErrorLine,
      getTextarea: function () { return text; },
      destroy: function () {
        // Restore the original textarea in place, preserving its content.
        ta.value = text.value;
        wrapper.parentNode.insertBefore(ta, wrapper);
        wrapper.parentNode.removeChild(wrapper);
      }
    };

    return editor;
  }

  window.PlainEditor = { create: create };
})();