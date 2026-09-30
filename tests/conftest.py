import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from saferoute import cache, network as NW, obstacles as O


@pytest.fixture(scope="session")
def grid():
    return cache.grid(quiet=True)


@pytest.fixture(scope="session")
def snap(grid):
    return cache.snapshot("synthetic", quiet=True)


@pytest.fixture(scope="session")
def nets():
    return {m: cache.network("PANAMAX", m, "synthetic", quiet=True) for m in NW.MODES}
