"""
webapp/tango_routes.py
------------------------
HTTP routes for the TANGO-category layers (strong surface wind and LLWS
potential). Registered from webapp/main.py by register_tango_routes(app),
before the StaticFiles mount, so these take priority over static files.

Per layer, three GET routes (key is "sfc_wind" or "llws"):

    /api/hazards/{key}/manifest          the loaded manifest
    /api/hazards/{key}/{fxx}             the stored GeoJSON snapshot
    /api/hazards/{key}/{fxx}/recompute   Phase B re-run from the cached grid

THE PATHS ARE LITERAL, NOT /api/hazards/{key}/... WITH A PATH PARAMETER.
The two layers are generated in a loop, but each route is registered with
its own literal string. A {key} parameter would also match "ifr" and
"mtn_obsc" and so compete with their routes, whose behaviour must not
depend on registration order; with literal paths the only strings these
routes can match are the ones listed above. (Registration order inside
this module still matters, in the usual way: /manifest goes before /{fxx}
so a generic string parameter never swallows the word "manifest".)

No PGEN or XML export: the export format for TANGO is undecided, so these
routes have no `format` parameter and the PGEN request path does not know
these layers.

The recompute parameters keep the names polygonize_tango_grid() and the
manifests use, so a client can send back what it read. Each is bounded,
and out of range is a 422 from FastAPI's validation rather than a
computation -- see the bounds below for why they exist.
"""

from datetime import datetime

from fastapi import HTTPException, Query
from fastapi.responses import FileResponse

from webapp import artifacts

# Imported at module level because the layer configs supply the DEFAULTS and
# BOUNDS of the query parameters, which FastAPI evaluates when each route is
# declared (the same reason main.py imports MOUNTAINOUS_RELIEF_THRESHOLD_FT at
# module level). They are plain dataclasses; the heavy work -- polygonize and
# grid loading -- is imported inside the handler, as in recompute_ifr_snapshot.
from pipeline.hazards.llws import LLWS
from pipeline.hazards.sfc_wind import SFC_WIND

LAYERS = (SFC_WIND, LLWS)

# Bounds that do NOT come from the layer config. They exist so one request
# cannot ask for an absurd amount of work, and so nonsense is refused before
# it is computed:
#   * smoothing sigma 0-3 cells: past 3 the Gaussian is wider than the
#     features, and the sigma is a kernel size, so cost grows with it;
#   * radius 0-150 nm: the closing runs two distance transforms over a
#     2.7M-cell grid and a radius past ~150 nm joins whole regions;
#   * minimum area 0-10,000 sq mi: above that nothing survives anyway.
# The speed threshold is bounded by the layer's own threshold_range_kt, the
# range the viewer's slider is meant to offer.
SMOOTH_SIGMA_RANGE = (0.0, 3.0)
NEIGHBORHOOD_RADIUS_RANGE_NM = (0.0, 150.0)
MIN_AREA_RANGE_SQ_MI = (0.0, 10000.0)


def register_tango_routes(app) -> None:
    # Imported here, not at module level: webapp.main imports this module,
    # so a module-level import back would be circular. By the time main.py
    # calls this function both helpers are defined.
    from webapp.main import _find_snapshot, _require_manifest

    for layer in LAYERS:
        _register_layer(app, layer, _require_manifest, _find_snapshot)


