"""Structural smoke tests for the GEOGloWS suite definition.

The suite definition is built entirely in memory here (no ecflow server, no
files written to disk) and the resulting node tree is asserted directly. These
pin the structure the routing workflow depends on so that changes to the
builder are caught.

Requires the ``ecflow`` Python bindings; the module is skipped where they are
not installed.
"""

import pytest

pytest.importorskip("ecflow")

from geoglows_ecflow.workflow.comfies.config import Config  # noqa: E402
from geoglows_ecflow.workflow.builders.builder import Builder  # noqa: E402

SUITE = "geoglows_test"

# Families that are only attached on the 00Z cycle (the full forecast
# pipeline). The 12Z cycle builds a reduced tree without them.
GATED_FAMILIES = {
    "initialize",
    "vpu_list",
    "nc_to_zarr",
    "combine_plain_table",
    "combine_forecast_warnings",
    "archive_qinit",
    "archive_to_aws",
    "web_push",
}


class _DictConfigSource:
    """Minimal in-memory config source, so no ``.cfg`` file is needed."""

    def __init__(self, data):
        self.name = SUITE
        self.origin = "<in-memory test config>"
        self.data = data


def _make_config(mode="prod", vpu_list=("101",)):
    """A minimal in-memory ``Config`` sufficient to construct the builder."""
    data = {
        "name": SUITE,
        "mode": mode,
        "first_date": "20230101",
        "last_date": "20230102",
        "first_barrier": "20230101",
        "exparch": "/tmp/arch",
        "workroot": "/tmp/work",
        "vpu_list": list(vpu_list),
        "ens_members": 51,
        "mars_workers": 1,
        "target": {"root": "/tmp/target"},
        "jobs": {
            "root": "/tmp/jobs",
            "destinations": {"default": {"name": "localhost"}},
        },
    }
    return Config(_DictConfigSource(data))


def build_defs(mode="prod", vpu_list=("101",)):
    """Build the suite definition in memory for the given run mode.

    The returned object is the ecflow ``Defs``. Building runs the builder's
    own structural validation (``defs.check()``), so a malformed tree raises
    here rather than producing a bad definition.
    """
    builder = Builder(_make_config(mode=mode, vpu_list=vpu_list))
    builder()  # runs build() and defs.check()
    return builder.defs


def _member_family_names(defs, cycle):
    """Names of the ensemble-member families on a cycle, or None if absent."""
    node = defs.find_abs_node(f"/{SUITE}/main/{cycle}/ens/ens_members")
    if node is None:
        return None
    return sorted(n.name() for n in node.nodes)


def _cycle_children(defs, cycle):
    node = defs.find_abs_node(f"/{SUITE}/main/{cycle}")
    return [n.name() for n in node.nodes]


@pytest.fixture(scope="module")
def prod_defs():
    """A production-mode suite definition, built once and treated read-only."""
    return build_defs(mode="prod")


def test_00_cycle_builds_one_family_per_ensemble_member(prod_defs):
    expected = sorted(f"101_{m:02d}" for m in range(1, 53))
    assert _member_family_names(prod_defs, "00") == expected


def test_12_cycle_has_no_ensemble_members(prod_defs):
    assert _member_family_names(prod_defs, "12") is None


def test_00_cycle_builds_the_full_pipeline(prod_defs):
    assert GATED_FAMILIES <= set(_cycle_children(prod_defs, "00"))


def test_12_cycle_skips_the_gated_families(prod_defs):
    assert _cycle_children(prod_defs, "12") == ["hres", "ens", "diss"]


def test_nominal_time_families_carry_their_cycle_in_emos_base(prod_defs):
    assert prod_defs.find_abs_node(
        f"/{SUITE}/main/00"
    ).find_variable("EMOS_BASE").value() == "00"
    assert prod_defs.find_abs_node(
        f"/{SUITE}/main/12"
    ).find_variable("EMOS_BASE").value() == "12"


def test_ensemble_and_archive_memory_limits(prod_defs):
    ens_members = prod_defs.find_abs_node(
        f"/{SUITE}/main/00/ens/ens_members"
    )
    archive_qinit = prod_defs.find_abs_node(
        f"/{SUITE}/main/00/archive_qinit"
    )
    assert ens_members.find_variable("MEM").value() == "6000"
    assert archive_qinit.find_variable("MEM").value() == "4000"


def test_vpu_count_scales_the_member_families():
    defs = build_defs(mode="prod", vpu_list=("101", "102"))
    assert len(_member_family_names(defs, "00")) == 2 * 52


def test_default_minimum_comfies_version_does_not_block_construction():
    # A builder that leaves comfies_minimum_version at its "any" default must
    # still construct: the version gate treats "any" as "no minimum".
    class _AnyVersionBuilder(Builder):
        comfies_minimum_version = "any"

    _AnyVersionBuilder(_make_config())
