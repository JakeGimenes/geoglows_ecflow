"""Return-period ladder tests for generate_esri_table."""

import netCDF4 as nc
import numpy as np
import pandas as pd
import xarray as xr

from geoglows_ecflow.resources.generate_esri_table import (
    postprocess_vpu_forecast_directory,
)

VPU = "101"
DATE = "20230101.00"


def _write_inputs(tmp_path):
    """Create the nces-average and return-period netCDFs the function reads."""
    base = tmp_path / DATE  # basename(dirname(output_dir)) becomes the date
    output_dir = base / "output"
    returnperiods = base / "rp"
    output_dir.mkdir(parents=True)
    returnperiods.mkdir(parents=True)

    # river 1 flows at 300 (exceeds rp5 but not rp10); river 2 stays at 5.
    times = pd.date_range("2023-01-01", periods=2, freq="3h")
    flows = np.array([[300.0, 5.0], [300.0, 5.0]])
    ds = xr.Dataset(
        {"Q": (("time", "river_id"), flows)},
        coords={"time": times, "river_id": [1, 2]},
    )
    ds.to_netcdf(output_dir / f"nces_avg_{VPU}.nc")

    rp = nc.Dataset(str(returnperiods / f"returnperiods_{VPU}.nc"), "w")
    rp.createDimension("river_id", 2)
    rp.createVariable("river_id", "i4", ("river_id",))[:] = [1, 2]
    thresholds = {"rp2": 10, "rp5": 100, "rp10": 500,
                  "rp25": 1000, "rp50": 2000, "rp100": 5000}
    for name, value in thresholds.items():
        rp.createVariable(name, "f8", ("river_id",))[:] = [value, value]
    rp.close()

    return output_dir, returnperiods


def test_return_period_ladder_assigns_expected_levels(tmp_path):
    output_dir, returnperiods = _write_inputs(tmp_path)

    postprocess_vpu_forecast_directory(
        str(output_dir), str(returnperiods), VPU
    )

    table = pd.read_parquet(
        output_dir
        / "map_style_tables"
        / f"mapstyletable_{VPU}_{DATE}.parquet"
    )
    ret_per_by_comid = table.groupby("comid")["ret_per"].unique()

    # 300 exceeds rp5 (100) but not rp10 (500) -> level 5.
    assert ret_per_by_comid[1].tolist() == [5]
    # 5 is below rp2 (10) -> level 0.
    assert ret_per_by_comid[2].tolist() == [0]


def test_thickness_ladder_assigns_expected_levels(tmp_path):
    output_dir, returnperiods = _write_inputs(tmp_path)

    postprocess_vpu_forecast_directory(
        str(output_dir), str(returnperiods), VPU
    )

    table = pd.read_parquet(
        output_dir
        / "map_style_tables"
        / f"mapstyletable_{VPU}_{DATE}.parquet"
    )
    thickness_by_comid = table.groupby("comid")["thickness"].unique()

    # 300 crosses the 250 threshold but not 1500 -> thickness 3.
    assert thickness_by_comid[1].tolist() == [3]
    # 5 is below the first (20) threshold -> thickness 1.
    assert thickness_by_comid[2].tolist() == [1]
