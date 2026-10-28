"""Append-based forecast discharge output for river-route v3.

v3 routes one ensemble member at a time and hands each result to a discharge
writer. The member tasks append straight into one shared (member, riverId,
time) zarr, which ``forecast_discharge_store`` then reduces into the published
forecast store the specification defines.

1. ``create_empty_forecast_zarr`` pre-creates the empty store, chunked one
   member per chunk so parallel member tasks write disjoint chunks safely. The
   member axis is ``rfs_spec.MEMBERS`` (0 the control, 1..50 the perturbed).
2. ``make_member_writer`` returns a river-route discharge writer that rounds a
   member's discharge to the spec keepbits and writes its slice via a plain
   zarr region write, so concurrent members do not race.
"""

import numpy as np
import xarray as xr
import zarr

from geoglows_ecflow.resources import rfs_spec

# Append-friendly river chunk; the reduce step sets the served scheme.
DEFAULT_RIVER_CHUNK = 100_000


def create_empty_forecast_zarr(
    zarr_path: str,
    river_ids,
    dates,
    members=None,
    river_chunk: int = DEFAULT_RIVER_CHUNK,
) -> None:
    """Pre-create the empty shared forecast zarr the member tasks append to.

    Args:
        zarr_path: Destination zarr store (overwritten if it exists).
        river_ids: River ids for the riverId coordinate, in routing order.
        dates: Forecast time steps for the time coordinate.
        members: Ensemble member numbers; defaults to ``rfs_spec.MEMBERS``.
        river_chunk: River chunk length; member is always chunked at 1.
    """
    members = rfs_spec.MEMBERS if members is None else np.asarray(members)
    river_ids = np.asarray(river_ids)
    n_rivers = river_ids.shape[0]
    n_time = len(dates)
    empty = np.full((members.size, n_rivers, n_time), np.nan, dtype="float32")
    ds = xr.Dataset(
        {"Q": (("member", "riverId", "time"), empty)},
        coords={
            "member": members.astype("int32"),
            "riverId": river_ids.astype("int64"),
            "time": np.asarray(dates),
        },
    )
    encoding = {"Q": {"chunks": (1, min(river_chunk, n_rivers), n_time)}}
    ds.to_zarr(zarr_path, mode="w", consolidated=True, encoding=encoding)


def make_member_writer(zarr_path: str, member: int):
    """Return a river-route discharge writer that appends one member's slice.

    river-route calls the writer as ``writer(router, dates, discharge_array,
    discharge_file, runoff_file='')`` with a river-major ``(river, time)``
    array in the network's river order, which matches the pre-created store.
    The discharge is rounded to ``rfs_spec.KEEP_BITS`` before it is written,
    the one keepbits every v3 store shares.

    Args:
        zarr_path: The shared zarr created by ``create_empty_forecast_zarr``.
        member: The ensemble member number; its slice is its row in the store.
    """

    def _writer(
        router, dates, discharge_array, discharge_file, runoff_file="",
        **kwargs,
    ):
        store = zarr.open(zarr_path, mode="r+")
        index = int(np.searchsorted(store["member"][:], member))
        values = rfs_spec.round_keepbits(
            np.array(discharge_array, dtype="float32")
        )
        store["Q"][index, :, :] = values

    return _writer
