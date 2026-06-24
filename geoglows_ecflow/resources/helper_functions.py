# Original code by Alan Snow, 2017 (released under BSD 3-Clause License)
# See: spt_compute (https://github.com/erdc/spt_compute)
# Updated by Michael Souffront, 2023

import json
import os
import sys
import re
import logging as log

# Return periods (years) used throughout the forecast post-processing. Defined
# once here and imported by the modules that build return-period ladders.
RETURN_PERIODS = [2, 5, 10, 25, 50, 100]

# The high-resolution (HRES) forecast is always ensemble member 52. Members
# 1-51 are the ensemble perturbations. Defined once here and imported wherever
# the workflow needs to single out (or exclude) the HRES member.
HRES_ENSEMBLE_MEMBER = 52

# Shared logging format so every resource module logs identically.
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging with the shared format, writing to stdout."""
    log.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        stream=sys.stdout,
    )


def load_forecast_run(workspace: str) -> dict:
    """Load and parse <workspace>/forecast_run.json.

    Args:
        workspace (str): Directory containing forecast_run.json.

    Returns:
        dict: The parsed forecast-run manifest.
    """
    with open(os.path.join(workspace, "forecast_run.json"), "r") as f:
        return json.load(f)


def create_logger(
    name: str, level: str = "INFO", log_file: str | None = None
) -> log.Logger:
    # Create a logger
    logger = log.getLogger(name)
    logger.setLevel(level)

    if log_file:
        # Create file handler
        handler = log.FileHandler(log_file)
    else:
        handler = log.StreamHandler(sys.stdout)

    handler.setLevel(level)
    handler.setFormatter(
        log.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )

    # Add the handler to the logger
    logger.addHandler(handler)

    return logger


def get_date_from_forecast_dir(
    forecast_dir: str, log: log.Logger = create_logger(__name__)
) -> str:
    """Gets the datetime from a forecast directory.

    Args:
        forecast_dir (str): Path representing forecast directory.
        log (log.Logger, optional): Logger. Defaults to log.

    Returns:
        A string representing the datetime in the format "%Y%m%d.%H".

    Raises:
        AttributeError: If forecast_dir is not found or in the expected format.
    """
    # Get the forecast datetime from the forecast directory
    forecast_date_timestep = os.path.basename(forecast_dir)
    log.info(f"Forecast timestep {forecast_date_timestep}")

    # Parse forecast datetime
    date_time_regex = r"\d{8}\.\d{2}"
    try:
        match = re.search(date_time_regex, forecast_date_timestep)
        date_time_str = match.group()
        return date_time_str
    except AttributeError:
        raise AttributeError("No datetime match found.")


def get_valid_vpucode_list(input_directory: str) -> list[str]:
    """
    Get a list of vpucodes from the input directory.

    Args:
        input_directory (str): Path to the per-VPU input directory.

    Returns:
        list[str]: List of valid directories (vpucodes).
    """
    valid_input_directories = []
    # Append to valid_input_directories if directory, skip if not
    for name in os.listdir(input_directory):
        match = re.search(r"\d{3}", name)
        if match and os.path.isdir(os.path.join(input_directory, name)):
            valid_input_directories.append(name)
        else:
            print(f"{name} is not a directory or a 3-digit VPU. Skipping...")
    return valid_input_directories


def get_ensemble_number_from_forecast(forecast_name: str) -> int:
    """Gets the datetimestep from forecast.

    Args:
        forecast_name (str): Path to or name of the forecast file.
            E.g. "1.runoff.nc".

    Returns:
        int: The ensemble number.
    """
    forecast_split = os.path.basename(forecast_name).split(".")
    if forecast_name.endswith(".205.runoff.grib.runoff.netcdf"):
        ensemble_number = int(forecast_split[2])
    else:
        ensemble_number = int(forecast_split[0])
    return ensemble_number


def case_insensitive_file_search(directory: str, pattern: str) -> str:
    """Looks for file with pattern with case insensitive search.

    Args:
        directory (str): Path to directory to search.
        pattern (str): Pattern to search for.

    Returns:
        (str): Path to file with pattern.
    """
    try:
        file_path = os.path.join(
            directory,
            [
                filename
                for filename in os.listdir(directory)
                if re.search(pattern, filename, re.IGNORECASE)
            ][0],
        )

        return file_path
    except IndexError:
        print(f"{pattern} not found")
        raise
