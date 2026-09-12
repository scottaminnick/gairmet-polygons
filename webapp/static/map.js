// map.js
// ------
// Sets up the Leaflet map, fetches GeoJSON from our own /api endpoints
// (NOT directly from static files -- keeps the door open to swap in a
// database or live-generated data later without touching this file),
// and wires up the layer toggle checkboxes in the top-right panel.

const map = L.map('map', {
  zoomControl: true,
  attributionControl: true,
}).setView([39.5, -98.5], 4.4); // roughly centers on CONUS

// Dark basemap to match the console theme (CARTO's "Dark Matter" tiles).
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
  subdomains: 'abcd',
  maxZoom: 19,
}).addTo(map);

const layers = {
  ifr: L.geoJSON(null, {
    style: {
      color: '#f5a623',
      weight: 1.5,
      fillColor: '#f5a623',
      fillOpacity: 0.28,
    },
    onEachFeature: (feature, layer) => {
      const p = feature.properties || {};
      const causeRow = p.cause ? `<div>cause: ${p.cause}</div>` : '';
      const weatherTypeRow = p.weather_type ? `<div>weather: ${p.weather_type}</div>` : '';
      layer.bindPopup(
        `<div><strong>${p.hazard || 'IFR'}</strong></div>` +
        causeRow +
        weatherTypeRow +
        `<div>threshold: &ge;${p.threshold_pct ?? '?'}%</div>` +
        `<div>valid: ${formatValidTime(p.valid_time)}</div>`
      );
    },
  }),

  mtn: L.geoJSON(null, {
    style: {
      color: '#b985ff',
      weight: 1.5,
      fillColor: '#b985ff',
      fillOpacity: 0.28,
    },
    onEachFeature: (feature, layer) => {
      const p = feature.properties || {};
      // weather_type is ALWAYS present for Mountain Obscuration (CLDS is
      // the base case when nothing more specific applies), unlike IFR
      // where it's only set when the cause involves visibility.
      const weatherTypeRow = p.weather_type ? `<div>weather: ${p.weather_type}</div>` : '';
      const clearanceRow = p.clearance_margin_ft != null
        ? `<div>clearance: ${p.clearance_margin_ft} ft</div>` : '';
      const terrainRadiusRow = p.terrain_radius_nm != null
        ? `<div>terrain search: ${p.terrain_radius_nm} nm</div>` : '';
      layer.bindPopup(
        `<div><strong>${p.hazard || 'MTN OBSC'}</strong></div>` +
        weatherTypeRow +
        clearanceRow +
        terrainRadiusRow +
        `<div>threshold: &ge;${p.threshold_pct ?? '?'}%</div>` +
        `<div>valid: ${formatValidTime(p.valid_time)}</div>`
      );
    },
  }),

  states: L.geoJSON(null, {
    style: {
      color: '#64749a',
      weight: 1,
      fill: false,
      dashArray: null,
    },
    onEachFeature: (feature, layer) => {
      const name = feature.properties && feature.properties.name;
      if (name) layer.bindTooltip(name, { sticky: true });
    },
  }),

  artcc: L.geoJSON(null, {
    style: {
      color: '#2dd4bf',
      weight: 1.5,
      fill: false,
      dashArray: '6 4',
    },
    onEachFeature: (feature, layer) => {
      const name = feature.properties && feature.properties.name;
      if (name) layer.bindTooltip(name, { sticky: true, className: 'artcc-tooltip' });
    },
  }),

  // The legacy (pre-automation) MTN OBSC areas. DISPLAY ONLY -- nothing
  // here feeds a gate, mask or filter; it exists so a forecaster can put
  // derived and legacy areas side by side during calibration. See
  // data/boundaries/LEGACY_MTNOBSC.md for what the geometry is worth.
  //
  // Styled to read as neither hazard nor airspace: outline only, in a
  // colour used nowhere else (states are slate, ARTCC teal, IFR amber,
  // MTN OBSC violet), with a long dash that says "reference line".
  legacyMtnObsc: L.geoJSON(null, {
    style: (feature) => {
      const name = (feature.properties && feature.properties.name) || '';
      // CentralValleyCutout is a HOLE in the Rockies area, not a hazard
      // area of its own. Drawn dotted and dimmer so it reads as "this
      // bit is taken out" rather than "here is another area", and
      // labelled to say so outright.
      const isCutout = name === 'CentralValleyCutout';
      return {
        color: '#ff5c8a',
        weight: isCutout ? 1 : 2,
        fill: false,
        opacity: isCutout ? 0.65 : 1,
        dashArray: isCutout ? '2 5' : '10 6',
      };
    },
    onEachFeature: (feature, layer) => {
      const name = feature.properties && feature.properties.name;
      const isCutout = name === 'CentralValleyCutout';
      const label = !name
        ? '&#9888; unnamed feature &mdash; legacy_mtnobsc.json has no <code>name</code> property'
        : isCutout
          ? `${name} &mdash; cutout from the Rockies area, not a hazard area`
          : `${name} &mdash; legacy area (reference only)`;
      layer.bindTooltip(label, { sticky: true, className: 'legacy-tooltip' });
    },
  }),
};

layers.states.addTo(map);
layers.ifr.addTo(map);
layers.mtn.addTo(map);

// --- Wire up the checkbox toggles in the top-right panel ---
document.getElementById('toggle-ifr').addEventListener('change', (e) => {
  if (e.target.checked) map.addLayer(layers.ifr);
  else map.removeLayer(layers.ifr);
});

document.getElementById('toggle-states').addEventListener('change', (e) => {
  if (e.target.checked) map.addLayer(layers.states);
  else map.removeLayer(layers.states);
});

document.getElementById('toggle-artcc').addEventListener('change', (e) => {
  if (e.target.checked) map.addLayer(layers.artcc);
  else map.removeLayer(layers.artcc);
});

// Off by default -- a comparison overlay, not part of the normal view.
document.getElementById('toggle-legacy-mtnobsc').addEventListener('change', (e) => {
  if (e.target.checked) map.addLayer(layers.legacyMtnObsc);
  else map.removeLayer(layers.legacyMtnObsc);
});

document.getElementById('toggle-mtn').addEventListener('change', (e) => {
  if (e.target.checked) map.addLayer(layers.mtn);
  else map.removeLayer(layers.mtn);
});

// --- Formats an ISO timestamp as a DDHHMMZ group, matching the date/time
//     group convention used in real SIGMET/AIRMET bulletins (e.g. "071800Z"
//     means the 7th of the month at 1800 UTC). Small touch, but it's the
//     actual convention aviation weather users expect. ---
function formatValidTime(iso) {
  if (!iso) return '--------Z';
  const d = new Date(iso);
  if (isNaN(d)) return '--------Z';
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `${dd}${hh}${mm}Z`;
}

