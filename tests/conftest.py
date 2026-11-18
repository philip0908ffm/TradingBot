"""Shared pytest configuration."""
from __future__ import annotations

import os

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "network: test needs internet access (run with RUN_NETWORK_TESTS=1)"
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_NETWORK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="Netzwerk-Test (RUN_NETWORK_TESTS=1 zum Aktivieren)")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
