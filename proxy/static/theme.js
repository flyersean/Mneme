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
  /* Collapse intro/help blocks by default (desktop AND mobile) so the header
   * stays compact; the ? button expands them. */
  document.querySelectorAll(".intro").forEach(function (el) {
    el.classList.add("collapsed");
  });

  /* Canonical nav — every page includes <nav class="mneme-nav"></nav> and this
   * renders the full link set, marks the current page active, and hides the
   * admin-only "Users" link for non-admin accounts. One source of truth. */
  var NAV_LINKS = [
    ["/", "Dashboard"], ["/chat", "Chat"], ["/memory", "Memory"],
    ["/instructions", "Instructions"], ["/templates", "Templates"],
    ["/ollama", "Ollama"], ["/runs/ui", "Runs"], ["/strategies/ui", "Strategies"],
    ["/extensions", "Extensions"], ["/swarmonitor", "Swarm Monitor"],
    ["/tokens", "Tokens"], ["/users", "Users"]
  ];
  var nav = document.querySelector(".mneme-nav");
  if (nav) {
    var path = location.pathname;
    nav.innerHTML = NAV_LINKS.map(function (l) {
      var active = (path === l[0] || (l[0] !== "/" && path.indexOf(l[0]) === 0)) ? " active" : "";
      return '<a href="' + l[0] + '"' + (active ? ' class="active"' : "") + '>' + l[1] + '</a>';
    }).join("");
    fetch("/auth/me").then(function (r) { return r.json(); }).then(function (d) {
      if (d.username && !d.admin) {
        var a = nav.querySelector('a[href="/users"]');
        if (a) a.style.display = "none";
      }
    }).catch(function () {});
  }

  /* Sticky top chrome — wrap header + nav in .topbar so both stay pinned. */
  var header = document.querySelector("body > header");
  if (header && nav && header.parentNode === nav.parentNode) {
    var bar = document.createElement("div");
    bar.className = "topbar";
    header.parentNode.insertBefore(bar, header);
    bar.appendChild(header);
    bar.appendChild(nav);
  }
});
