"""Shared test helpers: build graphs and log inputs/decisions for every case."""

import logging

import pytest

from branch_release_planner.models import Branch, Commit, Tag


@pytest.fixture(autouse=True)
def _case_logger(request):
    """Print the case name so -s logs align inputs with decisions."""
    logging.getLogger("planner-tests").info("CASE: %s", request.node.name)
    yield


def C(cid, parents=(), message="", timestamp=0) -> Commit:
    return Commit(id=cid, parent_ids=tuple(parents), message=message, timestamp=timestamp)


def B(name, head) -> Branch:
    return Branch(name=name, head=head)


def T(name, target) -> Tag:
    return Tag(name=name, target=target)


def log_case(title: str, **fields) -> None:
    logger = logging.getLogger("planner-tests")
    logger.info("INPUT[%s]: %s", title, fields)


def log_reasons(plan) -> None:
    logger = logging.getLogger("planner-tests")
    for r in getattr(plan, "reasons", ()):
        logger.info("DECISION: %s", r)
