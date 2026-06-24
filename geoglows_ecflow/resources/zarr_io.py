"""Shared helpers for writing xarray datasets to zarr.

The dask configuration and Blosc/zstd compression settings were previously
copy-pasted into both ``netcdf_to_zarr`` and ``day_one_forecast``. They live
here so the heavy ``dask``/``numcodecs`` imports stay out of the lightweight
``helper_functions`` module.
"""

import os

import dask
from numcodecs import Blosc

# Dask settings shared by every zarr write in the workflow.
DASK_ZARR_CONFIG = {
    "array.slicing.split_large_chunks": False,
    # cap the max chunk size
    "array.chunk-size": "40MB",
    # use the threads scheduler
    "scheduler": "threads",
    # set the maximum memory target usage to 80% of total memory
    "distributed.worker.memory.target": 0.80,
    # do not allow spilling to disk
    "distributed.worker.memory.spill": False,
    # resources to allocate to dask workers
    "distributed.worker.resources": {
        "memory": 3e9,  # 1e9=1GB, per worker
        "cpu": os.cpu_count(),  # num CPU per worker
    },
}


def write_dataset_to_zarr(
    ds,
    zarr_path: str,
    chunks: dict,
    drop_vars=(),
    **to_zarr_kwargs,
) -> None:
    """Write an open xarray dataset to a consolidated, Blosc-compressed zarr.

    The caller is responsible for opening (and closing) ``ds``; this helper
    only applies the shared dask config, drops the requested vars, rechunks,
    and writes. Per-call differences (chunk dims, dropped vars, ``mode``,
    ``zarr_version``) are passed in rather than hardcoded.

    Args:
        ds: An open xarray Dataset.
        zarr_path (str): Destination zarr store path.
        chunks (dict): Chunk spec passed to ``ds.chunk``.
        drop_vars: Variable names to drop (ignored if absent).
        **to_zarr_kwargs: Extra keyword args forwarded to ``to_zarr``
            (e.g. ``mode``, ``zarr_version``).
    """
    with dask.config.set(**DASK_ZARR_CONFIG):
        compressor = Blosc(cname="zstd", clevel=3, shuffle=Blosc.BITSHUFFLE)
        encoding = {"Q": {"compressor": compressor}}
        (
            ds.drop_vars(list(drop_vars), errors="ignore")
            .chunk(chunks)
            .to_zarr(
                zarr_path,
                consolidated=True,
                encoding=encoding,
                **to_zarr_kwargs,
            )
        )
