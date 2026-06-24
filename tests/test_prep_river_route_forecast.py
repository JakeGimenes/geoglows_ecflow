"""Tests for the forecast job-manifest builder (forecast_preprocess)."""

import json
import os

from geoglows_ecflow.resources.prep_river_route_forecast import (
    forecast_preprocess,
)


def _build_cycle(tmp_path):
    """Create a minimal input/runoff layout and return its paths."""
    input_dir = tmp_path / "input"
    for vpu in ("101", "102"):
        (input_dir / vpu).mkdir(parents=True)
    output_dir = tmp_path / "output"
    # The runoff dir's basename is taken verbatim as the cycle date.
    runoff_dir = tmp_path / "2023010100"
    runoff_dir.mkdir()
    for mem in (1, 2, 52):
        (runoff_dir / f"{mem}.runoff.nc").write_text("")
    return input_dir, output_dir, runoff_dir


def test_manifest_top_level_shape(tmp_path):
    input_dir, output_dir, runoff_dir = _build_cycle(tmp_path)

    master = forecast_preprocess(
        str(tmp_path), str(input_dir), str(output_dir), str(runoff_dir)
    )

    assert master["date"] == "2023010100"
    assert master["input_dir"] == str(input_dir)
    assert master["output_dir"] == str(output_dir)
    assert master["runoff_dir"] == str(runoff_dir)


def test_one_job_per_vpu_ensemble_pair(tmp_path):
    input_dir, output_dir, runoff_dir = _build_cycle(tmp_path)

    master = forecast_preprocess(
        str(tmp_path), str(input_dir), str(output_dir), str(runoff_dir)
    )

    job_keys = {k for k in master if k.startswith("job_")}
    assert job_keys == {
        "job_101_1", "job_101_2", "job_101_52",
        "job_102_1", "job_102_2", "job_102_52",
    }


def test_jobs_ordered_hres_first(tmp_path):
    input_dir, output_dir, runoff_dir = _build_cycle(tmp_path)

    master = forecast_preprocess(
        str(tmp_path), str(input_dir), str(output_dir), str(runoff_dir)
    )

    ens_order_101 = [
        int(k.rsplit("_", 1)[1]) for k in master if k.startswith("job_101_")
    ]
    # Largest ensemble number (HRES = 52) is scheduled first.
    assert ens_order_101 == [52, 2, 1]


def test_job_entry_fields(tmp_path):
    input_dir, output_dir, runoff_dir = _build_cycle(tmp_path)

    master = forecast_preprocess(
        str(tmp_path),
        str(input_dir),
        str(output_dir),
        str(runoff_dir),
        initialize_flows=False,
    )

    job = master["job_101_52"]
    assert job["vpu"] == "101"
    assert job["ensemble"] == 52
    assert job["input_dir"] == str(input_dir / "101")
    assert job["output_file"] == str(output_dir / "Qout_101_52.nc")
    assert job["init_flows"] is False
    assert job["runoff"].endswith("52.runoff.nc")


def test_manifest_written_to_disk(tmp_path):
    input_dir, output_dir, runoff_dir = _build_cycle(tmp_path)

    master = forecast_preprocess(
        str(tmp_path), str(input_dir), str(output_dir), str(runoff_dir)
    )

    written_path = os.path.join(str(tmp_path), "forecast_run.json")
    with open(written_path) as f:
        assert json.load(f) == master