// --- CYCLE STALENESS ---
//
//     The panel has always shown the loaded cycle honestly. What it could
//     not do was say when that cycle has stopped being the current one,
//     and a six-hour-old polygon set looks exactly like a fresh one.
//
//     THE COMPARISON IS NOT AGAINST THE WALL CLOCK. A G-AIRMET package is
//     built from the NBM cycle six hours before it (NBM_LEAD_OFFSET_HOURS
//     in pipeline/gairmet_cycle.py, deliberately 6), so the app NORMALLY
//     holds a cycle in the future: at 10:15Z it is serving the 15Z
//     package, 4h45m before 15Z. Comparing the loaded cycle to "now"
//     would call every healthy state stale.
//
//     What "stale" means instead: the loaded cycle is older than the
//     newest one that should have PUBLISHED by now, following the
//     NORMAL publish path end to end (pipeline/publish_schedule.py):
//
//       +0:45  a Railway cron POSTs workflow_dispatch to both hazards
//       +1:45  the job's 60-minute poll for its NBM cycle closes
//       +2:30  45 more minutes to install, generate and push
//
//     so PUBLISH_WINDOW_CLOSE_MINUTES is 45 + 60 + 45.
//
//     DELIBERATELY NOT THE +3:37 BACKSTOP. Each workflow keeps one
//     `schedule:` entry as a safety net for a failed dispatch, and it
//     could still publish this cycle at ~+4:22. Waiting for that before
//     saying anything would mean hiding a real failure for two hours to
//     avoid one honest warning. Past +2:30 the package IS late; the
//     backstop is a recovery, not extra runway.
//
//     Two ways to end up stale, and the indicator does not care which:
//       - a browser serving the manifest from cache (what prompted this:
//         raw.githubusercontent had 09Z, a normal window showed 03Z, a
//         private window showed 09Z);
//       - artifacts.refresh_hazard() failing and deliberately keeping the
//         last good cycle serving rather than blanking the site
//         (tests/test_artifacts.py::test_failed_refresh_keeps_the_last_
//         good_cycle). Right behaviour, but silent.
//
// These four MUST match pipeline/publish_schedule.py and
// pipeline/gairmet_cycle.py; tests/test_publish_schedule.py fails if they
// drift.
const CYCLE_HOURS = [3, 9, 15, 21];
const PUBLISH_WINDOW_CLOSE_MINUTES = 150;
const NBM_LEAD_OFFSET_HOURS = 6;
const STALENESS_RECHECK_MS = 5 * 60 * 1000;

// The newest G-AIRMET cycle that should be published by `now`: the last
// NBM synoptic hour whose publish window has fully closed, shifted by the
// lead offset to the package built from it.
//
// Mirrored in Python as pipeline.publish_schedule.expected_gairmet_cycle(),
// which tests/test_publish_schedule.py exercises against frozen clocks --
// including the boundary either side of the deadline and the midnight
// rollover. This copy exists because the check runs in the browser, with
// nothing to call; the constants above are pinned against the Python ones
// by the same test file.
function expectedGairmetCycle(now) {
  const cutoff = new Date(now.getTime() - PUBLISH_WINDOW_CLOSE_MINUTES * 60 * 1000);
  const midnight = new Date(Date.UTC(
    cutoff.getUTCFullYear(), cutoff.getUTCMonth(), cutoff.getUTCDate(), 0, 0, 0, 0));

  let synoptic = null;
  CYCLE_HOURS.forEach((hour) => {
    const start = new Date(midnight.getTime() + hour * 3600 * 1000);
    if (start <= cutoff) synoptic = start;
  });
  if (!synoptic) {
    const yesterday = new Date(midnight.getTime() - 24 * 3600 * 1000);
    synoptic = new Date(yesterday.getTime() + CYCLE_HOURS[CYCLE_HOURS.length - 1] * 3600 * 1000);
  }
  return new Date(synoptic.getTime() + NBM_LEAD_OFFSET_HOURS * 3600 * 1000);
}

// The cycle currently on screen, so the recheck timer can re-evaluate it
// without refetching anything.
let loadedModelCycle = null;

function updateStalenessIndicator(now = new Date()) {
  const el = document.getElementById('cycle-staleness');
  if (!el) return;
  if (!loadedModelCycle) {
    el.hidden = true;
    return;
  }

  const loaded = new Date(loadedModelCycle);
  if (isNaN(loaded)) {
    el.hidden = true;
    return;
  }

  const expected = expectedGairmetCycle(now);
  const behindMs = expected.getTime() - loaded.getTime();
  // A whole cycle or more. Deliberately not "any amount behind": the
  // comparison uses the BROWSER's clock, and a modest clock error should
  // not be able to raise a false alarm. Six hours of skew is a broken
  // machine, not a rounding difference.
  if (behindMs < 6 * 3600 * 1000) {
    el.hidden = true;
    return;
  }

  const hours = Math.round(behindMs / 3600000);
  el.innerHTML =
    `&#9888; <b>STALE DATA</b> &mdash; showing the ` +
    `${String(loaded.getUTCHours()).padStart(2, '0')}Z package, ` +
    `${hours} h behind. The ` +
    `${String(expected.getUTCHours()).padStart(2, '0')}Z package should have published by now. ` +
    `Hard-reload (Ctrl-Shift-R / Cmd-Shift-R); if it persists the publish may have failed ` +
    `&mdash; check <code>/api/data/status</code>.`;
  el.hidden = false;
}

// A page left open across a cycle boundary goes stale without any fetch
// happening, so the check is on a timer as well as on load.
setInterval(() => updateStalenessIndicator(), STALENESS_RECHECK_MS);

// --- Tracks which forecast hour is currently displayed, so the
//     adjustors know what to recompute against. Also tracks whether
//     adjustment is even possible (it isn't in the demo-fallback case,
//     where there's no cached grid to recompute from), and the list of
//     forecast hours the cycle carries, which APPLY ALL HOURS needs. ---
let currentFxx = null;
let liveAdjustAvailable = false;
let forecastHours = [];

// --- RIGHT-RAIL ACCORDION ---
//     Each hazard row in LAYERS owns its adjustors, expanded inline
//     beneath it. One open at a time: opening a row closes whatever was
//     open, which is what keeps the rail from growing back into a third
//     of the screen once there are seven hazards instead of two.
//
//     The checkbox and the expander are siblings in the markup, so
//     visibility and expansion never interfere: a hazard can be visible
//     and collapsed, or hidden and expanded. Rows without adjustors
//     (STATE, ARTCC, LEGACY MTN OBSC) are plain labels with no expander
//     and no disclosure triangle, so the difference is visible before
//     anyone clicks.
//
//     Adding a hazard is one entry here plus its row in index.html.
const HAZARD_PANELS = [
  { hazard: 'ifr', expander: 'expand-ifr', body: 'adjust-ifr-body' },
  { hazard: 'mtn', expander: 'expand-mtn', body: 'adjust-mtn-body' },
];

function setHazardExpanded(panel, expanded) {
  const expander = document.getElementById(panel.expander);
  const body = document.getElementById(panel.body);
  if (!expander || !body) return;
  body.hidden = !expanded;
  expander.setAttribute('aria-expanded', String(expanded));
  expander.closest('.layer-row').classList.toggle('layer-row-open', expanded);
}

HAZARD_PANELS.forEach((panel) => {
  const expander = document.getElementById(panel.expander);
  if (!expander) return;
  expander.addEventListener('click', () => {
    const body = document.getElementById(panel.body);
    const willOpen = body.hidden;
    HAZARD_PANELS.forEach((other) => setHazardExpanded(other, false));
    if (willOpen) setHazardExpanded(panel, true);
  });
});

