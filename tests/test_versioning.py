"""SemVer precedence, prerelease ordering and bump rules."""

import logging

import pytest

from branch_release_planner.errors import InvalidVersionError
from branch_release_planner.versioning import (
    SemVer,
    bump,
    compare_versions,
    is_prerelease,
    max_version,
    parse_version,
)

LOG = logging.getLogger("planner-tests")

ORDERED = [
    "1.0.0-alpha",
    "1.0.0-alpha.1",
    "1.0.0-alpha.beta",
    "1.0.0-beta",
    "1.0.0-beta.2",
    "1.0.0-beta.11",
    "1.0.0-rc.1",
    "1.0.0",
]


def test_semver_ordering_from_spec():
    LOG.info("INPUT: ordered SemVer chain %s", ORDERED)
    versions = [parse_version(v) for v in ORDERED]
    for earlier, later in zip(versions, versions[1:]):
        assert compare_versions(earlier, later) < 0, f"{earlier} should < {later}"
    assert compare_versions(versions[-1], versions[-1]) == 0
    LOG.info("DECISION: strict total order verified incl. numeric 2 < 11")


def test_build_metadata_ignored_in_precedence():
    a, b = parse_version("1.0.0+exp.sha.5114f85"), parse_version("1.0.0")
    LOG.info("INPUT: compare %s vs %s", a, b)
    assert compare_versions(a, b) == 0
    LOG.info("DECISION: build metadata never affects precedence")


def test_prerelease_lower_than_release():
    assert is_prerelease(parse_version("2.0.0-rc.3"))
    assert compare_versions(parse_version("2.0.0-rc.3"), parse_version("2.0.0")) < 0


def test_v_prefix_and_bumps():
    assert str(parse_version("v1.4.7")) == "1.4.7"
    assert str(bump(parse_version("1.2.3"), MAJOR := "major")) == "2.0.0"
    assert str(bump(parse_version("1.2.3"), "minor")) == "1.3.0"
    assert str(bump(parse_version("1.2.3"), "patch")) == "1.2.4"
    assert str(bump(parse_version("1.2.3"), "minor", prerelease="rc")) == "1.3.0-rc.1"


def test_invalid_versions_are_distinguishable():
    for bad in ("1.2", "1.x.0", "1.2.3-01", "not-a-version", ""):
        LOG.info("INPUT: invalid version %r", bad)
        with pytest.raises(InvalidVersionError) as ei:
            parse_version(bad)
        assert ei.value.code == "invalid_version"
        LOG.info("DECISION: rejected with code=%s details=%s", ei.value.code, ei.value.details)


def test_max_version_picks_highest():
    versions = [parse_version(v) for v in ("1.2.0", "1.10.0", "1.2.0-rc.1", "v2.0.0-alpha")]
    assert max_version(versions) == SemVer(2, 0, 0, ("alpha",))


def test_advance_prerelease_counter_rules():
    from branch_release_planner.versioning import advance_prerelease
    assert str(advance_prerelease(parse_version("1.1.0-rc.1"), "rc")) == "1.1.0-rc.2"
    assert str(advance_prerelease(parse_version("1.1.0-rc.9"), "rc")) == "1.1.0-rc.10"
    assert str(advance_prerelease(parse_version("1.1.0-beta.2"), "rc")) == "1.1.0-rc.1"
    assert str(advance_prerelease(parse_version("1.1.0"), "rc")) == "1.1.0-rc.1"
    assert str(advance_prerelease(parse_version("1.1.0-rc.1+build.7"), "rc")) == "1.1.0-rc.2"
