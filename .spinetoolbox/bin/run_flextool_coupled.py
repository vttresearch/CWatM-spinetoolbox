"""
CWatM-FlexTool rolling coupling orchestrator.
Runs FlexTool ONE ROLL at a time, then terminates. Between runs, CWatM
executes and provides updated reservoir levels.

The warm HiGHS problem cannot be persisted across process boundaries, so
each invocation cold-builds the LP. The roll-forward state (SolveHandoff)
IS persisted to disk as parquet files so the next invocation can resume
the cascade where the previous one left off.

The FlexTool database holds a single static (full-timeline) rolling-window
solve. Each invocation selects exactly ONE roll of that sequence — the
roll whose index equals the current coupling iteration — so successive
runs advance one ``rolling_solve_jump`` further along the timeline. The
per-roll realized-slice parquets accumulate under ``work/output_raw/`` and
are unioned into full-timeline combined results after every roll.

Workflow (each invocation):
    1. Read saved handoff state + iteration counter from disk
    2. Read CWatM reservoir storage from combined/ folder at the roll date
    3. Override roll_continue_state with CWatM values (iteration > 0)
    4. Run FlexTool for roll N only (N = iteration); persist its slice
    5. Union all accumulated roll slices into combined results
    6. Save handoff state to disk for next invocation
    7. Script terminates — CWatM runs next period with the dispatch

Usage:
    python run_flextool_coupled.py <flextool.sqlite> \
        --ini <cwatm_input.ini> \
        --results_dir <flextool_results> \
        --unit_conversion 1.0

Reservoir node coordinates (lat/lon) are read directly from the FlexTool
Spine database. Nodes with a "lat" and "lon" attribute whose name contains
"reservoir" are treated as coupled hydropower reservoirs.

State persistence:
    The handoff state is saved in <results_dir>/handoff_state/ as parquet
    files. Delete this folder to reset and start fresh.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from spinedb_api import DatabaseMapping
import spinedb_api as api
import numpy as np

try:
    import polars as pl
except ImportError:
    sys.exit("Missing dependency: pip install polars")

from coupling_datetime import (
    compute_coupling_date,
    cwatm_date_to_datetime,
    is_datetime_format,
    read_rolling_solve_jump,
    read_timeline_from_db,
)


# ---------------------------------------------------------------------------
# INI parsing (reuse from flextool_to_cwatm_dispatch.py)
# ---------------------------------------------------------------------------

def parse_ini(ini_path):
    """Parse CWatM settings ini and resolve variable references."""
    ini_path = Path(ini_path)
    sections = {}
    current_section = None

    with open(ini_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r"\[(.+)\]", line)
            if m:
                current_section = m.group(1)
                sections.setdefault(current_section, {})
                continue
            if "=" in line and current_section is not None:
                key, _, val = line.partition("=")
                sections[current_section][key.strip()] = val.strip()

    flat = {}
    for sec, kvs in sections.items():
        for k, v in kvs.items():
            flat[f"{sec}:{k}"] = v
            flat[k] = v

    def resolve(value, depth=0):
        if depth > 10:
            return value
        pattern = r"\$\(([^)]+)\)"
        def replacer(m):
            ref = m.group(1)
            return flat.get(ref, m.group(0))
        resolved = re.sub(pattern, replacer, value)
        if resolved != value:
            return resolve(resolved, depth + 1)
        return resolved

    for sec in sections:
        for k in sections[sec]:
            sections[sec][k] = resolve(sections[sec][k])

    return sections


# ---------------------------------------------------------------------------
# Handoff state persistence
# ---------------------------------------------------------------------------

HANDOFF_FIELDS = [
    "roll_end_state",
    "upward_roll_end_state",
    "realized_invest",
    "realized_existing",
    "divest_cumulative",
    "cumulative_co2",
    "cumulative_commodity",
    "cum_sim_hours",
    "fix_storage_quantity",
    "fix_storage_price",
    "fix_storage_usage",
]


def save_handoff_to_disk(handoff, state_dir: Path):
    """
    Persist SolveHandoff fields as parquet files.

    Parameters
    ----------
    handoff : SolveHandoff
        The handoff object from the last solve step.
    state_dir : Path
        Directory to write parquet files into.
    """
    state_dir.mkdir(parents=True, exist_ok=True)

    for field_name in HANDOFF_FIELDS:
        frame = getattr(handoff, field_name, None)
        if frame is not None and isinstance(frame, pl.DataFrame) and frame.height > 0:
            frame.write_parquet(state_dir / f"{field_name}.parquet")
        else:
            # Remove stale file if the field is now empty
            path = state_dir / f"{field_name}.parquet"
            if path.exists():
                path.unlink()

    # Write iteration counter
    counter_file = state_dir / "iteration.txt"
    if counter_file.exists():
        iteration = int(counter_file.read_text().strip()) + 1
    else:
        iteration = 1
    counter_file.write_text(str(iteration))

    print(f"  [state] Saved handoff to {state_dir} (iteration {iteration})")


def load_handoff_from_disk(state_dir: Path) -> dict[str, pl.DataFrame]:
    """
    Load persisted handoff state as a dict of key -> DataFrame.

    Returns empty dict if no state exists (first run).
    """
    from flextool.engine_polars import _provider_keys as K

    if not state_dir.exists():
        return {}, 0

    counter_file = state_dir / "iteration.txt"
    iteration = int(counter_file.read_text().strip()) if counter_file.exists() else 0

    # Map field names to provider keys
    field_to_key = {
        "roll_end_state": K.HANDOFF_ROLL_END_STATE,
        "upward_roll_end_state": K.HANDOFF_UPWARD_ROLL_END_STATE,
        "realized_invest": K.HANDOFF_REALIZED_INVEST,
        "realized_existing": K.HANDOFF_REALIZED_EXISTING,
        "divest_cumulative": K.HANDOFF_DIVEST_CUMULATIVE,
        "cumulative_co2": K.HANDOFF_CUMULATIVE_CO2,
        "cumulative_commodity": K.HANDOFF_CUMULATIVE_COMMODITY,
        "cum_sim_hours": K.HANDOFF_CUM_SIM_HOURS,
        "fix_storage_quantity": K.HANDOFF_FIX_STORAGE_QUANTITY,
        "fix_storage_price": K.HANDOFF_FIX_STORAGE_PRICE,
        "fix_storage_usage": K.HANDOFF_FIX_STORAGE_USAGE,
    }

    overrides = {}
    for field_name, key in field_to_key.items():
        path = state_dir / f"{field_name}.parquet"
        if path.exists():
            frame = pl.read_parquet(path)
            # Cast all columns to Utf8 to match the handoff contract
            frame = frame.cast({col: pl.Utf8 for col in frame.columns})
            overrides[key] = frame

    return overrides, iteration


# ---------------------------------------------------------------------------
# Read reservoir node coordinates from the FlexTool database
# ---------------------------------------------------------------------------

def read_reservoir_nodes_from_db(db_url):
    """
    Read reservoir node names and their lat/lon from the FlexTool Spine database.

    Looks for entities in the "node" class that have "lat" and "lon" parameter
    values and whose name contains "reservoir".

    Parameters
    ----------
    db_url : str
        Spine database URL (e.g. sqlite:///path/to/db.sqlite)

    Returns
    -------
    dict mapping node_name -> {"lat": float, "lon": float}
    """

    reservoir_nodes = {}
    with DatabaseMapping(db_url) as db_map:
        db_map.fetch_all("entity")
        db_map.fetch_all("parameter_value")
        for lat_db in db_map.find_parameter_values(entity_class_name = "node", parameter_definition_name = "lat"):
            print(f"  [DB] Found lat parameter for unit '{lat_db['entity_byname'][0]}': lat={lat_db['value']}")
            lon_db = db_map.find_parameter_values(
                entity_class_name="node",
                parameter_definition_name="lon",
                entity_byname=lat_db["entity_byname"],
            )
            if not lon_db:
                print(f"  WARNING: Node '{lat_db['entity_byname'][0]}' has lat but no lon, skipping")
                continue
            val_= api.from_database(lat_db["value"], lat_db["type"])
            if isinstance(val_, api.Array):
                lat_val = list(val_.values)
            else:
                lat_val = list([float(val_)])
            val_ = api.from_database(lon_db[0]["value"], lon_db[0]["type"])
            if isinstance(val_, api.Array):
                lon_val = list(val_.values)
            else:
                lon_val = list([float(val_)])
            print(f"  [DB] Found reservoir node '{lat_db['entity_byname'][0]}': lat={lat_val}, lon={lon_val}")
            storage_db = db_map.find_parameter_values(
                entity_class_name="node",
                parameter_definition_name="existing",
                entity_byname=lat_db["entity_byname"],
            )
            if storage_db:
                print(f"  [DB] Found existing storage for node '{lat_db['entity_byname'][0]}': {storage_db[0]['value']}")
                storage_val = api.from_database(storage_db[0]['value'], storage_db[0]['type'])
                reservoir_nodes[lat_db['entity_byname'][0]] = {"lat": lat_val, "lon": lon_val, "existing_storage": storage_val}
            else:
                print(f"  WARNING: No existing storage found for node '{lat_db['entity_byname'][0]}'")

    return reservoir_nodes


# ---------------------------------------------------------------------------
# Read CWatM reservoir storage at a point
# ---------------------------------------------------------------------------

def read_cwatm_storage(storage_dir, reservoir_nodes, date=None, coord_tol=0.05):
    """
    Read latest reservoir storage from CWatM hp_state_out.nc.

    hp_state_out.nc is indexed by an opaque ``reservoir`` axis carrying
    ``storage_m3[time, reservoir]`` plus static ``reservoir_lat`` /
    ``reservoir_lon`` (outlet-cell coordinates). Each FlexTool node is
    matched to the file reservoir whose outlet coordinate is nearest one of
    the node's cells; the value at that reservoir is used directly (no
    summing over cells is needed because the axis is already per-reservoir).

    Parameters
    ----------
    storage_dir : Path
        Path to the folder holding hp_state_out.nc (the 'temp' folder
        written by process_data.py, parallel to PathOut).
    reservoir_nodes : dict
        Mapping of FlexTool node name -> {"lat": [...], "lon": [...]}.
    date : datetime, optional
        Specific date to read. If None, reads the last available timestep.
    coord_tol : float
        Max coordinate distance (deg, Manhattan) for a node-reservoir match.

    Returns
    -------
    dict mapping node_name -> storage_value (in FlexTool units)
    """
    import netCDF4 as nc4

    storage_file = Path(storage_dir) / "hp_state_out.nc"
    if not storage_file.exists():
        print(f"  WARNING: {storage_file} not found, no storage override")
        return {}

    with nc4.Dataset(str(storage_file), "r") as ds:
        print("Columns in the dataset:", list(ds.variables.keys()))
        storage_data = ds["storage_m3"][:]
        times = nc4.num2date(
            ds["time"][:], ds["time"].units,
            calendar=getattr(ds["time"], "calendar", "standard"),
        )
        res_lat = ds["reservoir_lat"][:] if "reservoir_lat" in ds.variables else None
        res_lon = ds["reservoir_lon"][:] if "reservoir_lon" in ds.variables else None

        # Pick the time index
        if date is not None:
            diffs = [abs((datetime(t.year, t.month, t.day) - date).days)
                     for t in times]
            t_idx = int(np.argmin(diffs))
        else:
            t_idx = len(times) - 1

        storage_t = np.ma.filled(storage_data[t_idx], 0.0)
        n_res = storage_t.shape[0]

        result = {}
        for pos, (node_name, coords) in enumerate(reservoir_nodes.items()):
            if res_lat is not None and res_lon is not None:
                best_idx, best_dist = None, None
                for i in range(n_res):
                    for j, lat in enumerate(coords["lat"]):
                        d = abs(float(res_lat[i]) - lat) + abs(float(res_lon[i]) - coords["lon"][j])
                        if best_dist is None or d < best_dist:
                            best_dist, best_idx = d, i
                if best_idx is None or best_dist > coord_tol:
                    print(f"  WARNING: node '{node_name}' has no reservoir within "
                          f"{coord_tol} deg in hp_state_out.nc (min dist {best_dist})")
                    continue
                result[node_name] = float(storage_t[best_idx])
            elif pos < n_res:
                # No identity variables: fall back to positional alignment.
                result[node_name] = float(storage_t[pos])
            else:
                print(f"  WARNING: node '{node_name}' has no positional match in hp_state_out.nc")
    print(result)
    return result


# ---------------------------------------------------------------------------
# Read reservoir full volume and convert CWatM storage to usable storage
# ---------------------------------------------------------------------------

def _find_data_variable(ds):
    """Return the name of the main (lat/lon) data variable in a dataset."""
    coord_names = set(ds.dimensions.keys()) | {"lat", "lon"}
    for name, var in ds.variables.items():
        if name in coord_names:
            continue
        if {"lat", "lon"}.issubset(set(var.dimensions)) or var.ndim >= 2:
            return name
    for name in ds.variables:
        if name not in coord_names:
            return name
    raise KeyError("No data variable found in reservoir volume file")


def read_reservoir_volumes(volume_path, reservoir_nodes):
    """
    Read the whole-reservoir storage volume for each node from
    CWatM's lakesResVolRes.nc.

    CWatM represents the whole-reservoir volume as a single value per
    waterbody (the same value across the body's cells / at its outlet), so
    the maximum over the node's cells recovers that whole volume without
    double counting.

    Parameters
    ----------
    volume_path : str | Path
        Directory containing lakesResVolRes.nc, or the file itself.
    reservoir_nodes : dict
        Mapping node_name -> {"lat": [...], "lon": [...], ...}.

    Returns
    -------
    dict mapping node_name -> full_volume (native file units)
    """
    import netCDF4 as nc4

    volume_path = Path(volume_path)
    volume_file = (volume_path / "lakesResVolRes.nc")
    if not volume_file.exists():
        print(f"  WARNING: {volume_file} not found, no volume conversion")
        return {}

    with nc4.Dataset(str(volume_file), "r") as ds:
        lats = ds["lat"][:]
        lons = ds["lon"][:]
        vol_data = ds[_find_data_variable(ds)][:]

        result = {}
        for node_name, coords in reservoir_nodes.items():
            val = 0.0
            for i, lat in enumerate(coords["lat"]):
                lat_idx = int(np.argmin(np.abs(lats - lat)))
                lon_idx = int(np.argmin(np.abs(lons - coords["lon"][i])))
                cell = float(np.ma.filled(vol_data[lat_idx, lon_idx], 0.0))
                val = max(val, cell)
            result[node_name] = val
    print(f"  Reservoir full volumes: {result}")
    return result


def read_initload_storage(initload_file, reservoir_nodes):
    """
    Read reservoir storage from the CWatM InitLoad NetCDF at each node.

    The InitLoad file (``[INITITIAL CONDITIONS] initLoad``) stores CWatM
    state variables as 2D ``(lat, lon)`` maps. The ``reservoirStorage``
    variable carries the whole-reservoir volume placed at each waterbody's
    outlet cell, so the maximum over the node's cells recovers that volume
    (same coordinate convention as :func:`read_reservoir_volumes`).

    Used for the first (``--restart``) solve, which runs *before* CWatM so
    no CWatM storage output exists yet.

    Returns dict node_name -> storage (native file units, m3).
    """
    import netCDF4 as nc4

    initload_file = Path(initload_file)
    if not initload_file.exists():
        print(f"  WARNING: InitLoad file {initload_file} not found, no storage")
        return {}

    with nc4.Dataset(str(initload_file), "r") as ds:
        if "reservoirStorage" not in ds.variables:
            print(f"  WARNING: 'reservoirStorage' variable not in {initload_file}")
            return {}
        lats = ds["lat"][:]
        lons = ds["lon"][:]
        data = np.ma.filled(ds["reservoirStorage"][:], 0.0)

        result = {}
        for node_name, coords in reservoir_nodes.items():
            val = 0.0
            for i, lat in enumerate(coords["lat"]):
                lat_idx = int(np.argmin(np.abs(lats - lat)))
                lon_idx = int(np.argmin(np.abs(lons - coords["lon"][i])))
                cell = float(data[lat_idx, lon_idx])
                val = max(val, cell)
            result[node_name] = val
    print(f"  InitLoad reservoir storage: {result}")
    return result


def compute_usable_storage(cwatm_storage, reservoir_volumes, reservoir_nodes):
    """
    Convert CWatM whole-reservoir storage into FlexTool usable (top) storage.

    FlexTool's ``existing`` storage is only the usable volume, so the
    non-usable bottom volume is ``full_volume - existing``. The usable state
    handed to FlexTool is the CWatM storage above that bottom volume,
    clamped at zero (never negative).

    Returns a new dict node_name -> usable_storage.
    """
    usable = {}
    for node, cwatm_val in cwatm_storage.items():
        full_volume = reservoir_volumes.get(node)
        existing = reservoir_nodes.get(node, {}).get("existing_storage")
        if full_volume is None or existing is None:
            usable[node] = cwatm_val
            print(f"  [coupling] {node}: missing volume/existing_storage; "
                  f"passing CWatM value through ({cwatm_val:.4f})")
            continue
        dead_volume = full_volume - existing
        usable_val = max(0.0, cwatm_val - dead_volume)
        usable[node] = usable_val
        print(f"  [coupling] {node}: cwatm={cwatm_val:.4f}, full={full_volume:.4f}, "
              f"existing={existing:.4f}, dead={dead_volume:.4f} -> "
              f"usable={usable_val:.4f}")
    return usable



# ---------------------------------------------------------------------------
# Override provider factory
# ---------------------------------------------------------------------------

def make_override_callback(
    saved_overrides: dict[str, pl.DataFrame],
    cwatm_storage: dict[str, float],
    iteration: int,
    state_holder: dict | None = None,
    restart: bool = False,
):
    """
    Create the override_provider callable for the cascade.

    Merges the persisted handoff state with fresh CWatM reservoir storage.
    The callback fires once per sub-solve in the cascade. It only injects
    on the rolling sub-solve (identified by ``state.current_roll_index is
    not None``) so non-rolling sibling solves in a chained scenario (e.g.
    an invest solve preceding the dispatch solve) are left untouched.
    On iteration 0 (first ever run, no saved state) it returns empty so
    the roll's storage start comes from the DB initial state.

    Parameters
    ----------
    saved_overrides : dict
        Previously saved handoff state loaded from disk.
    cwatm_storage : dict
        Fresh reservoir storage from CWatM {node_name: value}.
    iteration : int
        Current coupling iteration (0 = first run ever).
    state_holder : dict | None
        Mutable holder whose ``"state"`` key is populated with the live
        ``RunnerState`` by :func:`_run_one_roll`.  Used to read
        ``current_roll_index`` so the override only lands on the rolling
        sub-solve.  When ``None`` (or state not yet set) the override
        falls back to firing on the first sub-solve call.
    """
    from flextool.engine_polars import _provider_keys as K

    call_state = {"applied": False}

    def fetch_override() -> dict[str, pl.DataFrame]:
        if iteration == 0 and not restart:
            # First run (no restart): no continuation state to inject.
            return {}

        state = state_holder.get("state") if state_holder else None
        if restart:
            # First (--restart) full-timeline storage solve: inject the
            # InitLoad storage once, ignoring the roll gating (this solve
            # may be non-rolling, so ``current_roll_index`` can be None).
            if call_state["applied"]:
                return {}
        elif state is not None:
            # Only inject on the rolling sub-solve.  ``current_roll_index``
            # is set to the roll index during rolling sub-solves and reset
            # to None otherwise; gate on it so chained non-rolling solves
            # don't receive the storage override.
            roll_idx = getattr(state, "current_roll_index", None)
            if roll_idx is None:
                return {}
        elif call_state["applied"]:
            # Fallback path (no state holder): fire only once.
            return {}

        # Build the override once; if it fires on multiple rolling
        # sub-solves the same continuation state is valid for each.
        overrides = dict(saved_overrides)

        # Override roll_end_state with CWatM storage if available.
        # Merge (not replace): keep the saved roll-end-state rows for any
        # non-reservoir storage nodes so their handoff continuity is
        # preserved, and overwrite only the coupled reservoir nodes with
        # the CWatM value.
        if cwatm_storage:
            saved_frame = saved_overrides.get(K.HANDOFF_ROLL_END_STATE)
            if saved_frame is not None and saved_frame.height > 0:
                kept = saved_frame.filter(
                    ~pl.col("node").is_in(list(cwatm_storage.keys()))
                )
            else:
                kept = pl.DataFrame(
                    schema={"node": pl.Utf8, "value": pl.Utf8}
                )
            cwatm_rows = pl.DataFrame(
                [{"node": name, "value": str(val)}
                 for name, val in cwatm_storage.items()],
                schema={"node": pl.Utf8, "value": pl.Utf8},
            )
            storage_frame = pl.concat([kept, cwatm_rows], how="vertical")
            overrides[K.HANDOFF_ROLL_END_STATE] = storage_frame
            print(f"  [coupling] Overriding roll_end_state with CWatM storage:")
            for name, val in cwatm_storage.items():
                print(f"    {name}: {val:.4f}")

        if overrides:
            print(f"  [coupling] Applying {len(overrides)} handoff override(s)")
        else:
            print(f"  [coupling] No overrides to apply")

        call_state["applied"] = True
        return overrides

    return fetch_override


# ---------------------------------------------------------------------------
# Single-roll runner
# ---------------------------------------------------------------------------

def _install_roll_selection_patch(iteration: int, logger, restart: bool = False):
    """Monkeypatch the recursive solve builder so the cascade expands
    *only the roll whose index equals* ``iteration``.

    FlexTool's ``create_rolling_solves`` walks the FULL static timeline
    and slices it into an overlapping sequence of rolls
    (``solve_roll_0``, ``solve_roll_1``, ...).  Normally the cascade
    solves every roll in one process.  For the CWatM coupling we must
    solve exactly one roll per process and terminate so CWatM can run.

    Rather than forcing ``duration = jump`` (which always re-runs roll 0
    at the start of the timeline — the previous broken behaviour), we
    let the builder compute *all* roll definitions (cheap — no LP is
    built) and then keep only roll ``iteration``.  Because the builder
    names each roll ``{solve}_roll_{counter}``, the kept roll is named
    ``{solve}_roll_{iteration}`` — unique across process invocations, so
    the per-roll output parquets accumulate in ``output_raw/`` without
    name collisions and the creation-order manifest grows correctly.

    For ``iteration > 0`` the kept roll is also removed from
    ``first_of_complete_solve`` so FlexTool treats it as a *continuation*
    roll: this activates the ``roll_continue_state`` start binding
    (model.py), which reads ``p_roll_continue_state`` from the
    ``handoff/roll_end_state`` Provider key that our override populates
    with the CWatM reservoir storage.  On ``iteration == 0`` the roll
    stays "first" so the storage start comes from the DB initial state.

    Returns a ``restore`` callable that undoes the monkeypatch.
    """
    from flextool.engine_polars._recursive_solve import RecursiveSolveBuilder

    _orig_create = RecursiveSolveBuilder.create_rolling_solves
    _orig_process = RecursiveSolveBuilder._process_rolling_solve

    # Records the roll name kept by ``_patched_create`` so
    # ``_patched_process`` can demote exactly that roll (and not any
    # nested child solves) from ``first_of_complete_solve``.
    kept_box: dict = {"name": None}

    def _patched_create(self, solve, full_active_time_list, jump, horizon,
                        start=None, duration=-1):
        solves, active, realized = _orig_create(
            self, solve, full_active_time_list, jump, horizon, start, duration,
        )
        if not solves:
            return solves, active, realized
        if iteration < len(solves):
            idx = iteration
        else:
            idx = len(solves) - 1
            logger.warning(
                "[coupling] iteration %d exceeds roll count %d for solve "
                "'%s'; clamping to last roll '%s'.",
                iteration, len(solves), solve, solves[idx],
            )
        keep = solves[idx]
        kept_box["name"] = keep
        print(f"  [coupling] Selecting roll {idx} of {len(solves)} "
              f"('{keep}') for solve '{solve}'")
        return [keep], {keep: active[keep]}, {keep: realized[keep]}

    def _patched_process(self, *args, **kwargs):
        result = _orig_process(self, *args, **kwargs)
        # ``restart`` also demotes at iteration 0 so the first full-timeline
        # storage solve reads the InitLoad storage override rather than the
        # DB initial state.
        if (iteration > 0 or restart) and kept_box["name"] is not None:
            # Demote ONLY the kept roll from first-of-complete so the
            # roll_continue_state binding fires and reads the CWatM
            # storage override instead of the DB initial state.  Leave
            # any nested child solves' first-of-complete entries intact.
            fcs = self.state.solve.first_of_complete_solve
            while kept_box["name"] in fcs:
                fcs.remove(kept_box["name"])
        return result

    RecursiveSolveBuilder.create_rolling_solves = _patched_create
    RecursiveSolveBuilder._process_rolling_solve = _patched_process

    def restore():
        RecursiveSolveBuilder.create_rolling_solves = _orig_create
        RecursiveSolveBuilder._process_rolling_solve = _orig_process

    return restore


def _run_one_roll(
    db_path: str,
    scenario_name: str,
    work_folder: "Path",
    override_provider,
    logger,
    iteration: int,
    state_holder: dict | None = None,
    restart: bool = False,
):
    """Run FlexTool for exactly ONE rolling step (roll ``iteration``),
    then return the orchestration result.

    Replicates the essential setup of ``run_chain_from_db`` but installs
    a roll-selection monkeypatch (see
    :func:`_install_roll_selection_patch`) so the cascade expands and
    solves only the roll at index ``iteration`` of the full static
    timeline. The handoff state (persisted to disk between invocations)
    plus the CWatM storage override carry the roll-forward state so each
    successive call advances one jump further along the timeline.

    The ``work_folder`` is reused across invocations so the per-roll
    realized-slice parquets accumulate under ``output_raw/`` for the
    final combine step.
    """
    import os
    from pathlib import Path
    from flextool.engine_polars._orchestration import (
        run_orchestration,
        _MemoryRecorder,
        _NoopMemoryRecorder,
        set_phase_recorder,
    )
    from flextool.engine_polars._solve_state import (
        RunnerState,
        PathConfig,
    )
    from flextool.engine_polars._solve_config import SolveConfig
    from flextool.engine_polars._timeline import TimelineConfig
    from flextool.engine_polars._db_loader import FlexToolRunner
    from flextool.engine_polars._native_input_writer import write_workdir_inputs
    from flextool.engine_polars._flex_data_provider import FlexDataProvider
    from flextool._resources import package_data_path

    work_folder = Path(work_folder)
    work_folder.mkdir(parents=True, exist_ok=True)

    db_url = str(db_path)
    if "://" not in db_url:
        db_url = f"sqlite:///{db_url}"

    # Memory recorder (no-op unless env var set)
    _mem_enabled = os.environ.get("FLEXTOOL_MEMORY_DIAGNOSTICS") == "1"
    if _mem_enabled:
        (work_folder / "solve_data").mkdir(parents=True, exist_ok=True)
        _memrec = _MemoryRecorder(
            work_folder / "solve_data" / "memory_diagnostics.csv",
            enabled=True,
        )
    else:
        _memrec = _NoopMemoryRecorder()
    set_phase_recorder(_memrec)

    # Cascade-input Provider
    cascade_input_provider = FlexDataProvider()
    write_workdir_inputs(
        db_url,
        scenario_name,
        work_folder,
        logger=logger,
        provider=cascade_input_provider,
        memory_recorder=_memrec if _mem_enabled else None,
    )

    # Load solve config and timeline
    sc = SolveConfig.load_from_db_url(db_url, scenario_name, logger=logger)
    tc = TimelineConfig.load_from_db_url(db_url, scenario_name, logger=logger)
    tc.create_assumptive_parts(sc)
    tc.create_timeline_from_timestep_duration(sc)

    # Build RunnerState
    state = RunnerState(
        paths=PathConfig(work_folder=work_folder),
        solve=sc,
        logger=logger,
        timeline=tc,
        handoffs={},
    )
    state._memory_recorder = _memrec
    state.cascade_input_provider = cascade_input_provider
    state.override_provider = override_provider

    # Expose the live state to the override callback so it can gate on
    # ``current_roll_index`` (only inject on the rolling sub-solve).
    if state_holder is not None:
        state_holder["state"] = state

    # Resolve flextool directories
    flextool_dir_resolved = package_data_path("")
    solver_config_dir_resolved = Path.cwd() / "solver_config"

    # Runner factory
    def _runner_factory():
        runner = FlexToolRunner(
            input_db_url=db_url,
            scenario_name=scenario_name,
            flextool_dir=flextool_dir_resolved,
            solver_config_dir=solver_config_dir_resolved,
            work_folder=work_folder,
        )
        runner.state.logger.setLevel(logging.ERROR)
        runner.state.solve.rolling_times = sc.rolling_times
        return runner

    # Install the roll-selection patch so only roll ``iteration`` runs.
    restore_patch = _install_roll_selection_patch(iteration, logger, restart=restart)
    try:
        return run_orchestration(
            state, work_folder, runner_factory=_runner_factory,
            db_url=db_url, scenario_name=scenario_name,
            warm=False, keep_solutions=True,
        )
    finally:
        restore_patch()


def combine_outputs(
    results,
    scenario_name: str,
    work_folder: "Path",
    results_dir: "Path",
    settings_db_url,
    logger,
):
    """Union the accumulated per-roll realized slices into full-timeline
    FlexTool results.

    Each ``_run_one_roll`` invocation persists its roll's realized-slice
    parquets under ``work_folder/output_raw/`` (plus a creation-order
    entry in ``_solve_order.txt``).  ``write_outputs`` detects those
    persisted slices (``has_persisted_slices``) and unions every roll's
    disjoint ``(period, time)`` window into the combined output, using
    the last step only for the solve-invariant attrs + shape template.

    Called after every roll so the combined results always reflect the
    rolls solved so far; after the final roll they cover the full
    timeline.
    """
    from pathlib import Path
    from flextool.process_outputs.write_outputs import write_outputs

    work_folder = Path(work_folder)
    results_dir = Path(results_dir)
    raw_output_dir = work_folder / "output_raw"

    # Pick the last successful step for the static attr / shape template.
    last_step = None
    for _name, step in results.items():
        last_step = step

    if last_step is None:
        logger.warning("[coupling] No solve step to combine; skipping outputs")
        return

    wo_solve_name = getattr(last_step, "solve_name", None) or scenario_name
    try:
        write_outputs(
            scenario_name=scenario_name,
            output_location=str(results_dir),
            subdir=scenario_name,
            settings_db_url=settings_db_url,
            fallback_output_location=str(results_dir),
            raw_output_dir=str(raw_output_dir),
            flex_data=getattr(last_step, "flex_data", None),
            solution=getattr(last_step, "solution", None),
            solve_name=wo_solve_name,
            flex_data_provider=getattr(last_step, "flex_data_provider", None),
        )
        print(f"  [coupling] Combined results written to {results_dir}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[coupling] combine_outputs (write_outputs) failed: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run FlexTool for ONE roll with CWatM coupling, then terminate. "
                    "Handoff state is persisted to disk between runs."
    )
    parser.add_argument("db",
                        help="Path to FlexTool Spine database (.sqlite)")
    parser.add_argument("--scenario", type=str, default=None,
                        help="FlexTool scenario name. If omitted, the scenario "
                             "filter embedded in the database connection is used.")
    parser.add_argument("--ini", type=str, required=True,
                        help="Path to cwatm_input.ini")
    parser.add_argument("--results_dir", type=str, required=True,
                        help="Path to write FlexTool result parquets")
    parser.add_argument("--work_dir", type=str, default=None,
                        help="FlexTool work directory (default: auto)")
    parser.add_argument("--unit_conversion", type=float, default=1.0,
                        help="Multiplier for CWatM storage units to FlexTool units")
    parser.add_argument("--reset", action="store_true",
                        help="Delete saved state and start fresh")
    parser.add_argument("--restart", action="store_true",
                        help="Full restart: clear handoff state, work dir, "
                             "and results dir, then start from iteration 0")

    args = parser.parse_args()

    # Parse ini
    ini_sections = parse_ini(args.ini)

    # Resolve the database URL.  Accept three forms:
    #   * an already-qualified URL (``sqlite:///...``, ``http(s)://...``,
    #     ``mysql+...``) — used as-is;
    #   * a bare filesystem path — wrapped in ``sqlite:///``.
    raw_db = str(args.db)
    if "://" in raw_db:
        db_url = raw_db
    else:
        db_url = f"sqlite:///{Path(raw_db).as_posix()}"
    print(f"Database URL: {db_url}")

    reservoir_nodes = read_reservoir_nodes_from_db(db_url)
    if not reservoir_nodes:
        print("ERROR: No reservoir nodes with lat/lon found in the database.")
        sys.exit(1)
    print(f"Reservoir nodes from DB: {list(reservoir_nodes.keys())}")
    for name, coords in reservoir_nodes.items():
        print(f"  {name}: lat={coords['lat']}, lon={coords['lon']}")

    # Reservoir storage is preserved by process_data.py in a 'temp' folder
    # parallel to PathOut; read lakeResStorage_daily.nc from there.
    temp_dir = Path(ini_sections["FILE_PATHS"]["PathOut"]).parent / "temp"
    print(f"Reservoir storage dir (temp): {temp_dir}")

    # Resolve paths
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(args.work_dir) if args.work_dir else results_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    inner_output_folders = ["output_parquet", "output_excel", "output_plots", "work", "handoff_state"]
    state_dir = results_dir / "handoff_state"

    # Handle restart (full clean slate)
    if args.restart:
        import shutil
        for folder in inner_output_folders:
            folder_path = results_dir / folder
            if folder_path.exists():
                shutil.rmtree(folder_path)
            work_dir.mkdir(parents=True, exist_ok=True)
        # Clear parquet results but keep the results_dir itself
        for f in results_dir.glob("*.parquet"):
            f.unlink()
        print("  [restart] Cleared handoff state, work dir, and result parquets")

    # Handle reset (handoff state only)
    elif args.reset and state_dir.exists():
        import shutil
        shutil.rmtree(state_dir)
        print("  [state] Cleared saved handoff state")

    # Load persisted handoff state from previous run
    saved_overrides, iteration = load_handoff_from_disk(state_dir)
    print(f"\n  Coupling iteration: {iteration}")
    if saved_overrides:
        print(f"  Loaded {len(saved_overrides)} handoff field(s) from disk")

    # Compute the coupling date for this iteration using FlexTool timeline
    # Read timeline and rolling_solve_jump from the FlexTool database
    timelines = read_timeline_from_db(db_url)
    if timelines:
        timeline_name = next(iter(timelines))
        timeline_steps = timelines[timeline_name]
        print(f"  Timeline '{timeline_name}': {len(timeline_steps)} steps")
    else:
        timeline_steps = []
        print("  WARNING: No timeline found in DB")

    rolling_jumps = read_rolling_solve_jump(db_url)
    if rolling_jumps:
        # Use the first solve's rolling_solve_jump
        solve_name = next(iter(rolling_jumps))
        rolling_solve_jump_hours = rolling_jumps[solve_name]
        print(f"  Rolling solve jump: {rolling_solve_jump_hours}h (solve '{solve_name}')")
    else:
        # Fallback: sum all timeline step durations (= full timeline = one pass)
        rolling_solve_jump_hours = sum(d for _, d in timeline_steps) if timeline_steps else 168.0
        print(f"  WARNING: No rolling_solve_jump in DB, using {rolling_solve_jump_hours}h")

    # For indexed timesteps (tNNNN), we need an external start date reference
    start_date_for_coupling = None
    if timeline_steps and not is_datetime_format(timeline_steps[0][0]):
        start_date_for_coupling = cwatm_date_to_datetime(
            ini_sections["TIME-RELATED_CONSTANTS"]["StepStart"]
        )

    coupling_date = compute_coupling_date(
        iteration=iteration,
        timeline_steps=timeline_steps,
        rolling_solve_jump_hours=rolling_solve_jump_hours,
        start_date=start_date_for_coupling,
    )
    print(f"  Coupling date: {coupling_date}")

    # Reservoir storage source.  The first (--restart) solve runs *before*
    # CWatM, so no CWatM output exists yet: seed the reservoir storage from
    # the CWatM InitLoad file's ``reservoirStorage`` map instead.  Later
    # (rolling) solves read the CWatM storage written by process_data.py.
    if args.restart:
        initload_file = ini_sections.get("INITITIAL CONDITIONS", {}).get("initLoad")
        if not initload_file:
            print("ERROR: [INITITIAL CONDITIONS] initLoad not found in ini; "
                  "cannot seed reservoir storage for the first solve")
            sys.exit(1)
        print(f"  [restart] Seeding reservoir storage from InitLoad: {initload_file}")
        cwatm_storage = read_initload_storage(initload_file, reservoir_nodes)
    else:
        cwatm_storage = read_cwatm_storage(temp_dir, reservoir_nodes, date=coupling_date)
    if cwatm_storage:
        print(f"  Reservoir storage read for {len(cwatm_storage)} reservoir(s)")
    else:
        print(f"  No reservoir storage data available")

    # CWatM storage is the whole reservoir; FlexTool's "existing" storage is
    # only the usable (top) volume. Read the whole-reservoir volume from
    # lakesResVolRes.nc and convert the CWatM storage into the usable part.
    volume_path = ini_sections.get("LAKES_RESERVOIRS", {}).get("PathLakesRes")
    if cwatm_storage and volume_path:
        reservoir_volumes = read_reservoir_volumes(volume_path, reservoir_nodes)
        cwatm_storage = compute_usable_storage(
            cwatm_storage, reservoir_volumes, reservoir_nodes
        )
    elif cwatm_storage:
        print(" ERROR: LAKES_RESERVOIRS:PathLakesRes not in ini; cannot convert CWatM storage to usable storage")
        sys.exit(1)

    # Import FlexTool
    from spinedb_api.filters.tools import name_from_dict
    # Set up logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logger = logging.getLogger("flextool_coupled")

    # Create the override callback.  The state holder lets the callback
    # gate on ``current_roll_index`` so the storage override only lands
    # on the rolling sub-solve, not on chained non-rolling solves.
    state_holder: dict = {}
    override_callback = make_override_callback(
        saved_overrides=saved_overrides,
        cwatm_storage=cwatm_storage,
        iteration=iteration,
        state_holder=state_holder,
        restart=args.restart,
    )
    # Resolve scenario name: from argument or from DB filter configs
    if args.scenario:
        scenario_name = args.scenario
    else:
        with DatabaseMapping(db_url) as db_map:
            _filters = db_map.get_filter_configs()
            if _filters:
                scenario_name = name_from_dict(_filters[0])
            else:
                print("ERROR: No --scenario provided and no scenario filter in DB")
                sys.exit(1)

    print(f"\nStarting FlexTool solve (iteration {iteration}):")
    print(f"  Database: {db_url}")
    print(f"  Scenario: {scenario_name}")
    print(f"  Work dir: {work_dir}")
    print()

    # On the very first roll (iteration 0) start from a clean
    # ``output_raw/`` so the accumulating per-roll realized-slice
    # parquets + the ``_solve_order.txt`` creation-order manifest don't
    # carry stale entries from a previous (aborted) coupling run.  Later
    # iterations MUST keep the directory so the slices accumulate.
    if iteration == 0:
        import shutil
        raw_dir = work_dir / "output_raw"
        if raw_dir.exists():
            shutil.rmtree(raw_dir, ignore_errors=True)
            print("  [coupling] Cleared output_raw/ for a fresh roll sequence")

    # Run FlexTool for exactly ONE roll (roll index == iteration).
    # The recursive solve builder is patched to expand the full static
    # timeline into its roll sequence and keep only roll ``iteration``,
    # so each invocation advances one jump further along the timeline.
    # The roll's realized slice is persisted under work_dir/output_raw/
    # and unioned into the combined results by combine_outputs below.
    results = _run_one_roll(
        db_path=db_url,
        scenario_name=scenario_name,
        work_folder=work_dir,
        override_provider=override_callback,
        logger=logger,
        iteration=iteration,
        state_holder=state_holder,
        restart=args.restart,
    )

    # Report results
    print(f"\n{'='*60}")
    print("FlexTool solve complete.")
    print(f"{'='*60}")
    last_step = None
    for name, step in results.items():
        print(f"  {name}: optimal={step.optimal}, obj={step.obj:.4f}")
        last_step = step

    # Save handoff state for next invocation.  The first (--restart) solve
    # is a standalone full-timeline storage solve: it must NOT persist a
    # handoff, because the following rolling solves start from the very
    # beginning of the timeline (iteration 0) rather than continuing it.
    if args.restart:
        print("  [restart] Skipping handoff save; next solve starts from the beginning")
    elif last_step and last_step.handoff:
        save_handoff_to_disk(last_step.handoff, state_dir)
    else:
        print("  WARNING: No handoff available to save")

    # Union every roll solved so far into the combined full-timeline
    # results.  After the final roll these cover the entire timeline;
    # on intermediate rolls they cover the rolls completed up to now.
    if not args.restart:
        combine_outputs(
        results=results,
        scenario_name=scenario_name,
        work_folder=work_dir,
        results_dir=results_dir,
        settings_db_url=None,
        logger=logger,
    )

    print(f"\nResults in: {work_dir}")
    print("Run flextool_to_cwatm_dispatch.py to generate hp_dispatch.nc,")
    print("then run CWatM, then invoke this script again for the next roll.")


if __name__ == "__main__":
    main()
