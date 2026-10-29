"""Tests for reducing the appended member zarr into the forecast store."""

import numpy as np
import pandas as pd
import pytest
import xarray as xr

from geoglows_ecflow.resources import rfs_spec
from geoglows_ecflow.resources.forecast_discharge_store import (
    build_forecast_discharge_store,
)


def _append_zarr(tmp_path, river_ids, members, steps):
    """Write a synthetic (member, riverId, time) append zarr; return it and the
    (river, member, time) discharge it holds, already keepbits-rounded."""
    n = river_ids.size
    q = rfs_spec.round_keepbits(
        np.ascontiguousarray(
            (np.arange(n)[:, None, None] % 7 + 1)
            * (1 + 0.01 * np.arange(members.size))[None, :, None]
            * (1 + 0.1 * np.arange(steps))[None, None, :],
            dtype="float32",
        )
    )
    path = str(tmp_path / "append.zarr")
    xr.Dataset(
        {"Q": (("member", "riverId", "time"), q.transpose(1, 0, 2))},
        coords={
            "member": members.astype("int32"),
            "riverId": river_ids.astype("int64"),
            "time": np.arange(steps),
        },
    ).to_zarr(path, mode="w")
    return path, q


def test_build_produces_spec_store(tmp_path):
    river_ids = np.arange(300, dtype=np.int64) + 1
    app, q = _append_zarr(
        tmp_path, river_ids, rfs_spec.MEMBERS, rfs_spec.FORECAST_STEPS
    )
    out = str(tmp_path / "discharge.zarr")

    build_forecast_discharge_store(
        app, out, pd.Timestamp("2026-09-27T00"), river_ids
    )

    ds = xr.open_zarr(out)
    assert ds["Q"].dims == ("riverId", "member", "time")
    assert ds["Q"].encoding["chunks"] == (
        1, rfs_spec.MEMBERS.size, rfs_spec.FORECAST_STEPS
    )
    assert ds["Q"].attrs["keepbits"] == rfs_spec.KEEP_BITS
    assert np.array_equal(ds["Q"].values, q)
    expected_mean = rfs_spec.round_keepbits(
        q.mean(axis=1, dtype=np.float64).astype("float32")
    )
    assert np.array_equal(ds["Qmean"].values, expected_mean)
    assert ds.sizes["percentiles"] == rfs_spec.PERCENTILES.size


def test_build_refuses_wrong_step_count(tmp_path):
    river_ids = np.arange(10, dtype=np.int64) + 1
    app, _ = _append_zarr(
        tmp_path, river_ids, rfs_spec.MEMBERS, rfs_spec.FORECAST_STEPS - 1
    )
    out = str(tmp_path / "discharge.zarr")

    with pytest.raises(ValueError):
        build_forecast_discharge_store(
            app, out, pd.Timestamp("2026-09-27T00"), river_ids
        )
