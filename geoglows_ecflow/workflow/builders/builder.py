from geoglows_ecflow.workflow.builders.base import GEOGLOWSBaseBuilder
from geoglows_ecflow.workflow.comfies.ooflow import Trigger, Defuser
from geoglows_ecflow.workflow.comfies.ooflow import all_complete, Event, complete
from geoglows_ecflow.workflow.comfies.ooflow import Limit, InLimit, Variable
from geoglows_ecflow.workflow.comfies.ooflow import RepeatDate, Defstatus
from geoglows_ecflow.workflow.parts.nodes import Family, Task, NominalTime
from geoglows_ecflow.workflow.parts.times import (
    t2t,
    CronDateRefresh,
    CronDataAvailability,
)
from geoglows_ecflow.workflow.comfies.dateandtime import Date, CalSeq
from geoglows_ecflow.workflow.parts.admin import AdminFamily
from geoglows_ecflow.workflow.parts.epilogs import DummyEpilog
from geoglows_ecflow.workflow.parts.repeats import calseq_repeat
from geoglows_ecflow.workflow.parts.packages import PackageInstallers
from geoglows_ecflow.workflow.comfies.partition import partition
from geoglows_ecflow.resources.helper_functions import HRES_ENSEMBLE_MEMBER

# Scheduler memory reservations (MB) for the heavier tasks.
ENS_TASK_MEM_MB = 6000
ARCHIVE_QINIT_MEM_MB = 4000


def is_00z_cycle(nominal_time):
    """Whether a nominal-time family is the 00Z cycle.

    The 00Z cycle runs the full ensemble pipeline; the 12Z cycle runs a
    reduced tree. The cycle is identified by the EMOS_BASE variable the
    NominalTime family carries ("00" or "12").
    """
    return nominal_time.get_variable("EMOS_BASE").value() != "12"


