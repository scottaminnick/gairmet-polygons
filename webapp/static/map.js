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
      layer.bindPopup(
        `<div><strong>${p.hazard || 'IFR'}</strong></div>` +
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
};

layers.states.addTo(map);
layers.ifr.addTo(map);

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

// toggle-mtn is intentionally disabled in the HTML until Mountain
// Obscuration is implemented -- see project README.

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

// --- Sets the three sliders (and their live labels) to match a
//     snapshot's actual parameters -- used both on initial load and
//     when switching forecast hours, so the sliders always reflect
//     what's actually on screen rather than leftover values from
//     fiddling with a different snapshot. ---
function updateSlidersFromProps(props) {
  if (!props) return;
  const thresholdEl = document.getElementById('adjust-threshold');
  const radiusEl = document.getElementById('adjust-radius');
  const minAreaEl = document.getElementById('adjust-minarea');

  if (props.threshold_pct != null) {
    thresholdEl.value = props.threshold_pct;
    document.getElementById('adjust-threshold-val').textContent = props.threshold_pct;
  }
  if (props.neighborhood_radius_nm != null) {
    radiusEl.value = props.neighborhood_radius_nm;
    document.getElementById('adjust-radius-val').textContent = props.neighborhood_radius_nm;
  }
  if (props.min_area_sq_mi != null) {
    minAreaEl.value = props.min_area_sq_mi;
    document.getElementById('adjust-minarea-val').textContent = props.min_area_sq_mi;
  }
}

// --- Re-processes the CURRENTLY selected forecast hour's cached grid
//     with whatever the sliders currently say, and swaps in the
//     result. Does NOT re-fit the map view or touch the fxx button
//     state -- this is the same forecast hour, just re-drawn with
//     different parameters. ---
async function recomputeCurrentSnapshot() {
  if (currentFxx == null || !liveAdjustAvailable) return;

  const threshold = document.getElementById('adjust-threshold').value;
  const radius = document.getElementById('adjust-radius').value;
  const minArea = document.getElementById('adjust-minarea').value;
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

// --- Wire up the three sliders: update the live numeric label
//     immediately (feels responsive even before the network call
//     resolves), and debounce the actual recompute. ---
document.getElementById('adjust-threshold').addEventListener('input', (e) => {
  document.getElementById('adjust-threshold-val').textContent = e.target.value;
  debouncedRecompute();
});
document.getElementById('adjust-radius').addEventListener('input', (e) => {
  document.getElementById('adjust-radius-val').textContent = e.target.value;
  debouncedRecompute();
});
document.getElementById('adjust-minarea').addEventListener('input', (e) => {
  document.getElementById('adjust-minarea-val').textContent = e.target.value;
  debouncedRecompute();
});

// --- Reset button: reloads the ORIGINAL scheduled snapshot (its
//     committed parameters, not whatever the sliders currently say). ---
document.getElementById('adjust-reset').addEventListener('click', async () => {
  if (currentFxx == null) return;
  try {
    await loadIfrSnapshot(currentFxx, { refit: false });
  } catch (err) {
    console.error('Failed to reset to scheduled snapshot:', err);
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
    updateSlidersFromProps(firstProps);
  }

  // Only re-fit the view the FIRST time data loads (on subsequent
  // snapshot switches, keep whatever pan/zoom the person already has --
  // re-fitting every time they click a forecast hour would be jarring).
  if (refit && !loadIfrSnapshot._hasFitBounds && geojson.features?.length) {
    map.fitBounds(layers.ifr.getBounds(), { padding: [60, 60], maxZoom: 7 });
    loadIfrSnapshot._hasFitBounds = true;
  }
}

// --- Builds the FCST HR button row from the manifest, and wires up
//     clicking a button to switch snapshots. ---
function buildFxxSelector(manifest) {
  const container = document.getElementById('fxx-buttons');
  container.innerHTML = '';

  document.getElementById('model-cycle').textContent = formatValidTime(manifest.model_cycle);

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
        await loadIfrSnapshot(snap.requested_forecast_hour);
      } catch (err) {
        console.error('Failed to switch forecast hour:', err);
      }
    });
    container.appendChild(btn);
  });
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

  // Try the manifest first -- if it exists, build the forecast-hour
  // selector and load its first (shortest) snapshot. If it doesn't
  // (e.g. demo-data-only situations, or an older deployment), fall back
  // to the single default endpoint and hide the selector row entirely
  // rather than show a selector with nothing behind it.
  try {
    const manifestResp = await fetch('/api/hazards/ifr/manifest');
    if (!manifestResp.ok) throw new Error(`manifest not available (${manifestResp.status})`);
    const manifest = await manifestResp.json();
    if (!manifest.snapshots?.length) throw new Error('manifest has no snapshots');

    buildFxxSelector(manifest);
    await loadIfrSnapshot(manifest.snapshots[0].requested_forecast_hour);
    liveAdjustAvailable = true;
  } catch (err) {
    console.warn('No forecast-hour manifest available, falling back to single snapshot:', err);
    document.getElementById('fxx-row').style.display = 'none';
    document.getElementById('adjust-panel').style.display = 'none'; // nothing cached to recompute from
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
