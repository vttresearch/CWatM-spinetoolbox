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
                 coupling_freq_days=1, n_reservoirs=None,
                 reservoir_ids=None, reservoir_lat=None, reservoir_lon=None,
                 coord_tol=0.02):
        """
        Parameters
        ----------
        state_out_path    : str | Path  Path where CWatM writes state
        dispatch_in_path  : str | Path  Path where energy model writes dispatch
        coupling_freq_days: int         How often state is exchanged (days)
        n_reservoirs      : int         Number of HP reservoirs
        reservoir_ids     : array-like  waterBodyID of each HP reservoir, in
                                        the same order CWatM writes/reads the
                                        `reservoir` axis (outlet-cell order)
        reservoir_lat     : array-like  Outlet-cell latitude per HP reservoir
        reservoir_lon     : array-like  Outlet-cell longitude per HP reservoir
        coord_tol         : float       Max distance (deg) for coordinate match
        """
        self.state_out_path = Path(state_out_path)
        self.dispatch_in_path = Path(dispatch_in_path)
        self.coupling_freq = coupling_freq_days
        self.n_res = n_reservoirs

        self.res_ids = None if reservoir_ids is None else np.asarray(reservoir_ids)
        self.res_lat = None if reservoir_lat is None else np.asarray(reservoir_lat, dtype=np.float64)
        self.res_lon = None if reservoir_lon is None else np.asarray(reservoir_lon, dtype=np.float64)
        self.coord_tol = coord_tol

        # Permutation mapping CWatM reservoir order -> dispatch-file columns,
        # resolved once from the dispatch file's identity variables.
        self._perm = None
        self._perm_status = None

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
                    #("inflow_m3s", "m3 s-1", ("time", "reservoir")),
                    ("storage_m3", "m3", ("time", "reservoir")),
                    #("head_m", "m", ("time", "reservoir")),
                    #("storage_capacity_m3", "m3", ("reservoir",)),
                ]:
                    v = ds.createVariable(vname, "f4", dims,
                                          fill_value=-9999.0)
                    v.units = units

                #ds["storage_capacity_m3"][:] = storage_capacity_m3

                # Static reservoir identity so the energy model can align its
                # dispatch to CWatM's reservoir axis by ID / coordinate.
                if self.res_ids is not None:
                    rid = ds.createVariable("reservoir_id", "i8", ("reservoir",))
                    rid.long_name = "CWatM waterBodyID"
                    rid[:] = self.res_ids
                if self.res_lat is not None and self.res_lon is not None:
                    rlat = ds.createVariable("reservoir_lat", "f8", ("reservoir",))
                    rlat.units = "degrees_north"
                    rlat.long_name = "Reservoir outlet-cell latitude"
                    rlon = ds.createVariable("reservoir_lon", "f8", ("reservoir",))
                    rlon.units = "degrees_east"
                    rlon.long_name = "Reservoir outlet-cell longitude"
                    rlat[:] = self.res_lat
                    rlon[:] = self.res_lon

            t_idx = len(ds["time"])
            ds["time"][t_idx] = nc4.date2num(
                date, units=ds["time"].units, calendar=ds["time"].calendar
            )
            #ds["inflow_m3s"][t_idx, :] = inflow_m3s
            ds["storage_m3"][t_idx, :] = storage_m3
            #ds["head_m"][t_idx, :] = head_m

    def read_dispatch(self, date):
        """
        Read turbine flow and spill targets from the energy model output.
        Returns (Q_turbine, Q_spill) arrays of shape (n_reservoirs,), reordered
        to CWatM's reservoir axis by matching the dispatch file's reservoir
        identity (id or coordinates) rather than trusting column position.
        Falls back to (None, None) if file is missing, stale, or reservoirs
        cannot be matched safely.
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

                if self._perm is None:
                    self._perm, self._perm_status = self._resolve_perm(ds)
                if self._perm is None:
                    return None, None

                return Q_turbine[self._perm], Q_spill[self._perm]

        except Exception as e:
            print(f"  WARNING: Could not read dispatch file: {e}")
            return None, None

    def _resolve_perm(self, ds):
        """
        Build a permutation `perm` such that ``Q_cwatm[i] = Q_file[perm[i]]``,
        aligning the dispatch file's `reservoir` axis to CWatM's order.

        Matching preference: waterBodyID, then outlet coordinates, then
        positional (only when axis lengths agree and no identity is available).
        Returns (perm, status) or (None, status) when no safe mapping exists.
        """
        n = self.n_res
        res_dim = (ds.dimensions["reservoir"].size
                   if "reservoir" in ds.dimensions else None)

        # 1) Match by waterBodyID when present on both sides.
        if self.res_ids is not None and "reservoir_id" in ds.variables:
            file_ids = np.asarray(ds["reservoir_id"][:]).astype(np.int64)
            perm = np.full(n, -1, dtype=np.int64)
            for i, rid in enumerate(self.res_ids):
                hits = np.where(file_ids == np.int64(rid))[0]
                if hits.size:
                    perm[i] = int(hits[0])
            if np.all(perm >= 0):
                return perm, "id"
            missing = [int(self.res_ids[i]) for i in np.where(perm < 0)[0]]
            print(f"  WARNING: hp_coupling could not ID-match reservoirs "
                  f"{missing}; trying coordinates.")

        # 2) Match by outlet coordinates (nearest within coord_tol).
        have_cwatm_coords = self.res_lat is not None and self.res_lon is not None
        have_file_coords = ("reservoir_lat" in ds.variables
                            and "reservoir_lon" in ds.variables)
        if have_cwatm_coords and have_file_coords:
            flat = np.asarray(ds["reservoir_lat"][:], dtype=np.float64)
            flon = np.asarray(ds["reservoir_lon"][:], dtype=np.float64)
            perm = np.full(n, -1, dtype=np.int64)
            used = np.zeros(flat.shape[0], dtype=bool)
            for i in range(n):
                d = np.hypot(flat - self.res_lat[i], flon - self.res_lon[i])
                d[used] = np.inf
                j = int(np.argmin(d))
                if np.isfinite(d[j]) and d[j] <= self.coord_tol:
                    perm[i] = j
                    used[j] = True
            if np.all(perm >= 0):
                return perm, "coord"
            missing = np.where(perm < 0)[0].tolist()
            print(f"  WARNING: hp_coupling coordinate match failed for CWatM "
                  f"reservoirs {missing} (tol={self.coord_tol} deg). "
                  f"Dispatch NOT applied.")
            return None, "coord_failed"

        # 3) No identity to match on: positional only if lengths agree.
        if res_dim is not None and res_dim == n:
            return np.arange(n, dtype=np.int64), "positional"

        print(f"  WARNING: hp_coupling reservoir count mismatch "
              f"(file={res_dim}, CWatM={n}) and no id/coordinates to match on. "
              f"Dispatch NOT applied.")
        return None, "mismatch"

    def should_write_state(self, step_n):
        """Check if state should be written at this step."""
        return (step_n % self.coupling_freq) == 0
