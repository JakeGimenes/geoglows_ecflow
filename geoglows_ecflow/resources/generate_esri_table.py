import argparse
import logging
import os

import netCDF4 as nc
import pandas as pd
import xarray as xr

from geoglows_ecflow.resources.helper_functions import (
    RETURN_PERIODS,
    configure_logging,
)

# Only the first 10 days of the forecast are summarized in the style table.
FORECAST_WINDOW_DAYS = 10

# Mean-flow thresholds (m^3/s) that drive the map line-thickness ladder. Flows
# below the first threshold get thickness 1; each threshold crossed bumps the
# thickness by one (levels 2..6).
THICKNESS_THRESHOLDS = [20, 250, 1500, 10000, 30000]


def postprocess_vpu_forecast_directory(
    output_dir: str,
    returnperiods: str,
    vpu: int | str,
):
    # creates file name for the csv file
    date_string = os.path.basename(
        os.path.dirname(output_dir)
    )  # should be a date in YYYYMMDDHH format
    style_table_file_name = f"mapstyletable_{vpu}_{date_string}.parquet"
    if os.path.exists(os.path.join(output_dir, style_table_file_name)):
        logging.info(f"Style table already exists: {style_table_file_name}")
        return
    logging.info(f"Creating style table: {style_table_file_name}")

    nces_output_filename = os.path.join(output_dir, f"nces_avg_{vpu}.nc")
    # read the date and COMID lists from one of the netcdfs
    with xr.open_dataset(nces_output_filename) as ds:
        comids = ds["river_id"][:].values
        dates = pd.to_datetime(ds["time"][:].values)
        mean_flows = ds["Q"][:].values.round(2)

    mean_flow_df = pd.DataFrame(mean_flows, columns=comids, index=dates)

    # limit both dataframes to the first 10 days
    mean_flow_df = mean_flow_df[
        mean_flow_df.index
        <= mean_flow_df.index[0] + pd.Timedelta(days=FORECAST_WINDOW_DAYS)
    ]

    # creating pandas dataframe with return periods
    rp_path = os.path.join(returnperiods, f"returnperiods_{vpu}.nc")
    logging.info(f"Return Period Path {rp_path}")
    with nc.Dataset(rp_path, "r") as rp_ncfile:
        rp_df = pd.DataFrame(
            {
                f"return_{rp}": rp_ncfile.variables[f"rp{rp}"][:]
                for rp in RETURN_PERIODS
            },
            index=rp_ncfile.variables["river_id"][:],
        )

    mean_thickness_df = pd.DataFrame(columns=comids, index=dates, dtype=int)
    mean_thickness_df[:] = 1
    for level, threshold in enumerate(THICKNESS_THRESHOLDS, start=2):
        mean_thickness_df[mean_flow_df >= threshold] = level

    mean_ret_per_df = pd.DataFrame(columns=comids, index=dates, dtype=int)
    mean_ret_per_df[:] = 0
    for rp in RETURN_PERIODS:
        mean_ret_per_df[mean_flow_df.gt(rp_df[f"return_{rp}"], axis=1)] = rp

    mean_flow_df = mean_flow_df.stack().to_frame().rename(columns={0: "mean"})
    mean_thickness_df = (
        mean_thickness_df.stack().to_frame().rename(columns={0: "thickness"})
    )
    mean_ret_per_df = (
        mean_ret_per_df.stack().to_frame().rename(columns={0: "ret_per"})
    )

    # merge all dataframes
    for df in [mean_thickness_df, mean_ret_per_df]:
        mean_flow_df = mean_flow_df.merge(
            df, left_index=True, right_index=True
        )

    maptable_outdir = os.path.join(output_dir, "map_style_tables")
    if not os.path.exists(maptable_outdir):
        os.makedirs(maptable_outdir)

    mean_flow_df.index.names = ["timestamp", "comid"]
    mean_flow_df = mean_flow_df.reset_index()
    mean_flow_df["mean"] = mean_flow_df["mean"].round(1)
    mean_flow_df.loc[mean_flow_df["mean"] < 0, "mean"] = 0
    mean_flow_df["thickness"] = mean_flow_df["thickness"].astype(int)
    mean_flow_df["ret_per"] = mean_flow_df["ret_per"].astype(int)
    mean_flow_df.to_parquet(
        os.path.join(maptable_outdir, style_table_file_name)
    )
    return


# runs function on file execution
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "workspace",
        nargs=1,
        help="Path to the daily workspace directory, named in YYYYMMDDHH "
        "format, containing (1) *.runoff.nc IFS forecast files, "
        "(2) an output directory of routed discharge netcdfs, "
        "(3) symlinks to the per-VPU inputs and return periods directories",
    )
    parser.add_argument("vpu", nargs=1, help="id number of vpu to process")
    args = parser.parse_args()
    workspace = args.workspace[0]
    output_dir = os.path.join(workspace, "output")
    returnperiods = os.path.join(workspace, "return_periods_dir")
    vpu = args.vpu[0]

    configure_logging()

    params = [output_dir, returnperiods, vpu]

    postprocess_vpu_forecast_directory(*params)
