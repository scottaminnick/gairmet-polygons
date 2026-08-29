"""
Tests for webapp/artifacts.py -- the runtime fetch layer that replaced
committing output/ back to main.

Everything here runs against a throwaway local HTTP server standing in
for raw.githubusercontent.com, with synthetic artifacts: no network, and
deliberately no dependency on output/ holding anything, since output/ is
untracked now and is empty in CI.

Deliberately no fastapi TestClient: that needs httpx, which is NOT in
requirements.txt, and this suite installs only the deployed (light) set
-- see .github/workflows/tests.yml. These call the fetch layer directly
instead, which is where all the logic worth protecting lives anyway.
"""
import http.server
import importlib
import json
import shutil
import socketserver
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _manifest(prefix, cycle, hours=(0, 3, 6)):
    return {
        "model_cycle": cycle,
        "nbm_source_cycle": "2026-08-19T03:00:00Z",
        "snapshots": [
            {
                "requested_forecast_hour": h,
                "filename": f"{prefix}_f{h:02d}.geojson",
                "cache_filename": f"{prefix}_f{h:02d}_grid.npz",
            }
            for h in hours
        ],
    }


def _publish(remote_root, branch, prefix, cycle):
    """Write one hazard's synthetic artifact set to the fake data branch."""
    branch_dir = remote_root / "testowner" / "testrepo" / branch
    if branch_dir.exists():
        shutil.rmtree(branch_dir)
    branch_dir.mkdir(parents=True)
    manifest = _manifest(prefix, cycle)
    (branch_dir / f"{prefix}_manifest.json").write_text(json.dumps(manifest))
    for snapshot in manifest["snapshots"]:
        (branch_dir / snapshot["filename"]).write_text(
            json.dumps({"type": "FeatureCollection", "features": [], "cycle": cycle})
        )
        # Stands in for the real .npz grid -- the fetch layer only ever
        # moves these bytes around, it never parses them.
        (branch_dir / snapshot["cache_filename"]).write_bytes(f"{prefix}-{cycle}".encode())
    return manifest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A fake data-branch host plus a freshly configured artifacts module."""
    remote_root = tmp_path / "remote"
    remote_root.mkdir()
    serve_dir = str(remote_root)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=serve_dir, **kwargs)

        def log_message(self, *args):
            pass

    httpd = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    local_output = tmp_path / "output"
    local_output.mkdir()
    monkeypatch.setenv("ARTIFACT_RAW_BASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    monkeypatch.setenv("ARTIFACT_REPO_OWNER", "testowner")
    monkeypatch.setenv("ARTIFACT_REPO_NAME", "testrepo")
    monkeypatch.setenv("ARTIFACT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("ARTIFACT_LOCAL_OUTPUT_DIR", str(local_output))
    monkeypatch.setenv("ARTIFACT_HTTP_TIMEOUT_SECONDS", "10")

    from webapp import artifacts

    importlib.reload(artifacts)  # config is read at import time

    yield artifacts, remote_root, local_output, httpd

    httpd.shutdown()
    httpd.server_close()


def test_fetches_and_serves_a_published_cycle(env):
    artifacts, remote, _local, _httpd = env
    _publish(remote, "data-ifr", "ifr", "2026-08-19T09:00:00Z")

    assert artifacts.load_manifest("ifr") is None  # nothing loaded before the first refresh
    assert artifacts.refresh_hazard("ifr") is True

    manifest = artifacts.load_manifest("ifr")
    assert manifest["model_cycle"] == "2026-08-19T09:00:00Z"
    for name in ("ifr_f00.geojson", "ifr_f00_grid.npz", "ifr_f06_grid.npz"):
        assert artifacts.artifact_path("ifr", name) is not None, name


def test_only_downloads_when_the_cycle_changes(env):
    artifacts, remote, _local, _httpd = env
    _publish(remote, "data-ifr", "ifr", "2026-08-19T09:00:00Z")
    assert artifacts.refresh_hazard("ifr") is True
    # Same cycle still published -- polling must be a no-op, not a
    # 20 MB re-download every five minutes.
    assert artifacts.refresh_hazard("ifr") is False

    _publish(remote, "data-ifr", "ifr", "2026-08-19T15:00:00Z")
    assert artifacts.refresh_hazard("ifr") is True
    assert artifacts.load_manifest("ifr")["model_cycle"] == "2026-08-19T15:00:00Z"


def test_hazards_stay_isolated_from_each_other(env):
    """Each hazard publishes to its own branch; neither may see the other's files."""
    artifacts, remote, _local, _httpd = env
    _publish(remote, "data-ifr", "ifr", "2026-08-19T09:00:00Z")
    _publish(remote, "data-mtnobsc", "mtn_obsc", "2026-08-19T09:00:00Z")
    artifacts.refresh_all()

    assert artifacts.artifact_path("ifr", "mtn_obsc_f00.geojson") is None
    assert artifacts.artifact_path("mtn_obsc", "ifr_f00.geojson") is None


