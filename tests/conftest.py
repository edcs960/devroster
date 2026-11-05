import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_app_home(tmp_path_factory):
    """Keep CLI app homes out of the real user profile and undo env changes."""
    saved = dict(os.environ)
    os.environ["DEVROSTER_HOME"] = str(tmp_path_factory.mktemp("devroster-home"))
    yield
    os.environ.clear()
    os.environ.update(saved)
