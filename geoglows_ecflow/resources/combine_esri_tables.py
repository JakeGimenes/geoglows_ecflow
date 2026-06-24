import argparse
import logging

import glob
import os

import pandas as pd

from geoglows_ecflow.resources.helper_functions import configure_logging


def combine_esri_tables(workspace: str):
    """Combines the map_style_tables from each VPU into 1 CSV file per time
        step with rows from all VPUs

    Args:
        workspace (str): Path to forecast_run.json base directory.

    """
    # get path to tables from workspace
    output_dir = os.path.join(workspace, "output", "map_style_tables")

    # select all outputs/VPUNUMBER/DATE/map_style_table*.parquet files
    logging.info("Concatenating parquet map_style_tables from each VPU")
    global_map_style_df = pd.concat(
        [
            pd.read_parquet(x)
            for x in glob.glob(
                os.path.join(output_dir, "mapstyletable*.parquet")
            )
        ]
    )

    # replace nans with 0
    logging.info("Preparing concatenated DF")
    global_map_style_df.fillna(0, inplace=True)
    global_map_style_df.set_index("timestamp", inplace=True)

    # for each unique date in the timestamp column, create a new dataframe
    for _, date in enumerate(global_map_style_df.index.unique()):
        file_save_path = os.path.join(
            output_dir, f'mapstyletable_{date.strftime("%Y-%m-%d-%H")}.csv'
        )
        logging.info(f"Writing map_style_table for {date}")
        logging.debug(f"Saving to {file_save_path}")
        (global_map_style_df.loc[date].to_csv(file_save_path, index=False))


if __name__ == "__main__":
    argparser = argparse.ArgumentParser()
    argparser.add_argument(
        "workspace",
        nargs=1,
        help="Path to suite home directory",
    )

    args = argparser.parse_args()
    workspace = args.workspace[0]

    configure_logging()

    combine_esri_tables(workspace)
