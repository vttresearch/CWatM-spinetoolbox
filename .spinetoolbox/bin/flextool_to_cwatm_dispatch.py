"""
FlexTool results to CWatM dispatch converter.
Reads v_flow parquet from FlexTool and creates hp_dispatch.nc for CWatM.

Usage:
    python flextool_to_cwatm_dispatch.py --results_dir <flextool_results_folder> \
        --ini cwatm_input.ini \
        --unit_conversion 1.0

    The script searches for a v_flow__*.parquet file in the results folder.
    Output path and start date are read from cwatm_input.ini.
"""

import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from coupling_datetime import (
    build_datetime_index,
    cwatm_date_to_datetime,
    get_timestep_duration_hours,
    is_datetime_format,
    is_indexed_timestep,
    read_timeline_from_db,
)


def parse_ini(ini_path):
    """
    Parse CWatM settings ini (TOML-like) and resolve variable references.

    Returns a flat dict of all key=value pairs with section prefix,
    and a nested dict {section: {key: value}}.
    """
    ini_path = Path(ini_path)
    sections = {}
    current_section = None

    with open(ini_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Section header
            m = re.match(r"\[(.+)\]", line)
            if m:
                current_section = m.group(1)
                sections.setdefault(current_section, {})
                continue
            # Key = value
            if "=" in line and current_section is not None:
                key, _, val = line.partition("=")
                sections[current_section][key.strip()] = val.strip()

    # Resolve $(SECTION:Key) references
    flat = {}
    for sec, kvs in sections.items():
        for k, v in kvs.items():
            flat[f"{sec}:{k}"] = v
            flat[k] = v  # also store without section prefix

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


def parse_flow_columns(df):
    """
    Parse v_flow column names to identify turbine and spill flows per reservoir.

    FlexTool v_flow columns are tuples: (unit, source_node, sink_node)
    - Turbine intake from reservoir: (hydro_unit, reservoir_node, hydro_unit)
      This represents water leaving the reservoir through the turbine.
    - Spill flow: (spill_unit, reservoir_node, downstream_node)
      This represents water leaving via spillway.

    Returns
    -------
    turbine_cols : dict mapping reservoir_name -> column name (intake flow)
    spill_cols : dict mapping reservoir_name -> column name (spill flow)
    """
    turbine_cols = {}
    spill_cols = {}

    flow_cols = [c for c in df.columns if c not in ('solve', 'period', 'time')]

    for col in flow_cols:
        # Column names are string representations of tuples
        parts = col.strip("()").split(", ")
        parts = [p.strip("' \"") for p in parts]

        if len(parts) != 3:
            continue

        unit_name, source_node, sink_node = parts

        if 'spill' in unit_name.lower():
            # Spill unit: source_node is the reservoir it drains from
            spill_cols[source_node] = col
        elif ('hydro' in unit_name.lower() or 'turbine' in unit_name.lower()):
            # Turbine intake: pattern is (unit, reservoir, unit)
            # source_node is the reservoir, sink_node is the unit itself
            if sink_node == unit_name:
                # This is the intake from reservoir → turbine discharge
                turbine_cols[source_node] = col

    return turbine_cols, spill_cols


def identify_reservoirs(turbine_cols, spill_cols):
    """
    Identify unique reservoirs from turbine and spill column mappings.
    Returns ordered list of reservoir names.
    """
    # Reservoir names come from the hydro_plant source or spill source
    reservoirs = set()
    for source in turbine_cols.keys():
        reservoirs.add(source)
    for source in spill_cols.keys():
        reservoirs.add(source)
    return sorted(reservoirs)


def build_dispatch(df, turbine_cols, spill_cols, reservoirs, unit_conversion,
                   seconds_per_step):
    """
    Build Q_turbine and Q_spill arrays from the flow dataframe.

    FlexTool v_flow is the flow accumulated over one timestep; dividing by
    ``seconds_per_step`` converts it to a per-second rate (m3/s).

    Parameters
    ----------
    df : DataFrame with flow data (only flow timesteps, no metadata cols)
    turbine_cols : dict mapping reservoir_name -> column with turbine flow
    spill_cols : dict mapping reservoir_name -> column with spill flow
    reservoirs : list of reservoir names (ordered)
    unit_conversion : float, multiplier applied to the FlexTool flow units
    seconds_per_step : float, duration of one timestep in seconds

    Returns
    -------
    Q_turbine : ndarray (n_timesteps, n_reservoirs) in m3/s
    Q_spill : ndarray (n_timesteps, n_reservoirs) in m3/s
    """
    n_t = len(df)
    n_res = len(reservoirs)

    factor = unit_conversion / seconds_per_step

    Q_turbine = np.zeros((n_t, n_res), dtype=np.float64)
    Q_spill = np.zeros((n_t, n_res), dtype=np.float64)

    for i, res_name in enumerate(reservoirs):
        if res_name in turbine_cols:
            Q_turbine[:, i] = df[turbine_cols[res_name]].values * factor
        if res_name in spill_cols:
            Q_spill[:, i] = df[spill_cols[res_name]].values * factor

    return Q_turbine, Q_spill


def generate_dates(start_date, n_timesteps, timestep_hours):
    """Generate datetime list from start date and timestep."""
    return [start_date + timedelta(hours=timestep_hours * i)
            for i in range(n_timesteps)]


def aggregate_daily(dates, Q_turbine, Q_spill):
    """
    Average sub-daily values into one value per calendar day.

    Each day's value is the mean of its timesteps, timestamped at the first
    hour (00:00) of that day. Day order follows first appearance.

    Returns
    -------
    daily_dates : list of datetime (one per day, at 00:00)
    Q_turbine_daily : ndarray (n_days, n_reservoirs)
    Q_spill_daily : ndarray (n_days, n_reservoirs)
    """
    days = pd.DatetimeIndex(dates).floor("D")
    unique_days = pd.unique(days)

    n_res = Q_turbine.shape[1]
    Q_turbine_daily = np.zeros((len(unique_days), n_res), dtype=np.float64)
    Q_spill_daily = np.zeros((len(unique_days), n_res), dtype=np.float64)
    daily_dates = []

    for k, day in enumerate(unique_days):
        mask = days == day
        Q_turbine_daily[k] = Q_turbine[mask].mean(axis=0)
        Q_spill_daily[k] = Q_spill[mask].mean(axis=0)
        daily_dates.append(pd.Timestamp(day).to_pydatetime())

    return daily_dates, Q_turbine_daily, Q_spill_daily


def _get_lat_lon(entry):
    """
    Extract (lat, lon) from a reservoir_map entry.

    Supports entries shaped as {"lat": .., "lon": ..} (also accepts
    "latitude"/"longitude") or a [lat, lon] / (lat, lon) pair. Returns
    (None, None) when no coordinate is available (e.g. a legacy index map).
    """
    if isinstance(entry, dict):
        lat = entry.get("lat", entry.get("latitude"))
        lon = entry.get("lon", entry.get("longitude"))
        if lat is not None and lon is not None:
            return float(lat), float(lon)
        return None, None
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return float(entry[0]), float(entry[1])
    return None, None


def _get_id(entry):
    """
    Extract an optional CWatM waterBodyID from a reservoir_map entry.

    Accepts ``{"id": ..}`` or ``{"waterBodyID": ..}``. Returns None when the
    entry carries no id (e.g. a bare [lat, lon] pair).
    """
    if isinstance(entry, dict):
        rid = entry.get("id", entry.get("waterBodyID"))
        if rid is not None:
            return int(rid)
    return None


def load_reservoir_filter(value):
    """
    Load the list of reservoir names to transfer.

    ``value`` is either a path to a JSON file containing a list of names
    (or an object whose keys are names) or a comma-separated string.
    """
    p = Path(value)
    if p.is_file():
        with open(p) as f:
            data = json.load(f)
        return data.get("Reservoirs", [])
    return [n.strip() for n in value.split(",") if n.strip()]


def filter_reservoirs_by_names(reservoirs, names):
    """
    Keep only reservoirs whose FlexTool name is in ``names`` (matched by
    name). Reservoirs absent from the list are not written to hp_dispatch.nc.
    """
    wanted = set(names)
    kept = [r for r in reservoirs if r in wanted]
    for r in reservoirs:
        if r not in wanted:
            print(f"  Skipping reservoir not in filter: {r}")
    return kept


def order_reservoirs_by_coords(reservoirs, res_map, decimals=2):
    """
    Order reservoirs and attach coordinates using a coordinate map.

    Coordinates are rounded to ``decimals`` decimals for the match/ordering
    only; the coordinates returned for the NetCDF are left un-rounded. This
    does not filter reservoirs -- if any reservoir lacks coordinates, the
    original order is kept and no coordinates are returned.

    Parameters
    ----------
    reservoirs : list of FlexTool reservoir names (already filtered)
    res_map : dict mapping FlexTool reservoir name -> {"lat": .., "lon": ..}
    decimals : int, number of decimals used when matching coordinates

    Returns
    -------
    ordered : list of reservoir names
    coords : list of (lat, lon) tuples aligned with ``ordered`` (or None
             when coordinates are unavailable)
    """
    missing = [n for n in reservoirs
               if n not in res_map or _get_lat_lon(res_map[n]) == (None, None)]
    if missing:
        print(f"  No coordinate ordering; missing coordinates for: {missing}")
        return reservoirs, None

    def sort_key(n):
        lat, lon = _get_lat_lon(res_map[n])
        # Round to `decimals` decimals for matching / ordering only.
        return (round(lat, decimals), round(lon, decimals), n)

    ordered = sorted(reservoirs, key=sort_key)
    # Stored coordinates stay un-rounded.
    coords = [_get_lat_lon(res_map[n]) for n in ordered]
    return ordered, coords


def write_dispatch_nc(output_path, dates, reservoirs, Q_turbine, Q_spill,
                      coords=None, ids=None):
    """
    Write hp_dispatch.nc in the format expected by CWatM coupling interface.

    Dimensions: time (unlimited), reservoir
    Variables: time, Q_turbine(time, reservoir), Q_spill(time, reservoir)
    Optional reservoir_lat/reservoir_lon(reservoir) when ``coords`` is given,
    and reservoir_id(reservoir) (CWatM waterBodyID) when ``ids`` is given.
    """
    import netCDF4 as nc4

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with nc4.Dataset(str(output_path), "w", format="NETCDF4") as ds:
        ds.createDimension("time", len(dates))
        ds.createDimension("reservoir", len(reservoirs))

        # Time variable
        tv = ds.createVariable("time", "f8", ("time",))
        tv.units = "days since 1901-01-01"
        tv.calendar = "standard"
        tv[:] = nc4.date2num(dates, units=tv.units, calendar=tv.calendar)
        print("dates", dates)
        # Reservoir names
        ds.createDimension("name_strlen", 64)
        res_var = ds.createVariable("reservoir_name", "S1",
                                    ("reservoir", "name_strlen"))
        for i, name in enumerate(reservoirs):
            print(name)
            res_var[i, :len(name)] = list(name)

        # Reservoir CWatM waterBodyID, when available (preferred match key)
        if ids is not None:
            id_var = ds.createVariable("reservoir_id", "i8", ("reservoir",))
            id_var.long_name = "CWatM waterBodyID"
            id_var[:] = ids

        # Reservoir coordinates (stored un-rounded), when available
        if coords is not None:
            lat_var = ds.createVariable("reservoir_lat", "f8", ("reservoir",))
            lat_var.units = "degrees_north"
            lat_var.long_name = "Reservoir latitude"
            lon_var = ds.createVariable("reservoir_lon", "f8", ("reservoir",))
            lon_var.units = "degrees_east"
            lon_var.long_name = "Reservoir longitude"
            lat_var[:] = [c[0] for c in coords]
            lon_var[:] = [c[1] for c in coords]

        # Q_turbine
        qt = ds.createVariable("Q_turbine", "f4", ("time", "reservoir"),
                               fill_value=-9999.0)
        qt.units = "m3 s-1"
        qt.long_name = "Turbine discharge from energy model dispatch"
        qt[:] = Q_turbine
        print("Q_turbine", Q_turbine)

        # Q_spill
        qs = ds.createVariable("Q_spill", "f4", ("time", "reservoir"),
                               fill_value=-9999.0)
        qs.units = "m3 s-1"
        qs.long_name = "Spillway discharge from energy model dispatch"
        qs[:] = Q_spill
        print("Q_spill", Q_spill)
        # Global attributes
        ds.description = "Hydropower dispatch from FlexTool for CWatM coupling"
        ds.source = "flextool_to_cwatm_dispatch.py"
        ds.history = f"Created {datetime.now().isoformat()}"


def main():
    parser = argparse.ArgumentParser(
        description="Convert FlexTool v_flow results to CWatM hp_dispatch.nc"
    )
    parser.add_argument("db",
                        help="Path to FlexTool Spine database (.sqlite)")
    parser.add_argument("--results_dir", type=str, required=True,
                        help="Path to FlexTool results folder (searches for "
                             "v_flow__ parquet file inside)")
    parser.add_argument("--ini", type=str, default=None,
                        help="Path to cwatm_input.ini (reads output path and "
                             "start date from it)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output path for hp_dispatch.nc "
                             "(overrides ini hp_dispatch_in_path)")
    parser.add_argument("--start_date", type=str, default=None,
                        help="Start date (YYYY-MM-DD). "
                             "If omitted, read from ini StepFlexTool")
    parser.add_argument("--timestep_hours", type=float, default=None,
                        help="Timestep duration in hours. "
                             "If omitted, read from FlexTool DB timeline")
    parser.add_argument("--unit_conversion", type=float, default=1.0,
                        help="Multiplier to convert FlexTool flow units to m3/s")
    parser.add_argument("--reservoir_filter", type=str, default=None,
                        help="Reservoirs to transfer, matched by name. Either "
                             "a comma-separated list of names or a path to a "
                             "JSON file containing a list of names. Only these "
                             "reservoirs are written to hp_dispatch.nc.")
    parser.add_argument("--reservoir_map", type=str, default=None,
                        help="JSON file mapping FlexTool node names to CWatM "
                             "reservoir coordinates, e.g. "
                             '{"R1_rogun_reservoir": {"lat": 38.68, "lon": '
                             "69.77}}. An optional \"id\" (CWatM waterBodyID) "
                             "per entry is written as reservoir_id for robust "
                             "matching. Used for coordinate/id matching and "
                             "ordering, not for filtering.")
    parser.add_argument("--coord_decimals", type=int, default=2,
                        help="Decimals used when matching reservoir "
                             "coordinates to the reservoir_map (default: 2). "
                             "Stored coordinates are left un-rounded.")

    args = parser.parse_args()

    # Parse ini if provided
    ini = None
    if args.ini:
        ini = parse_ini(args.ini)

    # Resolve output path
    output_path = args.output
    if output_path is None and ini:
        output_path = ini.get("LAKES_RESERVOIRS", {}).get("hp_dispatch_in_path")
    if output_path is None:
        parser.error("--output is required when --ini is not provided or "
                     "ini lacks hp_dispatch_in_path")

    # Read timeline from FlexTool database
    db_url = args.db
    if "://" not in db_url:
        db_url = f"sqlite:///{db_url}"
    timelines = read_timeline_from_db(db_url)
    # Use the first (or only) timeline
    timeline_steps = []
    if timelines:
        timeline_name = next(iter(timelines))
        timeline_steps = timelines[timeline_name]
        print(f"Timeline '{timeline_name}': {len(timeline_steps)} steps, "
              f"first='{timeline_steps[0][0]}', dur={timeline_steps[0][1]}h")

    # Resolve start date from INI (CWatM format: dd/mm/yyyy)
    start_date = None
    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
    elif ini:
        raw = ini.get("TIME-RELATED_CONSTANTS", {}).get("StepStart")
        if raw:
            start_date = cwatm_date_to_datetime(raw)
    if start_date is None:
        parser.error("--start_date is required when --ini is not provided or "
                     "ini lacks StepStart")

    # Find v_flow parquet file in results directory
    results_dir = Path(args.results_dir) / "work" / "output_raw"
    flow_files = list(results_dir.glob("v_flow__*.parquet"))
    if not flow_files:
        parser.error(f"No v_flow__*.parquet file found in {results_dir}")
    if len(flow_files) > 1:
        print(f"WARNING: Multiple v_flow files found, using: {flow_files[0].name}")
    flow_file = flow_files[0]
    print(f"Using flow file: {flow_file}")

    # Read flow data
    df = pd.read_parquet(flow_file)
    n_timesteps = len(df)

    # Build datetime index from FlexTool time column
    time_col_values = df["time"].tolist() if "time" in df.columns else []

    if time_col_values and is_datetime_format(str(time_col_values[0])):
        # FlexTool already uses datetime format — parse directly
        dates = build_datetime_index(
            [str(t) for t in time_col_values], timeline_steps, start_date
        )
        timestep_hours = args.timestep_hours
        if timestep_hours is None and len(dates) >= 2:
            timestep_hours = (dates[1] - dates[0]).total_seconds() / 3600.0
        if timestep_hours is None:
            timestep_hours = 1.0
        print(f"  Time format: ISO datetime, timestep={timestep_hours}h")
    elif time_col_values and is_indexed_timestep(str(time_col_values[0])):
        # FlexTool uses tNNNN format — convert using timeline from DB
        dates = build_datetime_index(
            [str(t) for t in time_col_values], timeline_steps, start_date
        )
        timestep_hours = args.timestep_hours
        if timestep_hours is None:
            timestep_hours = get_timestep_duration_hours(
                [str(t) for t in time_col_values], timeline_steps
            )
        print(f"  Time format: indexed (t0001..), timestep={timestep_hours}h")
        print(f"  Mapped: {time_col_values[0]} -> {dates[0]}, "
              f"{time_col_values[-1]} -> {dates[-1]}")
    else:
        # Fallback: generate dates from start + duration
        timestep_hours = args.timestep_hours or 1.0
        dates = generate_dates(start_date, n_timesteps, timestep_hours)
        print(f"  Time format: unknown/missing, using start_date + {timestep_hours}h steps")

    # Parse columns to identify turbine and spill flows
    turbine_cols, spill_cols = parse_flow_columns(df)

    if not turbine_cols and not spill_cols:
        print("ERROR: No turbine or spill flow columns identified in the file.")
        print("Available columns:", [c for c in df.columns
                                      if c not in ('solve', 'period', 'time')])
        return

    # Identify reservoirs
    reservoirs = identify_reservoirs(turbine_cols, spill_cols)
    print(reservoirs)
    print(f"Identified {len(reservoirs)} reservoir(s): {reservoirs}")
    print(f"Turbine flow columns: {turbine_cols}")
    print(f"Spill flow columns: {spill_cols}")

    # Coordinates aligned with reservoirs (populated when a map is provided)
    reservoir_coords = None
    # CWatM waterBodyID aligned with reservoirs (populated from the map)
    reservoir_ids = None

    # Apply the name-based reservoir filter (matched by name).
    if args.reservoir_filter:
        filter_names = load_reservoir_filter(args.reservoir_filter)
        reservoirs = filter_reservoirs_by_names(reservoirs, filter_names)
        print(f"After name filter: {len(reservoirs)} reservoir(s) "
              f"transferred: {reservoirs}")

    if not reservoirs:
        print("ERROR: No reservoirs left to transfer after filtering.")
        return

    # Optional coordinate matching: order reservoirs and attach lat/lon,
    # rounding coordinates to args.coord_decimals decimals for the match.
    if args.reservoir_map:
        with open(args.reservoir_map) as f:
            res_map = json.load(f)
        reservoirs, reservoir_coords = order_reservoirs_by_coords(
            reservoirs, res_map, decimals=args.coord_decimals
        )
        print(f"After coordinate ordering "
              f"({args.coord_decimals}-decimal match): {reservoirs}")

        # Attach CWatM waterBodyID when every kept reservoir has one.
        ids = [_get_id(res_map.get(n)) for n in reservoirs]
        if all(i is not None for i in ids):
            reservoir_ids = ids
            print(f"Reservoir waterBodyIDs: {reservoir_ids}")
        elif any(i is not None for i in ids):
            missing = [n for n, i in zip(reservoirs, ids) if i is None]
            print(f"  No reservoir_id written; missing id for: {missing}")

    print(reservoirs)
    # Convert per-timestep flow volumes to a per-second rate (m3/s).
    seconds_per_step = timestep_hours * 3600.0
    Q_turbine, Q_spill = build_dispatch(
        df, turbine_cols, spill_cols, reservoirs, args.unit_conversion,
        seconds_per_step
    )

    # CWatM uses daily values: average sub-daily flows to a daily mean,
    # timestamped at the first hour of each day.
    dates, Q_turbine, Q_spill = aggregate_daily(dates, Q_turbine, Q_spill)

    # Write output
    write_dispatch_nc(output_path, dates, reservoirs, Q_turbine, Q_spill,
                      coords=reservoir_coords, ids=reservoir_ids)

    print(f"Dispatch written: {output_path}")
    print(f"  Sub-daily timesteps read: {n_timesteps}")
    print(f"  Daily timesteps written: {len(dates)}")
    print(f"  Date range: {dates[0]} to {dates[-1]}")
    print(f"  Source timestep: {timestep_hours} hours "
          f"({seconds_per_step:.0f} s)")
    print(f"  Unit conversion factor: {args.unit_conversion / seconds_per_step} "
          f"(flow/step -> m3/s)")
    print(f"  Q_turbine range: [{Q_turbine.min():.4f}, {Q_turbine.max():.4f}] m3/s")
    print(f"  Q_spill range: [{Q_spill.min():.4f}, {Q_spill.max():.4f}] m3/s")


if __name__ == "__main__":
    main()
