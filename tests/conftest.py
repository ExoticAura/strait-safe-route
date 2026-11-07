import os, sys
# The suite tests the model on GEBCO + OpenStreetMap (what CI has). The MPA
# chart layers are tested in test_mpa.py on a small synthetic chart; run the
# whole suite on the real MPA data (if in data/gss) with SAFEROUTE_MPA=1.
os.environ.setdefault("SAFEROUTE_MPA", "0")
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
