import datetime
import glob
import logging
import os
import argparse
import numpy as np
import pandas as pd
import xarray as xr
import netCDF4 as nc

from geoglows_ecflow.resources.helper_functions import (
    RETURN_PERIODS,
    configure_logging,
)
from geoglows_ecflow.resources.zarr_io import write_dataset_to_zarr

# Only streams of at least this Strahler order are checked against return
# periods for the warnings summary (smaller headwater streams are skipped).
MIN_STREAM_ORDER = 3


def check_for_return_period_flow(
    largeflows_df, forecasted_flows_df, stream_order, rp_data
):
    max_flow = max(forecasted_flows_df["means"])

    # retrieve return period flow levels from the dataframe
    thresholds = {
        rp: float(rp_data[f"rp{rp}"].values[0]) for rp in RETURN_PERIODS
    }

    # if the flow is not larger than the smallest return period, return the
    # dataframe without appending anything
    if max_flow < thresholds[RETURN_PERIODS[0]]:
        return largeflows_df

    # compare the timeseries to each return period threshold, ascending, so the
    # progressive masking inside get_time_of_first_exceedance is preserved
    exceedance_dates = {}
    for rp in RETURN_PERIODS:
        if max_flow >= thresholds[rp]:
            exceedance_dates[rp] = get_time_of_first_exceedance(
                forecasted_flows_df, thresholds[rp]
            )
        else:
            exceedance_dates[rp] = ""

    row = {
        "comid": rp_data.index[0],
        "stream_order": stream_order,
        "max_forecasted_flow": round(max_flow, 2),
    }
    for rp in RETURN_PERIODS:
        row[f"date_exceeds_return_period_{rp}"] = exceedance_dates[rp]

    new_row = pd.DataFrame(row, index=[0])

    return pd.concat([largeflows_df, new_row], ignore_index=True)


def get_time_of_first_exceedance(forecasted_flows_df, flow):
    # replace the flows that are too small (don't exceed the return period)
    forecasted_flows_df[forecasted_flows_df.means < flow] = np.nan
    daily_flows = forecasted_flows_df.dropna()
    return daily_flows["times"].values[0]


def postprocess_vpu(
    vpu,
    input_dir,
    output_dir,
    return_periods_dir,
    forecast_records,
):
    # build the propert directory paths

    # make the pandas dataframe to store the summary info
    largeflows = pd.DataFrame(
        columns=[
            "comid",
            "stream_order",
            "max_forecasted_flow",
            "date_exceeds_return_period_2",
            "date_exceeds_return_period_5",
            "date_exceeds_return_period_10",
            "date_exceeds_return_period_25",
            "date_exceeds_return_period_50",
            "date_exceeds_return_period_100",
        ]
    )

    # merge the most recent forecast files into a single xarray dataset
    logging.info("  merging forecasts")

    merged_forecasts = xr.open_dataset(
        os.path.join(output_dir, f"nces_avg_{vpu}.nc")
    )

    # collect the times and comids from the forecasts
    logging.info("  reading info from forecasts")
    times = pd.to_datetime(pd.Series(merged_forecasts.time))
    comids = pd.Series(merged_forecasts.river_id)
    tomorrow = times[0] + pd.Timedelta(days=1)
    year = times[0].strftime("%Y")

    # read the return period file
    logging.info("  reading return period file")
    return_period_file = os.path.join(
        return_periods_dir, f"returnperiods_{vpu}.nc"
    )
    return_period_data = xr.open_dataset(return_period_file).to_dataframe()

    # read the list of large streams
    logging.info("  creating dataframe of large streams")
    streams_file_path = os.path.join(input_dir, "master_table.parquet")
    streams_df = pd.read_parquet(streams_file_path)
    large_vpu_streams_df = streams_df[
        (streams_df["VPUCode"] == int(vpu))
        & (streams_df["strmOrder"] >= MIN_STREAM_ORDER)
    ]

    # get the list of comids
    large_list = large_vpu_streams_df["LINKNO"].tolist()

    # store the first day flows in a huge array
    logging.info("  beginning to iterate over the comids")
    first_day_flows = []

    # now process the mean flows for each river in the vpu
    for comid in comids:
        # compute the timeseries of average flows
        means = merged_forecasts.sel(river_id=comid).Q.values.flatten()

        # put it in a dataframe with the times series
        forecasted_flows = (
            times.to_frame(name="times")
            .join(pd.Series(means, name="means"))
            .dropna()
        )
        # select flows in 1st day and save them to the forecast record
        first_day_flows.append(
            forecasted_flows[forecasted_flows.times < tomorrow][
                "means"
            ].tolist()
        )

        # if stream order is larger than 2, check if it needs to be included on
        # the return periods summary csv
        if comid in large_list:
            order = large_vpu_streams_df[
                large_vpu_streams_df["LINKNO"] == comid
            ]["strmOrder"].values
            rp_data = return_period_data[return_period_data.index == comid]
            largeflows = check_for_return_period_flow(
                largeflows, forecasted_flows, order, rp_data
            )

    # add the forecasted flows to the forecast records file for this vpu
    logging.info("  updating the forecast records file")
    try:
        update_forecast_records(
            vpu, forecast_records, output_dir, year, first_day_flows, times
        )
    except Exception as e:
        logging.info("  unexpected error updating the forecast records")
        logging.info(e)

    largeflows = (
        largeflows.merge(
            streams_df[["lat", "lon", "LINKNO"]],
            how="inner",
            left_on="comid",
            right_on="LINKNO",
        )
        .drop(columns=["comid"])
        .replace({"": np.nan})
    )
    largeflows.to_parquet(
        os.path.join(output_dir, f"forecastwarnings_{vpu}.parquet")
    )

    return


