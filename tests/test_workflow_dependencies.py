"""
tests/test_workflow_dependencies.py
--------------------------------------
Each workflow installs a different dependency set, and a module-level
import added anywhere in `pipeline/` can silently widen what every one of
them needs. This is the failure class that only appears at runtime, in
the one environment missing the package.

It has already happened twice:

  - `pipeline/fetch_terrain.py` imported `requests` at module level while
    `webapp/main.py` imports `load_terrain_grid()` from it. requests is
    not in requirements.txt, so that endpoint 500'd in production.
  - The land-only baseline made `compute_output_grids` import
    `pipeline.boundaries` -> `pipeline.polygons`, which imported `geojson`
    at module scope. The Fetch Terrain Grid job installs neither geojson
    nor pyproj, and the run died with ModuleNotFoundError *after*
    downloading all 1,708 tiles.

Both were invisible to the test suite, because the test suite installs a
set that has everything.

WHAT THIS CHECKS, AND WHAT IT CANNOT. It walks each entry point's
first-party import graph -- following imports inside function bodies too,
not just module level, because that is precisely how fetch_terrain
reaches boundaries -- and collects the third-party modules each reached
file imports AT MODULE SCOPE, since those are the ones that run on
import. It then asserts every one is covered by the requirements file
that workflow installs.

It is static, so it cannot see `importlib.import_module(name)` or an
import built from a variable. Nothing in this repo does that today; if
something starts to, this test will not know. The stronger check remains
actually running the entry point under the reduced set, which is how the
fix for the geojson failure was verified.

It also guards what the workflows COMMIT, which is the same shape of
problem pointing the other way: a step that stages a directory picks up
whatever a later change happens to put in it.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Third-party module name -> the distribution that provides it, where
# they differ. Anything not listed is assumed to match its package name.
MODULE_TO_PACKAGE = {
    "skimage": "scikit-image",
    "yaml": "pyyaml",
    "dateutil": "python-dateutil",
    "herbie": "herbie-data",
    "PIL": "pillow",
    "cv2": "opencv-python",
}

# Modules in the standard library get no requirement.
STDLIB = set(sys.stdlib_module_names)

# Repo packages, not distributions -- scripts/ has an __init__.py because
# webapp/pgen_export.py imports the PGEN mapping from it wholesale.
FIRST_PARTY_ROOTS = ("pipeline", "webapp", "scripts", "tests")

# Modules a walker reaches through a first-party import that the job in
# question never actually executes. Static analysis cannot tell a
# deferred-and-taken path (fetch_terrain -> boundaries, which is exactly
# what broke) from a deferred-and-never-taken one, so the difference is
# recorded here, with the reason, rather than guessed at. Keep this list
# short and specific: every entry is a check being turned off.
# Keys may be exact paths or fnmatch globs. An allowance only ever
# excuses a module reached THROUGH something else -- if the entry point
# imports it by name itself, the allowance does not apply and the check
# still fails. Otherwise one glob would quietly excuse a direct
# `import requests` added to a test later.
DEFERRED_AND_UNUSED = {
    "tests/test_*.py": {
        "requests": (
            "reached only through pipeline.hazards.ifr's function-level import of "
            "pipeline.fetch_nbm, which fetches from NOAA. Deferred there deliberately (see "
            "that module's docstring); no test calls fetch_probability_grid, because doing so "
            "would make the suite depend on the network and on NBM being up."
        ),
    },
    "webapp/main.py": {
        "requests": (
            "reached only via pipeline.hazards.ifr's function-level import of "
            "pipeline.fetch_nbm, which fetches from NOAA. The webapp never fetches: "
            "it recomputes from cached grids, which is why main.py imports its "
            "pipeline dependencies lazily in the first place. tests.yml separately "
            "imports every module main.py does reach, under requirements.txt only."
        ),
    },
}

# entry point -> the requirements file the workflow installing it uses.
WORKFLOWS = {
    "pipeline/fetch_terrain.py": "requirements-terrain.txt",
    "pipeline/inspect_nbm.py": "requirements-terrain.txt",
    "pipeline/generate_latest_ifr.py": "requirements-pipeline.txt",
    "pipeline/generate_latest_mtn_obsc.py": "requirements-pipeline.txt",
    "pipeline/test_live_ifr_fetch.py": "requirements-pipeline.txt",
    # Railway installs requirements.txt; webapp/main.py imports its
    # pipeline dependencies lazily, which is why tests.yml also imports
    # them explicitly.
    "webapp/main.py": "requirements.txt",
}


def _declared_packages(requirements_name):
    text = (REPO_ROOT / requirements_name).read_text()
    packages = set()
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        # Strip extras and version pins: uvicorn[standard]>=0.1 -> uvicorn
        name = line.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("!")[0]
        packages.add(name.strip().lower())
    return packages


def _module_scope_imports(tree):
    """
    FULL dotted module names imported where they run at import time.

    Full names, not first segments: the traversal asks whether a
    particular first-party module ("pipeline.fetch_nbm") was imported at
    module scope, and truncating to "pipeline" makes that question
    unanswerable -- every hop then looks deferred, which is the safe-
    looking answer and the wrong one.
    """
    names = set()

    def collect(node):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            collect(node)
        elif isinstance(node, (ast.If, ast.Try)):
            # `if TYPE_CHECKING:` and try/except ImportError blocks still
            # sit at module scope; recurse into them.
            for sub in ast.walk(node):
                collect(sub)
    return names


def _all_imports(tree):
    """Every import anywhere, including inside functions."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def _first_party_path(module, relative_to=None):
    """pipeline.hazards.ifr -> Path to that file, or None if not ours."""
    # A test importing a sibling ("from test_polygons import ...") is a
    # first-party import written unqualified, because tests/ is on
    # sys.path when pytest runs. Resolved against the importing file's own
    # directory, or it looks like a third-party package that nobody
    # installs.
    if relative_to is not None:
        sibling = relative_to.parent / (module.replace(".", "/") + ".py")
        if sibling.exists():
            return sibling

    if module.split(".")[0] not in FIRST_PARTY_ROOTS:
        return None
    candidate = REPO_ROOT / (module.replace(".", "/") + ".py")
    if candidate.exists():
        return candidate
    package_init = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    return package_init if package_init.exists() else None


