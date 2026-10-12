import asyncio

import httpx
import pytest

from mcp_server import config
from mcp_server.clients import payflow


class FakePayflow:
    """The gateway as the client sees it: a token endpoint and an API that wants the newest token."""

    def __init__(self, expires_in=900, api_status=200):
        self.tokens_issued = 0
        self.expires_in = expires_in
        self.api_status = api_status
        self.rejects_tokens_before = 0   # tokens numbered below this are answered with 401
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/oauth2/token":
            if request.headers["Authorization"] != httpx.BasicAuth("incident-agent", "s3cret")._auth_header:
                return httpx.Response(401, json={"error": "invalid_client"})
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"token-{self.tokens_issued}", "expires_in": self.expires_in})
        number = int(request.headers["Authorization"].removeprefix("Bearer token-"))
        if number < self.rejects_tokens_before:
            return httpx.Response(401, json={"status": 401})
        return httpx.Response(self.api_status, json={"path": request.url.path, "token": number})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://payflow", transport=httpx.MockTransport(self))


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(config, "PAYFLOW_CLIENT_ID", "incident-agent")
    monkeypatch.setattr(config, "PAYFLOW_CLIENT_SECRET", "s3cret")
    payflow.forget_token()
    yield
    payflow.forget_token()


def run(fake, *calls):
    async def go():
        async with fake.client() as client:
            return [await payflow._call(client, method, path) for method, path in calls]

    return asyncio.run(go())


def test_one_token_is_fetched_with_the_client_credentials_and_reused():
    fake = FakePayflow()

    answers = run(fake, ("GET", "/api/v1/payments/sagas/attention"), ("GET", "/api/v1/payouts/sagas/attention"))

    assert [answer["token"] for answer in answers] == [1, 1]
    assert fake.tokens_issued == 1
    token_request = fake.requests[0]
    assert token_request.content == b"grant_type=client_credentials"
    assert token_request.headers["Authorization"].startswith("Basic ")


def test_a_token_about_to_expire_is_replaced_before_the_next_call():
    fake = FakePayflow(expires_in=30)  # less than the renewal margin: already too old to use again

    answers = run(fake, ("GET", "/a"), ("GET", "/b"))

    assert [answer["token"] for answer in answers] == [1, 2]


def test_a_token_payflow_no_longer_accepts_is_replaced_once_and_the_call_repeated():
    fake = FakePayflow()
    run(fake, ("GET", "/a"))
    fake.rejects_tokens_before = 2  # PayFlow's signing key changed: token 1 is dead

    answer = run(fake, ("GET", "/b"))[0]

    assert answer == {"path": "/b", "token": 2}
    assert fake.tokens_issued == 2


def test_a_call_still_refused_with_a_fresh_token_is_a_missing_scope_not_an_endless_loop():
    fake = FakePayflow(api_status=403)

    with pytest.raises(payflow.PayflowUnavailable, match="lacks the scope"):
        run(fake, ("POST", "/api/v1/reconciliation/runs"))

    assert fake.tokens_issued == 1
    fake.api_status, fake.rejects_tokens_before = 200, 99
    with pytest.raises(payflow.PayflowUnavailable):
        run(fake, ("GET", "/a"))
    assert fake.tokens_issued == 2  # one retry with a new token, then it gives up


def test_missing_and_wrong_ids_are_told_apart_from_payflow_being_down():
    with pytest.raises(payflow.NotFound):
        run(FakePayflow(api_status=404), ("GET", "/api/v1/payments/pay_x/saga"))
    with pytest.raises(payflow.BadRequest):
        run(FakePayflow(api_status=400), ("GET", "/api/v1/payments/nonsense/saga"))
    with pytest.raises(httpx.HTTPStatusError):
        run(FakePayflow(api_status=503), ("GET", "/a"))


def test_a_refused_or_missing_secret_says_so_without_calling_the_api(monkeypatch):
    fake = FakePayflow()
    monkeypatch.setattr(config, "PAYFLOW_CLIENT_SECRET", "wrong")
    with pytest.raises(payflow.PayflowUnavailable, match="refused the client id or secret"):
        run(fake, ("GET", "/a"))

    monkeypatch.setattr(config, "PAYFLOW_CLIENT_SECRET", "")
    with pytest.raises(payflow.PayflowUnavailable, match="PAYFLOW_CLIENT_SECRET is not set"):
        run(fake, ("GET", "/a"))

    assert [request.url.path for request in fake.requests] == ["/oauth2/token"]  # the API itself was never called


def test_concurrent_calls_share_one_token_request():
    fake = FakePayflow()

    async def go():
        async with fake.client() as client:
            return await asyncio.gather(*(payflow._call(client, "GET", f"/{n}") for n in range(5)))

    answers = asyncio.run(go())

    assert {answer["token"] for answer in answers} == {1}
    assert fake.tokens_issued == 1


def test_the_env_file_fills_in_what_the_environment_does_not_set(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# local only\nPAYFLOW_CLIENT_SECRET=from-file\nPAYFLOW_URL = http://elsewhere \n\nnot a setting\n", encoding="utf-8")
    monkeypatch.delenv("PAYFLOW_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("PAYFLOW_URL", "http://from-environment")

    config._load_env_file(env_file)
    config._load_env_file(tmp_path / "missing.env")

    import os
    assert os.environ["PAYFLOW_CLIENT_SECRET"] == "from-file"
    assert os.environ["PAYFLOW_URL"] == "http://from-environment"  # the environment wins
    monkeypatch.delenv("PAYFLOW_CLIENT_SECRET")
