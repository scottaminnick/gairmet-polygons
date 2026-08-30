// Minimal stand-in for Leaflet: just the surface webapp/static/map.js uses.
// Present so the rail can be driven in a browser where the CDN is blocked
// by policy -- it is not a test of Leaflet.
(function () {
  function Layer(opts) {
    this.opts = opts || {};
    this.features = [];
    this.addTo = function () { return this; };
    this.addData = function (geojson) {
      const feats = (geojson && geojson.features) || [];
      this.features = this.features.concat(feats);
      const cb = this.opts.onEachFeature;
      if (cb) feats.forEach((f) => cb(f, { bindPopup() {}, bindTooltip() {} }));
      return this;
    };
    this.clearLayers = function () { this.features = []; return this; };
    this.getBounds = function () { return [[24, -125], [50, -66]]; };
  }
  window.L = {
    map: function () {
      return {
        setView() { return this; },
        addLayer() { return this; },
        removeLayer() { return this; },
        fitBounds() { return this; },
      };
    },
    tileLayer: function () { return { addTo() { return this; } }; },
    geoJSON: function (_data, opts) { return new Layer(opts); },
  };
})();
