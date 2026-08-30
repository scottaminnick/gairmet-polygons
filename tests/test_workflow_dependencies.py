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
DEFERRED_AND_UNUSED = {
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
    """Third-party module roots imported where they run at import time."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            # `if TYPE_CHECKING:` and try/except ImportError blocks still
            # sit at module scope; recurse into them.
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    names |= {alias.name.split(".")[0] for alias in sub.names}
                elif isinstance(sub, ast.ImportFrom) and sub.level == 0 and sub.module:
                    names.add(sub.module.split(".")[0])
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


def _first_party_path(module):
    """pipeline.hazards.ifr -> Path to that file, or None if not ours."""
    if module.split(".")[0] not in FIRST_PARTY_ROOTS:
        return None
    candidate = REPO_ROOT / (module.replace(".", "/") + ".py")
    if candidate.exists():
        return candidate
    package_init = REPO_ROOT / module.replace(".", "/") / "__init__.py"
    return package_init if package_init.exists() else None


def _reachable_third_party(entry_point):
    """
    Third-party modules that will be imported when `entry_point` runs,
    following first-party imports transitively (function-level ones
    included -- that is how fetch_terrain reaches boundaries).

    Returns {module_root: [chain that reaches it]}.
    """
    found, seen, queue = {}, set(), [(REPO_ROOT / entry_point, (entry_point,))]
    while queue:
        path, chain = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        tree = ast.parse(path.read_text())

        for module in _module_scope_imports(tree):
            if module not in STDLIB and _first_party_path(module) is None:
                found.setdefault(module, chain)

        for module in _all_imports(tree):
            nested = _first_party_path(module)
            if nested is not None and nested not in seen:
                queue.append((nested, chain + (module,)))
    return found


@pytest.mark.parametrize("entry_point,requirements_name", sorted(WORKFLOWS.items()))
def test_every_workflow_installs_what_its_entry_point_imports(entry_point, requirements_name):
    declared = _declared_packages(requirements_name)
    reachable = _reachable_third_party(entry_point)

    allowed = DEFERRED_AND_UNUSED.get(entry_point, {})
    missing = []
    for module, chain in sorted(reachable.items()):
        package = MODULE_TO_PACKAGE.get(module, module).lower()
        if package in declared or module in allowed:
            continue
        missing.append(f"  {module} (package {package}) via {' -> '.join(chain)}")

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
            f"(via {' -> '.join(reachable[module])}). It is imported lazily in "
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
    chain = reachable["shapely"]
    assert "pipeline.boundaries" in chain, chain


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_every_dependency_allowance_carries_a_reason():
    """
    Each entry in DEFERRED_AND_UNUSED switches off a real check, so it has
    to say why -- and stop existing once the import it excuses does.
    """
    for entry_point, allowances in DEFERRED_AND_UNUSED.items():
        reachable = _reachable_third_party(entry_point)
        for module, reason in allowances.items():
            assert len(reason) > 60, f"{entry_point}: {module} is excused without a real reason"
            assert module in reachable, (
                f"{entry_point} no longer reaches {module}; drop the allowance rather "
                f"than leaving a check switched off for an import that is gone"
            )
