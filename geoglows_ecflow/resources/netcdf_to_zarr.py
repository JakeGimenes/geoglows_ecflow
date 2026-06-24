import argparse
import glob
import logging
import os
import shutil

import numpy as np
import xarray as xr

from geoglows_ecflow.resources.helper_functions import (
    HRES_ENSEMBLE_MEMBER,
    configure_logging,
    load_forecast_run,
)
from geoglows_ecflow.resources.zarr_io import write_dataset_to_zarr

configure_logging()


def netcdf_forecasts_to_zarr(workspace: str) -> None:
    """
    Converts the netcdf forecast files to zarr.

    Args:
        workspace (str): Path to forecast_run.json base directory.
    """
    data = load_forecast_run(workspace)
    output_dir = data["output_dir"]
    date = data["date"]

    vpu_nums = sorted(
        set([os.path.basename(x).split("_")[1] for x in glob.glob(os.path.join(output_dir, f"Qout_*_{HRES_ENSEMBLE_MEMBER}.nc"))])
    )

    qout_1_51_files = sorted([os.path.join(output_dir, f"Qout_{vpu}.nc") for vpu in vpu_nums])
    qout_52_files = sorted(glob.glob(os.path.join(output_dir, f"Qout_*_{HRES_ENSEMBLE_MEMBER}.nc")))
    zarr_file_path = os.path.join(output_dir, f"Qout_{date}.zarr")

    if os.path.exists(zarr_file_path):
        shutil.rmtree(zarr_file_path)

    logging.info("Opening ensembles 1-51 datasets")
    with xr.open_mfdataset(
        qout_1_51_files, combine="nested", concat_dim="river_id"
    ) as ds151:
        logging.info("Assigning the ensemble coordinate variable")
        ds151 = ds151.assign_coords(
            ensemble=np.arange(1, HRES_ENSEMBLE_MEMBER)
        )
        logging.info("Opening ensemble 52 dataset")
        with xr.open_mfdataset(
            qout_52_files, combine="nested", concat_dim="river_id"
        ) as ds52:
            logging.info("Assigning the ensemble coordinate variable")
            ds52 = ds52.assign_coords(ensemble=HRES_ENSEMBLE_MEMBER)

            logging.info("Concatenating 1-51 and 52 datasets")
            ds = xr.concat([ds151, ds52], dim="ensemble")

            logging.info("Writing to zarr")
            write_dataset_to_zarr(
                ds,
                zarr_file_path,
                {"time": -1, "river_id": "auto", "ensemble": -1},
                drop_vars=["crs", "lat", "lon", "time_bnds"],
            )
            logging.info("Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "workspace",
        help="Path to the suite home directory.",
    )
    args = parser.parse_args()
    netcdf_forecasts_to_zarr(workspace=args.workspace)
