import pytest

from agent import run


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    """A test must never reach the model server: it may or may not be running, and its answers
    are not repeatable. A test that goes through the command line sets run.local_model itself."""
    def refuse():
        raise AssertionError("this test would call the real model; give it a ScriptedModel")

    monkeypatch.setattr(run, "local_model", refuse)