def _allowances_for(entry_point):
    """Merged allowances for an entry point, exact keys and globs alike."""
    import fnmatch

    merged = {}
    for pattern, entries in DEFERRED_AND_UNUSED.items():
        if pattern == entry_point or fnmatch.fnmatch(entry_point, pattern):
            merged.update(entries)
    return merged


def _direct_imports(entry_point):
    """Module roots the entry point file imports itself, at any scope."""
    tree = ast.parse((REPO_ROOT / entry_point).read_text())
    return {module.split(".")[0] for module in _all_imports(tree)}


def _reachable_third_party(entry_point):
    """
    Third-party modules an execution of `entry_point` can import, as
    {module_root: (chain, deferred_only)}.

    THE DEFERRED FLAG IS THE WHOLE POINT. Following first-party imports
    inside function bodies is necessary -- that is how fetch_terrain
    reaches boundaries, and the missing dependency there was real. But a
    module reached only through a function-level hop may never be
    imported at all, while one reachable by module-scope imports the
    whole way down is imported the instant the entry point is.

    deferred_only=False means every hop was module scope: importing the
    entry point imports this, full stop. deferred_only=True means at
    least one hop was a function-level import that may never run.

    Distinguishing them is what makes the allowance list safe. Excusing
    "requests for test modules" outright would also excuse
    test_publish_schedule.py -> pipeline.gairmet_cycle ->
    pipeline.fetch_nbm, which is module scope the whole way and does fail
    -- the precise CI-only break this check exists to catch.

    An entry point's OWN function-level imports are NOT deferred: a test
    body executes.
    """
    found = {}
    # (path, chain, deferred_so_far, is_entry_point)
    queue = [(REPO_ROOT / entry_point, (entry_point,), False, True)]
    seen = {}

    while queue:
        path, chain, deferred, is_entry = queue.pop()
        # Revisit a file only if we arrive by a strictly better (less
        # deferred) route, so a deferred path found first cannot mask a
        # module-scope one found later.
        if path in seen and seen[path] <= deferred:
            continue
        seen[path] = deferred

        tree = ast.parse(path.read_text())
        module_scope = _module_scope_imports(tree)
        every_import = _all_imports(tree)

        for module in (every_import if is_entry else module_scope):
            root = module.split(".")[0]
            if root in STDLIB or _first_party_path(module, path) is not None:
                continue
            previous = found.get(root)
            if previous is None or (previous[1] and not deferred):
                found[root] = (chain, deferred)

        for module in every_import:
            nested = _first_party_path(module, path)
            if nested is None:
                continue
            # For the entry point both scopes run; deeper in, a
            # function-level import is deferred.
            hop_deferred = deferred or (not is_entry and module not in module_scope)
            queue.append((nested, chain + (module,), hop_deferred, False))

    return found


