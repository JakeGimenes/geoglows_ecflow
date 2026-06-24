import argparse
import os

import pandas as pd
import xarray as xr

from geoglows_ecflow.resources.helper_functions import load_forecast_run


# Time index 7 of the ensemble-mean Q corresponds to t+24h on the ENS
# 3h-resolution grid (the HRES member is excluded from the average upstream by
# nco_calc.ecf). The next cycle reads this file as river-route's
# channel_state_init_file via run_river_route_forecast._find_state_init.
INIT_TIME_INDEX = 7


def main(workspace: str, vpu: str) -> None:
    ymd = load_forecast_run(workspace)["date"]

    avg_path = os.path.join(workspace, "output", f"nces_avg_{vpu}.nc")
    out_path = os.path.join(workspace, "input", vpu, f"Qinit_{ymd}.parquet")

    with xr.open_dataset(avg_path) as ds:
        # river-route's channel_state_init_file is a single-column parquet
        # whose row order must match river_id ordering in params.parquet.
        pd.DataFrame({"Q": ds["Q"].isel(time=INIT_TIME_INDEX).values}).to_parquet(
            out_path
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", help="Path to forecast_run.json base directory.")
    parser.add_argument("vpu", help="VPU number to process.")
    args = parser.parse_args()
    main(args.workspace, args.vpu)
