"""
webapp/artifacts.py
-------------------
Runtime artifact fetching. The pipeline's output (per-forecast-hour
GeoJSON, the cached .npz probability grids, and the manifest) used to
arrive here by being committed back to main and redeployed with the app;
this module fetches it over HTTP instead.

Why: .npz files don't delta-compress, so committing ~20 MB of fresh
grids to main 8x/day stored FULL copies every time. main reached 2.2 GB
against a 23.5 MB working tree and Railway began failing at its
"Snapshot code" step, unable to finish the clone. Each hazard workflow
now force-pushes its artifacts to its own single-commit data branch
(rebuilt from scratch every run, so it never grows), and the deployed
app pulls from there.

How it works:

  * A daemon thread polls each hazard's manifest at
    https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{manifest}
    on startup and every REFRESH_INTERVAL_SECONDS after.
  * If the manifest's model_cycle differs from what's loaded, that
    hazard's whole file set is downloaded into a staging directory and
    swapped in with a directory rename -- so a request never sees a
    half-downloaded cycle, and a fetch that dies partway through leaves
    the previous good cycle exactly where it was.
  * The cache lives on the container filesystem and is deliberately
    disposable: nothing here needs to survive a restart, since a fresh
    container just re-downloads on startup.

Three properties this file is specifically shaped around:

  1. Startup must not block. The initial download is ~20 MB and takes
     real seconds; Railway's health check must not be waiting on it, so
     the very first fetch happens on the background thread like every
     other one, and the app serves a "not loaded yet" state until it
     lands (see not_loaded_detail()).
  2. A failed refresh must not lose data. Every failure path leaves the
     currently-loaded cycle in place and simply retries on the next
     tick -- the site keeps serving the last good cycle rather than
     going blank because GitHub had a bad minute.
  3. Local development must be unaffected. A local pipeline run still
     writes to output/ exactly as it always did, and anything present
     there WINS over fetched data (see hazard_dir()), so running the
     pipeline locally and then the web app shows your own output, not
     production's.
"""

import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Configuration -------------------------------------------------------
# All of it lives here rather than being spread across the endpoints, so
# pointing a fork/staging deploy at different branches or a different
# repo is one set of environment variables and no code change.

REPO_OWNER = os.environ.get("ARTIFACT_REPO_OWNER", "scottaminnick")
REPO_NAME = os.environ.get("ARTIFACT_REPO_NAME", "gairmet-polygons")
RAW_BASE_URL = os.environ.get("ARTIFACT_RAW_BASE_URL", "https://raw.githubusercontent.com").rstrip("/")

REFRESH_INTERVAL_SECONDS = float(os.environ.get("ARTIFACT_REFRESH_SECONDS", "300"))
HTTP_TIMEOUT_SECONDS = float(os.environ.get("ARTIFACT_HTTP_TIMEOUT_SECONDS", "60"))

# Ephemeral by design -- see the module docstring. Kept inside the repo
# directory (and gitignored) rather than /tmp so it's obvious where the
# downloads went when debugging a container.
CACHE_DIR = Path(os.environ.get("ARTIFACT_CACHE_DIR", str(BASE_DIR / ".artifact-cache")))

# Where a local pipeline run writes. Untracked now, but still the same
# path it always was.
LOCAL_OUTPUT_DIR = Path(os.environ.get("ARTIFACT_LOCAL_OUTPUT_DIR", str(BASE_DIR / "output")))

