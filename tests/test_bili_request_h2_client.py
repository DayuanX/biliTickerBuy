import urllib.parse
from typing import Any

import httpx

from util.request.BiliRequest import AbstractH2Client, BiliRequest


def _is_randomized_form_of(actual: str, expected: str) -> bool:
    actual_parts = urllib.parse.urlsplit(actual)
    expected_parts = urllib.parse.urlsplit(expected)
    return (
        actual_parts.scheme == expected_parts.scheme
        and actual_parts.netloc == expected_parts.netloc
        and urllib.parse.unquote(actual_parts.path).endswith(expected_parts.path)
    )


class FakeCookies:
    def __init__(self) -> None:
        self.values: list[tuple[str, str, str]] = []

    def set(
        self,
        name: str,
        value: str,
        domain: str = "",
        path: str = "/",
    ) -> None:
        self.values.append((name, value, domain))


class FakeH2Client(AbstractH2Client):
    instances: list["FakeH2Client"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self._headers = dict(kwargs.get("headers", {}))
        self._cookies = FakeCookies()
        self.calls: list[tuple] = []
        self.closed = False
        self.instances.append(self)

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    @property
    def cookies(self) -> FakeCookies:
        return self._cookies

    def head(self, url: str) -> httpx.Response:
        self.calls.append(("head", url))
        return httpx.Response(200, request=httpx.Request("HEAD", url))

    def get(self, url: str, *, params: Any = None) -> httpx.Response:
        self.calls.append(("get", url, params))
        return httpx.Response(
            200,
            json={"msg": ""},
            request=httpx.Request("GET", url),
        )

    def post(
        self,
        url: str,
        *,
        data: Any = None,
        json: Any = None,
    ) -> httpx.Response:
        self.calls.append(("post", url, data, json))
        return httpx.Response(
            200,
            json={"msg": ""},
            request=httpx.Request("POST", url),
        )

    def close(self) -> None:
        self.closed = True


def test_h2_client_constructor_uses_abstract_client_interface():
    FakeH2Client.instances = []
    request = BiliRequest(
        cookies=[{"name": "SESSDATA", "value": "abc"}],
        h2_client_type=FakeH2Client,
    )
    url = "https://show.bilibili.com/api/ticket/order/createV2"

    request.prewarm_h2_connection(url)
    request._h2_send("post", url, data={"project_id": 1}, isJson=True)
    request._h2_send("get", url, data={"project_id": 1})

    client = FakeH2Client.instances[0]
    assert client.kwargs["http2"] is True
    assert client.headers["user-agent"] == request.get_user_agent()
    assert client.cookies.values == [
        ("SESSDATA", "abc", ".bilibili.com"),
        ("SESSDATA", "abc", ".bilibili.com"),
        ("SESSDATA", "abc", ".bilibili.com"),
    ]
    assert client.calls[0] == ("head", url)
    assert client.calls[1][0] == "post"
    assert _is_randomized_form_of(client.calls[1][1], url)
    assert client.calls[1][2:] == (None, {"project_id": 1})
    assert client.calls[2][0] == "get"
    assert _is_randomized_form_of(client.calls[2][1], url)
    assert client.calls[2][2] == {"project_id": 1}

    request._invalidate_h2_client()

    assert client.closed is True


def test_h2_path_randomization_is_limited_to_create_v2():
    FakeH2Client.instances = []
    request = BiliRequest(
        cookies=[{"name": "SESSDATA", "value": "abc"}],
        h2_client_type=FakeH2Client,
    )
    create_url = "https://show.bilibili.com/api/ticket/order/createV2"
    buyer_url = "https://show.bilibili.com/api/ticket/buyer/list?is_default&projectId=1"

    request._h2_send("post", create_url, data={"project_id": 1}, isJson=True)
    request._h2_send("get", buyer_url)

    client = FakeH2Client.instances[0]
    assert client.calls[0][0] == "post"
    assert _is_randomized_form_of(client.calls[0][1], create_url)
    assert client.calls[1] == ("get", buyer_url, None)


def test_h2_path_randomization_matches_create_v2_case_insensitively():
    assert BiliRequest._should_randomize_h2_path(
        "https://show.bilibili.com/api/ticket/order/createv2"
    )
    assert BiliRequest._should_randomize_h2_path(
        "https://show.bilibili.com/api/ticket/order/create%56%32"
    )
    assert not BiliRequest._should_randomize_h2_path(
        "https://show.bilibili.com/api/ticket/buyer/list"
    )


def test_encoded_create_path_keeps_its_meaning_when_randomized(monkeypatch):
    import random

    monkeypatch.setattr(random, "choice", lambda values: values[-1])
    FakeH2Client.instances = []
    request = BiliRequest(cookies=[], h2_client_type=FakeH2Client)
    url = "https://show.bilibili.com/api/ticket/order/create%56%32?project_id=1"
    request._h2_send("post", url, data={})
    actual = FakeH2Client.instances[0].calls[0][1]
    assert urllib.parse.unquote(actual) == urllib.parse.unquote(url)
    request._invalidate_h2_client()