// Default state on load: everything collapsed.
HAZARD_PANELS.forEach((panel) => setHazardExpanded(panel, false));

// --- WHOLE-RAIL COLLAPSE ---
//     Collapses to a narrow icon strip so the map can go full width.
//     Session-only by design: this environment has no localStorage, so
//     the state lives in the DOM for as long as the page does and a
//     reload comes back expanded.
function setRailCollapsed(collapsed) {
  const panels = document.getElementById('rail-panels');
  const strip = document.getElementById('rail-strip');
  if (!panels || !strip) return;
  panels.hidden = collapsed;
  strip.hidden = !collapsed;
  document.getElementById('right-rail').classList.toggle('rail-is-collapsed', collapsed);
}

document.getElementById('rail-collapse').addEventListener('click', () => setRailCollapsed(true));
document.getElementById('rail-expand').addEventListener('click', () => setRailCollapsed(false));

// The strip's icons re-open the rail and scroll the panel they name into
// view -- with seven hazards the rail may well be taller than the window.
document.querySelectorAll('.rail-icon').forEach((button) => {
  button.addEventListener('click', () => {
    setRailCollapsed(false);
    const target = document.getElementById(button.dataset.scrollTo);
    if (target) target.scrollIntoView({ block: 'nearest' });
  });
});

setRailCollapsed(false);

// Called when there is no cached grid behind this cycle: recompute and
// export both depend on one, so the controls that drive them are
// collapsed, disabled and labelled rather than left looking operable.
function disableLiveAdjust(reason) {
  HAZARD_PANELS.forEach((panel) => {
    setHazardExpanded(panel, false);
    const expander = document.getElementById(panel.expander);
    expander.disabled = true;
    expander.title = reason;
    expander.closest('.layer-row').classList.add('layer-row-disabled');
    document.getElementById(panel.body).querySelectorAll('button, input')
      .forEach((control) => { control.disabled = true; });
  });

  const exportPanel = document.getElementById('export-panel');
  exportPanel.classList.add('panel-disabled');
  exportPanel.title = reason;
  exportPanel.querySelectorAll('button, input').forEach((control) => { control.disabled = true; });
  document.getElementById('export-status').textContent = 'unavailable';
}

function disableLegacyToggle(reason) {
  const toggle = document.getElementById('toggle-legacy-mtnobsc');
  if (!toggle) return;
  toggle.checked = false;
  toggle.disabled = true;
  const row = toggle.closest('.layer-row');
  if (row) {
    row.classList.add('layer-toggle-disabled');
    row.title = reason;
  }
}

// --- The legacy overlay's features are identified by a `name`
//     property, which both the styling and the tooltips key off. A file
//     that spells it differently is NOT a broken file in any way the
//     browser can detect: it parses, it draws three areas, and every
//     tooltip quietly falls back. The Central Valley cutout is the real
//     casualty -- without its name it loses the dotted styling and the
//     label that say it is a hole in the Rockies area, and reads as a
//     third hazard area instead.
//
//     That is a wrong map with nothing on screen admitting it, so it gets
//     said out loud: in the layer list, where the operator is, and in the
//     console for whoever is deploying. Deliberately NOT a reason to
//     disable the layer -- the geometry is still worth looking at, and a
//     forecaster mid-calibration should not lose it over a key name.
function checkLegacyFeatureNames(geojson) {
  const features = (geojson && geojson.features) || [];
  const unnamed = features.filter((f) => !(f.properties && f.properties.name));
  const warning = document.getElementById('legacy-mtnobsc-warning');
  if (!unnamed.length) {
    warning.hidden = true;
    return;
  }

  // Name the key that IS there, when there is exactly one candidate --
  // it turns "something is wrong" into a one-line fix.
  const keys = new Set();
  unnamed.forEach((f) => Object.keys(f.properties || {}).forEach((k) => keys.add(k)));
  const suspect = [...keys].find((k) => k !== 'source' && k !== 'bearing_datum');
  const found = suspect ? ` (found \`${suspect}\`)` : '';

  warning.innerHTML =
    `&#9888; ${unnamed.length} of ${features.length} legacy features have no ` +
    `<code>name</code>${found}. Areas are drawn, but the Central Valley cutout ` +
    `is not distinguishable from a hazard area. Regenerate with ` +
    `<code>scripts/build_legacy.py</code>.`;
  warning.hidden = false;
  console.error(
    `legacy_mtnobsc.json: ${unnamed.length}/${features.length} features lack a "name" property` +
    (suspect ? `; found "${suspect}" instead` : '') +
    ' -- the Central Valley cutout will render as an ordinary legacy area'
  );
}

// --- PER-FORECAST-HOUR ADJUSTMENT STATE ---
//     Each forecast hour keeps its OWN settings, per hazard, rather than
//     one set shared across the whole cycle. Forecasters need this:
//     overnight hours want a lower threshold for radiation fog than the
//     daytime hours do, and switching hours used to silently discard
//     whatever had been dialled in.
//
//     One store per hazard, built from a field list so IFR's three
//     parameters and MTN OBSC's five get identical semantics without
//     duplicating the logic. Each field maps a store key to its number
//     input, the hint element under its label, and the GeoJSON property
//     the scheduled snapshot carries it in.
//
//     TWO VALUES PER PARAMETER. The store holds what is APPLIED -- the
//     parameters the polygons on the map were actually cut with. The
//     number inputs hold what is PENDING -- wherever the forecaster has
//     stepped or typed them since. The two agree except between an edit
//     and the APPLY that commits it; while they differ the row is marked
//     dirty and the hint shows the applied value. Nothing recomputes on
//     input: only APPLY (or Enter in a field) copies pending into the
//     store and fires ONE recompute for the whole set.
//
//     An hour is seeded from its own scheduled snapshot the first time it
//     is shown, and remembers what was applied to it from then on.
//     Keyed by zero-padded forecast hour ("00", "03").
function hourKey(fxx) {
  return String(fxx).padStart(2, '0');
}

const IFR_FIELDS = [
  { key: 'threshold', input: 'adjust-threshold', hint: 'adjust-threshold-val', prop: 'threshold_pct' },
  { key: 'radius', input: 'adjust-radius', hint: 'adjust-radius-val', prop: 'neighborhood_radius_nm' },
  { key: 'minArea', input: 'adjust-minarea', hint: 'adjust-minarea-val', prop: 'min_area_sq_mi' },
];

const MTN_FIELDS = [
  { key: 'threshold', input: 'adjust-mtn-threshold', hint: 'adjust-mtn-threshold-val', prop: 'threshold_pct' },
  { key: 'relief', input: 'adjust-mtn-relief', hint: 'adjust-mtn-relief-val', prop: 'mountainous_relief_ft' },
  { key: 'clearance', input: 'adjust-mtn-clearance', hint: 'adjust-mtn-clearance-val', prop: 'clearance_margin_ft' },
  { key: 'radius', input: 'adjust-mtn-radius', hint: 'adjust-mtn-radius-val', prop: 'neighborhood_radius_nm' },
  { key: 'minArea', input: 'adjust-mtn-minarea', hint: 'adjust-mtn-minarea-val', prop: 'min_area_sq_mi' },
];

