import argparse
import glob
import os

import boto3
import yaml

from geoglows_ecflow.resources.helper_functions import load_forecast_run


def upload_to_s3(workspace: str, aws_config_file: str):
    """
    Uploads GEOGloWS forecast output to AWS.

    Args:
        workspace (str): Path to forecast_run.json base directory.
        aws_config_file (str): Path to AWS config file.
    """
    with open(aws_config_file, "r") as f:
        config = yaml.safe_load(f)
        ACCESS_KEY_ID = config["aws_access_key_id"]
        SECRET_ACCESS_KEY = config["aws_secret_access_key"]
        forecast_bucket_uri = config["bucket_forecast_archive"]
        mapstyletable_bucket_uri = config["bucket_maptable_archive"]

    data = load_forecast_run(workspace)
    date = data["date"]
    output_dir = data["output_dir"]

    # Create an S3 client
    s3 = boto3.client(
        "s3",
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=SECRET_ACCESS_KEY,
    )

    for forecast_nc in sorted(
        glob.glob(os.path.join(output_dir, f"Qout_*.nc"))
    ):
        s3.upload_file(
            forecast_nc,
            forecast_bucket_uri,
            f"{date}/{os.path.basename(forecast_nc)}",
        )

    for mapstyletable in sorted(
        glob.glob(os.path.join(workspace, f"mapstyletable_*"))
    ):
        s3.upload_file(
            mapstyletable,
            mapstyletable_bucket_uri,
            f"{date}/{os.path.basename(mapstyletable)}",
        )

    return


if __name__ == "__main__":
    # Get ecflow home directory
    argparser = argparse.ArgumentParser()
    argparser.add_argument(
        "workspace",
        help="Path to suite home directory",
    )
    argparser.add_argument(
        "aws_config_file",
        help="Path to AWS config file",
    )

    args = argparser.parse_args()
    workspace = args.workspace
    aws_config_file = args.aws_config_file

    upload_to_s3(workspace, aws_config_file)
