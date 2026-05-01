"""
Extract a time series for a specific lat/lon point from a NetCDF file.

Modes:
  --year YYYY               Extract a single full year
  --start YYYY-MM-DD        Extract between two dates (requires --end)
  --end   YYYY-MM-DD

Usage:
    python extract_nc_timeseries.py <file.nc> --lat <lat> --lon <lon> [--year YEAR | --start DATE --end DATE] [options]

Examples:
    # Single year
    python extract_nc_timeseries.py data.nc --lat 60.17 --lon 24.94 --year 2010

    # Date range
    python extract_nc_timeseries.py data.nc --lat 60.17 --lon 24.94 --start 2005-06-01 --end 2010-12-31

    # With variable selection and plot
    python extract_nc_timeseries.py data.nc --lat 60.17 --lon 24.94 --year 2010 --var temperature --plot
"""

import argparse
import sys
from datetime import datetime
import numpy as np

try:
    import xarray as xr
except ImportError:
    sys.exit("Missing dependency: pip install xarray netCDF4")


def find_nearest(array, value):
    """Return the index of the nearest value in an array."""
    return int(np.abs(array - value).argmin())


def extract_timeseries(nc_file, lat, lon, year=None, date_start=None, date_end=None,
                       variable=None, plot=False):
    """
    Extract a time series for a given lat/lon point.

    Parameters
    ----------
    nc_file     : str   – path to the NetCDF file
    lat         : float – target latitude
    lon         : float – target longitude
    year        : int   – single year to extract (mutually exclusive with date range)
    date_start  : str   – start date string 'YYYY-MM-DD' (use with date_end)
    date_end    : str   – end date string   'YYYY-MM-DD' (use with date_start)
    variable    : str   – variable name (auto-detected if None)
    plot        : bool  – show a quick matplotlib plot
    """
    # Validate mode
    if year is not None and (date_start is not None or date_end is not None):
        sys.exit("Error: --year and --start/--end are mutually exclusive.")
    if (date_start is None) != (date_end is None):
        sys.exit("Error: --start and --end must be used together.")
    if year is None and date_start is None:
        sys.exit("Error: provide either --year or both --start and --end.")

    # ------------------------------------------------------------------ #
    # 1. Open dataset                                                      #
    # ------------------------------------------------------------------ #
    print(f"\nOpening: {nc_file}")
    ds = xr.open_dataset(nc_file, use_cftime=True)

    print("\n── Dataset overview ──────────────────────────────────────")
    print(ds)

    # ------------------------------------------------------------------ #
    # 2. Resolve variable name                                            #
    # ------------------------------------------------------------------ #
    data_vars = list(ds.data_vars)
    if not data_vars:
        sys.exit("No data variables found in the file.")

    if variable is None:
        variable = data_vars[0]
        if len(data_vars) > 1:
            print(f"\nMultiple variables found: {data_vars}")
            print(f"Auto-selected: '{variable}'  (use --var to override)")
    elif variable not in data_vars:
        sys.exit(f"Variable '{variable}' not found. Available: {data_vars}")

    da = ds[variable]
    print(f"\nExtracting variable : '{variable}'")
    print(f"Dimensions          : {dict(da.dims)}")

    # ------------------------------------------------------------------ #
    # 3. Detect coordinate names (flexible naming)                        #
    # ------------------------------------------------------------------ #
    def detect_coord(candidates, da):
        for name in candidates:
            for coord in da.coords:
                if coord.lower() == name.lower():
                    return coord
        return None

    lat_name = detect_coord(["lat", "latitude", "y"], da)
    lon_name = detect_coord(["lon", "longitude", "x"], da)
    time_name = detect_coord(["time", "t", "date"], da)

    if not lat_name:
        sys.exit("Could not detect latitude coordinate. Check coordinate names.")
    if not lon_name:
        sys.exit("Could not detect longitude coordinate. Check coordinate names.")
    if not time_name:
        sys.exit("Could not detect time coordinate. Check coordinate names.")

    print(f"Latitude coord      : '{lat_name}'")
    print(f"Longitude coord     : '{lon_name}'")
    print(f"Time coord          : '{time_name}'")

    # ------------------------------------------------------------------ #
    # 4. Nearest-neighbour lat/lon selection                              #
    # ------------------------------------------------------------------ #
    lat_vals = da[lat_name].values.astype(float)
    lon_vals = da[lon_name].values.astype(float)

    lat_idx = find_nearest(lat_vals, lat)
    lon_idx = find_nearest(lon_vals, lon)

    actual_lat = float(lat_vals[lat_idx])
    actual_lon = float(lon_vals[lon_idx])

    print(f"\nRequested point     : lat={lat}, lon={lon}")
    print(f"Nearest grid point  : lat={actual_lat:.4f}, lon={actual_lon:.4f}")
    print(f"Grid indices        : lat_idx={lat_idx}, lon_idx={lon_idx}")

    # ------------------------------------------------------------------ #
    # 5. Select point and filter by year or date range                    #
    # ------------------------------------------------------------------ #
    point = da.sel({lat_name: actual_lat, lon_name: actual_lon}, method="nearest")

    if year is not None:
        # ── Single year mode ──────────────────────────────────────────
        try:
            point_filtered = point.sel({time_name: str(year)})
        except KeyError:
            point_filtered = point.where(point[time_name].dt.year == year, drop=True)

        if point_filtered.sizes[time_name] == 0:
            sys.exit(f"No data found for year {year}. Check the time range of your file.")

        print(f"\nYear filtered       : {year}  ({point_filtered.sizes[time_name]} time steps)")
        period_label = str(year)

    else:
        # ── Date range mode ───────────────────────────────────────────
        try:
            datetime.strptime(date_start, "%Y-%m-%d")
            datetime.strptime(date_end,   "%Y-%m-%d")
        except ValueError as e:
            sys.exit(f"Invalid date format ({e}). Use YYYY-MM-DD.")

        if date_start > date_end:
            sys.exit("Error: --start date must be before or equal to --end date.")

        try:
            point_filtered = point.sel({time_name: slice(date_start, date_end)})
        except Exception:
            # Fallback for cftime or string-incompatible time axes
            times = point[time_name].values
            try:
                import cftime
                mask = np.array([
                    date_start <= t.strftime("%Y-%m-%d") <= date_end
                    for t in times
                ])
            except Exception:
                mask = np.array([
                    date_start <= str(t)[:10] <= date_end
                    for t in times
                ])
            point_filtered = point.isel({time_name: np.where(mask)[0]})

        if point_filtered.sizes[time_name] == 0:
            sys.exit(f"No data found between {date_start} and {date_end}. "
                     f"Check the time range of your file.")

        print(f"\nDate range          : {date_start} → {date_end}  "
              f"({point_filtered.sizes[time_name]} time steps)")
        period_label = f"{date_start} to {date_end}"

    # ------------------------------------------------------------------ #
    # 6. Build a clean results table                                      #
    # ------------------------------------------------------------------ #
    times = point_filtered[time_name].values
    values = point_filtered.values

    units = da.attrs.get("units", "–")
    long_name = da.attrs.get("long_name", variable)

    print(f"\n── Time series: {long_name} [{units}] ───────────────────────")
    print(f"  {'Timestamp':<26}  {'Value':>12}")
    print(f"  {'─'*26}  {'─'*12}")
    for t, v in zip(times, values):
        print(f"  {str(t):<26}  {v:>12.4f}")

    # ------------------------------------------------------------------ #
    # 7. Summary statistics                                               #
    # ------------------------------------------------------------------ #
    print(f"\n── Summary ───────────────────────────────────────────────")
    print(f"  Min    : {float(np.nanmin(values)):.4f} {units}")
    print(f"  Max    : {float(np.nanmax(values)):.4f} {units}")
    print(f"  Mean   : {float(np.nanmean(values)):.4f} {units}")
    print(f"  Std    : {float(np.nanstd(values)):.4f} {units}")

    # ------------------------------------------------------------------ #
    # 8. Optional plot                                                    #
    # ------------------------------------------------------------------ #
    if plot:
        try:
            import matplotlib.pyplot as plt

            fig, ax = plt.subplots(figsize=(12, 4))
            point_filtered.plot(ax=ax, marker="o", markersize=3, linewidth=1)
            ax.set_title(
                f"{long_name} at lat={actual_lat:.3f}°, lon={actual_lon:.3f}° — {period_label}"
            )
            ax.set_xlabel("Time")
            ax.set_ylabel(f"{long_name} [{units}]")
            ax.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.show()
        except ImportError:
            print("\n[info] matplotlib not installed — skipping plot.")

    ds.close()
    return point_filtered


