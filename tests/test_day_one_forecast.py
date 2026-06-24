"""Tests for the return-period exceedance logic in day_one_forecast."""

import pandas as pd

from geoglows_ecflow.resources.day_one_forecast import (
    check_for_return_period_flow,
    get_time_of_first_exceedance,
)


def test_first_exceedance_returns_earliest_time_at_or_above_flow(make_flows):
    flows = make_flows([1, 5, 10, 2], times=["a", "b", "c", "d"])

    # Rows below 4 (1 and 2) are dropped; first remaining time is "b" (5).
    assert get_time_of_first_exceedance(flows, 4) == "b"


def test_below_smallest_return_period_returns_input_unchanged(
    make_flows, rp_table
):
    largeflows = pd.DataFrame()
    flows = make_flows([0.5, 1.0])  # max 1.0 < rp2 (2.0)

    result = check_for_return_period_flow(largeflows, flows, 3, rp_table)

    assert result is largeflows
    assert len(result) == 0


def test_appends_row_with_exceeded_thresholds(make_flows, rp_table):
    largeflows = pd.DataFrame()
    flows = make_flows([1, 3, 6, 8], times=["a", "b", "c", "d"])  # max 8

    result = check_for_return_period_flow(largeflows, flows, 3, rp_table)

    assert len(result) == 1
    row = result.iloc[0]
    assert row["comid"] == 12345
    assert row["stream_order"] == 3
    assert row["max_forecasted_flow"] == 8
    # rp2 (2) and rp5 (5) are exceeded; the input frame is mutated between
    # calls, so the first time >= rp5 is "c", not "b".
    assert row["date_exceeds_return_period_2"] == "b"
    assert row["date_exceeds_return_period_5"] == "c"
    # rp10 (10) and above are not reached (max is 8).
    assert row["date_exceeds_return_period_10"] == ""
    assert row["date_exceeds_return_period_25"] == ""
    assert row["date_exceeds_return_period_50"] == ""
    assert row["date_exceeds_return_period_100"] == ""
