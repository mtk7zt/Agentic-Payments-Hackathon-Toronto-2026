"""Regression tests always start with mock integrations, even in a live-configured shell."""

import pytest

_ENV = pytest.StashKey[pytest.MonkeyPatch]()


def pytest_configure(config):
    patch = pytest.MonkeyPatch()
    patch.setenv("MANDATE_RAIL", "mock")
    patch.setenv("MANDATE_SELLER_MODE", "mock")
    config.stash[_ENV] = patch


def pytest_unconfigure(config):
    patch = config.stash.get(_ENV, None)
    if patch is not None:
        patch.undo()
