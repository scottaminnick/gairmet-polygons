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

// toggle-mtn and toggle-artcc are intentionally disabled in the HTML
// until those data sources exist -- see project README.

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
    const ifrResp = await fetch('/api/hazards/demo');
    const ifrGeoJSON = await ifrResp.json();
    layers.ifr.addData(ifrGeoJSON);

    // Pull the valid time / threshold from the first feature's properties
    // to populate the top-left readout and legend, so they're never
    // hardcoded out of sync with the actual data being shown.
    const firstProps = ifrGeoJSON.features?.[0]?.properties;
    if (firstProps) {
      document.getElementById('valid-time').textContent = formatValidTime(firstProps.valid_time);
      document.getElementById('legend-threshold').textContent = firstProps.threshold_pct ?? '?';
    }

    // Zoom to fit the hazard polygons so first-time viewers immediately
    // see something instead of an empty CONUS view.
    if (ifrGeoJSON.features?.length) {
      map.fitBounds(layers.ifr.getBounds(), { padding: [60, 60], maxZoom: 7 });
    }
  } catch (err) {
    console.error('Failed to load demo hazard polygons:', err);
  }
}

loadData();