// Snap a typed value onto the input's min/max/step lattice. A number
// input accepts anything typed into it -- "47" against a 5-step field,
// "99999" against a 10000 max -- and the server would happily compute
// that. Snapping keeps the panel honest about what the buttons could
// have reached, and keeps hour-to-hour settings comparable.
function normalizeStepInput(el, fallback) {
  const min = Number(el.min);
  const max = Number(el.max);
  const step = Number(el.step) || 1;
  let v = Number(el.value);
  if (el.value === '' || Number.isNaN(v)) v = fallback ?? Number(el.defaultValue);
  v = Math.min(max, Math.max(min, v));
  v = min + Math.round((v - min) / step) * step;
  el.value = v;
  return v;
}

function makeHourStore(fields) {
  const byHour = {};
  return {
    fields,

    // The hazard's inputs, read as numbers, as one settings object. This
    // is the PENDING set.
    read() {
      const out = {};
      fields.forEach((f) => { out[f.key] = Number(document.getElementById(f.input).value); });
      return out;
    },

    // Push a settings object onto the inputs. Used when an hour is shown
    // (its applied settings become the pending ones) and by REVERT.
    apply(settings) {
      if (!settings) return;
      fields.forEach((f) => {
        if (settings[f.key] == null) return;
        document.getElementById(f.input).value = settings[f.key];
      });
    },

    // Starting settings for an hour, from its scheduled snapshot's
    // properties.
    //
    // A property the snapshot does not carry falls back to the input's
    // DEFAULT, not to where the input currently sits. This path is also
    // what RESET runs through, and resetting to "wherever you left it" is
    // not a reset. It matters for any parameter added after a snapshot was
    // written -- mountainous_relief_ft is the first -- where every cached
    // file predates the property and the fallback is the only branch taken.
    // The markup's default value is the pipeline default, so this restores
    // exactly what the scheduled run would have used.
    fromProps(props) {
      if (!props) return null;
      const out = {};
      fields.forEach((f) => {
        const el = document.getElementById(f.input);
        out[f.key] = props[f.prop] ?? Number(el.defaultValue);
      });
      return out;
    },

    get(fxx) { return byHour[hourKey(fxx)]; },
    set(fxx, settings) { byHour[hourKey(fxx)] = settings; },

    // Commit the inputs against an hour: pending becomes applied. Called
    // by APPLY, never by input events.
    saveCurrent(fxx) {
      if (fxx == null) return;
      byHour[hourKey(fxx)] = this.read();
    },

    // Commit the inputs against EVERY hour of the cycle. Hours never
    // visited get an entry too, so showForecastHour() finds saved settings
    // for them and recomputes rather than loading the scheduled file.
    saveAll(hours) {
      const settings = this.read();
      hours.forEach((fxx) => { byHour[hourKey(fxx)] = { ...settings }; });
    },

    // Whether the inputs differ from what is applied to this hour.
    isDirty(fxx) {
      const applied = this.get(fxx);
      if (!applied) return false;
      const pending = this.read();
      return fields.some((f) => applied[f.key] !== pending[f.key]);
    },

    clear(fxx) { delete byHour[hourKey(fxx)]; },
    clearAll() { Object.keys(byHour).forEach((k) => delete byHour[k]); },

    // Snapshot of every hour's settings -- used to build the PGEN export
    // payload and its sidecar.
    all() { return { ...byHour }; },
  };
}

const ifrHours = makeHourStore(IFR_FIELDS);
const mtnHours = makeHourStore(MTN_FIELDS);


// --- Re-processes the CURRENTLY selected forecast hour's cached grid
//     with the given settings (or, absent those, the hour's APPLIED
//     settings) and swaps in the result. Does NOT re-fit the map view or
//     touch the fxx button state -- this is the same forecast hour, just
//     re-drawn with different parameters.
//
//     The sequence counter guards against out-of-order responses: two
//     recomputes in flight (APPLY, then a quick hour switch) have no
//     guaranteed arrival order, and a slow earlier one landing last would
//     paint polygons that match nothing on the panel. Only the newest
//     request is allowed to draw. ---
let ifrRecomputeSeq = 0;

async function recomputeCurrentSnapshot(settings = null) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  const { threshold, radius, minArea } = settings || ifrHours.get(currentFxx) || ifrHours.read();
  const statusEl = document.getElementById('adjust-status');
  const fxxStr = String(currentFxx).padStart(2, '0');
  const seq = ++ifrRecomputeSeq;

  statusEl.textContent = 'computing...';
  try {
    const url = `/api/hazards/ifr/${fxxStr}/recompute?threshold_pct=${threshold}&neighborhood_radius_nm=${radius}&min_area_sq_mi=${minArea}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`recompute failed (${resp.status})`);
    const geojson = await resp.json();
    if (seq !== ifrRecomputeSeq) return; // superseded while in flight

    layers.ifr.clearLayers();
    layers.ifr.addData(geojson);

    const firstProps = geojson.features?.[0]?.properties;
    document.getElementById('legend-threshold').textContent = threshold;
    document.getElementById('legend-radius').textContent = radius;
    document.getElementById('legend-min-area').textContent = minArea;
    if (firstProps) {
      document.getElementById('valid-time').textContent = formatValidTime(firstProps.valid_time);
    }
    statusEl.textContent = '';
  } catch (err) {
    if (seq !== ifrRecomputeSeq) return;
    console.error('Recompute failed:', err);
    statusEl.textContent = 'error (see console)';
  }
}

// --- Same idea as recomputeCurrentSnapshot() above, but for the
//     Mountain Obscuration layer and its own parameters. Kept as a
//     separate function (and a separate panel) rather than having one set
//     of controls drive both layers: MTN OBSC has parameters IFR doesn't
//     (relief, clearance_margin_ft), and the two hazards are genuinely
//     tuned independently -- a forecaster dialing in an IFR threshold
//     shouldn't silently move the mountain obscuration boundaries too. ---
// --- The mountainous-area readout under RELIEF.
//
//     The figure is a FeatureCollection foreign member rather than a
//     feature property, because it describes the mask the polygons were
//     cut from, not any one polygon -- at a relief threshold high enough
//     to leave no mountains at all there are no features to hang it on,
//     and that is exactly the case worth showing.
//
//     Absent means "this snapshot predates the measurement" (a scheduled
//     file written by an older pipeline run), not "zero". Those are very
//     different numbers, so an absent member shows as -- and a real zero
//     shows as 0.
function setMountainousArea(geojson) {
  const el = document.getElementById('adjust-mtn-relief-area');
  const sqMi = geojson?.mountainous_area_sq_mi;
  if (sqMi == null) {
    el.textContent = '--';
    el.title = 'not measured in this snapshot -- APPLY any change to recompute';
    return;
  }
  el.textContent = sqMi >= 1e6
    ? `${(sqMi / 1e6).toFixed(2)}M mi\u00b2`
    : `${Math.round(sqMi).toLocaleString()} mi\u00b2`;
  el.title = `${Math.round(sqMi).toLocaleString()} sq mi of the grid is mountainous at this relief threshold`;
}

let mtnRecomputeSeq = 0;

async function recomputeCurrentMtnSnapshot(settings = null) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  const { threshold, relief, clearance, radius, minArea } =
    settings || mtnHours.get(currentFxx) || mtnHours.read();
  const statusEl = document.getElementById('adjust-mtn-status');
  const fxxStr = String(currentFxx).padStart(2, '0');
  const seq = ++mtnRecomputeSeq;

  statusEl.textContent = 'computing...';
  try {
    const url = `/api/hazards/mtn_obsc/${fxxStr}/recompute?threshold_pct=${threshold}` +
      `&mountainous_relief_ft=${relief}&clearance_margin_ft=${clearance}&neighborhood_radius_nm=${radius}&min_area_sq_mi=${minArea}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`MTN OBSC recompute failed (${resp.status})`);
    const geojson = await resp.json();
    if (seq !== mtnRecomputeSeq) return; // superseded while in flight

    layers.mtn.clearLayers();
    layers.mtn.addData(geojson);
    document.getElementById('legend-mtn-threshold').textContent = threshold;
    setMountainousArea(geojson);
    statusEl.textContent = '';
  } catch (err) {
    if (seq !== mtnRecomputeSeq) return;
    console.error('MTN OBSC recompute failed:', err);
    statusEl.textContent = 'error (see console)';
  }
}

