import argparse
import json
import logging
import os
import sys
from glob import glob

from geoglows_ecflow.resources.helper_functions import (
    configure_logging,
    get_ensemble_number_from_forecast,
    get_valid_vpucode_list,
)


configure_logging()


def forecast_preprocess(
    workspace: str,
    input_dir: str,
    output_dir: str,
    runoff_dir: str,
    initialize_flows: bool = True,
) -> dict:
    """Build the per-(vpu, ens) job manifest that the cycle's ens_member tasks
    consume.

    Walks input_dir for VPU subdirs (3-digit names) and runoff_dir for ensemble
    runoff netCDFs, then writes <workspace>/forecast_run.json with one
    job_<vpu>_<ens> entry per (VPU, ensemble member) pair. Each entry is read
    verbatim by run_river_route_forecast and compute_init_flows.

    Args:
        workspace: Where to write forecast_run.json.
        input_dir: Per-VPU static-data root (each VPU subdir holds
            params.parquet, weights.nc, and Qinit_<past>.parquet files).
        output_dir: Where ens_member tasks will write Qout_<vpu>_<ens>.nc.
        runoff_dir: Directory holding ensemble runoff netCDF files
            (`<mem>.runoff.nc` etc.). The directory's basename is taken as
            the cycle date.
        initialize_flows: Whether each job should look for a prior-cycle
            Qinit; pass False for cold starts.

    Returns:
        The full master dict serialized to forecast_run.json.
    """
    os.makedirs(output_dir, exist_ok=True)

    master = {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "runoff_dir": runoff_dir,
        "date": os.path.basename(runoff_dir),
    }

    vpus = get_valid_vpucode_list(input_dir)

    # Largest ensemble number first so HRES (mem 52) starts early; helps
    # spread load across workers since HRES has the longest single-task time.
    runoff_files = sorted(
        glob(os.path.join(runoff_dir, "*.runoff.*nc")),
        key=get_ensemble_number_from_forecast,
        reverse=True,
    )

    for vpu in vpus:
        logging.info(f"Adding VPU input directory {vpu}")
        vpu_input_dir = os.path.join(input_dir, vpu)
        for runoff in runoff_files:
            ens = get_ensemble_number_from_forecast(runoff)
            master[f"job_{vpu}_{ens}"] = {
                "runoff": runoff,
                "vpu": vpu,
                "ensemble": ens,
                "input_dir": vpu_input_dir,
                "output_file": os.path.join(output_dir, f"Qout_{vpu}_{ens}.nc"),
                "init_flows": initialize_flows,
            }

    with open(os.path.join(workspace, "forecast_run.json"), "w") as f:
        json.dump(master, f)

    logging.info("Wrote forecast_run.json")
    return master


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the per-(vpu, ens) job manifest for one forecast cycle."
    )
    parser.add_argument("workspace", help="Path to workspace directory")
    args = parser.parse_args(argv)

    forecast_preprocess(
        workspace=args.workspace,
        input_dir=os.path.join(args.workspace, "input"),
        output_dir=os.path.join(args.workspace, "output"),
        runoff_dir=args.workspace,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
