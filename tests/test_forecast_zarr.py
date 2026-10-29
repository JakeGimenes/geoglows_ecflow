"""Tests for the appended forecast zarr helpers."""

import numpy as np
import xarray as xr

from geoglows_ecflow.resources import rfs_spec
from geoglows_ecflow.resources.forecast_zarr import (
    create_empty_forecast_zarr,
    make_member_writer,
)


def test_create_empty_forecast_zarr_uses_spec_members(tmp_path):
    river_ids = np.array([11, 12, 13], dtype=np.int64)
    dates = np.arange(5)
    path = str(tmp_path / "Qout.zarr")

    create_empty_forecast_zarr(path, river_ids, dates)

    ds = xr.open_zarr(path)
    assert ds.sizes == {
        "member": rfs_spec.MEMBERS.size, "riverId": 3, "time": 5
    }
    assert ds["member"].values.tolist() == rfs_spec.MEMBERS.tolist()
    assert ds["riverId"].values.tolist() == [11, 12, 13]
    assert bool(np.isnan(ds["Q"].values).all())


def test_make_member_writer_rounds_and_writes_only_its_slice(tmp_path):
    river_ids = np.array([11, 12, 13], dtype=np.int64)
    dates = np.arange(4)
    path = str(tmp_path / "Qout.zarr")
    create_empty_forecast_zarr(path, river_ids, dates)

    member = 7
    discharge = np.arange(3 * 4, dtype="float32").reshape(3, 4) + 0.123456
    writer = make_member_writer(path, member)
    writer(None, dates, discharge, "out.nc")

    ds = xr.open_zarr(path)
    written = ds["Q"].sel(member=member).values
    assert np.array_equal(written, rfs_spec.round_keepbits(discharge.copy()))
    # exactly one member is filled; every other row stays NaN
    assert int(np.isfinite(ds["Q"].values).sum()) == discharge.size