// --- ADJUSTOR WIRING ---
//     One entry per hazard binds its store to its buttons and recompute.
//     Everything below is driven off this list, so a new hazard's
//     adjustor is an entry here plus its markup -- no per-hazard handlers.
const ADJUSTORS = [
  { hazard: 'ifr', store: ifrHours, body: 'adjust-ifr-body',
    apply: 'adjust-apply', applyAll: 'adjust-apply-all', revert: 'adjust-revert',
    status: 'adjust-status', recompute: recomputeCurrentSnapshot },
  { hazard: 'mtn', store: mtnHours, body: 'adjust-mtn-body',
    apply: 'adjust-mtn-apply', applyAll: 'adjust-mtn-apply-all', revert: 'adjust-mtn-revert',
    status: 'adjust-mtn-status', recompute: recomputeCurrentMtnSnapshot },
];

// Re-derive the dirty marks, hints and button states from the inputs vs
// the store. Cheap, so it runs after every input event and every store
// change rather than trying to track deltas.
function syncAdjustor(adj) {
  const applied = adj.store.get(currentFxx);
  const pending = adj.store.read();
  let anyDirty = false;

  adj.store.fields.forEach((f) => {
    const input = document.getElementById(f.input);
    const hint = document.getElementById(f.hint);
    const dirty = !!applied && applied[f.key] !== pending[f.key];
    anyDirty = anyDirty || dirty;
    input.closest('.adjust-row').classList.toggle('is-dirty', dirty);
    hint.textContent = dirty ? `was ${applied[f.key]}` : '';
  });

  const canAct = liveAdjustAvailable && currentFxx != null;
  document.getElementById(adj.apply).disabled = !(canAct && anyDirty);
  document.getElementById(adj.revert).disabled = !(canAct && anyDirty);
  // APPLY ALL HOURS is useful even when this hour is clean -- it is how a
  // setting dialled in on F06 gets pushed to F00..F12 -- so it is live
  // whenever adjustment is.
  document.getElementById(adj.applyAll).disabled = !canAct;
}

function syncAllAdjustors() {
  ADJUSTORS.forEach(syncAdjustor);
}

// APPLY: commit pending to this hour and recompute once.
async function applyAdjustor(adj) {
  if (currentFxx == null || !liveAdjustAvailable) return;
  adj.store.fields.forEach((f) => {
    const el = document.getElementById(f.input);
    normalizeStepInput(el, adj.store.get(currentFxx)?.[f.key]);
  });
  adj.store.saveCurrent(currentFxx);
  syncAdjustor(adj);
  await adj.recompute(adj.store.get(currentFxx));
}

// APPLY ALL HOURS: commit pending to every hour of the cycle, recompute
// the one on screen. The others pick their settings up when shown.
async function applyAdjustorAllHours(adj) {
  if (currentFxx == null || !liveAdjustAvailable) return;
  adj.store.fields.forEach((f) => {
    const el = document.getElementById(f.input);
    normalizeStepInput(el, adj.store.get(currentFxx)?.[f.key]);
  });
  const hours = forecastHours.length ? forecastHours : [currentFxx];
  adj.store.saveAll(hours);
  syncAdjustor(adj);
  await adj.recompute(adj.store.get(currentFxx));
  const statusEl = document.getElementById(adj.status);
  if (!statusEl.textContent) {
    statusEl.textContent = `applied to ${hours.length} hours`;
    setTimeout(() => { if (statusEl.textContent.startsWith('applied')) statusEl.textContent = ''; }, 3000);
  }
}

// REVERT: pending goes back to applied. No network.
function revertAdjustor(adj) {
  adj.store.apply(adj.store.get(currentFxx));
  syncAdjustor(adj);
}

ADJUSTORS.forEach((adj) => {
  const body = document.getElementById(adj.body);

  // Stepper buttons: +/- one step, via the input's own stepping so the
  // min/max/step attributes are the single source of bounds.
  body.querySelectorAll('.step-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const input = btn.parentElement.querySelector('.step-input');
      if (input.disabled) return;
      // A typed off-lattice value is snapped first, so a click always
      // lands on a reachable step rather than 47 -> 52.
      const field = adj.store.fields.find((f) => f.input === input.id);
      normalizeStepInput(input, adj.store.get(currentFxx)?.[field.key]);
      if (btn.dataset.step === 'up') input.stepUp(); else input.stepDown();
      syncAdjustor(adj);
    });
  });

  adj.store.fields.forEach((f) => {
    const input = document.getElementById(f.input);
    // Typing: mark dirty as they go, but do not snap until they finish --
    // snapping mid-keystroke would fight "3" on the way to "3000".
    input.addEventListener('input', () => syncAdjustor(adj));
    // Finished (blur or Enter): snap to the lattice.
    input.addEventListener('change', () => {
      normalizeStepInput(input, adj.store.get(currentFxx)?.[f.key]);
      syncAdjustor(adj);
    });
    // Enter applies. The `change` above fires first on Enter in browsers
    // that fire it, so the value is already normalized here.
    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      input.blur();
      applyAdjustor(adj);
    });
  });

  document.getElementById(adj.apply).addEventListener('click', () => applyAdjustor(adj));
  document.getElementById(adj.applyAll).addEventListener('click', () => applyAdjustorAllHours(adj));
  document.getElementById(adj.revert).addEventListener('click', () => revertAdjustor(adj));
});

// --- Reset button: reloads the ORIGINAL scheduled snapshot (its
//     committed parameters, not whatever is applied or pending). Reloading
//     re-seeds the store and the inputs, so pending edits are dropped too. ---
document.getElementById('adjust-reset').addEventListener('click', async () => {
  if (currentFxx == null) return;
  // This hour only -- other hours keep their own adjustments.
  ifrHours.clear(currentFxx);
  try {
    await loadIfrSnapshot(currentFxx, { refit: false });
  } catch (err) {
    console.error('Failed to reset to scheduled snapshot:', err);
  }
});

