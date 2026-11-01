/*
 * Office dashboard: navigation without full page reloads, and the Alpine components.
 *
 * layouts/app.html boosts its links and forms (hx-boost). A boosted request gets the
 * full page back; we swap only the children of #main and keep the sidebar on screen.
 * The sidebar <nav> comes back out of band (hx-swap-oob in components/sidebar.html), so
 * its active entry and badges follow the page.
 *
 * Anything that is not an office page (sign-in after a session expired, the badge print
 * sheet, a file download, a bare error page) falls back to a normal page load.
 *
 * Back / Forward: htmx keeps no page snapshots (historyCacheSize 0 in base.html, they
 * would hold names and pay amounts). Instead of htmx's restore, which would reload the
 * page, we fetch the page again and swap it like a click: #main plus the sidebar <nav>.
 * The window scroll position of each page is remembered in memory for the way back.
 *
 * Preloading: a boosted link starts loading when it is pressed (mousedown, touchstart)
 * rather than when it is released (click), usually 100–200 ms earlier. The click then
 * shows that response instead of asking again. It is kept in memory only, never in the
 * browser cache (names and pay amounts), for one click within PRELOAD_TTL_MS; a touch
 * that turns into a scroll drops it. Put data-preload="false" on a link (or a container)
 * whose GET must only run on a real click.
 *
 * Loaded before Alpine (both deferred) so Alpine.data() is registered on alpine:init.
 */