def test_failed_refresh_keeps_the_last_good_cycle(env):
    """
    The whole point of staging + swap: a refresh that can't reach GitHub
    must leave the previous cycle serving, not blank the site out.
    """
    artifacts, remote, _local, httpd = env
    _publish(remote, "data-ifr", "ifr", "2026-08-19T09:00:00Z")
    assert artifacts.refresh_hazard("ifr") is True

    httpd.shutdown()  # GitHub is now unreachable
    httpd.server_close()  # refuse connections outright rather than hanging until the timeout
    assert artifacts.refresh_hazard("ifr") is False

    assert artifacts.load_manifest("ifr")["model_cycle"] == "2026-08-19T09:00:00Z"
    assert artifacts.artifact_path("ifr", "ifr_f00_grid.npz") is not None
    state = artifacts.status()["hazards"]["ifr"]
    assert state["last_error"]  # and the failure is visible in /api/data/status
    assert state["model_cycle"] == "2026-08-19T09:00:00Z"


def test_missing_data_branch_is_reported_not_raised(env):
    """Before the pipeline's first publish the branch 404s -- an expected state, not a crash."""
    artifacts, _remote, _local, _httpd = env
    assert artifacts.refresh_hazard("ifr") is False
    assert artifacts.load_manifest("ifr") is None
    assert "404" in artifacts.status()["hazards"]["ifr"]["last_error"]
    assert "not loaded yet" in artifacts.not_loaded_detail("ifr")


def test_local_output_wins_over_fetched_data(env):
    """Local dev: a pipeline run into output/ is what the local app serves."""
    artifacts, remote, local, _httpd = env
    _publish(remote, "data-ifr", "ifr", "2026-08-19T09:00:00Z")
    (local / "ifr_manifest.json").write_text(json.dumps(_manifest("ifr", "LOCAL-CYCLE")))

    assert artifacts.hazard_dir("ifr") == local
    assert artifacts.refresh_hazard("ifr") is False  # no download spent on unused data
    assert artifacts.load_manifest("ifr")["model_cycle"] == "LOCAL-CYCLE"
    assert artifacts.status()["hazards"]["ifr"]["source"] == "local"

    # ...and only for the hazard that actually has local output.
    assert artifacts.hazard_dir("mtn_obsc") != local


@pytest.mark.parametrize(
    "filename",
    ["../../etc/passwd", "/etc/passwd", "sub/dir/grid.npz"],
)
def test_artifact_path_refuses_to_escape_the_hazard_directory(env, filename):
    """Filenames reach artifact_path() from URL path params, not just the trusted manifest."""
    artifacts, remote, _local, _httpd = env
    _publish(remote, "data-ifr", "ifr", "2026-08-19T09:00:00Z")
    artifacts.refresh_hazard("ifr")

    assert artifacts.artifact_path("ifr", filename) is None
