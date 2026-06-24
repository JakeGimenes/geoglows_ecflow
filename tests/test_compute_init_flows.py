"""Test that compute_init_flows selects the configured time index."""

import json
import os

import numpy as np
import pandas as pd
import xarray as xr

from geoglows_ecflow.resources import compute_init_flows


def test_init_time_index_constant():
    # t+24h on the ENS 3h grid; documented contract relied on downstream.
    assert compute_init_flows.INIT_TIME_INDEX == 7


def test_qinit_parquet_holds_q_at_init_time_index(tmp_path):
    vpu = "101"
    ymd = "2023010100"

    (tmp_path / "forecast_run.json").write_text(json.dumps({"date": ymd}))
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (tmp_path / "input" / vpu).mkdir(parents=True)

    # Q[time, river_id] with distinct values so the selected slice is unique.
    data = np.arange(10 * 3).reshape(10, 3)
    ds = xr.Dataset(
        {"Q": (("time", "river_id"), data)},
        coords={"time": range(10), "river_id": [1, 2, 3]},
    )
    ds.to_netcdf(output_dir / f"nces_avg_{vpu}.nc")

    compute_init_flows.main(str(tmp_path), vpu)

    out = pd.read_parquet(
        os.path.join(str(tmp_path), "input", vpu, f"Qinit_{ymd}.parquet")
    )
    expected = data[compute_init_flows.INIT_TIME_INDEX]
    assert out["Q"].tolist() == expected.tolist()
