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
 * Loaded before Alpine (both deferred) so Alpine.data() is registered on alpine:init.
 */
(function () {
  "use strict";

  const SHELL = '#main[data-app-shell]';
  const PROGRESS_DELAY_MS = 150;
  let progressTimer = null;

  function main() {
    return document.querySelector(SHELL);
  }

  function isOfficePage(xhr) {
    const type = xhr.getResponseHeader("Content-Type") || "";
    if (!type.startsWith("text/html")) return false;
    const doc = new DOMParser().parseFromString(xhr.responseText, "text/html");
    return doc.querySelector(SHELL) !== null;
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

    if (isOfficePage(xhr)) {
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
    // Move focus to the new page's heading so screen readers announce it.
    const area = main();
    if (!event.detail.boosted || event.detail.target !== area) return;
    const heading = area.querySelector("h1") || area;
    if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
    heading.focus({ preventScroll: true });
  });

  document.addEventListener("alpine:init", function () {
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
