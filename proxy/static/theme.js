/* Mneme shared theme loader + switcher.
 *
 * Themes are CSS files in /static/themes/ (light.css, dark.css, and any custom
 * <name>.css you drop there). GET /themes lists them, so a new theme shows up in
 * the switcher with no other change. The chosen theme is saved to localStorage
 * under "mneme.theme" and applied before first paint (this script runs in <head>).
 */
(function () {
  var KEY = "mneme.theme";
  var DEFAULT = "light";
  function saved() {
    try { return localStorage.getItem(KEY) || DEFAULT; } catch (e) { return DEFAULT; }
  }
  var link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/static/themes/" + encodeURIComponent(saved()) + ".css";
  document.head.appendChild(link);

  window.mnemeTheme = {
    current: saved,
    set: function (name) {
      try { localStorage.setItem(KEY, name); } catch (e) {}
      link.href = "/static/themes/" + encodeURIComponent(name) + ".css";
      if (selEl) selEl.value = name;
    }
  };
  var selEl = null;

  document.addEventListener("DOMContentLoaded", function () {
    fetch("/themes").then(function (r) { return r.json(); }).then(function (d) {
      var themes = d.themes || [];
      if (themes.indexOf(DEFAULT) < 0) themes.unshift(DEFAULT);
      var sel = document.createElement("select");
      sel.className = "theme-switcher";
      sel.title = "Theme";
      themes.forEach(function (t) {
        var o = document.createElement("option");
        o.value = t;
        o.textContent = t;
        if (t === saved()) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () { window.mnemeTheme.set(sel.value); });
      selEl = sel;
      var host = document.querySelector(".mneme-nav") ||
                 document.querySelector("header .toolbar") ||
                 document.querySelector("header") ||
                 document.body;
      host.appendChild(sel);
    }).catch(function () {});
  });
})();

/* Collapsible intro/help blocks (see theme.css .intro / .intro-btn).
 * Pages add <div class="intro" id="..."> around their header description and a
 * button that calls toggleIntro(id). Expanded on desktop; collapsed by default
 * on mobile so the header chrome stays compact. */
window.toggleIntro = function (id) {
  var el = document.getElementById(id);
  if (el) el.classList.toggle("collapsed");
};
document.addEventListener("DOMContentLoaded", function () {
  if (window.innerWidth <= 720) {
    document.querySelectorAll(".intro").forEach(function (el) {
      el.classList.add("collapsed");
    });
  }
});