def update_forecast_records(
    vpu, forecast_records, qout_dir, year, first_day_flows, times
):
    if not os.path.exists(forecast_records):
        os.mkdir(forecast_records)

    record_path = os.path.join(
        forecast_records, f"forecastrecord_{vpu}_{year}.nc"
    )

    # if there isn't a forecast record for this year, make one
    if not os.path.exists(record_path):
        # using a forecast file as a reference
        reference = glob.glob(os.path.join(qout_dir, f"Qout_{vpu}_*.nc"))[0]
        reference = nc.Dataset(reference)
        # make a new record file
        record = nc.Dataset(record_path, "w")
        # copy the right dimensions and variables. lat/lon are deliberately
        # not carried into the record: they're dropped during the zarr
        # conversion below, and river-route's native output doesn't
        # necessarily include them.
        record.createDimension("time", None)
        record.createDimension("river_id", reference.dimensions["river_id"].size)
        record.createVariable(
            "time", reference.variables["time"].dtype, dimensions=("time",)
        )
        record.createVariable(
            "river_id", reference.variables["river_id"].dtype, dimensions=("river_id",)
        )
        record.createVariable(
            "Q",
            reference.variables["Q"].dtype,
            dimensions=("time", "river_id"),
            fill_value=np.nan,
        )
        record.variables["river_id"][:] = reference.variables["river_id"][:]

        # set the time variable attributes
        record.variables["time"].setncattr(
            "units", f"hours since {year}-01-01 00:00:00"
        )

        # calculate the number of 3-hourly timesteps that will occur this year
        # and store them in the time variable
        date = datetime.datetime(
            year=int(year), month=1, day=1, hour=0, minute=0, second=0
        )
        end = int(year) + 1
        timesteps = 0
        while date.year < end:
            date += datetime.timedelta(hours=3)
            timesteps += 1
        record.variables["time"][:] = [i * 3 for i in range(timesteps)]
        record.close()

    # open the record netcdf
    logging.info("  writing first day flows to forecast record netcdf")
    record_netcdf = nc.Dataset(record_path, mode="a")

    # figure out the right times
    startdate = datetime.datetime(
        year=int(year), month=1, day=1, hour=0, minute=0, second=0
    )
    record_times = [
        startdate + datetime.timedelta(hours=int(i))
        for i in record_netcdf.variables["time"][:]
    ]
    start_time_index = record_times.index(times[0])
    end_time_index = start_time_index + len(first_day_flows[0])
    # convert all those saved flows to a np array and write to the netcdf
    first_day_flows = np.asarray(first_day_flows)
    record_netcdf.variables["Q"][
        start_time_index:end_time_index, :
    ] = first_day_flows.T

    # save and close the netcdf
    record_netcdf.sync()
    record_netcdf.close()
    netcdf_forecast_record_to_zarr(record_path)

    return
def netcdf_forecast_record_to_zarr(record_path) -> None:
    """
    Converts the netcdf forecast record to zarr.

    Args:
        record_path (str): Path to the forecast_record netcdf file.
    """
    
    logging.info("Converting the forecast record to zarr")
    zarr_path = record_path.replace(".nc", ".zarr")
    record_nc = xr.open_dataset(record_path)
    
    logging.info("Writing to zarr")
    write_dataset_to_zarr(
        record_nc,
        zarr_path,
        {"time": -1, "river_id": "auto"},
        drop_vars=["lat", "lon"],
        mode="w",
        zarr_version=2,
    )
    record_nc.close()
    logging.info("Done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "workspace",
        help="path to the daily workspace directory",
    )
    parser.add_argument(
        "vpu",
        help="VPU number",
    )
    parser.add_argument(
        "output_dir",
        help="path to the forecast records output directory",
    )

    args = parser.parse_args()
    workspace = args.workspace
    vpu = args.vpu
    input_dir = os.path.join(workspace, "input")
    output_dir = os.path.join(workspace, "output")
    returnperiods = os.path.join(workspace, "return_periods_dir")
    forecast_records = args.output_dir
    output_dir = os.path.join(workspace, "output")

    # start logging
    configure_logging()

    postprocess_vpu(
        vpu, input_dir, output_dir, returnperiods, forecast_records
    )

    logging.info("Finished at " + datetime.datetime.now().strftime("%c"))
