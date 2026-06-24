"""Tests for the shared zarr-writing helper."""

import os

import numpy as np
import xarray as xr

from geoglows_ecflow.resources.zarr_io import write_dataset_to_zarr


def test_write_dataset_to_zarr_round_trips_and_drops_vars(tmp_path):
    data = np.arange(5 * 3).reshape(5, 3).astype(float)
    ds = xr.Dataset(
        {
            "Q": (("time", "river_id"), data),
            "lat": (("river_id",), [10.0, 11.0, 12.0]),
        },
        coords={"time": range(5), "river_id": [1, 2, 3]},
    )
    zarr_path = str(tmp_path / "out.zarr")

    write_dataset_to_zarr(
        ds,
        zarr_path,
        {"time": -1, "river_id": "auto"},
        drop_vars=["lat"],
        mode="w",
        zarr_version=2,
    )

    # consolidated=True writes a .zmetadata file at the store root.
    assert os.path.exists(os.path.join(zarr_path, ".zmetadata"))
    with xr.open_zarr(zarr_path) as result:
        assert "lat" not in result.variables
        assert result["Q"].values.tolist() == data.tolist()
