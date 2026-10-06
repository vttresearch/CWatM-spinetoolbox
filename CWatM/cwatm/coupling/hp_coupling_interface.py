"""
CWatM Hydropower Coupling Interface (Option 3)
===============================================
Manages file-based exchange between CWatM and an external energy dispatch
model (e.g. FlexTool) using netCDF files.

Architecture (MODE B - Sequential Exchange):
    1. CWatM runs period T using last-period dispatch
    2. At end of T: writes hp_state_out.nc (inflow, storage, head)
    3. Energy model reads state, optimises next period T+1
    4. Energy model writes hp_dispatch.nc (Q_turbine, Q_spill)
    5. CWatM reads dispatch and continues

MODE A (Offline forced):
    Energy model pre-computed -> Q_turbine(t) read each step. No iteration.
"""

import numpy as np
from pathlib import Path


class HydropowerCouplingInterface:
    """
    Manages the file-based exchange between CWatM and an energy model.

    State written by CWatM (hp_state_out.nc):
        - inflow_m3s      [m3/s]   per reservoir, per timestep
        - storage_m3      [m3]     end-of-step storage
        - head_m          [m]      net head (linear approximation if no
                                   storage-elevation curve available)
        - storage_capacity_m3 [m3] static capacity (written once)

    Dispatch read by CWatM (hp_dispatch.nc):
        - Q_turbine       [m3/s]   turbine flow target per reservoir
        - Q_spill         [m3/s]   mandatory spill from energy model
    """

    def __init__(self, state_out_path, dispatch_in_path,
                 coupling_freq_days=1, n_reservoirs=None):
        """
        Parameters
        ----------
        state_out_path    : str | Path  Path where CWatM writes state
        dispatch_in_path  : str | Path  Path where energy model writes dispatch
        coupling_freq_days: int         How often state is exchanged (days)
        n_reservoirs      : int         Number of HP reservoirs
        """
        self.state_out_path = Path(state_out_path)
        self.dispatch_in_path = Path(dispatch_in_path)
        self.coupling_freq = coupling_freq_days
        self.n_res = n_reservoirs

        self._dispatch_cache = None
        self._dispatch_valid = None
        self._step_counter = 0

    def write_state(self, date, inflow_m3s, storage_m3, head_m,
                    storage_capacity_m3):
        """
        Write CWatM reservoir state to netCDF for the energy model.
        Called every `coupling_freq_days` steps.
        """
        import netCDF4 as nc4

        path = self.state_out_path
        path.parent.mkdir(parents=True, exist_ok=True)

        mode = "a" if path.exists() else "w"
        with nc4.Dataset(str(path), mode, format="NETCDF4") as ds:
            if mode == "w":
                ds.createDimension("time", None)
                ds.createDimension("reservoir", self.n_res)

                t_var = ds.createVariable("time", "f8", ("time",))
                t_var.units = "days since 1901-01-01"
                t_var.calendar = "standard"

                for vname, units, dims in [
                    ("inflow_m3s", "m3 s-1", ("time", "reservoir")),
                    ("storage_m3", "m3", ("time", "reservoir")),
                    ("head_m", "m", ("time", "reservoir")),
                    ("storage_capacity_m3", "m3", ("reservoir",)),
                ]:
                    v = ds.createVariable(vname, "f4", dims,
                                          fill_value=-9999.0)
                    v.units = units

                ds["storage_capacity_m3"][:] = storage_capacity_m3

            t_idx = len(ds["time"])
            ds["time"][t_idx] = nc4.date2num(
                date, units=ds["time"].units, calendar=ds["time"].calendar
            )
            ds["inflow_m3s"][t_idx, :] = inflow_m3s
            ds["storage_m3"][t_idx, :] = storage_m3
            ds["head_m"][t_idx, :] = head_m

    def read_dispatch(self, date):
        """
        Read turbine flow and spill targets from the energy model output.
        Returns (Q_turbine, Q_spill) arrays of shape (n_reservoirs,).
        Falls back to (None, None) if file is missing or stale.
        """
        import netCDF4 as nc4
        from datetime import datetime

        if not self.dispatch_in_path.exists():
            return None, None

        try:
            with nc4.Dataset(str(self.dispatch_in_path), "r") as ds:
                times = nc4.num2date(ds["time"][:],
                                     units=ds["time"].units,
                                     calendar=ds["time"].calendar)
                dates = [datetime(t.year, t.month, t.day) for t in times]
                if date not in dates:
                    return None, None

                idx = dates.index(date)
                Q_turbine = np.array(ds["Q_turbine"][idx, :], dtype=np.float64)
                Q_spill = np.array(ds["Q_spill"][idx, :], dtype=np.float64)
                return Q_turbine, Q_spill

        except Exception as e:
            print(f"  WARNING: Could not read dispatch file: {e}")
            return None, None

    def should_write_state(self, step_n):
        """Check if state should be written at this step."""
        return (step_n % self.coupling_freq) == 0
