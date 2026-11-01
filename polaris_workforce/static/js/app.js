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
 * Loaded before Alpine (both deferred) so Alpine.data() is registered on alpine:init.
 */
(function () {
  "use strict";

  const SHELL = '#main[data-app-shell]';
  const PROGRESS_DELAY_MS = 150;
  let progressTimer = null;
  let restoring = null; // AbortController of the Back / Forward fetch in flight
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
    if (!event.detail.boosted || !main()) return;
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
    showProgress(true);
    window.dispatchEvent(new CustomEvent("polaris-navigate"));

    fetch(url, {
      credentials: "same-origin",
      signal: controller.signal,
      // The headers htmx sends for a history restore: the server answers with the full page.
      headers: { "HX-Request": "true", "HX-History-Restore-Request": "true", "HX-Current-URL": url },
    })
      .then(function (response) {
        return response.text().then(function (html) {
          if (controller.signal.aborted || window.location.href !== url) return;
          const area = main();
          const contentType = response.headers.get("Content-Type");
          if (!area || response.redirected || !isOfficePage(contentType, html)) {
            window.location.reload();
            return;
          }
          window.htmx.swap(area, html, { swapStyle: "innerHTML" }, { select: SHELL + " > *" });
          window.scrollTo(0, scrollPositions.get(url) || 0);
          focusHeading(area);
        });
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

  document.addEventListener("alpine:init", function () {
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
