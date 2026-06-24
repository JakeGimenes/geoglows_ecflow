"""Unit tests pinning current behavior of helper_functions pure functions."""

import json
import os

import pytest

from geoglows_ecflow.resources.helper_functions import (
    get_date_from_forecast_dir,
    get_ensemble_number_from_forecast,
    get_valid_vpucode_list,
    load_forecast_run,
)


@pytest.mark.parametrize(
    "forecast_name, expected",
    [
        ("1.runoff.nc", 1),
        ("52.runoff.nc", 52),
        ("/abs/path/to/7.runoff.nc", 7),
        # Legacy RAPID-era name: the ".205.runoff.grib.runoff.netcdf" suffix
        # routes to split(".")[2] for the ensemble number.
        ("20230101.00.52.205.runoff.grib.runoff.netcdf", 52),
    ],
)
def test_get_ensemble_number_from_forecast(forecast_name, expected):
    assert get_ensemble_number_from_forecast(forecast_name) == expected


def test_get_valid_vpucode_list_keeps_three_digit_dirs(tmp_path):
    (tmp_path / "101").mkdir()
    (tmp_path / "102").mkdir()
    # Non-three-digit dir and a file are both skipped.
    (tmp_path / "abc").mkdir()
    (tmp_path / "103.txt").write_text("not a dir")

    result = get_valid_vpucode_list(str(tmp_path))

    assert sorted(result) == ["101", "102"]


def test_get_date_from_forecast_dir_parses_timestamp():
    forecast_dir = os.path.join("some", "root", "20230101.00")

    assert get_date_from_forecast_dir(forecast_dir) == "20230101.00"


def test_get_date_from_forecast_dir_raises_without_match():
    with pytest.raises(AttributeError):
        get_date_from_forecast_dir("no-date-here")


def test_load_forecast_run_round_trips_manifest(tmp_path):
    manifest = {"date": "2023010100", "output_dir": "/out", "job_101_52": {}}
    (tmp_path / "forecast_run.json").write_text(json.dumps(manifest))

    assert load_forecast_run(str(tmp_path)) == manifest
