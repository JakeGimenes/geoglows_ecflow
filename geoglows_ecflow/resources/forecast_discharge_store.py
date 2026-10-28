"""Reduce the appended member zarr into the published forecast discharge store.

The ens_member tasks append each member's routed discharge into one shared
``(member, riverId, time)`` zarr (``forecast_zarr``). This step reduces that
into the forecast store the specification defines, reusing ``rfs_spec`` as the
single source of truth for its layout, encoding and keepbits:

    discharge.zarr/   Q (riverId, member, time), Qpercentiles, Qmean

so no chunk shape, shard shape, codec or keepbits value is chosen here. Rivers
are written a block of whole shards at a time, so the store's chunks and shards
are exactly ``rfs_spec.FORECAST_LAYOUT``.
"""

import argparse
import os
import shutil
import sys

import numpy as np
import zarr

from geoglows_ecflow.resources import rfs_spec


def build_forecast_discharge_store(
    append_zarr: str,
    out_store: str,
    initialization,
    river_ids: np.ndarray,
    block: int | None = None,
) -> None:
    """Build one forecast discharge store from the appended member zarr.

    Args:
        append_zarr: The shared ``(member, riverId, time)`` zarr the member
            tasks appended to.
        out_store: The ``discharge.zarr`` to write, replaced if it exists.
        initialization: The forecast initialization timestamp.
        river_ids: Every riverId in store order, matching the append zarr.
        block: Rivers written at once; defaults to ``rfs_spec.FORECAST_BLOCK``,
            a whole number of shards so no two writes share a shard.
    """
    block = rfs_spec.FORECAST_BLOCK if block is None else block
    river_ids = np.asarray(river_ids)
    source = zarr.open(str(append_zarr), mode="r")
    _check_source(source, river_ids)
    discharge = source["Q"]  # (member, riverId, time)

    partial = f"{out_store}.partial"
    shutil.rmtree(partial, ignore_errors=True)
    rfs_spec.create_forecast_store(partial, initialization, river_ids)
    store = zarr.open(partial, mode="r+")

    n_rivers = river_ids.shape[0]
    for start in range(0, n_rivers, block):
        stop = min(start + block, n_rivers)
        # (member, block, time) -> (block, member, time), the store's order
        members = np.asarray(discharge[:, start:stop, :])
        chunk = np.ascontiguousarray(
            members.transpose(1, 0, 2), dtype="float32"
        )
        chunk = rfs_spec.round_keepbits(chunk)
        percentiles, mean = rfs_spec.ensemble_summaries(chunk)
        store["Q"][start:stop] = chunk
        store["Qpercentiles"][start:stop] = percentiles
        store["Qmean"][start:stop] = mean

    if os.path.exists(out_store):
        shutil.rmtree(out_store)
    os.replace(partial, out_store)


def _check_source(source, river_ids: np.ndarray) -> None:
    """Refuse an append zarr not of the forecast members, rivers and steps."""
    members = np.asarray(source["member"][:])
    if not np.array_equal(members, rfs_spec.MEMBERS):
        raise ValueError(
            f"append zarr members are {members.tolist()}, "
            f"expected {rfs_spec.MEMBERS.tolist()}"
        )
    if not np.array_equal(np.asarray(source["riverId"][:]), river_ids):
        raise ValueError("append zarr riverId axis is not river_ids")
    steps = source["Q"].shape[2]
    if steps != rfs_spec.FORECAST_STEPS:
        raise ValueError(
            f"append zarr has {steps} time steps, "
            f"forecast is {rfs_spec.FORECAST_STEPS}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reduce an appended member zarr into a forecast store."
    )
    parser.add_argument(
        "append_zarr", help="shared (member, riverId, time) zarr"
    )
    parser.add_argument("out_store", help="discharge.zarr to write")
    parser.add_argument(
        "initialization", help="forecast initialization, e.g. 2026-05-03T00"
    )
    parser.add_argument(
        "network", help="network parquet whose riverId order the store follows"
    )
    args = parser.parse_args(argv)

    import pandas as pd

    river_ids = pd.read_parquet(
        args.network, columns=["riverId"]
    )["riverId"].to_numpy()
    build_forecast_discharge_store(
        args.append_zarr,
        args.out_store,
        pd.Timestamp(args.initialization),
        river_ids,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