@pytest.mark.parametrize("entry_point,requirements_name", sorted(WORKFLOWS.items()))
def test_every_workflow_installs_what_its_entry_point_imports(entry_point, requirements_name):
    declared = _declared_packages(requirements_name)
    reachable = _reachable_third_party(entry_point)

    allowed = _allowances_for(entry_point)
    direct = _direct_imports(entry_point)
    missing = []
    for module, (chain, deferred_only) in sorted(reachable.items()):
        package = MODULE_TO_PACKAGE.get(module, module).lower()
        if package in declared:
            continue
        if deferred_only and module in allowed and module not in direct:
            continue
        how = "deferred" if deferred_only else "at import time"
        missing.append(f"  {module} (package {package}, {how}) via {' -> '.join(chain)}")

    assert not missing, (
        f"{entry_point} imports modules that {requirements_name} does not install.\n"
        f"This is the failure that only shows up at runtime, in the workflow's own\n"
        f"environment, often long after the job started:\n" + "\n".join(missing)
    )


def test_the_terrain_job_does_not_quietly_acquire_the_hazard_output_stack():
    """
    geojson and pyproj are imported lazily in pipeline/polygons.py
    precisely so the terrain job never needs them -- it downloads 1,708
    tiles before reaching that code, so an unnecessary dependency there is
    an expensive way to fail. A module-level import puts them back in
    reach; this says so at the point the change is made.
    """
    reachable = _reachable_third_party("pipeline/fetch_terrain.py")
    for module in ("geojson", "pyproj"):
        assert module not in reachable, (
            f"pipeline/fetch_terrain.py now reaches {module} "
            f"(via {' -> '.join(reachable[module][0])}). It is imported lazily in "
            f"pipeline/polygons.py to keep the terrain job light; either restore that "
            f"or add it to requirements-terrain.txt deliberately."
        )


def test_the_walker_actually_follows_function_level_first_party_imports():
    """
    A test with no teeth would be worse than none here. fetch_terrain
    imports pipeline.boundaries INSIDE compute_output_grids, so if the
    walker only looked at module scope it would report a clean bill of
    health for the exact failure that motivated it.
    """
    reachable = _reachable_third_party("pipeline/fetch_terrain.py")
    assert "shapely" in reachable, "the walker is not following into pipeline.boundaries"
    chain, deferred_only = reachable["shapely"]
    assert "pipeline.boundaries" in chain, chain

    # And NOT counted as deferred, which is the subtle half. The hop is a
    # function-level import, but it is inside the entry point itself --
    # main() calls compute_output_grids on every run, so that import
    # always executes. Treating "function-level" as "might not happen"
    # here would have excused the original failure: the job died on this
    # exact chain after downloading 1,708 tiles.
    assert deferred_only is False, (
        "an entry point's own function-level imports run; shapely must be required outright"
    )
    assert "shapely" in _declared_packages("requirements-terrain.txt")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_every_dependency_allowance_carries_a_reason():
    """
    Each entry in DEFERRED_AND_UNUSED switches off a real check, so it has
    to say why -- and stop existing once the import it excuses does.
    """
    import fnmatch

    for pattern, allowances in DEFERRED_AND_UNUSED.items():
        targets = (
            [pattern] if (REPO_ROOT / pattern).exists()
            else [e for e in TEST_ENTRY_POINTS if fnmatch.fnmatch(e, pattern)]
        )
        assert targets, f"the allowance pattern {pattern!r} matches nothing"
        for module, reason in allowances.items():
            assert len(reason) > 60, f"{pattern}: {module} is excused without a real reason"
            assert any(module in _reachable_third_party(t) for t in targets), (
                f"nothing matching {pattern} reaches {module} any more; drop the allowance "
                f"rather than leaving a check switched off for an import that is gone"
            )


