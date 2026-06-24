"""Shared pytest fixtures and import shims for the resources test suite.

These tests pin the *current* behavior of the pure helper functions used by
the river-route forecast workflow. The functions under test never touch the
heavy optional dependency ``river_route``, so when it is not installed we
inject a Dummy module into ``sys.modules`` before any test imports
``run_river_route_forecast``. When the real package *is* installed (e.g. on the
Ubuntu workflow box), it is used as-is.
"""

import sys
import types

import pandas as pd
import pytest

# ``run_river_route_forecast`` does ``import river_route as rr`` at module top,
# but ``_find_state_init`` (the function under test) never touches it. Prefer
# the real package when installed; only fall back to a Dummy module so the
# import succeeds where the dependency is absent.
if "river_route" not in sys.modules:
    try:
        import river_route  # noqa: F401  (use the real package when present)
    except ModuleNotFoundError:
        sys.modules["river_route"] = types.ModuleType("river_route")


@pytest.fixture
def make_flows():
    """Return a factory building a forecasted-flows DataFrame.

    The frame mirrors what ``day_one_forecast`` passes around: a ``means``
    column of forecast values and a parallel ``times`` column.
    """

    def _make(means, times=None):
        if times is None:
            times = [f"t{i}" for i in range(len(means))]
        return pd.DataFrame({"means": list(means), "times": list(times)})

    return _make


@pytest.fixture
def rp_table():
    """A single-row return-period table indexed by comid.

    Columns match what ``check_for_return_period_flow`` reads: rp2..rp100.
    """
    return pd.DataFrame(
        {
            "rp2": [2.0],
            "rp5": [5.0],
            "rp10": [10.0],
            "rp25": [25.0],
            "rp50": [50.0],
            "rp100": [100.0],
        },
        index=[12345],
    )