def _register_layer(app, layer, require_manifest, find_snapshot) -> None:
    key = layer.key

    def get_manifest():
        require_manifest(key)
        return FileResponse(artifacts.manifest_path(key), media_type="application/json")

    def lookup(fxx):
        manifest = require_manifest(key)
        snapshot = find_snapshot(manifest, fxx)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"No {layer.phenomenon} snapshot for F{fxx}")
        return manifest, snapshot

    def get_snapshot(fxx: str):
        _manifest, snapshot = lookup(fxx)
        path = artifacts.artifact_path(key, snapshot["filename"])
        if path is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Snapshot {snapshot['filename']} is named in the manifest but isn't on disk -- "
                    f"{artifacts.not_loaded_detail(key)}"
                ),
            )
        return FileResponse(path, media_type="application/geo+json")

    def recompute(
        fxx: str,
        speed_threshold_kt: float = Query(layer.default_threshold_kt, ge=layer.threshold_range_kt[0], le=layer.threshold_range_kt[1]),
        smooth_sigma_cells: float = Query(layer.default_smooth_sigma_cells, ge=SMOOTH_SIGMA_RANGE[0], le=SMOOTH_SIGMA_RANGE[1]),
        neighborhood_radius_nm: float = Query(
            layer.default_neighborhood_radius_nm,
            ge=NEIGHBORHOOD_RADIUS_RANGE_NM[0],
            le=NEIGHBORHOOD_RADIUS_RANGE_NM[1],
        ),
        min_area_sq_mi: float = Query(layer.default_min_area_sq_mi, ge=MIN_AREA_RANGE_SQ_MI[0], le=MIN_AREA_RANGE_SQ_MI[1]),
    ):
        """
        Live parameter adjustment: Phase B only, from the cached raw speed
        grid (knots x 2 in the file, restored to knots on load). No network,
        no NBM. The first request after a restart also pays for rasterizing
        the ARTCC boundary onto the grid (memoized after that, see
        pipeline.boundaries.get_boundary_mask).
        """
        manifest, snapshot = lookup(fxx)

        cache_filename = snapshot.get("cache_filename")
        if not cache_filename:
            raise HTTPException(status_code=404, detail=f"No cached grid for F{fxx}")
        cache_path = artifacts.artifact_path(key, cache_filename)
        if cache_path is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Cached grid {cache_filename} is named in the manifest but isn't on disk -- "
                    f"{artifacts.not_loaded_detail(key)}"
                ),
            )

        # Imported here rather than at module level, for the reason given in
        # recompute_ifr_snapshot(): keep this module's own top-level imports
        # minimal and obviously safe on Railway. These are the light-set
        # libraries (numpy/scipy/shapely), never the GRIB2 stack.
        from pipeline.hazards.tango_common import CACHE_GRID_KEY, CACHE_SCALE, polygonize_tango_grid
        from pipeline.polygons import load_grid_cache

        outdated = (
            f"Cached grid for F{fxx} is in an outdated format. This happens when new pipeline code "
            f"deploys before the next scheduled run regenerates the cache -- manually trigger the "
            f"'Generate Latest TANGO (Surface Wind + LLWS) Polygons' workflow to fix."
        )
        try:
            grids, grid_spec = load_grid_cache(cache_path, scale=CACHE_SCALE)
        except ValueError as err:  # saved with a different scale than this code expects
            raise HTTPException(status_code=409, detail=f"{outdated} ({err})")
        if CACHE_GRID_KEY not in grids:
            raise HTTPException(
                status_code=409,
                detail=f"{outdated} (found keys: {sorted(grids.keys())}, expected ['{CACHE_GRID_KEY}'])",
            )

        model_cycle = datetime.fromisoformat(manifest["model_cycle"].rstrip("Z"))
        return polygonize_tango_grid(
            grids[CACHE_GRID_KEY],
            grid_spec,
            layer,
            model_cycle,
            snapshot["requested_forecast_hour"],
            speed_threshold_kt=speed_threshold_kt,
            smooth_sigma_cells=smooth_sigma_cells,
            neighborhood_radius_nm=neighborhood_radius_nm,
            min_area_sq_mi=min_area_sq_mi,
        )

    # LITERAL paths, /manifest before /{fxx}. Distinct names so the three
    # handlers of one layer, and the two layers, never collide in the
    # OpenAPI schema.
    app.add_api_route(f"/api/hazards/{key}/manifest", get_manifest, methods=["GET"], name=f"get_{key}_manifest")
    app.add_api_route(f"/api/hazards/{key}/{{fxx}}/recompute", recompute, methods=["GET"], name=f"recompute_{key}_snapshot")
    app.add_api_route(f"/api/hazards/{key}/{{fxx}}", get_snapshot, methods=["GET"], name=f"get_{key}_snapshot")
