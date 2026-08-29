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
      const name = (feature.properties && feature.properties.name) || 'legacy area';
      const isCutout = name === 'CentralValleyCutout';
      const label = isCutout
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

// --- Tracks which forecast hour is currently displayed, so the
//     live-adjustment sliders know what to recompute against. Also
//     tracks whether live adjustment is even possible (it isn't in the
//     demo-fallback case, where there's no cached grid to recompute
//     from). ---
let currentFxx = null;
let liveAdjustAvailable = false;

// --- Simple debounce: waits `delay` ms after the LAST call before
//     actually running `fn`, so dragging a slider doesn't fire a
//     network request on every pixel of movement. ---
function debounce(fn, delay) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

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

// --- PER-FORECAST-HOUR ADJUSTMENT STATE ---
//     Each forecast hour keeps its OWN slider settings, per hazard, rather
//     than one set shared across the whole cycle. Forecasters need this:
//     overnight hours want a lower threshold for radiation fog than the
//     daytime hours do, and switching hours used to silently discard
//     whatever had been dialled in.
//
//     One store per hazard, built from a field list so IFR's three sliders
//     and MTN OBSC's four (it has clearance_margin_ft as well) get
//     identical semantics without duplicating the logic. Each field maps a
//     store key to its slider element, its live label, and the GeoJSON
//     property the scheduled snapshot carries it in.
//
//     An hour is seeded from its own scheduled snapshot the first time it
//     is shown, and remembers wherever the sliders were left from then on.
//     Keyed by zero-padded forecast hour ("00", "03").
function hourKey(fxx) {
  return String(fxx).padStart(2, '0');
}

const IFR_FIELDS = [
  { key: 'threshold', slider: 'adjust-threshold', label: 'adjust-threshold-val', prop: 'threshold_pct' },
  { key: 'radius', slider: 'adjust-radius', label: 'adjust-radius-val', prop: 'neighborhood_radius_nm' },
  { key: 'minArea', slider: 'adjust-minarea', label: 'adjust-minarea-val', prop: 'min_area_sq_mi' },
];

const MTN_FIELDS = [
  { key: 'threshold', slider: 'adjust-mtn-threshold', label: 'adjust-mtn-threshold-val', prop: 'threshold_pct' },
  { key: 'relief', slider: 'adjust-mtn-relief', label: 'adjust-mtn-relief-val', prop: 'mountainous_relief_ft' },
  { key: 'clearance', slider: 'adjust-mtn-clearance', label: 'adjust-mtn-clearance-val', prop: 'clearance_margin_ft' },
  { key: 'radius', slider: 'adjust-mtn-radius', label: 'adjust-mtn-radius-val', prop: 'neighborhood_radius_nm' },
  { key: 'minArea', slider: 'adjust-mtn-minarea', label: 'adjust-mtn-minarea-val', prop: 'min_area_sq_mi' },
];

function makeHourStore(fields) {
  const byHour = {};
  return {
    fields,

    // The hazard's sliders, read as numbers, as one settings object.
    read() {
      const out = {};
      fields.forEach((f) => { out[f.key] = Number(document.getElementById(f.slider).value); });
      return out;
    },

    // Push a settings object onto the sliders and their live labels.
    apply(settings) {
      if (!settings) return;
      fields.forEach((f) => {
        if (settings[f.key] == null) return;
        document.getElementById(f.slider).value = settings[f.key];
        document.getElementById(f.label).textContent = settings[f.key];
      });
    },

    // Starting settings for an hour, from its scheduled snapshot's
    // properties.
    //
    // A property the snapshot does not carry falls back to the slider's
    // DEFAULT, not to where the slider currently sits. This path is also
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
        const el = document.getElementById(f.slider);
        out[f.key] = props[f.prop] ?? Number(el.defaultValue);
      });
      return out;
    },

    get(fxx) { return byHour[hourKey(fxx)]; },
    set(fxx, settings) { byHour[hourKey(fxx)] = settings; },

    // Persist the sliders against an hour. Called on every slider input --
    // NOT debounced, so a fast switch away cannot lose the value.
    saveCurrent(fxx) {
      if (fxx == null) return;
      byHour[hourKey(fxx)] = this.read();
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
//     with whatever the sliders currently say, and swaps in the
//     result. Does NOT re-fit the map view or touch the fxx button
//     state -- this is the same forecast hour, just re-drawn with
//     different parameters. ---
async function recomputeCurrentSnapshot(settings = null) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  // Slider-driven calls pass nothing and read the DOM; hour switches pass
  // that hour's stored settings explicitly.
  const { threshold, radius, minArea } = settings || ifrHours.read();
  const statusEl = document.getElementById('adjust-status');
  const fxxStr = String(currentFxx).padStart(2, '0');

  statusEl.textContent = 'computing...';
  try {
    const url = `/api/hazards/ifr/${fxxStr}/recompute?threshold_pct=${threshold}&neighborhood_radius_nm=${radius}&min_area_sq_mi=${minArea}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`recompute failed (${resp.status})`);
    const geojson = await resp.json();

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
    console.error('Recompute failed:', err);
    statusEl.textContent = 'error (see console)';
  }
}

const debouncedRecompute = debounce(recomputeCurrentSnapshot, 300);

// --- Same idea as recomputeCurrentSnapshot() above, but for the
//     Mountain Obscuration layer and its own sliders. Kept as a separate
//     function (and a separate panel) rather than having one set of
//     sliders drive both layers: MTN OBSC has a parameter IFR doesn't
//     (clearance_margin_ft), and the two hazards are genuinely tuned
//     independently -- a forecaster dialing in an IFR threshold
//     shouldn't silently move the mountain obscuration boundaries too. ---
// --- The mountainous-area readout under the RELIEF slider.
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
    el.title = 'not measured in this snapshot -- move a slider to recompute';
    return;
  }
  el.textContent = sqMi >= 1e6
    ? `${(sqMi / 1e6).toFixed(2)}M mi\u00b2`
    : `${Math.round(sqMi).toLocaleString()} mi\u00b2`;
  el.title = `${Math.round(sqMi).toLocaleString()} sq mi of the grid is mountainous at this relief threshold`;
}

async function recomputeCurrentMtnSnapshot(settings = null) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  // Slider-driven calls pass nothing and read the DOM; hour switches pass
  // that hour's stored settings explicitly.
  const { threshold, relief, clearance, radius, minArea } = settings || mtnHours.read();
  const statusEl = document.getElementById('adjust-mtn-status');
  const fxxStr = String(currentFxx).padStart(2, '0');

  statusEl.textContent = 'computing...';
  try {
    const url = `/api/hazards/mtn_obsc/${fxxStr}/recompute?threshold_pct=${threshold}` +
      `&mountainous_relief_ft=${relief}&clearance_margin_ft=${clearance}&neighborhood_radius_nm=${radius}&min_area_sq_mi=${minArea}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`MTN OBSC recompute failed (${resp.status})`);
    const geojson = await resp.json();

    layers.mtn.clearLayers();
    layers.mtn.addData(geojson);
    document.getElementById('legend-mtn-threshold').textContent = threshold;
    setMountainousArea(geojson);
    statusEl.textContent = '';
  } catch (err) {
    console.error('MTN OBSC recompute failed:', err);
    statusEl.textContent = 'error (see console)';
  }
}