# ---------------------------------------------------------------------------
# The test suite is an entry point too.
#
# tests.yml runs the whole suite under `requirements.txt pytest pyyaml`
# -- the LIGHT set, deliberately, so CI fails if a module-level heavy
# import creeps into a path the webapp touches. That makes the suite
# itself subject to exactly the failure this file guards against, and it
# was not covered: a test importing something CI does not install passes
# locally, where everything is installed, and fails only in CI.
#
# That is not hypothetical. tests/test_publish_schedule.py imported
# NBM_LEAD_TIME_OFFSET_HOURS from pipeline.gairmet_cycle, which imports
# pipeline.fetch_nbm, which imports requests at module scope. requests is
# not in requirements.txt. Green locally, ModuleNotFoundError in CI.
# ---------------------------------------------------------------------------

TESTS_WORKFLOW = "tests.yml"

# Collected by pytest, so run by CI. Files in tests/ that pytest does NOT
# collect are excluded below -- they are developer tools, and holding them
# to CI's dependency set would be a constraint nobody asked for.
TEST_ENTRY_POINTS = sorted(
    str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "tests").glob("test_*.py")
)

NOT_COLLECTED_BY_PYTEST = {
    "tests/demo_visualize.py": (
        "a hand-run diagnostic that renders PNGs; it imports matplotlib, which is in "
        "requirements-pipeline.txt but not the light set CI installs. pytest does not "
        "collect it (no test_ prefix), so it never runs in CI."
    ),
}


def _workflow_installed_packages(workflow_name):
    """
    What a workflow's `pip install` line actually installs: the contents
    of any -r file, plus any package named inline.

    Read from the workflow rather than copied into this file, so adding a
    test dependency in one place cannot silently disagree with the guard
    that checks for it.
    """
    packages = set()
    for _name, script in _run_steps(WORKFLOW_DIR / workflow_name):
        for line in script.splitlines():
            line = line.strip()
            if not line.startswith("pip install"):
                continue
            tokens = line.split()[2:]
            index = 0
            while index < len(tokens):
                token = tokens[index]
                if token == "-r":
                    packages |= _declared_packages(tokens[index + 1])
                    index += 2
                    continue
                if not token.startswith("-"):
                    packages.add(token.split("[")[0].split("=")[0].lower())
                index += 1
    return packages


@pytest.mark.parametrize("entry_point", TEST_ENTRY_POINTS)
def test_every_test_module_runs_under_the_dependency_set_ci_installs(entry_point):
    """
    Walked exactly like a workflow entry point, with one difference that
    matters: a test module's OWN function-level imports count, because a
    test body executes. For the first-party modules it reaches, only
    module-scope imports count -- a deferred import there may never be
    taken, which is what DEFERRED_AND_UNUSED records.
    """
    declared = _workflow_installed_packages(TESTS_WORKFLOW)
    allowed = _allowances_for(entry_point)
    direct = _direct_imports(entry_point)

    missing = []
    for module, (chain, deferred_only) in sorted(_reachable_third_party(entry_point).items()):
        package = MODULE_TO_PACKAGE.get(module, module).lower()
        if package in declared:
            continue
        if deferred_only and module in allowed and module not in direct:
            continue
        how = "deferred" if deferred_only else "at import time"
        missing.append(f"  {module} (package {package}, {how}) via {' -> '.join(chain)}")

    assert not missing, (
        f"{entry_point} imports modules that {TESTS_WORKFLOW}'s install step does not "
        f"provide. This passes locally and fails only in CI:\n" + "\n".join(missing)
    )


def test_the_test_entry_point_list_covers_everything_pytest_collects():
    """
    TEST_ENTRY_POINTS is a glob, so it keeps up with new files by itself --
    but only for files pytest actually collects. Anything else in tests/
    has to be named as a known non-test, so a real test module cannot go
    unchecked by being called something unexpected.
    """
    every_file = {str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "tests").glob("*.py")}
    unaccounted = sorted(every_file - set(TEST_ENTRY_POINTS) - set(NOT_COLLECTED_BY_PYTEST))
    assert not unaccounted, (
        f"files in tests/ that are neither checked nor recorded as non-tests: {unaccounted}"
    )


def test_the_excluded_files_really_are_the_ones_pytest_skips():
    """
    The exclusions turn the check off, so they have to stay true. A file
    renamed to test_*.py must start being checked, not stay excused.
    """
    for path, reason in NOT_COLLECTED_BY_PYTEST.items():
        assert (REPO_ROOT / path).exists(), f"{path} is gone; drop it from the exclusion list"
        assert not Path(path).name.startswith("test_"), (
            f"{path} is collected by pytest now, so it runs in CI and must be checked"
        )
        assert len(reason) > 60, f"{path} is excused without a real reason"


