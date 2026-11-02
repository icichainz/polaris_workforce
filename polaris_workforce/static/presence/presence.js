/*
 * Presence tracking in the office: the live map of a site and a person's track on the
 * timeline page (templates/presence/live.html, person_day.html).
 *
 * Loaded by the pageModule component of static/js/app.js, which calls mount(root) and,
 * when the page is left, the function mount returns (streams closed, timers stopped).
 *
 * The page renders everything the user reads: messages for each connection state
 * (elements with data-status, one shown at a time), the row templates (<template>) with
 * their Tailwind classes, and a JSON config in data-config (tracker URL, a short dashboard
 * token, names by id from Django, zone names, the company time zone). This module never
 * builds markup from strings and puts names in the page as text only. The tracker only
 * knows ids: a person missing from the config is shown as "Worker no. 12".
 *
 * Live map: GET /v1/sites/{id}/presence, then the server-sent events of
 * /v1/sites/{id}/live?access_token= (EventSource cannot send headers). Events are applied
 * once per event_id. The token is renewed from Django (session cookie) five minutes
 * before it expires, and the stream reopened with it. When the stream closes, a plain
 * GET tells why: 401 → new token, 403 → tracking is off or not allowed, 404 → the tracker
 * has no config for the site yet, no answer → unreachable; it retries with a growing
 * delay. A stream silent for 60 s (pings come every 25 s) is reopened.
 */

const RENEW_BEFORE_S = 300;
const RETRY_MIN_MS = 3000;
const RETRY_MAX_MS = 60000;
const SILENCE_MS = 60000;
const FOREMAN_TRACK_MS = 60000;
const FOREMAN_WINDOW_MS = 20 * 60 * 1000;
const SEEN_EVENTS_MAX = 2000;

let leafletPromise = null;