const debouncedMtnRecompute = debounce(recomputeCurrentMtnSnapshot, 300);

// --- Wire up the three sliders: update the live numeric label
//     immediately (feels responsive even before the network call
//     resolves), and debounce the actual recompute. ---
document.getElementById('adjust-threshold').addEventListener('input', (e) => {
  document.getElementById('adjust-threshold-val').textContent = e.target.value;
  ifrHours.saveCurrent(currentFxx);
  debouncedRecompute();
});
document.getElementById('adjust-radius').addEventListener('input', (e) => {
  document.getElementById('adjust-radius-val').textContent = e.target.value;
  ifrHours.saveCurrent(currentFxx);
  debouncedRecompute();
});
document.getElementById('adjust-minarea').addEventListener('input', (e) => {
  document.getElementById('adjust-minarea-val').textContent = e.target.value;
  ifrHours.saveCurrent(currentFxx);
  debouncedRecompute();
});

// --- MTN OBSC sliders: same pattern as the IFR ones above (immediate
//     label update, debounced recompute). ---
[
  ['adjust-mtn-threshold', 'adjust-mtn-threshold-val'],
  ['adjust-mtn-relief', 'adjust-mtn-relief-val'],
  ['adjust-mtn-clearance', 'adjust-mtn-clearance-val'],
  ['adjust-mtn-radius', 'adjust-mtn-radius-val'],
  ['adjust-mtn-minarea', 'adjust-mtn-minarea-val'],
].forEach(([sliderId, labelId]) => {
  document.getElementById(sliderId).addEventListener('input', (e) => {
    document.getElementById(labelId).textContent = e.target.value;
    mtnHours.saveCurrent(currentFxx);
    debouncedMtnRecompute();
  });
});

// --- Reset button: reloads the ORIGINAL scheduled snapshot (its
//     committed parameters, not whatever the sliders currently say). ---
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

// --- Generate button: downloads BOTH GeoJSON and XML for whatever the
//     sliders currently say -- lets a forecaster dial in thresholds,
//     review the result, then hand off exactly that draft rather than
//     only ever being able to export the default scheduled version. ---
async function generateIfrFiles(statusEl) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  const threshold = document.getElementById('adjust-threshold').value;
  const radius = document.getElementById('adjust-radius').value;
  const minArea = document.getElementById('adjust-minarea').value;
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

// --- MTN OBSC generate: downloads GeoJSON + XML for whatever the MTN
//     sliders currently say, same as the IFR generate button. ---
async function generateMtnFiles(statusEl) {
  if (currentFxx == null || !liveAdjustAvailable) return;

  const threshold = document.getElementById('adjust-mtn-threshold').value;
  const relief = document.getElementById('adjust-mtn-relief').value;
  const clearance = document.getElementById('adjust-mtn-clearance').value;
  const radius = document.getElementById('adjust-mtn-radius').value;
  const minArea = document.getElementById('adjust-mtn-minarea').value;
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
    // scheduled snapshot and sync the sliders to what's actually on
    // screen. Hours with saved settings go through showForecastHour() and
    // never reach here, so switching hours no longer clobbers them.
    if (firstProps) {
      const seeded = mtnHours.fromProps(firstProps);
      mtnHours.set(requestedFxx, seeded);
      mtnHours.apply(seeded);
    }
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
  document.getElementById('nbm-source-cycle').textContent = manifest.nbm_source_cycle
    ? formatValidTime(manifest.nbm_source_cycle)
    : '--------Z'; // older manifests generated before this field existed

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
      layers.legacyMtnObsc.addData(await legacyResp.json());
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