(function () {
  "use strict";

  const SHELL = '#main[data-app-shell]';
  const PROGRESS_DELAY_MS = 150;
  const PRELOAD_TTL_MS = 5000;
  let progressTimer = null;
  let restoring = null; // AbortController of the Back / Forward fetch in flight
  let preloaded = null; // {url, at, controller, page} of the link being pressed
  let showing = null; // the preload whose page is about to be shown
  const scrollPositions = new Map(); // url → window.scrollY when the page was left

  function main() {
    return document.querySelector(SHELL);
  }

  function isOfficePage(contentType, html) {
    if (!(contentType || "").startsWith("text/html")) return false;
    const doc = new DOMParser().parseFromString(html, "text/html");
    return doc.querySelector(SHELL) !== null;
  }

  function focusHeading(area) {
    // Move focus to the new page's heading so screen readers announce it.
    const heading = area.querySelector("h1") || area;
    if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
    heading.focus({ preventScroll: true });
  }

  function loadPage(url) {
    window.location.assign(url);
  }

  function fetchPage(url, signal, headers) {
    // Resolves to {response, html}: the full page, as for a browser request (apps/core/htmx.py).
    return fetch(url, { credentials: "same-origin", signal: signal, headers: headers }).then(
      function (response) {
        return response.text().then(function (html) {
          return { response: response, html: html };
        });
      }
    );
  }

  function showPage(area, html, scrollY) {
    // Swap #main's children and the sidebar <nav> (out of band); the <title> follows.
    window.htmx.swap(area, html, { swapStyle: "innerHTML" }, { select: SHELL + " > *" });
    window.scrollTo(0, scrollY);
    markAllScrollMore();
    focusHeading(area);
  }

  function showProgress(busy) {
    const bar = document.getElementById("nav-progress");
    const area = main();
    window.clearTimeout(progressTimer);
    if (area) area.toggleAttribute("aria-busy", busy);
    if (!bar) return;
    if (busy) {
      progressTimer = window.setTimeout(function () { bar.hidden = false; }, PROGRESS_DELAY_MS);
    } else {
      bar.hidden = true;
    }
  }

  document.addEventListener("htmx:beforeRequest", function (event) {
    const detail = event.detail;
    const entry = takePreload(detail);
    if (detail.boosted) showing = null; // A newer navigation wins.
    if (entry) {
      event.preventDefault(); // htmx sends nothing: the page is already on its way.
      showPreloaded(entry);
      return;
    }
    if (!detail.boosted || !main()) return;
    showProgress(true);
    window.dispatchEvent(new CustomEvent("polaris-navigate"));
  });

  document.addEventListener("htmx:afterRequest", function (event) {
    if (event.detail.boosted) showProgress(false);
  });

  document.addEventListener("htmx:beforeSwap", function (event) {
    const detail = event.detail;
    const area = main();
    if (!detail.boosted || !area) return;
    const xhr = detail.xhr;

    if (isOfficePage(xhr.getResponseHeader("Content-Type"), xhr.responseText)) {
      // Office page, including a 4xx/5xx rendered with the app layout: show it.
      detail.shouldSwap = true;
      detail.isError = false;
      detail.target = area;
      detail.selectOverride = SHELL + " > *";
      detail.swapOverride = "innerHTML";
      return;
    }

    detail.shouldSwap = false;
    const verb = detail.requestConfig ? detail.requestConfig.verb : "get";
    const redirected = xhr.responseURL && xhr.responseURL !== new URL(detail.pathInfo.finalRequestPath, window.location.href).href;
    if (verb === "get" || redirected) {
      loadPage(xhr.responseURL || detail.pathInfo.finalRequestPath);
    } else {
      // A post answered with a page that is not an office page: show it as it is.
      detail.shouldSwap = true;
      detail.isError = false;
      detail.target = document.body;
      detail.selectOverride = null;
    }
  });

  document.addEventListener("htmx:sendError", function (event) {
    // Network failure: let the browser try, it shows its own offline page.
    const detail = event.detail;
    if (detail.boosted && detail.requestConfig && detail.requestConfig.verb === "get") {
      loadPage(detail.pathInfo.finalRequestPath);
    }
  });

  document.addEventListener("htmx:afterSettle", function (event) {
    const area = main();
    if (event.detail.boosted && event.detail.target === area) focusHeading(area);
  });

  // --- Back / Forward ----------------------------------------------------------------

  document.addEventListener("htmx:beforeHistoryUpdate", function () {
    // Still on the page being left: remember where it was scrolled to.
    scrollPositions.set(window.location.href, window.scrollY);
  });

  function restorePage() {
    const url = window.location.href;
    if (restoring) restoring.abort();
    const controller = new AbortController();
    restoring = controller;
    dropPreload();
    showing = null;
    showProgress(true);
    window.dispatchEvent(new CustomEvent("polaris-navigate"));

    // The headers htmx sends for a history restore: the server answers with the full page.
    fetchPage(url, controller.signal, {
      "HX-Request": "true",
      "HX-History-Restore-Request": "true",
      "HX-Current-URL": url,
    })
      .then(function (page) {
        if (controller.signal.aborted || window.location.href !== url) return;
        const area = main();
        const response = page.response;
        const contentType = response.headers.get("Content-Type");
        if (!area || response.redirected || !isOfficePage(contentType, page.html)) {
          window.location.reload();
          return;
        }
        showPage(area, page.html, scrollPositions.get(url) || 0);
      })
      .catch(function (error) {
        if (error.name !== "AbortError") window.location.reload();
      })
      .finally(function () {
        if (restoring === controller) {
          restoring = null;
          showProgress(false);
        }
      });
  }

  // htmx sets window.onpopstate when the document is ready (its listener was added before
  // this one, so it has run). Office pages restore with restorePage(); anything else keeps
  // htmx's handling (a reload, as there is no snapshot).
  document.addEventListener("DOMContentLoaded", function () {
    const htmxPopstate = window.onpopstate;
    window.onpopstate = function (event) {
      if (event.state && event.state.htmx && main()) {
        restorePage();
      } else if (htmxPopstate) {
        htmxPopstate.call(window, event);
      }
    };
  });

  // --- Preloading ----------------------------------------------------------------------

  function preloadUrl(target) {
    // The URL a press on ``target`` would open with a boosted navigation, or null.
    const link = target instanceof Element ? target.closest("a[href]") : null;
    if (!link || !main()) return null;
    const boost = link.closest("[hx-boost]");
    if (!boost || boost.getAttribute("hx-boost") !== "true") return null;
    if (link.closest('[data-preload="false"]')) return null;
    if (link.hasAttribute("download") || (link.target && link.target !== "_self")) return null;
    if (["hx-get", "hx-post", "hx-put", "hx-patch", "hx-delete"].some((a) => link.hasAttribute(a))) {
      return null; // An explicit htmx request, not a navigation.
    }
    const url = new URL(link.href, window.location.href);
    if (url.origin !== window.location.origin || url.hash) return null;
    return url.href;
  }

  function preload(url) {
    if (preloaded && preloaded.url === url && Date.now() - preloaded.at < PRELOAD_TTL_MS) return;
    dropPreload();
    const controller = new AbortController();
    // The headers of a boosted navigation, as htmx would send them for the click.
    const page = fetchPage(url, controller.signal, {
      "HX-Request": "true",
      "HX-Boosted": "true",
      "HX-Current-URL": window.location.href,
    });
    page.catch(function () {}); // Failed or dropped: the click is handled by htmx instead.
    preloaded = { url: url, at: Date.now(), controller: controller, page: page };
  }

  function dropPreload() {
    if (preloaded) preloaded.controller.abort();
    preloaded = null;
  }

  function takePreload(detail) {
    // The preload for the request htmx is about to send (a boosted GET of the same URL,
    // pressed moments ago), or null. Any other request drops the preload: it may change
    // what the preloaded page shows.
    const entry = preloaded;
    if (!entry) return null;
    preloaded = null;
    const verb = detail.requestConfig ? detail.requestConfig.verb : "";
    const path = detail.pathInfo ? detail.pathInfo.finalRequestPath : null; // with GET values
    const fresh = Date.now() - entry.at < PRELOAD_TTL_MS;
    if (detail.boosted && verb === "get" && path && fresh && main()) {
      if (new URL(path, window.location.href).href === entry.url) return entry;
    }
    entry.controller.abort();
    return null;
  }

  function showPreloaded(entry) {
    showing = entry;
    showProgress(true);
    window.dispatchEvent(new CustomEvent("polaris-navigate"));
    entry.page
      .then(function (page) {
        if (showing !== entry) return; // Another page was opened meanwhile.
        const area = main();
        const response = page.response;
        const url = response.url || entry.url; // after any redirect
        if (!area || !isOfficePage(response.headers.get("Content-Type"), page.html)) {
          loadPage(url); // Sign-in after a session expired, an error page…: a normal load.
          return;
        }
        // As htmx does for a boosted click: remember the scroll, add the history entry.
        scrollPositions.set(window.location.href, window.scrollY);
        window.history.pushState({ htmx: true }, "", url);
        showPage(area, page.html, 0);
      })
      .catch(function () {
        if (showing === entry) loadPage(entry.url);
      })
      .finally(function () {
        if (showing === entry) {
          showing = null;
          showProgress(false);
        }
      });
  }

  function pressedWithoutModifiers(event) {
    return !(event.metaKey || event.ctrlKey || event.shiftKey || event.altKey);
  }

  document.addEventListener(
    "mousedown",
    function (event) {
      if (event.button !== 0 || !pressedWithoutModifiers(event)) return;
      const url = preloadUrl(event.target);
      if (url) preload(url);
    },
    { capture: true, passive: true }
  );

  document.addEventListener(
    "touchstart",
    function (event) {
      if (event.touches.length !== 1) return;
      const url = preloadUrl(event.target);
      if (url) preload(url);
    },
    { capture: true, passive: true }
  );

  // The finger moved: a scroll, not a tap.
  document.addEventListener("touchmove", dropPreload, { capture: true, passive: true });

  // --- Sideways scrolling -------------------------------------------------------------

  // A box that scrolls sideways (wide tables on a phone, tab rows) gets
  // data-scroll-more="start" | "end" | "both" while there is more to see on that side;
  // the CSS fades that edge (static/src/app.css). Without JavaScript it simply scrolls.
  const SCROLL_X = ".overflow-x-auto";

  function markScrollMore(box) {
    const hidden = box.scrollWidth - box.clientWidth;
    const atStart = box.scrollLeft <= 1;
    const atEnd = box.scrollLeft >= hidden - 1;
    let more = "";
    if (hidden > 1 && !(atStart && atEnd)) more = atStart ? "end" : atEnd ? "start" : "both";
    if (more && box.dataset.scrollMore !== more) box.dataset.scrollMore = more;
    if (!more && box.dataset.scrollMore) delete box.dataset.scrollMore;
  }

  function markAllScrollMore() {
    document.querySelectorAll(SCROLL_X).forEach(markScrollMore);
  }

  document.addEventListener(
    "scroll",
    function (event) {
      const box = event.target;
      if (box instanceof Element && box.matches(SCROLL_X)) markScrollMore(box);
    },
    { capture: true, passive: true }
  );
  window.addEventListener("resize", markAllScrollMore, { passive: true });
  document.addEventListener("DOMContentLoaded", function () {
    markAllScrollMore();
    // Widths change once the web fonts are in.
    if (document.fonts) document.fonts.ready.then(markAllScrollMore);
  });
  document.addEventListener("htmx:afterSettle", markAllScrollMore);

  document.addEventListener("alpine:init", function () {
    // Page module: a page whose behaviour is too big for this file (the presence live
    // map) puts x-data="pageModule" data-module="{% static '….js' %}" on its root. The
    // ES module is imported once and its mount(element) called; the function mount
    // returns runs when the page is left (boosted navigation replaces #main, and Alpine
    // calls destroy()), so streams and timers do not outlive their page.
    window.Alpine.data("pageModule", function () {
      return {
        unmount: null,
        init() {
          const el = this.$el;
          import(el.dataset.module)
            .then((module) => {
              if (!el.isConnected) return;
              const unmount = module.mount(el);
              this.unmount = typeof unmount === "function" ? unmount : null;
            })
            .catch(function () {
              el.dataset.moduleFailed = "true";
            });
        },
        destroy() {
          if (this.unmount) this.unmount();
          this.unmount = null;
        },
      };
    });

    // Theme switch (components/theme_switch.html): show the theme at once; the form's
    // hx-post saves it (204). "system" is left to the CSS (prefers-color-scheme).
    window.Alpine.data("themeSwitch", function (initial) {
      return {
        theme: initial,
        pick(value) {
          this.theme = value;
          document.documentElement.dataset.theme = value;
        },
      };
    });

    // Settings form (core/settings.html): unsaved-change count, currency warning,
    // "every N days" field.
    window.Alpine.data("settingsForm", function () {
      return {
        initial: {},
        count: 0,
        currencyChanged: false,
        frequency: "",
        snapshot() {
          const values = {};
          for (const [name, value] of new FormData(this.$el).entries()) {
            if (name === "csrfmiddlewaretoken") continue;
            (values[name] = values[name] || []).push(String(value));
          }
          return values;
        },
        differs(now, name) {
          return JSON.stringify(now[name] || []) !== JSON.stringify(this.initial[name] || []);
        },
        init() {
          this.initial = this.snapshot();
          this.update();
        },
        update() {
          const now = this.snapshot();
          const names = new Set([...Object.keys(now), ...Object.keys(this.initial)]);
          this.count = [...names].filter((name) => this.differs(now, name)).length;
          this.currencyChanged = this.differs(now, "currency") || this.differs(now, "currency_decimals");
          this.frequency = (now.pay_frequency || [""])[0];
        },
      };
    });
  });
})();