function addStylesheet(href) {
  if (!href || document.querySelector(`link[rel="stylesheet"][href="${CSS.escape(href)}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.appendChild(link);
}

function loadLeaflet(root) {
  addStylesheet(root.dataset.leafletCss);
  addStylesheet(root.dataset.presenceCss);
  if (window.L) return Promise.resolve(window.L);
  if (!leafletPromise) {
    leafletPromise = new Promise(function (resolve, reject) {
      const script = document.createElement("script");
      script.src = root.dataset.leafletJs;
      script.onload = function () { resolve(window.L); };
      script.onerror = function () {
        leafletPromise = null;
        reject(new Error("Leaflet did not load"));
      };
      document.head.appendChild(script);
    });
  }
  return leafletPromise;
}

function baseMap(L, element) {
  const map = L.map(element, { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  return map;
}

function textElement(text) {
  // Tooltips get an element, never an HTML string: names are text.
  const span = document.createElement("span");
  span.textContent = text;
  return span;
}

function showStatus(root, state) {
  root.querySelectorAll("[data-status]").forEach(function (el) {
    el.hidden = el.dataset.status !== state;
  });
}

// 24-hour times in the company time zone, as the server-rendered pages show them (H:i).
function timeFormatter(timeZone) {
  const options = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  try {
    return new Intl.DateTimeFormat(document.documentElement.lang || undefined, {
      ...options,
      timeZone: timeZone,
    });
  } catch (error) {
    return new Intl.DateTimeFormat(undefined, options);
  }
}

function fill(template, values) {
  return template.replace(/\{(\w+)\}/g, function (match, key) {
    return key in values ? String(values[key]) : match;
  });
}

function setText(scope, selector, text) {
  scope.querySelectorAll(selector).forEach(function (el) { el.textContent = text; });
}

// The tracker may sit under a path (behind nginx: http://host:8080/tracker): join, never
// assume the root. ``path`` may carry a query string.
function trackerUrl(config, path) {
  return String(config.trackerUrl).replace(/\/+$/, "") + "/" + path.replace(/^\/+/, "");
}

function trackerFetch(config, path) {
  return fetch(trackerUrl(config, path), {
    headers: { Authorization: "Bearer " + config.token, Accept: "application/json" },
    credentials: "omit",
    cache: "no-store",
  });
}

// --- live map -----------------------------------------------------------------------------

function mountLive(root, config) {
  const people = new Map(); // "worker:12" → {kind, id, state, zone, since, lastSeen}
  const seen = new Set();
  const format = timeFormatter(config.timeZone);
  const strings = config.strings || {};
  const list = root.querySelector("[data-people]");
  const rowTemplate = root.querySelector("template[data-person-row]");
  let source = null;
  let stopped = false;
  let loaded = false; // a first snapshot arrived: the list says something true
  let retryMs = RETRY_MIN_MS;
  let authFailures = 0;
  let retryTimer = null;
  let renewTimer = null;
  let silenceTimer = null;
  let foremanTimer = null;
  let map = null;
  let centre = null;
  const foremanDots = new Map();

  function keyOf(subject) {
    return subject.kind + ":" + subject.id;
  }

  function personInfo(key, subject) {
    const known = (config.people || {})[key];
    if (known) return known;
    const template = subject.kind === "foreman" ? strings.unknownForeman : strings.unknownWorker;
    return { name: fill(template || "{id}", { id: subject.id }), detail: "", url: "" };
  }

  function zoneLabel(zone) {
    if (!zone) return strings.wholeSite || "";
    const name = (config.zones || {})[zone];
    return name ? zone + " · " + name : zone;
  }

  function at(value) {
    const date = value ? new Date(value) : null;
    return date && !isNaN(date) ? format.format(date) : "";
  }

  function render() {
    const rows = Array.from(people.entries()).map(function ([key, person]) {
      return { key: key, person: person, info: personInfo(key, person) };
    });
    rows.sort(function (a, b) { return a.info.name.localeCompare(b.info.name); });
    const fragment = document.createDocumentFragment();
    rows.forEach(function (row) {
      const node = rowTemplate.content.cloneNode(true);
      const link = node.querySelector('[data-field="link"]');
      const plain = node.querySelector('[data-field="plain"]');
      if (row.info.url) {
        link.href = row.info.url;
        link.textContent = row.info.name;
        plain.remove();
      } else {
        plain.textContent = row.info.name;
        link.remove();
      }
      setText(node, '[data-field="detail"]', row.info.detail || "");
      setText(node, '[data-field="zone"]', zoneLabel(row.person.zone));
      setText(node, '[data-field="since"]', at(row.person.since));
      setText(node, '[data-field="last-seen"]', at(row.person.lastSeen));
      node.querySelectorAll("[data-state]").forEach(function (el) {
        el.hidden = el.dataset.state !== row.person.state;
      });
      fragment.appendChild(node);
    });
    list.replaceChildren(fragment);
    const onSite = rows.filter(function (row) { return row.person.state === "on_site"; }).length;
    setText(root, '[data-count="on_site"]', String(onSite));
    setText(root, '[data-count="arriving"]', String(rows.length - onSite));
    root.querySelectorAll("[data-empty]").forEach(function (el) { el.hidden = rows.length > 0; });
    setText(root, "[data-updated]", format.format(new Date()));
    if (centre) {
      centre.setTooltipContent(textElement(fill(strings.onSiteTooltip || "{count}", { count: onSite })));
    }
    updateForemen();
  }

  function applySnapshot(snapshot) {
    loaded = true;
    people.clear();
    (snapshot.people || []).forEach(function (p) {
      people.set(keyOf(p.subject), {
        kind: p.subject.kind,
        id: p.subject.id,
        state: p.state,
        zone: p.zone_id,
        since: p.since,
        lastSeen: p.last_seen,
      });
    });
    render();
  }

  function applyEvent(event) {
    if (!event || !event.event_id || seen.has(event.event_id)) return;
    seen.add(event.event_id);
    if (seen.size > SEEN_EVENTS_MAX) seen.delete(seen.values().next().value);
    const key = keyOf(event.subject);
    const person = people.get(key);
    if (event.type === "left") {
      people.delete(key);
    } else if (event.type === "entered") {
      people.set(key, {
        kind: event.subject.kind,
        id: event.subject.id,
        state: "on_site",
        zone: event.zone_id,
        since: event.at,
        lastSeen: event.at,
      });
    } else if (event.type === "moved_zone") {
      if (person) {
        person.zone = event.zone_id;
        person.lastSeen = event.at;
        person.state = "on_site";
      } else {
        people.set(key, {
          kind: event.subject.kind,
          id: event.subject.id,
          state: "on_site",
          zone: event.zone_id,
          since: event.at,
          lastSeen: event.at,
        });
      }
    }
    render();
  }

  // Foremen carry the phone: their latest GPS fix on site is a dot on the map.
  function updateForemen() {
    if (!map) return;
    const onSite = new Set();
    people.forEach(function (person, key) {
      if (person.kind === "foreman") onSite.add(key);
    });
    foremanDots.forEach(function (dot, key) {
      if (!onSite.has(key)) {
        dot.remove();
        foremanDots.delete(key);
      }
    });
  }

  function refreshForemen() {
    if (!map || stopped) return;
    const to = new Date();
    const from = new Date(to.getTime() - FOREMAN_WINDOW_MS);
    people.forEach(function (person, key) {
      if (person.kind !== "foreman") return;
      const path = "/v1/people/foreman/" + person.id + "/track?from=" +
        encodeURIComponent(from.toISOString()) + "&to=" + encodeURIComponent(to.toISOString());
      trackerFetch(config, path)
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (track) {
          if (!track || stopped || !people.has(key)) return;
          const fixes = (track.points || []).filter(function (p) {
            return p.type === "gps" && p.site_id === config.siteId;
          });
          if (!fixes.length) return;
          const last = fixes[fixes.length - 1];
          let dot = foremanDots.get(key);
          if (!dot) {
            dot = window.L.circleMarker([last.lat, last.lng], { radius: 7, className: "presence-foreman" });
            dot.bindTooltip(textElement(personInfo(key, person).name));
            dot.addTo(map);
            foremanDots.set(key, dot);
          } else {
            dot.setLatLng([last.lat, last.lng]);
          }
        })
        .catch(function () {});
    });
  }

  function clearTimers() {
    window.clearTimeout(retryTimer);
    window.clearTimeout(silenceTimer);
    retryTimer = silenceTimer = null;
  }

  function closeStream() {
    if (source) {
      source.close();
      source = null;
    }
    window.clearTimeout(silenceTimer);
  }

  function retryLater(state) {
    closeStream();
    showStatus(root, state);
    window.clearTimeout(retryTimer);
    retryTimer = window.setTimeout(start, retryMs);
    retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
  }

  function heard() {
    window.clearTimeout(silenceTimer);
    silenceTimer = window.setTimeout(function () {
      if (!stopped) retryLater("reconnecting");
    }, SILENCE_MS);
  }

  function scheduleRenewal() {
    window.clearTimeout(renewTimer);
    const ms = (config.expiresAt - RENEW_BEFORE_S) * 1000 - Date.now();
    renewTimer = window.setTimeout(function () {
      renew().then(function (ok) { if (ok) connect(); });
    }, Math.max(ms, 10000));
  }

  function renew() {
    return fetch(config.tokenUrl, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
      redirect: "manual",
    })
      .then(function (response) {
        if (!response.ok) throw response;
        return response.json();
      })
      .then(function (data) {
        config.token = data.token;
        config.expiresAt = data.expires_at;
        scheduleRenewal();
        return true;
      })
      .catch(function (failure) {
        if (stopped) return false;
        if (failure && (failure.status === 401 || failure.type === "opaqueredirect")) {
          closeStream();
          showStatus(root, "expired");
        } else if (failure && failure.status === 403) {
          closeStream();
          showStatus(root, "forbidden");
        } else {
          retryLater("unreachable"); // Django did not answer: try again later.
        }
        return false;
      });
  }

  function connect() {
    if (stopped) return;
    closeStream();
    const url = trackerUrl(config, "v1/sites/" + config.siteId + "/live?access_token=" +
      encodeURIComponent(config.token));
    const stream = new EventSource(url);
    source = stream;
    heard();
    stream.addEventListener("snapshot", function (message) {
      heard();
      retryMs = RETRY_MIN_MS;
      applySnapshot(JSON.parse(message.data));
      showStatus(root, "live");
      refreshForemen();
    });
    stream.addEventListener("presence", function (message) {
      heard();
      applyEvent(JSON.parse(message.data));
    });
    stream.addEventListener("ping", heard);
    stream.onerror = function () {
      if (stopped || source !== stream) return;
      if (stream.readyState === EventSource.CLOSED) {
        retryLater("reconnecting"); // Then a plain request tells why it closed.
      } else {
        showStatus(root, "reconnecting"); // The browser reconnects by itself.
      }
    };
  }

  // A plain GET first: the people on site, and why the stream would fail.
  function start() {
    if (stopped) return;
    window.clearTimeout(retryTimer);
    trackerFetch(config, "/v1/sites/" + config.siteId + "/presence")
      .then(function (response) {
        if (stopped) return;
        if (response.status === 401) {
          // A fresh token refused too: the two sides do not share the same secret.
          authFailures += 1;
          if (authFailures > 1) {
            closeStream();
            showStatus(root, "rejected");
            return;
          }
          renew().then(function (ok) { if (ok) start(); });
        } else if (response.status === 403) {
          closeStream();
          showStatus(root, "forbidden");
        } else if (response.status === 404) {
          retryLater("not_configured");
        } else if (!response.ok) {
          retryLater("unreachable");
        } else {
          authFailures = 0;
          return response.json().then(function (snapshot) {
            applySnapshot(snapshot);
            connect();
          });
        }
      })
      .catch(function () {
        if (!stopped) retryLater("unreachable");
      });
  }

  const mapElement = root.querySelector("[data-map]");
  loadLeaflet(root)
    .then(function (L) {
      if (stopped || !mapElement || !config.geofence) return;
      const g = config.geofence;
      map = baseMap(L, mapElement);
      map.setView([g.lat, g.lng], 17); // A circle measures itself on a map with a view.
      const circle = L.circle([g.lat, g.lng], { radius: g.radius, className: "presence-geofence" }).addTo(map);
      centre = L.circleMarker([g.lat, g.lng], { radius: 5, className: "presence-centre" })
        .bindTooltip(textElement(fill(strings.onSiteTooltip || "{count}", { count: 0 })), {
          permanent: true,
          direction: "top",
        })
        .addTo(map);
      map.fitBounds(circle.getBounds(), { padding: [16, 16] });
      if (loaded) render();
      foremanTimer = window.setInterval(refreshForemen, FOREMAN_TRACK_MS);
    })
    .catch(function () {
      root.querySelectorAll("[data-map-failed]").forEach(function (el) { el.hidden = false; });
    });

  showStatus(root, "connecting");
  scheduleRenewal();
  start();

  return function unmount() {
    stopped = true;
    closeStream();
    clearTimers();
    window.clearTimeout(renewTimer);
    window.clearInterval(foremanTimer);
    if (map) map.remove();
  };
}

// --- a person's track ------------------------------------------------------------------

function mountTrack(root, config) {
  const format = timeFormatter(config.timeZone);
  const strings = config.strings || {};
  const list = root.querySelector("[data-stretches]");
  const rowTemplate = root.querySelector("template[data-stretch-row]");
  let stopped = false;
  let map = null;

  function siteName(id) {
    if (id === null || id === undefined) return strings.offSite || "";
    return (config.sites || {})[String(id)] || fill(strings.unknownSite || "{id}", { id: id });
  }

  function zoneName(siteId, zone) {
    if (!zone) return strings.wholeSite || "";
    const name = ((config.zones || {})[String(siteId)] || {})[zone];
    return name ? zone + " · " + name : zone;
  }

  // Consecutive sightings at the same place become one line: "07:02–09:30 · North · B".
  function stretches(points) {
    const result = [];
    points.forEach(function (p) {
      const place = p.type === "gps" ? "gps:" + p.site_id : "ble:" + p.site_id + ":" + (p.zone_id || "");
      const last = result[result.length - 1];
      if (last && last.place === place) {
        last.to = p.at;
        last.count += 1;
      } else {
        result.push({ place: place, point: p, from: p.at, to: p.at, count: 1 });
      }
    });
    return result;
  }

  function renderList(points) {
    const fragment = document.createDocumentFragment();
    stretches(points).forEach(function (s) {
      const node = rowTemplate.content.cloneNode(true);
      const p = s.point;
      const from = format.format(new Date(s.from));
      const to = format.format(new Date(s.to));
      setText(node, '[data-field="time"]', from === to ? from : from + "–" + to);
      setText(node, '[data-field="site"]', siteName(p.site_id));
      setText(node, '[data-field="zone"]', p.type === "ble" ? zoneName(p.site_id, p.zone_id) : "");
      setText(node, '[data-field="count"]', String(s.count));
      node.querySelectorAll("[data-kind]").forEach(function (el) {
        const kind = p.type === "gps" ? (p.site_id === null ? "travel" : "gps") : "ble";
        el.hidden = el.dataset.kind !== kind;
      });
      fragment.appendChild(node);
    });
    list.replaceChildren(fragment);
  }

  function renderMap(L, fixes) {
    const mapElement = root.querySelector("[data-map]");
    if (!mapElement) return;
    mapElement.hidden = false;
    map = baseMap(L, mapElement);
    const line = L.polyline(fixes.map(function (p) { return [p.lat, p.lng]; }), { className: "presence-track" }).addTo(map);
    fixes.forEach(function (p) {
      L.circleMarker([p.lat, p.lng], {
        radius: 4,
        className: p.site_id === null ? "presence-point-off" : "presence-point",
      })
        .bindTooltip(textElement(format.format(new Date(p.at)) + " · " + siteName(p.site_id)))
        .addTo(map);
    });
    map.fitBounds(line.getBounds(), { padding: [16, 16], maxZoom: 17 });
  }

  showStatus(root, "loading");
  const path = "/v1/people/" + config.kind + "/" + config.id + "/track?from=" +
    encodeURIComponent(config.from) + "&to=" + encodeURIComponent(config.to);
  trackerFetch(config, path)
    .then(function (response) {
      if (!response.ok) throw response;
      return response.json();
    })
    .then(function (track) {
      if (stopped) return;
      const points = (track.points || []).slice().sort(function (a, b) {
        return a.at < b.at ? -1 : a.at > b.at ? 1 : 0;
      });
      const fixes = points.filter(function (p) { return p.type === "gps"; });
      setText(root, '[data-count="points"]', String(points.length));
      setText(root, '[data-count="gps"]', String(fixes.length));
      setText(root, '[data-count="travel"]', String(fixes.filter(function (p) { return p.site_id === null; }).length));
      if (!points.length) {
        showStatus(root, "empty");
        return;
      }
      renderList(points);
      showStatus(root, "ok");
      if (fixes.length) {
        loadLeaflet(root)
          .then(function (L) { if (!stopped) renderMap(L, fixes); })
          .catch(function () {
            root.querySelectorAll("[data-map-failed]").forEach(function (el) { el.hidden = false; });
          });
      }
    })
    .catch(function (failure) {
      if (stopped) return;
      showStatus(root, failure && failure.status ? "failed" : "unreachable");
    });

  return function unmount() {
    stopped = true;
    if (map) map.remove();
  };
}

export function mount(root) {
  let config;
  try {
    config = JSON.parse(root.dataset.config || "null");
  } catch (error) {
    config = null;
  }
  if (!config) return null;
  if (root.dataset.view === "live") return mountLive(root, config);
  if (root.dataset.view === "track") return mountTrack(root, config);
  return null;
}
