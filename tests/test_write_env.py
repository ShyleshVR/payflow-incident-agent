import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "write_env.py"


@pytest.fixture
def write_env(tmp_path, monkeypatch):
    """scripts/write_env.py, writing its .env into tmp_path instead of the repository."""
    spec = importlib.util.spec_from_file_location("write_env", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.__file__ = str(tmp_path / "scripts" / "write_env.py")     # .env goes to tmp_path
    return module


def payflow(tmp_path, token=None):
    secrets = tmp_path / "payflow" / "k8s" / "overlays" / "local" / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "auth.env").write_text("admin-client-secret=aaa\nincident-agent-client-secret=client-s3cret\n", encoding="utf-8")
    if token is not None:
        (secrets / "incident-agent-token").write_text(token, encoding="utf-8")
    return tmp_path / "payflow"


def test_the_client_secret_and_the_alert_token_go_into_env_and_are_never_printed(tmp_path, write_env, monkeypatch, capsys):
    (tmp_path / ".env").write_text("LLM_MODEL=gpt-oss-20b\nAGENT_TOKEN=old\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["write_env.py", str(payflow(tmp_path, token="t0ken\r\n"))])

    assert write_env.main() == 0

    assert (tmp_path / ".env").read_text(encoding="utf-8").splitlines() == [
        "LLM_MODEL=gpt-oss-20b", "PAYFLOW_CLIENT_SECRET=client-s3cret", "AGENT_TOKEN=t0ken"]
    printed = capsys.readouterr().out
    assert "wrote PAYFLOW_CLIENT_SECRET, AGENT_TOKEN" in printed
    assert "client-s3cret" not in printed and "t0ken" not in printed


def test_without_the_token_file_only_the_client_secret_is_written(tmp_path, write_env, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["write_env.py", str(payflow(tmp_path))])

    assert write_env.main() == 0

    assert (tmp_path / ".env").read_text(encoding="utf-8") == "PAYFLOW_CLIENT_SECRET=client-s3cret\n"
    assert "PayFlow does not send its alerts to the agent" in capsys.readouterr().out
