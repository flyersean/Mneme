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
  /* Canonical nav — PINNED TABS + OVERFLOW MENU (Gmail/Linear pattern).
   * Header shows up to 10 user-pinned buttons (default 5) plus a Menu button,
   * fixed in the LEFT slot. Menu lists
   * every page, each with a "Pin" checkbox that saves to localStorage
   * (mneme_nav_pins). The current page is always visible in the header.
   * If a page renders its own nav server-side (auth pages), leave it alone. */
  var NAV_LINKS = [
    ["/", "Dashboard"], ["/health/ui", "Health"], ["/chat", "Chat"], ["/memory", "Memory"],
    ["/instructions", "Instructions"], ["/templates", "Templates"],
    ["/ollama", "Ollama"], ["/runs/ui", "Runs"], ["/strategies/ui", "Strategies"],
    ["/skills/ui", "Skills"],
    ["/extensions", "Extensions"], ["/swarmonitor", "Swarm Monitor"],
    ["/secrets", "Secrets"], ["/tokens", "Tokens"], ["/users", "Users"]
  ];
  var ADMIN_ONLY = ["/users", "/secrets", "/tokens", "/providers"];
  var PIN_KEY = "mneme_nav_pins";
  var MAX_PINS = 10;
  var DEFAULT_PINS = ["/", "/chat", "/memory", "/health/ui", "/runs/ui"];
  var nav = document.querySelector(".mneme-nav");
  if (nav && !nav.children.length) {
    var path = location.pathname;
    function linkFor(p) {
      for (var i = 0; i < NAV_LINKS.length; i++)
        if (NAV_LINKS[i][0] === p) return NAV_LINKS[i];
      for (var i = 0; i < NAV_LINKS.length; i++)
        if (NAV_LINKS[i][0] !== "/" && p.indexOf(NAV_LINKS[i][0]) === 0) return NAV_LINKS[i];
      return null;
    }
    var current = linkFor(path) || NAV_LINKS[0];
    function pins() {
      try {
        var p = JSON.parse(localStorage.getItem(PIN_KEY) || "null");
        if (p && p.slice) return p.filter(function (x) { return linkFor(x); });
      } catch (e) {}
      return DEFAULT_PINS.slice();
    }
    function build(admin, keepMenuOpen) {
      var _m = nav.querySelector(".mneme-menu");
      var wasOpen = !!(keepMenuOpen && _m && _m.classList.contains("open"));
      var pinned = pins().filter(function (h) {
        return admin || ADMIN_ONLY.indexOf(h) === -1;
      }).slice(0, MAX_PINS);
      if (pinned.indexOf(current[0]) === -1) pinned.push(current[0]);
      var html = '' +
        '<button type="button" class="nav-menu-btn" aria-haspopup="true">Menu \u2630</button>' +
        pinned.map(function (h) {
        var l = linkFor(h);
        var active = h === current[0] ? ' class="active"' : "";
        return '<a href="' + h + '"' + active + '>' + l[1] + '</a>';
      }).join("");
      html += '<div class="mneme-menu" role="menu">' + NAV_LINKS.map(function (l) {
        var hide = (!admin && ADMIN_ONLY.indexOf(l[0]) !== -1) ? ' style="display:none"' : "";
        var act = l === current ? " active" : "";
        var pin = pins().indexOf(l[0]) !== -1;
        return '<div class="mneme-menu-row"' + hide + act + '>' +
          '<a href="' + l[0] + '"' + (act ? ' class="active"' : "") + '>' + l[1] + '</a>' +
          '<label><input type="checkbox" data-pin="' + l[0] + '"' + (pin ? " checked" : "") + '>Pin</label>' +
          '</div>';
      }).join("") + '<div class="mneme-menu-row mneme-menu-clear"><button type="button" data-clearpins="1">Clear all pins</button></div>' + '</div>';
      nav.innerHTML = html;
      if (wasOpen) {
        var m2 = nav.querySelector(".mneme-menu");
        var b2 = nav.querySelector(".nav-menu-btn");
        if (m2) m2.classList.add("open");
        if (b2) b2.setAttribute("aria-expanded", "true");
      }
      var btn = nav.querySelector(".nav-menu-btn");
      var menu = nav.querySelector(".mneme-menu");
      if (btn && menu) {
        btn.addEventListener("click", function (e) {
          e.stopPropagation();
          menu.classList.toggle("open");
          btn.setAttribute("aria-expanded", menu.classList.contains("open"));
        });
        document.addEventListener("click", function (e) {
          if (!menu.contains(e.target)) menu.classList.remove("open");
        });
        menu.addEventListener("click", function (e) {
          var c = e.target.closest ? e.target.closest("[data-clearpins]") : null;
          if (!c) return;
          e.stopPropagation();
          localStorage.setItem(PIN_KEY, JSON.stringify([]));
          build(admin, true);
        });
        menu.addEventListener("change", function (e) {
          var t = e.target;
          var p = pins();
          var i = p.indexOf(t.dataset.pin);
          if (t.checked && i === -1) p.push(t.dataset.pin);
          if (!t.checked && i !== -1) p.splice(i, 1);
          if (p.length > MAX_PINS) p = p.slice(0, MAX_PINS);
          localStorage.setItem(PIN_KEY, JSON.stringify(p));
          build(admin, true);
        });
      }
    }
    build(true); /* assume admin until /auth/me says otherwise */
    fetch("/auth/me").then(function (r) { return r.json(); }).then(function (d) {
      build(!!d.admin);
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
