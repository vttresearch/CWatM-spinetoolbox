"""
Datetime conversion between FlexTool and CWatM time formats.

FlexTool time columns can be:
  - ISO datetime strings: "2023-01-01T00:00:00"
  - Indexed timesteps: "t0001", "t0048" (variable zero-padding)

CWatM uses:
  - dd/mm/yyyy format in INI files (e.g. "01/01/2050")
  - netCDF time as "days since 1901-01-01"

This module provides conversion between the two systems by reading
the timeline configuration from the FlexTool Spine database.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta


# Pattern matching FlexTool indexed timesteps: t0001, t001, t00048, etc.
_TIMESTEP_PATTERN = re.compile(r"^t(\d+)$")


def is_datetime_format(time_str: str) -> bool:
    """Check if a FlexTool time string is an ISO datetime."""
    return bool(re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", str(time_str)))


def is_indexed_timestep(time_str: str) -> bool:
    """Check if a FlexTool time string is an indexed timestep (e.g. t0001)."""
    return bool(_TIMESTEP_PATTERN.match(str(time_str)))


def timestep_index(time_str: str) -> int:
    """Extract the integer index from a timestep string like 't0001' -> 1."""
    m = _TIMESTEP_PATTERN.match(str(time_str))
    if not m:
        raise ValueError(f"Not a valid timestep format: {time_str!r}")
    return int(m.group(1))


def read_timeline_from_db(db_url: str) -> dict[str, list[tuple[str, float]]]:
    """
    Read all timeline definitions from the FlexTool database.

    Returns a dict: timeline_name -> [(timestep_id, duration_hours), ...]

    The timestep_id is the key in the timestep_duration Map parameter,
    and duration_hours is the value. The list is ordered as stored.
    """
    from spinedb_api import DatabaseMapping

    url = str(db_url)
    if "://" not in url:
        url = f"sqlite:///{url}"

    timelines: dict[str, list[tuple[str, float]]] = {}

    with DatabaseMapping(url) as db_map:
        db_map.fetch_all("entity")
        db_map.fetch_all("parameter_value")

        for pval in db_map.find_parameter_values(
            entity_class_name="timeline",
            parameter_definition_name="timestep_duration",
        ):
            timeline_name = pval["entity_name"]
            value = pval["parsed_value"]

            # The value is a spinedb_api.Map: indexes are timestep IDs,
            # values are durations in hours
            steps = []
            if hasattr(value, "indexes") and hasattr(value, "values"):
                for idx, val in zip(value.indexes, value.values):
                    steps.append((str(idx), float(val)))
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        steps.append((str(item[0]), float(item[1])))

            if steps:
                timelines[timeline_name] = steps

    return timelines


def build_datetime_index(
    time_ids: list[str],
    timeline_steps: list[tuple[str, float]],
    start_date: datetime,
) -> list[datetime]:
    """
    Convert FlexTool timestep IDs to datetime objects.

    For indexed timesteps (t0001, t0002, ...), uses the timeline duration
    map and a start_date to compute cumulative offsets.

    For ISO datetime strings, parses them directly.

    Parameters
    ----------
    time_ids : list[str]
        The time column values from FlexTool output (e.g. ['t0001', 't0002', ...])
    timeline_steps : list[tuple[str, float]]
        Timeline definition: [(timestep_id, duration_hours), ...]
        Only needed when time_ids are indexed timesteps.
    start_date : datetime
        The start date of the simulation. Only needed when time_ids are
        indexed timesteps. Corresponds to CWatM's StepStart or StepFlexTool.

    Returns
    -------
    list[datetime]
        One datetime per timestep, representing the START of that timestep.
    """
    if not time_ids:
        return []

    # Case 1: ISO datetime strings
    if is_datetime_format(time_ids[0]):
        dates = []
        for t in time_ids:
            # Handle both "2023-01-01T00:00:00" and "2023-01-01 00:00:00"
            t_str = str(t).replace("T", " ")
            # Try common formats
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dates.append(datetime.strptime(t_str, fmt))
                    break
                except ValueError:
                    continue
            else:
                raise ValueError(f"Cannot parse FlexTool datetime: {t!r}")
        return dates

    # Case 2: Indexed timesteps (t0001, t0002, ...)
    if is_indexed_timestep(time_ids[0]):
        # Build a lookup: timestep_id -> cumulative hours from start
        step_to_hours: dict[str, float] = {}
        cumulative = 0.0
        for step_id, duration in timeline_steps:
            step_to_hours[step_id] = cumulative
            cumulative += duration

        dates = []
        for t_id in time_ids:
            if t_id in step_to_hours:
                offset_hours = step_to_hours[t_id]
            else:
                # Fallback: use index position * first duration
                idx = timestep_index(t_id) - 1  # t0001 -> index 0
                if timeline_steps:
                    offset_hours = idx * timeline_steps[0][1]
                else:
                    offset_hours = float(idx)
            dates.append(start_date + timedelta(hours=offset_hours))
        return dates

    # Case 3: Unknown format — try to parse as-is
    raise ValueError(
        f"Unknown FlexTool time format: {time_ids[0]!r}. "
        f"Expected ISO datetime (2023-01-01T00:00:00) or indexed timestep (t0001)."
    )


def get_timestep_duration_hours(
    time_ids: list[str],
    timeline_steps: list[tuple[str, float]],
) -> float:
    """
    Determine the timestep duration in hours.

    For ISO datetime: computed from the difference between first two timestamps.
    For indexed timesteps: read from the timeline definition.

    Parameters
    ----------
    time_ids : list[str]
        The time column values from FlexTool output.
    timeline_steps : list[tuple[str, float]]
        Timeline definition from DB.

    Returns
    -------
    float
        Duration of one timestep in hours.
    """
    if not time_ids:
        return 1.0

    if is_datetime_format(time_ids[0]) and len(time_ids) >= 2:
        dates = build_datetime_index(time_ids[:2], timeline_steps, datetime.min)
        delta = dates[1] - dates[0]
        return delta.total_seconds() / 3600.0

    if is_indexed_timestep(time_ids[0]) and timeline_steps:
        # Find the duration for the first time_id in the timeline
        for step_id, duration in timeline_steps:
            if step_id == time_ids[0]:
                return duration
        # Fallback to first entry
        return timeline_steps[0][1]

    return 1.0


def cwatm_date_to_datetime(date_str: str) -> datetime:
    """Parse a CWatM date string (dd/mm/yyyy) to datetime."""
    return datetime.strptime(date_str.strip(), "%d/%m/%Y")


def datetime_to_cwatm_date(dt: datetime) -> str:
    """Format a datetime as CWatM date string (dd/mm/yyyy)."""
    return dt.strftime("%d/%m/%Y")


def read_rolling_solve_jump(db_url: str) -> dict[str, float]:
    """
    Read rolling_solve_jump (hours) per solve from the FlexTool database.

    Parameters
    ----------
    db_url : str
        Spine database URL.

    Returns
    -------
    dict mapping solve_name -> jump_hours
    """
    from spinedb_api import DatabaseMapping

    url = str(db_url)
    if "://" not in url:
        url = f"sqlite:///{url}"

    result = {}
    with DatabaseMapping(url) as db_map:
        db_map.fetch_all("entity")
        db_map.fetch_all("parameter_value")

        for pval in db_map.find_parameter_values(
            entity_class_name="solve",
            parameter_definition_name="rolling_solve_jump",
        ):
            solve_name = pval["entity_name"]
            value = pval["parsed_value"]
            result[solve_name] = float(value)

    return result


def compute_coupling_date(
    iteration: int,
    timeline_steps: list[tuple[str, float]],
    rolling_solve_jump_hours: float,
    start_date: datetime | None = None,
) -> datetime:
    """
    Compute the coupling date for a given iteration.

    For iteration 0 (first roll): returns the start of the first timeline step.
    For later iterations: returns first_step_start + iteration * rolling_solve_jump.

    If the timeline uses ISO datetime format, the start date is parsed
    directly from the first timestep ID. If it uses indexed format (tNNNN),
    the provided start_date is used as the absolute reference.

    Parameters
    ----------
    iteration : int
        Current coupling iteration (0-based).
    timeline_steps : list[tuple[str, float]]
        Timeline definition: [(timestep_id, duration_hours), ...]
    rolling_solve_jump_hours : float
        Hours between roll start points (from solve.rolling_solve_jump).
    start_date : datetime, optional
        Absolute start date (needed when timeline uses tNNNN format).
        Typically from CWatM INI StepStart.

    Returns
    -------
    datetime
        The date at which to read CWatM storage for this iteration.
    """
    if not timeline_steps:
        if start_date is None:
            raise ValueError("No timeline steps and no start_date provided")
        return start_date + timedelta(hours=rolling_solve_jump_hours * iteration)

    first_step_id = timeline_steps[0][0]

    # Determine the absolute start datetime
    if is_datetime_format(first_step_id):
        # Timeline uses ISO datetime — parse directly
        t_str = first_step_id.replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                base_date = datetime.strptime(t_str, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Cannot parse timeline start: {first_step_id!r}")
    else:
        # Timeline uses tNNNN — need external start_date
        if start_date is None:
            raise ValueError(
                f"Timeline uses indexed format ({first_step_id!r}) but no "
                f"start_date provided"
            )
        base_date = start_date

    # Offset by iteration * jump
    return base_date + timedelta(hours=rolling_solve_jump_hours * iteration)
