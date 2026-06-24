"""Tests for _find_state_init prior-cycle Qinit lookup.

``run_river_route_forecast`` imports ``river_route`` at module scope; the
conftest Dummy shim lets this import succeed without the real dependency.
"""

from geoglows_ecflow.resources.run_river_route_forecast import _find_state_init

DATE = "2023010100"  # base cycle: 2023-01-01 00 UTC


def _touch(directory, name):
    path = directory / name
    path.write_text("")
    return str(path)


def test_returns_24h_lookback_when_present(tmp_path):
    # 24h before the base cycle.
    expected = _touch(tmp_path, "Qinit_2022123100.parquet")

    assert _find_state_init(str(tmp_path), DATE) == expected


def test_falls_through_to_72h_lookback(tmp_path):
    # 24h and 48h missing; only the 72h-prior file exists.
    expected = _touch(tmp_path, "Qinit_2022122900.parquet")

    assert _find_state_init(str(tmp_path), DATE) == expected


def test_seasonal_fallback_when_no_recent_qinit(tmp_path):
    _touch(tmp_path, "seasonal_qinit_b.parquet")
    expected = _touch(tmp_path, "seasonal_qinit_a.parquet")

    # Seasonal candidates are sorted; the lexicographically first is chosen.
    assert _find_state_init(str(tmp_path), DATE) == expected


def test_returns_none_when_nothing_found(tmp_path):
    assert _find_state_init(str(tmp_path), DATE) is None