# Escape hatch for offline/local work: ARTIFACT_FETCH_ENABLED=0 keeps
# the app serving whatever is already on disk and never touches the
# network. Not normally needed even locally, since local output/ already
# takes precedence over fetched data.
FETCH_ENABLED = os.environ.get("ARTIFACT_FETCH_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Hazard:
    """Static description of one hazard's published artifact set."""

    key: str  # "ifr" -- also the cache subdirectory name
    label: str
    branch: str
    manifest_name: str


HAZARDS = {
    h.key: h
    for h in (
        Hazard(
            key="ifr",
            label="IFR",
            branch=os.environ.get("ARTIFACT_BRANCH_IFR", "data-ifr"),
            manifest_name="ifr_manifest.json",
        ),
        Hazard(
            key="mtn_obsc",
            label="Mountain Obscuration",
            branch=os.environ.get("ARTIFACT_BRANCH_MTN_OBSC", "data-mtnobsc"),
            manifest_name="mtn_obsc_manifest.json",
        ),
    )
}


@dataclass
class HazardState:
    """
    What the app currently knows about one hazard. Purely informational
    -- the endpoints read files off disk, they don't serve from here --
    but it's what makes /api/data/status able to explain a stale or
    missing cycle instead of just 404ing at people.
    """

    model_cycle: Optional[str] = None
    nbm_source_cycle: Optional[str] = None
    source: Optional[str] = None  # "local" | "remote" | None
    last_attempt_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_STATE = {key: HazardState() for key in HAZARDS}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- Path resolution -----------------------------------------------------


def hazard_dir(key: str) -> Path:
    """
    Which directory this hazard's files should be served from.

    Local output/ wins whenever it holds this hazard's manifest: that
    means someone ran the pipeline on this machine and wants to see
    THAT, not production's cycle. Checked per hazard rather than per
    file, so a local IFR run can't end up half-mixed with a fetched
    Mountain Obscuration cycle (or vice versa) -- each hazard is served
    whole from one place or the other.

    On a deployed container output/ is empty (untracked and gitignored),
    so this always resolves to the fetched cache there.
    """
    hazard = HAZARDS[key]
    if (LOCAL_OUTPUT_DIR / hazard.manifest_name).exists():
        return LOCAL_OUTPUT_DIR
    return CACHE_DIR / key


def manifest_path(key: str) -> Path:
    return hazard_dir(key) / HAZARDS[key].manifest_name


def artifact_path(key: str, filename: str) -> Optional[Path]:
    """
    Resolve one artifact filename within this hazard's directory, or
    None if it isn't there.

    Returns None (rather than a path outside the directory) for anything
    that escapes via .. or an absolute path -- filenames reach here from
    URL path parameters like /api/hazards/ifr/{fxx}, not just from the
    trusted manifest.
    """
    base = hazard_dir(key).resolve()
    candidate = (base / filename).resolve()
    if base != candidate.parent:
        return None
    return candidate if candidate.exists() else None


def load_manifest(key: str) -> Optional[dict]:
    """The hazard's manifest as a dict, or None if no cycle is loaded yet."""
    path = manifest_path(key)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        # A manifest that is unreadable or half-written is indistinguishable
        # from no data at all as far as callers are concerned -- the next
        # refresh replaces it.
        return None


def not_loaded_detail(key: str) -> str:
    """
    The message endpoints return when a hazard has no data yet. Spells
    out where the app was looking and what went wrong last time, since
    the alternative ("404 not found") tells an operator nothing about
    whether this is a cold start, a bad branch name, or GitHub being
    down.
    """
    hazard = HAZARDS[key]
    state = _STATE[key]
    detail = (
        f"{hazard.label} data is not loaded yet. The app fetches it at runtime from the "
        f"'{hazard.branch}' branch of {REPO_OWNER}/{REPO_NAME}; it is not part of the deployment."
    )
    if not FETCH_ENABLED:
        return detail + " Fetching is disabled (ARTIFACT_FETCH_ENABLED=0) and no local output/ is present."
    if state.last_error:
        return detail + f" Last fetch attempt ({state.last_attempt_at}) failed: {state.last_error}"
    if state.last_attempt_at is None:
        return detail + " The first fetch is still in progress (this is normal for the first few seconds after a restart)."
    return detail + f" Last checked {state.last_attempt_at}; the pipeline may not have published a cycle yet."


def status() -> dict:
    """Full picture of what's loaded and how the last refresh went, for /api/data/status."""
    return {
        "fetch_enabled": FETCH_ENABLED,
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "refresh_interval_seconds": REFRESH_INTERVAL_SECONDS,
        "cache_dir": str(CACHE_DIR),
        "hazards": {
            key: {
                "label": HAZARDS[key].label,
                "branch": HAZARDS[key].branch,
                "loaded": manifest_path(key).exists(),
                "serving_from": str(hazard_dir(key)),
                "model_cycle": state.model_cycle,
                "nbm_source_cycle": state.nbm_source_cycle,
                "source": state.source,
                "last_attempt_at": state.last_attempt_at,
                "last_success_at": state.last_success_at,
                "last_error": state.last_error,
            }
            for key, state in _STATE.items()
        },
    }


# --- Fetching ------------------------------------------------------------


def _raw_url(hazard: Hazard, filename: str) -> str:
    return f"{RAW_BASE_URL}/{REPO_OWNER}/{REPO_NAME}/{hazard.branch}/{filename}"


def _download(url: str, dest: Path) -> None:
    """
    Stream one file to disk. urllib rather than requests/httpx on
    purpose: this is the web app's dependency set, which is kept
    deliberately thin (see requirements.txt's own notes about what a
    heavy dependency once did to Railway deploys), and the stdlib
    covers a plain HTTPS GET perfectly well.
    """
    request = urllib.request.Request(url, headers={"User-Agent": f"{REPO_NAME}-webapp"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response, open(dest, "wb") as out:
        shutil.copyfileobj(response, out)


def _fetch_manifest(hazard: Hazard) -> dict:
    request = urllib.request.Request(
        _raw_url(hazard, hazard.manifest_name), headers={"User-Agent": f"{REPO_NAME}-webapp"}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _swap_in(staging: Path, final: Path) -> None:
    """
    Replace final/ with staging/ via renames only, so there is no window
    where a request could see a directory holding half a cycle's files.
    """
    previous = final.with_name(final.name + ".previous")
    shutil.rmtree(previous, ignore_errors=True)
    if final.exists():
        final.rename(previous)
    staging.rename(final)
    shutil.rmtree(previous, ignore_errors=True)


def refresh_hazard(key: str, force: bool = False) -> bool:
    """
    Check one hazard's data branch and download its set if the published
    model_cycle differs from what's loaded. Returns True if a new cycle
    was swapped in.

    Every failure is caught and recorded rather than raised: a refresh
    that fails must leave the previous cycle serving untouched, which is
    the difference between "the site is showing 09Z instead of 15Z for
    another five minutes" and "the site is empty".
    """
    hazard = HAZARDS[key]
    state = _STATE[key]

    with state.lock:
        # Someone's local pipeline output is in play -- serve that and
        # don't spend a download on data that wouldn't be used anyway.
        if hazard_dir(key) == LOCAL_OUTPUT_DIR:
            local = load_manifest(key) or {}
            state.model_cycle = local.get("model_cycle")
            state.nbm_source_cycle = local.get("nbm_source_cycle")
            state.source = "local"
            state.last_error = None
            return False

        state.last_attempt_at = _now_iso()
        try:
            manifest = _fetch_manifest(hazard)
            cycle = manifest.get("model_cycle")
            snapshots = manifest.get("snapshots") or []
            if not snapshots:
                raise ValueError("published manifest contains no snapshots")

            already_current = cycle is not None and cycle == state.model_cycle and manifest_path(key).exists()
            if already_current and not force:
                state.last_success_at = _now_iso()
                state.last_error = None
                return False

            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".staging-{key}-", dir=CACHE_DIR))
            try:
                for snapshot in snapshots:
                    for filename in (snapshot.get("filename"), snapshot.get("cache_filename")):
                        if filename:
                            _download(_raw_url(hazard, filename), staging / filename)
                # Manifest written LAST: while it's absent the staging
                # directory can't be mistaken for a complete cycle, and
                # its presence in the swapped-in directory is what every
                # endpoint treats as "this hazard has data".
                with open(staging / hazard.manifest_name, "w") as f:
                    json.dump(manifest, f, indent=2)
                _swap_in(staging, CACHE_DIR / key)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise

            state.model_cycle = cycle
            state.nbm_source_cycle = manifest.get("nbm_source_cycle")
            state.source = "remote"
            state.last_success_at = _now_iso()
            state.last_error = None
            print(f"[artifacts] {hazard.label}: loaded model cycle {cycle} from {hazard.branch}")
            return True

        except urllib.error.HTTPError as err:
            # 404 is the ordinary "this data branch doesn't exist yet"
            # case -- before the workflow's first publish -- not a bug.
            state.last_error = f"HTTP {err.code} fetching from branch '{hazard.branch}'"
        except Exception as err:  # noqa: BLE001 -- a refresh must never take the app down
            state.last_error = f"{type(err).__name__}: {err}"

        print(f"[artifacts] {hazard.label}: refresh failed ({state.last_error}); keeping cycle {state.model_cycle}")
        return False


def refresh_all(force: bool = False) -> None:
    for key in HAZARDS:
        refresh_hazard(key, force=force)


class ArtifactRefresher:
    """
    Background poller. A plain daemon thread rather than an asyncio task
    because the work is blocking I/O (urllib + file writes) -- keeping
    it off the event loop entirely means a slow or hung GitHub response
    can't stall request handling or the health check.
    """

    def __init__(self, interval_seconds: float = REFRESH_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not FETCH_ENABLED:
            print("[artifacts] fetching disabled (ARTIFACT_FETCH_ENABLED=0); serving whatever is already on disk")
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="artifact-refresher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        # The first pass happens here, not at startup, so the ~20 MB
        # initial download never delays the app becoming ready.
        while True:
            refresh_all()
            if self._stop.wait(self.interval_seconds):
                return
