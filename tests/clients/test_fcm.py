"""FCM 웹 푸시 클라이언트 기대 동작 (#63). 네트워크 대신 MockTransport 를 쓴다.

형식 근거: firebase.google.com/docs/cloud-messaging/send/v1-api,
developers.google.com/identity/protocols/oauth2/service-account
"""

import json
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.clients.fcm import FCM_SCOPE, TOKEN_URL, FcmClient
from app.core.exceptions import AppException, ErrorCode

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
SERVICE_ACCOUNT = json.dumps(
    {
        "type": "service_account",
        "project_id": "saetgil-test",
        "private_key_id": "kid-1",
        "private_key": PEM,
        "client_email": "fcm@saetgil-test.iam.gserviceaccount.com",
    }
)
SEND_URL = "https://fcm.googleapis.com/v1/projects/saetgil-test/messages:send"


class Recorder:
    def __init__(self, send_status: int = 200) -> None:
        self.send_status = send_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            return httpx.Response(
                200, json={"access_token": "ya29.test", "expires_in": 3600, "token_type": "Bearer"}
            )
        if self.send_status == 200:
            return httpx.Response(200, json={"name": "projects/saetgil-test/messages/0:1"})
        return httpx.Response(self.send_status, json={})


def client(recorder: Recorder) -> FcmClient:
    return FcmClient(SERVICE_ACCOUNT, transport=httpx.MockTransport(recorder))


async def test_send_posts_webpush_message_with_bearer_token() -> None:
    recorder = Recorder()

    sent = await client(recorder).send(
        "device-token", title="오늘의 샛길", body="4분 돌아가면 숲", link="https://saetgil.app"
    )

    assert sent is True
    send = recorder.requests[-1]
    assert str(send.url) == SEND_URL
    assert send.headers["Authorization"] == "Bearer ya29.test"
    message = json.loads(send.content)["message"]
    assert message["token"] == "device-token"
    assert message["webpush"]["notification"] == {"title": "오늘의 샛길", "body": "4분 돌아가면 숲"}
    assert message["webpush"]["fcm_options"] == {"link": "https://saetgil.app"}


async def test_access_token_request_is_a_signed_service_account_jwt() -> None:
    recorder = Recorder()

    await client(recorder).send("t", title="a", body="b", link=None)

    form = parse_qs(recorder.requests[0].content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(
        form["assertion"][0],
        PRIVATE_KEY.public_key(),
        algorithms=["RS256"],
        audience=TOKEN_URL,
    )
    assert claims["iss"] == "fcm@saetgil-test.iam.gserviceaccount.com"
    assert claims["scope"] == FCM_SCOPE
    assert claims["exp"] - claims["iat"] <= 3600
    assert jwt.get_unverified_header(form["assertion"][0])["kid"] == "kid-1"


async def test_access_token_is_reused_until_it_expires() -> None:
    recorder = Recorder()
    fcm = client(recorder)

    await fcm.send("a", title="t", body="b", link=None)
    await fcm.send("b", title="t", body="b", link=None)

    token_calls = [r for r in recorder.requests if str(r.url) == TOKEN_URL]
    assert len(token_calls) == 1


async def test_http_link_is_not_sent_because_fcm_requires_https() -> None:
    recorder = Recorder()

    await client(recorder).send("t", title="a", body="b", link="http://localhost:3000")

    message = json.loads(recorder.requests[-1].content)["message"]
    assert "fcm_options" not in message["webpush"]


async def test_unregistered_token_returns_false() -> None:
    # 문서: UNREGISTERED 는 HTTP 404, 토큰을 지우고 다시 쓰지 말 것
    assert (
        await client(Recorder(send_status=404)).send("t", title="a", body="b", link=None) is False
    )


async def test_server_error_raises_external_api_failed() -> None:
    with pytest.raises(AppException) as raised:
        await client(Recorder(send_status=500)).send("t", title="a", body="b", link=None)

    assert raised.value.code == ErrorCode.EXTERNAL_API_FAILED


async def test_unconfigured_client_reports_not_configured() -> None:
    assert FcmClient("").configured is False
    assert client(Recorder()).configured is True