def test_the_suite_is_run_under_the_light_set_not_the_pipeline_set():
    """
    The whole check rests on this. If tests.yml ever installs
    requirements-pipeline.txt, the suite stops proving the webapp's
    deployed dependency set is sufficient -- which is the reason it
    installs the light one.
    """
    declared = _workflow_installed_packages(TESTS_WORKFLOW)
    assert "pytest" in declared and "pyyaml" in declared
    for heavy in ("cfgrib", "xarray", "herbie-data", "pandas", "matplotlib"):
        assert heavy not in declared, (
            f"tests.yml now installs {heavy}; the suite no longer proves the webapp runs "
            f"under requirements.txt alone"
        )


# ---------------------------------------------------------------------------
# What the workflows commit back to the repo.
# ---------------------------------------------------------------------------

WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


def _run_steps(workflow_path):
    """Every `run:` script in a workflow, as (step name, script) pairs."""
    import yaml

    spec = yaml.safe_load(workflow_path.read_text())
    steps = []
    for job in (spec.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if "run" in step:
                steps.append((step.get("name", "<unnamed>"), step["run"]))
    return steps


def test_the_terrain_workflow_stages_the_grid_and_not_the_directory():
    """
    `git add data/terrain/` committed a 30 MB mosaic checkpoint to main
    (3975d0f) the first run after that checkpoint was added -- the file
    already persists through actions/cache, so the commit bought nothing
    and would have repeated on every run. .npz does not delta-compress,
    so each one is a full fresh copy in history; that is precisely how
    this repo reached 2.2 GB before output/ was untracked.

    Gitignoring the checkpoint fixes today's file. Naming the staged file
    is what stops the NEXT thing written into that directory from being
    committed by a step that was never asked to.
    """
    workflow = WORKFLOW_DIR / "fetch_terrain.yml"
    adds = [
        (name, line.strip())
        for name, script in _run_steps(workflow)
        for line in script.splitlines()
        if line.strip().startswith("git add")
    ]
    assert adds, "fetch_terrain.yml no longer stages anything -- did the commit step move?"

    for name, line in adds:
        paths = line.split()[2:]                       # everything after `git add`
        assert paths, f"{name!r}: bare `git add`"
        for path in paths:
            assert path not in ("-A", "--all", ".", "data/terrain", "data/terrain/"), (
                f"{workflow.name} step {name!r} stages {path!r} -- a directory or the whole "
                f"tree. That is what committed the 30 MB mosaic checkpoint in 3975d0f; "
                f"name the files to publish instead."
            )
            assert not path.endswith("/"), f"{name!r} stages a directory: {path!r}"
        assert "data/terrain/terrain_grid.npz" in paths, (
            f"{workflow.name} step {name!r} stages {paths}; the terrain grid is the file "
            f"that belongs in history"
        )


def test_the_mosaic_checkpoint_is_ignored_and_untracked():
    """Belt to the workflow's braces: even a hand-run `git add -A` locally
    must not pick the checkpoint up."""
    import subprocess

    ignore = (REPO_ROOT / ".gitignore").read_text()
    assert "data/terrain/mosaic_cache.npz" in ignore, "the mosaic checkpoint is not gitignored"

    tracked = subprocess.run(
        ["git", "ls-files", "data/terrain"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.split()
    assert "data/terrain/mosaic_cache.npz" not in tracked, (
        "the mosaic checkpoint is tracked again; it persists via actions/cache and "
        "committing it puts a full 30 MB copy in history every terrain run"
    )


def test_the_checkpoint_path_the_workflow_caches_is_the_one_that_is_ignored():
    """
    Three places name this file -- the cache steps, .gitignore, and
    fetch_terrain.py's default. If they drift, the checkpoint either stops
    being cached or starts being committed, and neither says so.
    """
    from pipeline.fetch_terrain import DEFAULT_MOSAIC_CACHE

    workflow = (WORKFLOW_DIR / "fetch_terrain.yml").read_text()
    assert workflow.count(f"path: {DEFAULT_MOSAIC_CACHE}") == 2, (
        f"the restore and save steps should both cache {DEFAULT_MOSAIC_CACHE}"
    )
    assert DEFAULT_MOSAIC_CACHE in (REPO_ROOT / ".gitignore").read_text(), (
        f"fetch_terrain.py writes {DEFAULT_MOSAIC_CACHE}, which .gitignore does not cover"
    )
