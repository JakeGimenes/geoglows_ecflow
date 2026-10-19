"""Append-based forecast zarr output for river-route v3.

v3 routes one ensemble member at a time and hands each result to a discharge
writer. Instead of writing a per-member NetCDF and concatenating them in a
later step, the member tasks append straight into one shared zarr:

1. ``create_empty_forecast_zarr`` pre-creates an empty (member, riverId, time)
   store, chunked one member per chunk so parallel member tasks write disjoint
   chunks safely.
2. ``make_member_writer`` returns a river-route discharge writer that drops a
   member's routed discharge into its slice via a plain zarr region write (no
   metadata rewrite, so concurrent members do not race).

A later step rechunks/shards this append-friendly store into the serving
layout the forecast spec prescribes.
"""

import numpy as np
import xarray as xr
import zarr

# Append-friendly river chunk; the serving rechunk step sets the final scheme.
DEFAULT_RIVER_CHUNK = 100_000


def create_empty_forecast_zarr(
    zarr_path: str,
    river_ids,
    dates,
    n_members: int = 52,
    river_chunk: int = DEFAULT_RIVER_CHUNK,
) -> None:
    """Pre-create the empty shared forecast zarr the member tasks append to.

    Args:
        zarr_path: Destination zarr store (overwritten if it exists).
        river_ids: River ids for the riverId coordinate, in routing order.
        dates: Forecast time steps for the time coordinate.
        n_members: Ensemble size; the member coordinate is 1..n_members.
        river_chunk: River chunk length; member is always chunked at 1.
    """
    river_ids = np.asarray(river_ids)
    n_rivers = river_ids.shape[0]
    n_time = len(dates)
    empty = np.full((n_members, n_rivers, n_time), np.nan, dtype="float32")
    ds = xr.Dataset(
        {"Q": (("member", "riverId", "time"), empty)},
        coords={
            "member": np.arange(1, n_members + 1, dtype="int64"),
            "riverId": river_ids,
            "time": np.asarray(dates),
        },
    )
    encoding = {"Q": {"chunks": (1, min(river_chunk, n_rivers), n_time)}}
    ds.to_zarr(zarr_path, mode="w", consolidated=True, encoding=encoding)


def make_member_writer(zarr_path: str, member_number: int):
    """Return a river-route discharge writer that appends one member's slice.

    river-route calls the writer as ``writer(router, dates, discharge_array,
    discharge_file, runoff_file='')`` with a river-major ``(river, time)``
    array in the network's river order, which matches the pre-created store.

    Args:
        zarr_path: The shared zarr created by ``create_empty_forecast_zarr``.
        member_number: 1-based ensemble member; its slice is index member-1.
    """

    def _writer(
        router, dates, discharge_array, discharge_file, runoff_file="",
        **kwargs,
    ):
        store = zarr.open(zarr_path, mode="r+")
        store["Q"][member_number - 1, :, :] = np.asarray(
            discharge_array, dtype="float32"
        )

    return _writer