// --- Reset-all button: clears every hour's saved IFR settings. Only the
//     current hour is on screen so only it needs reloading -- the others
//     re-seed from their own scheduled snapshots next time they are shown,
//     which is exactly the un-adjusted state. ---
document.getElementById('adjust-reset-all').addEventListener('click', async () => {
  if (currentFxx == null) return;
  ifrHours.clearAll();
  try {
    await loadIfrSnapshot(currentFxx, { refit: false });
  } catch (err) {
    console.error('Failed to reset all forecast hours:', err);
  }
});

// --- Triggers a client-side file download from in-memory text content
//     (no server round-trip needed beyond the fetch already done) ---
function downloadTextFile(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// --- Generate button: downloads BOTH GeoJSON and XML for whatever is
//     APPLIED to this hour -- lets a forecaster dial in thresholds,
//     review the result, then hand off exactly that draft rather than
//     only ever being able to export the default scheduled version. ---
async function generateIfrFiles(statusEl) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  // APPLIED settings, not the inputs: the export must match the polygons
  // on screen, and a pending edit that was never applied is not on screen.
  const { threshold, radius, minArea } = ifrHours.get(currentFxx) || ifrHours.read();
  const fxxStr = String(currentFxx).padStart(2, '0');
  const baseName = `ifr_f${fxxStr}_t${threshold}_r${radius}_a${minArea}`;

  try {
    const baseUrl = `/api/hazards/ifr/${fxxStr}/recompute?threshold_pct=${threshold}&neighborhood_radius_nm=${radius}&min_area_sq_mi=${minArea}`;

    const geojsonResp = await fetch(baseUrl);
    if (!geojsonResp.ok) throw new Error(`GeoJSON fetch failed (${geojsonResp.status})`);
    const geojsonText = await geojsonResp.text();

    const xmlResp = await fetch(`${baseUrl}&format=xml`);
    if (!xmlResp.ok) throw new Error(`XML fetch failed (${xmlResp.status})`);
    const xmlText = await xmlResp.text();

    downloadTextFile(geojsonText, `${baseName}.geojson`, 'application/geo+json');
    downloadTextFile(xmlText, `${baseName}.xml`, 'application/xml');
  } catch (err) {
    console.error('Generate failed:', err);
    statusEl.textContent = 'IFR error (see console)';
    throw err;
  }
}

// --- MTN OBSC reset: drops THIS hour's saved settings and reloads its
//     original scheduled snapshot. Other hours keep their adjustments.
//     Same semantics as the IFR reset above. ---
document.getElementById('adjust-mtn-reset').addEventListener('click', async () => {
  if (currentFxx == null) return;
  mtnHours.clear(currentFxx);
  await loadMtnObscSnapshot(currentFxx);
});

// --- MTN OBSC reset-all: clears every hour's saved MTN settings. Only the
//     current hour is on screen so only it needs reloading -- the others
//     re-seed from their own scheduled snapshots when next shown. ---
document.getElementById('adjust-mtn-reset-all').addEventListener('click', async () => {
  if (currentFxx == null) return;
  mtnHours.clearAll();
  await loadMtnObscSnapshot(currentFxx);
});

// --- MTN OBSC generate: downloads GeoJSON + XML for whatever MTN OBSC
//     settings are applied to this hour, same as the IFR generate button. ---
async function generateMtnFiles(statusEl) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  // APPLIED settings, as in generateIfrFiles.
  const { threshold, relief, clearance, radius, minArea } = mtnHours.get(currentFxx) || mtnHours.read();
  const fxxStr = String(currentFxx).padStart(2, '0');
  const baseName = `mtn_obsc_f${fxxStr}_t${threshold}_e${relief}_c${clearance}_r${radius}_a${minArea}`;

  try {
    const baseUrl = `/api/hazards/mtn_obsc/${fxxStr}/recompute?threshold_pct=${threshold}` +
      `&mountainous_relief_ft=${relief}&clearance_margin_ft=${clearance}&neighborhood_radius_nm=${radius}&min_area_sq_mi=${minArea}`;

    const geojsonResp = await fetch(baseUrl);
    if (!geojsonResp.ok) throw new Error(`GeoJSON fetch failed (${geojsonResp.status})`);
    const geojsonText = await geojsonResp.text();

    const xmlResp = await fetch(`${baseUrl}&format=xml`);
    if (!xmlResp.ok) throw new Error(`XML fetch failed (${xmlResp.status})`);
    const xmlText = await xmlResp.text();

    downloadTextFile(geojsonText, `${baseName}.geojson`, 'application/geo+json');
    downloadTextFile(xmlText, `${baseName}.xml`, 'application/xml');
  } catch (err) {
    console.error('MTN OBSC generate failed:', err);
    statusEl.textContent = 'MTN OBSC error (see console)';
    throw err;
  }
}

// --- COMBINED PGEN EXPORT ---
//     Downloads ONE PGEN XML document holding all five forecast hours,
//     each polygonized at that hour's own settings, plus a sidecar JSON
//     recording those settings. NMAP2's Filter Control steps through hours
//     within a single file, so they have to travel together.
//
//     The sidecar exists because provenance deliberately does NOT go into
//     the XML: their parser was verified against a specific Gfa attribute
//     set and this export does not perturb it.
//
//     Hours the forecaster never opened are simply absent from the store;
//     the server fills those in from the manifest's scheduled parameters,
//     so the file always covers the whole cycle. ---
function pgenHoursPayload(store) {
  const saved = store.all();
  return Object.keys(saved).map((key) => {
    const entry = { forecast_hour: Number(key) };
    // field.prop is deliberately the API parameter name as well as the
    // GeoJSON property name -- they agree, so no translation table.
    store.fields.forEach((f) => { entry[f.prop] = saved[key][f.key]; });
    return entry;
  });
}

async function downloadPgen(hazardPath, store, statusEl, label = hazardPath) {
  statusEl.textContent = `building ${label} (all hours)...`;
  try {
    const resp = await fetch(`/api/hazards/${hazardPath}/pgen`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ hours: pgenHoursPayload(store) }),
    });
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error(`PGEN export failed (${resp.status}): ${detail}`);
    }
    const data = await resp.json();

    downloadTextFile(data.xml, data.filename, 'application/xml');
    downloadTextFile(JSON.stringify(data.sidecar, null, 2),
                     data.sidecar_filename, 'application/json');

    statusEl.textContent = `${label}: ${data.sidecar.total_gfa_elements} elements`;
    setTimeout(() => { statusEl.textContent = ''; }, 4000);
  } catch (err) {
    console.error('PGEN export failed:', err);
    statusEl.textContent = `${label} error (see console)`;
  }
}

