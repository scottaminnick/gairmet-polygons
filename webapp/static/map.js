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

// --- Loads one specific IFR snapshot by its REQUESTED forecast hour
//     (matching the manifest's "requested_forecast_hour" and the
//     filename convention ifr_fNN.geojson), replaces the ifr layer's
//     data, and updates the valid-time/legend readouts. ---
async function loadIfrSnapshot(requestedFxx) {
  const fxxStr = String(requestedFxx).padStart(2, '0');
  const resp = await fetch(`/api/hazards/ifr/${fxxStr}`);
  if (!resp.ok) throw new Error(`Snapshot F${fxxStr} not available (${resp.status})`);
  const geojson = await resp.json();

  layers.ifr.clearLayers();
  layers.ifr.addData(geojson);

  const firstProps = geojson.features?.[0]?.properties;
  if (firstProps) {
    document.getElementById('valid-time').textContent = formatValidTime(firstProps.valid_time);
    document.getElementById('legend-threshold').textContent = firstProps.threshold_pct ?? '?';
    document.getElementById('legend-radius').textContent = firstProps.neighborhood_radius_nm ?? '--';
    document.getElementById('legend-min-area').textContent = firstProps.min_area_sq_mi ?? '--';
  }

  // Only re-fit the view the FIRST time data loads (on subsequent
  // snapshot switches, keep whatever pan/zoom the person already has --
  // re-fitting every time they click a forecast hour would be jarring).
  if (!loadIfrSnapshot._hasFitBounds && geojson.features?.length) {
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
  } catch (err) {
    console.warn('No forecast-hour manifest available, falling back to single snapshot:', err);
    document.getElementById('fxx-row').style.display = 'none';
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
