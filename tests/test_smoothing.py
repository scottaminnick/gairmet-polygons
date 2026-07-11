"""
tests/test_smoothing.py
-------------------------
Tests pipeline/smoothing.py using synthetic data with a KNOWN real-world
separation distance between two features -- lets us directly verify
that a radius smaller than the gap keeps them separate, while a radius
larger than the gap correctly merges them.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.polygons import GridSpec, grid_to_polygons
from pipeline.smoothing import gaussian_smooth, neighborhood_max_smooth


def make_two_blobs(gap_km: float, mean_lat: float = 40.0, resolution_deg: float = 0.02):
    """
    Builds a grid with two separate 80%-probability blobs, their centers
    separated by exactly gap_km (real-world distance, computed properly
    for the given latitude), with near-zero probability in between.
    """
    km_per_deg_lon = 111.32 * np.cos(np.radians(mean_lat))
    gap_deg_lon = gap_km / km_per_deg_lon

    nx, ny = 400, 150
    lons = np.arange(nx) * resolution_deg - (nx * resolution_deg) / 2
    lats = mean_lat + np.arange(ny) * resolution_deg - (ny * resolution_deg) / 2
    grid_lon, grid_lat = np.meshgrid(lons, lats)

    center1_lon = -gap_deg_lon / 2
    center2_lon = gap_deg_lon / 2

    blob_radius_deg = 0.15  # small, tight blobs so the gap dominates the picture
    blob1 = 85 * np.exp(-(((grid_lon - center1_lon) ** 2 + (grid_lat - mean_lat) ** 2) / (2 * blob_radius_deg ** 2)))
    blob2 = 85 * np.exp(-(((grid_lon - center2_lon) ** 2 + (grid_lat - mean_lat) ** 2) / (2 * blob_radius_deg ** 2)))
    values = np.maximum(blob1, blob2)

    grid_spec = GridSpec(west=lons[0], north=lats[-1], dx=resolution_deg, dy=-resolution_deg)
    # Note: our GridSpec/array convention has row 0 = north, so flip lat axis to match
    values = values[::-1, :]

    return values, grid_spec, gap_km


def count_polygons_at_threshold(values, grid_spec, threshold=50.0):
    polygons = grid_to_polygons(values, grid_spec, threshold=threshold, min_area_deg2=0.0001)
    return len(polygons)


def test_small_radius_keeps_blobs_separate():
    values, grid_spec, gap_km = make_two_blobs(gap_km=100)
    gap_nm = gap_km / 1.852

    n_before = count_polygons_at_threshold(values, grid_spec)
    print(f"Gap: {gap_km:.0f} km ({gap_nm:.0f} nm). Polygons before smoothing: {n_before}")
    assert n_before == 2, "Sanity check failed: should start as 2 separate blobs"

    small_radius_nm = gap_nm * 0.2  # much smaller than the gap
    smoothed = neighborhood_max_smooth(values, grid_spec, radius_nm=small_radius_nm)
    n_after = count_polygons_at_threshold(smoothed, grid_spec)
    print(f"Radius={small_radius_nm:.0f}nm (small): {n_after} polygon(s) after smoothing")

    assert n_after == 2, f"A radius much smaller than the gap should NOT merge the blobs, got {n_after}"
    print("[OK] Small radius correctly left the two blobs separate.\n")


def test_large_radius_merges_blobs():
    values, grid_spec, gap_km = make_two_blobs(gap_km=100)
    gap_nm = gap_km / 1.852

    large_radius_nm = gap_nm * 0.75  # large enough that both blobs' footprints overlap the gap
    smoothed = neighborhood_max_smooth(values, grid_spec, radius_nm=large_radius_nm)
    n_after = count_polygons_at_threshold(smoothed, grid_spec)
    print(f"Radius={large_radius_nm:.0f}nm (large, gap={gap_nm:.0f}nm): {n_after} polygon(s) after smoothing")

    assert n_after == 1, f"A radius large enough to span the gap SHOULD merge the blobs into one, got {n_after}"
    print("[OK] Large radius correctly merged the two blobs into one polygon.\n")


def test_gaussian_smooth_reduces_noise_without_moving_peak():
    rng = np.random.default_rng(0)
    base = 70 * np.exp(-(((np.arange(100)[:, None] - 50) ** 2 + (np.arange(100)[None, :] - 50) ** 2) / (2 * 15 ** 2)))
    noisy = base + rng.normal(0, 15, size=base.shape)

    smoothed = gaussian_smooth(noisy, sigma_cells=2.0)

    print(f"Noisy std dev: {noisy.std():.2f}, smoothed std dev: {smoothed.std():.2f}")
    assert smoothed.std() < noisy.std(), "Gaussian smoothing should reduce overall variance/noise"

    peak_before = np.unravel_index(np.argmax(base), base.shape)
    peak_after = np.unravel_index(np.argmax(smoothed), smoothed.shape)
    dist = np.hypot(peak_before[0] - peak_after[0], peak_before[1] - peak_after[1])
    print(f"Peak location shift: {dist:.1f} cells")
    assert dist < 5, "Smoothing shouldn't move the main feature's location by much"
    print("[OK] Gaussian smoothing reduced noise without relocating the main feature.\n")


if __name__ == "__main__":
    test_small_radius_keeps_blobs_separate()
    test_large_radius_merges_blobs()
    test_gaussian_smooth_reduces_noise_without_moving_peak()
    print("All manual checks passed.")