// --- EXPORT PANEL ---
//     One panel driving both hazards, replacing the pair of export
//     buttons that used to sit inside each adjustor. The requests and
//     the routes behind them are unchanged -- this only decides which
//     hazards each click covers.
//
//     The hazard checkboxes FOLLOW layer visibility until someone edits
//     them, which is what "default to whichever layers are visible"
//     means once visibility can change after load. Editing one pins it,
//     because at that point it is a deliberate choice rather than a
//     default.
const EXPORT_HAZARDS = [
  { checkbox: 'export-hazard-ifr', visibility: 'toggle-ifr', hazardPath: 'ifr', label: 'IFR',
    store: () => ifrHours, generate: generateIfrFiles },
  { checkbox: 'export-hazard-mtn', visibility: 'toggle-mtn', hazardPath: 'mtn_obsc', label: 'MTN OBSC',
    store: () => mtnHours, generate: generateMtnFiles },
];

EXPORT_HAZARDS.forEach((entry) => {
  const box = document.getElementById(entry.checkbox);
  const visibility = document.getElementById(entry.visibility);
  box.checked = visibility.checked;
  box.addEventListener('change', () => { box.dataset.userSet = 'true'; });
  visibility.addEventListener('change', () => {
    if (box.dataset.userSet !== 'true') box.checked = visibility.checked;
  });
});

function selectedExportHazards() {
  return EXPORT_HAZARDS.filter((entry) => document.getElementById(entry.checkbox).checked);
}

document.getElementById('export-generate').addEventListener('click', async () => {
  const statusEl = document.getElementById('export-status');
  const selected = selectedExportHazards();
  if (!selected.length) { statusEl.textContent = 'pick a hazard'; return; }
  if (!liveAdjustAvailable) { statusEl.textContent = 'no cached grid to export'; return; }

  statusEl.textContent = 'generating...';
  try {
    // Sequential rather than parallel: each hazard fires two downloads,
    // and browsers throttle simultaneous ones from a single gesture.
    for (const entry of selected) {
      await entry.generate(statusEl);
    }
    statusEl.textContent = 'downloaded';
    setTimeout(() => { statusEl.textContent = ''; }, 3000);
  } catch (err) {
    // generate* already wrote the specific message and logged the error.
  }
});

document.getElementById('export-pgen').addEventListener('click', async () => {
  const statusEl = document.getElementById('export-status');
  const selected = selectedExportHazards();
  if (!selected.length) { statusEl.textContent = 'pick a hazard'; return; }
  if (!liveAdjustAvailable) { statusEl.textContent = 'no cached grid to export'; return; }

  for (const entry of selected) {
    await downloadPgen(entry.hazardPath, entry.store(), statusEl, entry.label);
  }
});


// --- Loads one specific IFR snapshot by its REQUESTED forecast hour
//     (matching the manifest's "requested_forecast_hour" and the
//     filename convention ifr_fNN.geojson), replaces the ifr layer's
//     data, and updates the valid-time/legend readouts. ---
async function loadIfrSnapshot(requestedFxx, { refit = true } = {}) {
  const fxxStr = String(requestedFxx).padStart(2, '0');
  const resp = await fetch(`/api/hazards/ifr/${fxxStr}`);
  if (!resp.ok) throw new Error(`Snapshot F${fxxStr} not available (${resp.status})`);
  const geojson = await resp.json();

  layers.ifr.clearLayers();
  layers.ifr.addData(geojson);
  currentFxx = requestedFxx;

  const firstProps = geojson.features?.[0]?.properties;
  if (firstProps) {
    document.getElementById('valid-time').textContent = formatValidTime(firstProps.valid_time);
    document.getElementById('legend-threshold').textContent = firstProps.threshold_pct ?? '?';
    document.getElementById('legend-radius').textContent = firstProps.neighborhood_radius_nm ?? '--';
    document.getElementById('legend-min-area').textContent = firstProps.min_area_sq_mi ?? '--';
    // First visit to this hour (or a reset): seed its settings from the
    // scheduled snapshot. Hours that already have saved settings go through
    // showForecastHour() and never reach here, so switching hours no longer
    // clobbers what was dialled in.
    const seeded = ifrHours.fromProps(firstProps);
    ifrHours.set(requestedFxx, seeded);
    ifrHours.apply(seeded);
  }
  syncAllAdjustors();

  // Only re-fit the view the FIRST time data loads (on subsequent
  // snapshot switches, keep whatever pan/zoom the person already has --
  // re-fitting every time they click a forecast hour would be jarring).
  if (refit && !loadIfrSnapshot._hasFitBounds && geojson.features?.length) {
    map.fitBounds(layers.ifr.getBounds(), { padding: [60, 60], maxZoom: 7 });
    loadIfrSnapshot._hasFitBounds = true;
  }
}

// --- Loads one Mountain Obscuration snapshot for the given forecast
//     hour. Deliberately tolerant of failure: if MTN OBSC data isn't
//     available (pipeline hasn't run yet, older deployment, etc.) this
//     clears the layer and returns quietly rather than throwing, so a
//     missing second hazard can never break the IFR display that
//     forecasters actually depend on. ---
async function loadMtnObscSnapshot(requestedFxx) {
  const fxxStr = String(requestedFxx).padStart(2, '0');
  try {
    const resp = await fetch(`/api/hazards/mtn_obsc/${fxxStr}`);
    if (!resp.ok) throw new Error(`MTN OBSC F${fxxStr} not available (${resp.status})`);
    const geojson = await resp.json();

    layers.mtn.clearLayers();
    layers.mtn.addData(geojson);

    const firstProps = geojson.features?.[0]?.properties;
    document.getElementById('legend-mtn-threshold').textContent =
      firstProps?.threshold_pct ?? '--';
    setMountainousArea(geojson);

    // First visit to this hour (or a reset): seed its settings from the
    // scheduled snapshot and sync the inputs to what's actually on
    // screen. Hours with saved settings go through showForecastHour() and
    // never reach here, so switching hours no longer clobbers them.
    if (firstProps) {
      const seeded = mtnHours.fromProps(firstProps);
      mtnHours.set(requestedFxx, seeded);
      mtnHours.apply(seeded);
    }
    syncAllAdjustors();
  } catch (err) {
    console.warn('Mountain Obscuration layer unavailable:', err);
    layers.mtn.clearLayers();
    document.getElementById('legend-mtn-threshold').textContent = '--';
    setMountainousArea(null);
  }
}

// --- Switches the displayed forecast hour for BOTH hazards, restoring
//     each panel's own saved settings for that hour. A hazard's hour that
//     was previously adjusted comes back at its adjusted values and is
//     recomputed to match; an hour not yet visited loads its scheduled
//     snapshot and seeds its settings from it.
//
//     currentFxx is set before either hazard starts so both recomputes
//     target the hour being switched TO, not the one being left. ---
async function showForecastHour(requestedFxx, { refit = true } = {}) {
  currentFxx = requestedFxx;
  await Promise.all([
    showIfrForHour(requestedFxx, { refit }),
    showMtnForHour(requestedFxx),
  ]);
}

async function showIfrForHour(fxx, { refit = true } = {}) {
  const saved = ifrHours.get(fxx);
  if (saved && liveAdjustAvailable) {
    ifrHours.apply(saved);
    syncAllAdjustors();
    await recomputeCurrentSnapshot(saved);
    return;
  }
  await loadIfrSnapshot(fxx, { refit });
}