class Builder(GEOGLOWSBaseBuilder):
    
    comfies_minimum_version = "1.6.2"
    
    ecflow_module = "geoglows_ecflow.workflow.parts.nodes"

    scripts = [
        "geoglows_ecflow/workflow/scripts/routing",
        "geoglows_ecflow/workflow/scripts/common",
    ]

    includes = [
        "geoglows_ecflow/workflow/scripts/routing",
        "geoglows_ecflow/workflow/scripts/common",
    ]

    def build(self):
        """
        Create parts and wire them together into a GEOGloWS suite.
        Naming conventions:
        n_*  -- ecFlow Node object
        e_*  -- ecFlow Event object
        """
        super(Builder, self).build()
        cfg = self.config

        # All tunable parameters read from the deployment config file are
        # gathered here so what the suite exposes is visible at a glance.
        # (exparch/workroot are consumed by the task scripts via templating,
        # so they are intentionally not read here.)
        suite_name = cfg.get("name")
        # Validate the run mode (consumed by suite.h via templating); the
        # builder no longer branches on it, so the return is discarded.
        cfg.get("mode", choices=["prod", "test"])
        first_date = cfg.get("first_date", type=int)
        last_date = cfg.get("last_date", type=int, default="20300101")
        first_barrier = cfg.get("first_barrier", type=int, default=first_date)
        mars_nworkers = cfg.get("mars_workers", type=int, default=1)
        ens_members = cfg.get("ens_members", type=int, default=51)
        vpu_list = cfg.get("vpu_list", type=list, default=[])

        # Operational suites this suite triggers off (normalized to lead "/").
        o_suite = cfg.get("o_suite", default="/o")
        mc_suite = cfg.get("mc_suite", default="/mc")
        if o_suite[0] != "/":
            o_suite = f"/{o_suite}"
        if mc_suite[0] != "/":
            mc_suite = f"/{mc_suite}"

        suite = self.suite

        # admin family
        n_admin = Family("admin")
        n_admin_toggles = Task("toggles")
        n_admin.add(n_admin_toggles)

        e_no_diss = Event("no_diss")
        e_no_web_prod = Event("no_webprod")
        e_no_web_test = Event("no_webtest")
        e_no_ecfs_archive = Event("no_ecfs")
        n_admin_toggles.add(
            e_no_diss,
            e_no_web_prod,
            e_no_web_test,
            e_no_ecfs_archive,
            Trigger("1 == 0"),
            Defuser("1 == 1"),
        )

        with_webpush = False
        with_diss = False

        # make family
        n_make = Family("make")
        n_packages = PackageInstallers(
            packages=["scripts"]
        )

        n_build_venv = Task("build_venv")
        n_packages.trigger = n_build_venv.complete
        n_statics = Task("install_static_data")
        n_statics.trigger = n_packages.complete
        n_initialize = Task("initialize")
        n_initialize.trigger = n_packages.complete
        calseq_se = CalSeq(monthdays=[1])

        first_ms_date = calseq_se.shift(Date.from_ymd(str(first_date)), -3)
        n_statics.add_variable("YMD", first_ms_date.ymd)

        n_make.add(
            Variable("SMSTRIES", 1),
            n_build_venv,
            n_packages,
            n_statics,
            n_initialize,
        )

        n_make.add_inlimit("make")

        n_make.add(Variable("YMD", first_date))
        suite.add(n_make, n_admin)

        n_barrier = Family("barrier")
        n_barrier_daily = Family("daily")
        barrier_ymd = RepeatDate("YMD", int(first_barrier), int(last_date))
        n_barrier_daily.add(barrier_ymd)

        n_main = Family("main")
        n_main.add(InLimit("main"))
        n_main.trigger = n_make.complete
        n_main.trigger &= n_admin.complete
        main_ymd = calseq_repeat(
            Date.from_ymd(str(first_date)), Date.from_ymd(str(last_date))
        )
        n_main.add(main_ymd)

        n_lag = Family("lag")
        n_lag.trigger = n_make.complete
        lag_ymd = calseq_repeat(
            Date.from_ymd(str(first_date)), Date.from_ymd(str(last_date))
        )
        n_daily_lag = Family("daily")
        n_daily_lag.add(lag_ymd)

        n_barrier.add(n_barrier_daily)

        barrier_00 = NominalTime("00", delta_day=1)
        main_00 = NominalTime("00", delta_day=1)
        lag_00 = NominalTime("00", delta_day=1)

        barrier_12 = NominalTime("12")
        main_12 = NominalTime("12")
        lag_12 = NominalTime("12")

        for barrier_hh, main_hh, lag_hh in (
            (barrier_12, main_12, lag_12),
            (barrier_00, main_00, lag_00),
        ):
            cycle = str(main_hh.time.hh)

            self.defs.add_extern(f"{mc_suite}/main:YMD")
            self.defs.add_extern(f"{mc_suite}/main/{cycle}/fc0015d/fc")
            self.defs.add_extern(f"{o_suite}/main:YMD")
            self.defs.add_extern(f"{o_suite}/main/{cycle}/fc/model")
            n_run_hr = Family("run_hr").add(
                Trigger(
                    f"({o_suite}/main:YMD == /{suite_name}/barrier/daily:YMD "
                    f"and {o_suite}/main/{cycle}/fc/model == complete) "
                    f"or ({o_suite}/main:YMD > /{suite_name}/barrier/daily:YMD)"
                )
            )
            n_run_hr.add(
                Task("dummy").add(Trigger("0==1")).add(Defuser("1==1"))
            )
            n_run_en = Family("run_en").add(
                Trigger(
                    f"({mc_suite}/main:YMD == /{suite_name}/barrier/daily:YMD "
                    f"and {mc_suite}/main/{cycle}/fc0015d/fc == complete) "
                    f"or ({mc_suite}/main:YMD > /{suite_name}/barrier/daily:YMD)"
                )
            )
            n_run_en.add(
                Task("dummy").add(Trigger("0==1")).add(Defuser("1==1"))
            )

            n_barrier_epilog = Family("last").add(
                Trigger(
                    f"{o_suite}/main:YMD > /{suite_name}/barrier/daily:YMD"
                ),
                Task("sleep").add(Trigger("0==1"), Defuser("1==1")),
            )

            barrier_hh.add(n_run_hr, n_run_en)
            n_barrier_daily.add(barrier_hh)
            n_initialize = Family("initialize")
            n_initialize.trigger = n_run_hr.complete.across("YMD")
            n_initialize.add(Task("initialize"))
            n_initialize.add(Defstatus("complete"))
            n_hr = Family("hres")
            n_hr.trigger = n_run_hr.complete.across("YMD")
            n_hr.trigger &= n_initialize.complete
            n_hr.add(Variable("CONTEXT", "hres"))
            n_ret_hr = Task("retrieve_hres")
            n_hr.add(n_ret_hr)

            n_ens = Family("ens")
            n_ens.trigger = n_run_en.complete.across("YMD")
            n_ens.trigger &= n_initialize.complete
            n_ens.add(Variable("CONTEXT", "ens"))
            n_ret_ens = Family("retrieve")
            n_ret_ens.add(Variable("NWORKERS", mars_nworkers))
            members = [str(x).zfill(2) for x in range(0, ens_members)]
            mars_members = partition(members, mars_nworkers)

            for worker in range(1, mars_nworkers + 1):
                n_worker = Family(str(worker))
                n_worker.add(Variable("WORKER", worker))
                n_worker.add(
                    Task("retrieve_ens"),
                )
                n_ret_ens.add(n_worker)

            n_prep_ens = Task("prep_task")
            n_prep_ens.trigger = n_ret_ens.complete
            n_prep_ens.trigger &= n_ret_hr.complete
            n_ens_ens = Family("ens_members")
            n_ens_ens.trigger = n_prep_ens.complete
            n_ens_ens.add_variable("MEM", ENS_TASK_MEM_MB)
            n_ens.add(n_ret_ens)
            if is_00z_cycle(main_hh):
                n_ens.add(n_prep_ens, n_ens_ens)

            for vpu in vpu_list:
                # Create the ensemble tasks
                for mem in reversed(range(1, HRES_ENSEMBLE_MEMBER + 1)):
                    n_member = Family(f"{vpu}_{mem:02d}").add(
                        Task("ens_member"),
                        Variable("JOB_ID", f"job_{vpu}_{mem}"),
                    )
                    n_ens_ens.add(n_member)

            n_vpus = Family("vpu_list")
            for vpu in vpu_list:
                n_vpu = Family(vpu.replace("-", "_"))
                n_vpu.add_variable("VPU", vpu)
                n_vpu.add_task("nco_calc")
                n_vpu.add_task("comp_init").add_trigger(
                    "nco_calc == complete"
                )
                n_vpu.add_task("plain_table").add_trigger(
                    "nco_calc == complete"
                )
                n_vpu.add_task("day_one").add_trigger(
                    "plain_table == complete"
                )

                n_vpus.add(n_vpu)
            n_vpus.trigger = n_ens.complete & n_hr.complete

            n_nc_to_zarr = Task("nc_to_zarr")
            n_nc_to_zarr.trigger = n_ens.complete & n_hr.complete & n_vpus.complete
            
            n_plain_table = Task("combine_plain_table")
            n_plain_table.trigger = n_vpus.complete

            n_forecast_warnings = Task("combine_forecast_warnings")
            n_forecast_warnings.trigger = n_vpus.complete

            n_archive_qinit = Task("archive_qinit")
            n_archive_qinit.add_variable('MEM', ARCHIVE_QINIT_MEM_MB)
            n_archive_qinit.add_variable('NCPUS', 12)
            n_archive_qinit.trigger = n_vpus.complete

            n_archive_to_aws = Task("archive_to_aws")
            n_archive_to_aws.trigger = (
                n_nc_to_zarr.complete & n_plain_table.complete
            )

            n_diss = Family("diss")
            n_diss_ip = Family("diss_ip")
            n_diss_ip.add(Variable("CONTEXT", "input"))
            n_diss_ip.defuser = e_no_diss
            n_diss_ip.add(Task("diss"))
            n_diss_ip.trigger = n_ret_ens.complete & n_ret_hr.complete
            n_diss.add(n_diss_ip)

            n_web = Family("web_push")
            n_web.trigger = n_nc_to_zarr.complete & n_vpus.complete
            n_web_prod = Family("prod")
            n_web_prod.add(Task("web_push"))
            n_web_prod.defuser = e_no_web_prod
            n_web_test = Family("test")
            n_web_test.add(Task("web_push"))
            n_web_test.defuser = e_no_web_test
            n_web.add(n_web_prod, n_web_test)

            if is_00z_cycle(main_hh):
                main_hh.add(
                    n_initialize,
                    n_hr,
                    n_ens,
                    n_vpus,
                    n_nc_to_zarr,
                    n_plain_table,
                    n_forecast_warnings,
                    n_archive_qinit,
                    n_archive_to_aws,
                    n_diss,
                    n_web,
                )
            else:
                main_hh.add(n_hr, n_ens, n_diss)

            n_main.add(main_hh)

            lag_hh.add_task("clean")

            n_lag_arch_init = Task("arch_init")
            n_lag_arch_init.defuser = e_no_ecfs_archive
            n_lag_arch_fc = Task("arch_fc")
            n_lag_arch_fc.defuser = e_no_ecfs_archive
            if is_00z_cycle(main_hh):
                lag_hh.add(n_lag_arch_init, n_lag_arch_fc)
            lag_hh.trigger = main_hh.complete.across("YMD")
            n_daily_lag.add(lag_hh)

        n_barrier_daily.add(n_barrier_epilog)
        n_main.add(DummyEpilog(done=barrier_ymd > main_ymd))
        n_lag.add(n_daily_lag)
        n_daily_lag.add(DummyEpilog(done=barrier_ymd > lag_ymd))
        suite.add(n_barrier, n_main, n_lag)