# ------------------------------------------------------------------ #
# CLI                                                                 #
# ------------------------------------------------------------------ #
def main():
    parser = argparse.ArgumentParser(
        description="Extract a time series for a lat/lon point from a NetCDF file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes (pick one):
  --year YYYY                     Extract a single full year
  --start YYYY-MM-DD --end YYYY-MM-DD   Extract between two dates

Examples:
  python extract_nc_timeseries.py data.nc --lat 60.17 --lon 24.94 --year 2010
  python extract_nc_timeseries.py data.nc --lat 60.17 --lon 24.94 --start 2005-06-01 --end 2010-12-31 --plot
        """
    )
    parser.add_argument("nc_file", help="Path to the NetCDF (.nc) file")
    parser.add_argument("--lat",   type=float, required=True, help="Target latitude")
    parser.add_argument("--lon",   type=float, required=True, help="Target longitude")

    # Mutually exclusive time selection
    time_group = parser.add_argument_group("Time selection (choose one mode)")
    time_group.add_argument("--year",  type=int, default=None,
                            help="Single year to extract (e.g. 2010)")
    time_group.add_argument("--start", type=str, default=None, metavar="YYYY-MM-DD",
                            help="Start date for range extraction")
    time_group.add_argument("--end",   type=str, default=None, metavar="YYYY-MM-DD",
                            help="End date for range extraction")

    parser.add_argument("--var",  type=str, default=None,
                        help="Variable name (auto-detected if omitted)")
    parser.add_argument("--plot", action="store_true",
                        help="Show a matplotlib time-series plot")

    args = parser.parse_args()
    extract_timeseries(
        nc_file=args.nc_file,
        lat=args.lat,
        lon=args.lon,
        year=args.year,
        date_start=args.start,
        date_end=args.end,
        variable=args.var,
        plot=args.plot,
    )


if __name__ == "__main__":
    main()