// Mirrors showIfrForHour. loadMtnObscSnapshot never throws (see its
// docstring), so a missing MTN OBSC cycle still can't block the IFR switch.
async function showMtnForHour(fxx) {
  const saved = mtnHours.get(fxx);
  if (saved && liveAdjustAvailable) {
    mtnHours.apply(saved);
    syncAllAdjustors();
    await recomputeCurrentMtnSnapshot(saved);
    return;
  }
  await loadMtnObscSnapshot(fxx);
}


// --- Builds the FCST HR button row from the manifest, and wires up
//     clicking a button to switch snapshots. ---
function buildFxxSelector(manifest) {
  const container = document.getElementById('fxx-buttons');
  container.innerHTML = '';

  document.getElementById('model-cycle').textContent = formatValidTime(manifest.model_cycle);
  loadedModelCycle = manifest.model_cycle;
  updateStalenessIndicator();
  document.getElementById('nbm-source-cycle').textContent = manifest.nbm_source_cycle
    ? formatValidTime(manifest.nbm_source_cycle)
    : '--------Z'; // older manifests generated before this field existed

  forecastHours = manifest.snapshots.map((snap) => snap.requested_forecast_hour);

  manifest.snapshots.forEach((snap, i) => {
    const btn = document.createElement('button');
    btn.className = 'fxx-btn' + (i === 0 ? ' active' : '');
    btn.textContent = `F${String(snap.requested_forecast_hour).padStart(2, '0')}`;
    if (snap.substituted) {
      btn.title = `NBM has no true F${String(snap.requested_forecast_hour).padStart(2, '0')} -- showing F${String(snap.actual_forecast_hour).padStart(2, '0')} instead`;
    }
    btn.addEventListener('click', async () => {
      if (btn.classList.contains('active')) return;
      container.querySelectorAll('.fxx-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      try {
        // Both hazards share this one selector -- they're generated on
        // the same G-AIRMET forecast-hour schedule (F00/03/06/09/12), so
        // showing IFR at F06 alongside MTN OBSC at F00 would be
        // misleading. loadMtnObscSnapshot never throws (see its
        // docstring), so a missing MTN OBSC file can't block the IFR
        // switch here.
        await showForecastHour(snap.requested_forecast_hour);
      } catch (err) {
        console.error('Failed to switch forecast hour:', err);
      }
    });
    container.appendChild(btn);
  });
}

// --- Fetches the IFR manifest, waiting out a cold start.
//
// The deployed app no longer ships its data: artifacts are downloaded at
// runtime from the pipeline's data branches (see webapp/artifacts.py), so
// for the first few seconds after a restart there is genuinely nothing to
// serve and the API answers 503 "not loaded yet". Without this wait, a page
// loaded in that window would fall straight through to the demo-data path
// and keep showing SYNTHETIC polygons until someone reloaded by hand --
// which looks exactly like real data to anyone who wasn't watching. Any
// other status (or a slow-loading deploy that blows the deadline) still
// falls back as before.
async function fetchIfrManifestWhenReady() {
  const DEADLINE_MS = 120000;
  const POLL_MS = 5000;
  const startedAt = Date.now();
  while (true) {
    const resp = await fetch('/api/hazards/ifr/manifest');
    if (resp.ok) return resp.json();
    if (resp.status !== 503 || Date.now() - startedAt > DEADLINE_MS) {
      throw new Error(`manifest not available (${resp.status})`);
    }
    document.getElementById('valid-time').textContent = 'LOADING\u2026';
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

// --- Load data from our API and populate the layers ---
async function loadData() {
  try {
    const statesResp = await fetch('/api/boundaries/states');
    const statesGeoJSON = await statesResp.json();
    layers.states.addData(statesGeoJSON);
  } catch (err) {
    console.error('Failed to load state boundaries:', err);
  }

  try {
    const artccResp = await fetch('/api/boundaries/artcc');
    const artccGeoJSON = await artccResp.json();
    layers.artcc.addData(artccGeoJSON);
  } catch (err) {
    console.error('Failed to load ARTCC boundaries:', err);
  }

  // The legacy overlay is optional data: a deployment without the file
  // gets a 404 here, which disables the toggle rather than logging an
  // error the operator can do nothing about.
  try {
    const legacyResp = await fetch('/api/boundaries/legacy_mtnobsc');
    if (legacyResp.ok) {
      const legacyGeoJSON = await legacyResp.json();
      checkLegacyFeatureNames(legacyGeoJSON);
      layers.legacyMtnObsc.addData(legacyGeoJSON);
    } else {
      disableLegacyToggle('legacy_mtnobsc.json not deployed');
    }
  } catch (err) {
    disableLegacyToggle('legacy boundaries unavailable');
  }

  // Try the manifest first -- if it exists, build the forecast-hour
  // selector and load its first (shortest) snapshot. If it doesn't
  // (e.g. demo-data-only situations, or an older deployment), fall back
  // to the single default endpoint and hide the selector row entirely
  // rather than show a selector with nothing behind it.
  try {
    const manifest = await fetchIfrManifestWhenReady();
    if (!manifest.snapshots?.length) throw new Error('manifest has no snapshots');

    buildFxxSelector(manifest);
    await Promise.all([
      loadIfrSnapshot(manifest.snapshots[0].requested_forecast_hour),
      loadMtnObscSnapshot(manifest.snapshots[0].requested_forecast_hour),
    ]);
    liveAdjustAvailable = true;
    syncAllAdjustors();
  } catch (err) {
    console.warn('No forecast-hour manifest available, falling back to single snapshot:', err);
    document.getElementById('valid-time').textContent = '--------Z'; // clear any "loading" text
    document.getElementById('fxx-row').style.display = 'none';
    // Nothing cached to recompute from, so neither the adjustors nor the
    // exports can do anything. The adjustors used to be whole panels that
    // were hidden here; now they live inside their hazard rows, so the
    // equivalent is to collapse and disable the rows themselves and dim
    // the export panel -- the visibility checkboxes stay live, since
    // showing and hiding layers still works fine without a cached grid.
    disableLiveAdjust('no cached grid for this cycle -- adjustment and export need one');
    try {
      const ifrResp = await fetch('/api/hazards/ifr');
      const ifrGeoJSON = await ifrResp.json();
      layers.ifr.addData(ifrGeoJSON);
      const firstProps = ifrGeoJSON.features?.[0]?.properties;
      if (firstProps) {
        document.getElementById('valid-time').textContent = formatValidTime(firstProps.valid_time);
        document.getElementById('legend-threshold').textContent = firstProps.threshold_pct ?? '?';
        document.getElementById('legend-radius').textContent = firstProps.neighborhood_radius_nm ?? '--';
        document.getElementById('legend-min-area').textContent = firstProps.min_area_sq_mi ?? '--';
      }
      if (ifrGeoJSON.features?.length) {
        map.fitBounds(layers.ifr.getBounds(), { padding: [60, 60], maxZoom: 7 });
      }
    } catch (fallbackErr) {
      console.error('Failed to load any hazard polygons:', fallbackErr);
    }
  }
}

loadData();
