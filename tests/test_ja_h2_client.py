from __future__ import annotations

import subprocess
import time
import unittest
from unittest.mock import patch
from typing import Any

import httpx

from h2client_loader import load_h2client_module


h2connection = load_h2client_module("h2connection")
ja_h2_client = load_h2client_module("ja_h2_client")
local_sources = load_h2client_module("local_sources")

H2Response = h2connection.H2Response
RotatingIPJA3H2Client = ja_h2_client.RotatingIPJA3H2Client
CreateV2FanoutJA3H2Client = ja_h2_client.CreateV2FanoutJA3H2Client
SourceAddress = ja_h2_client.SourceAddress


class SourceDiscoveryTests(unittest.TestCase):
    def test_source_interface_aliases_can_be_read_from_environment(self) -> None:
        with patch.dict(
            local_sources.os.environ,
            {"BTB_H2_SOURCE_INTERFACE_ALIASES": "en0,en7"},
        ):
            self.assertEqual(
                local_sources._source_interface_aliases(),
                ("en0", "en7"),
            )

    def test_ifconfig_discovers_macos_style_interfaces(self) -> None:
        output = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
    inet6 fe80::1%en0 prefixlen 64 secured scopeid 0xe
    inet6 2001:db8::1 prefixlen 64 autoconf secured
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
    inet 10.8.0.2 --> 10.8.0.2 netmask 0xffffffff
"""
        completed = subprocess.CompletedProcess(
            ["ifconfig"],
            0,
            stdout=output,
            stderr="",
        )

        with patch.object(local_sources.subprocess, "run", return_value=completed):
            sources = local_sources._discover_sources_with_ifconfig(())

        self.assertEqual(
            [(source.ip, source.family, source.interface_alias) for source in sources],
            [
                ("192.0.2.10", "ipv4", "en0"),
                ("2001:db8::1", "ipv6", "en0"),
                ("10.8.0.2", "ipv4", "utun4"),
            ],
        )

    def test_ifconfig_prefers_matching_interface_names(self) -> None:
        output = """
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
en7: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.0.2.11 netmask 0xffffff00 broadcast 192.0.2.255
"""
        completed = subprocess.CompletedProcess(["ifconfig"], 0, stdout=output)

        with patch.object(local_sources.subprocess, "run", return_value=completed):
            sources = local_sources._discover_sources_with_ifconfig(("en7",))

        self.assertEqual(
            [(source.ip, source.interface_alias) for source in sources],
            [("192.0.2.11", "en7")],
        )

    def test_discover_uses_ifconfig_on_macos_without_powershell(self) -> None:
        expected = [SourceAddress("192.0.2.10", "ipv4", "en0")]

        with (
            patch.object(local_sources.platform, "system", return_value="Darwin"),
            patch.object(
                local_sources,
                "_discover_sources_with_powershell",
                side_effect=AssertionError("PowerShell should not run on macOS"),
            ),
            patch.object(
                local_sources,
                "_discover_sources_with_ifconfig",
                return_value=expected,
            ),
            patch.object(
                local_sources, "_discover_sources_with_socket", return_value=[]
            ),
        ):
            sources = local_sources.discover_interface_source_ips(("en0",))

        self.assertEqual(sources, expected)


class FakeH2Connection:
    fail_healthcheck_sources: set[str] = set()
    fail_request_sources: set[str] = set()
    status_by_source: dict[str, int] = {}
    body_by_source: dict[str, bytes] = {}
    delay_by_source: dict[str, float] = {}
    instances: list["FakeH2Connection"] = []

    def __init__(
        self,
        remote_host: str,
        source_ip: str | None,
        *,
        port: int = 443,
        sni: str | None = None,
        family: str = "auto",
        timeout: float = 10.0,
        assert_ja: bool = False,
        proxy_url: str | None = None,
    ) -> None:
        self.remote_host = remote_host
        self.source_ip = source_ip or ""
        self.port = port
        self.sni = sni
        self.family = family
        self.timeout = timeout
        self.assert_ja = assert_ja
        self.calls: list[tuple[str, str, Any, Any]] = []
        self.closed = False
        self.instances.append(self)

    def get(self, url: str, headers=None) -> H2Response:
        self.calls.append(("GET", url, headers, None))
        if url == "https://show.bilibili.com/":
            if self.source_ip in self.fail_healthcheck_sources:
                raise OSError("healthcheck failed")
        elif self.source_ip in self.fail_request_sources:
            raise OSError("request failed")
        return H2Response(
            status=200,
            headers=[(":status", "200"), ("content-type", "text/plain")],
            body=self.source_ip.encode("ascii"),
            stream_id=len(self.calls),
        )

    def post(self, url: str, headers=None, content=None) -> H2Response:
        self.calls.append(("POST", url, headers, content))
        if self.source_ip in self.fail_request_sources:
            raise OSError("request failed")
        delay = self.delay_by_source.get(self.source_ip, 0)
        if delay > 0:
            time.sleep(delay)
        has_forced_status = self.source_ip in self.status_by_source
        status = self.status_by_source.get(self.source_ip, 200)
        if self.source_ip in self.body_by_source:
            body = self.body_by_source[self.source_ip]
        elif has_forced_status:
            body = f"{self.source_ip}:{status}".encode("ascii")
        else:
            body = content or b""
        return H2Response(
            status=status,
            headers=[(":status", str(status)), ("content-type", "text/plain")],
            body=body,
            stream_id=len(self.calls),
        )

    def close(self) -> None:
        self.closed = True


class FakeFallbackClient:
    instances: list["FakeFallbackClient"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.headers = httpx.Headers(kwargs.get("headers") or {})
        self.cookies = httpx.Cookies(kwargs.get("cookies"))
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False
        self.instances.append(self)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(("GET", url, kwargs))
        return httpx.Response(
            200,
            text="fallback-get",
            request=httpx.Request("GET", url),
        )

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(("POST", url, kwargs))
        return httpx.Response(
            200,
            text="fallback-post",
            request=httpx.Request("POST", url),
        )

    def head(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(("HEAD", url, kwargs))
        return httpx.Response(
            200,
            request=httpx.Request("HEAD", url),
        )

    def close(self) -> None:
        self.closed = True


def first_slot(slots):
    return slots[0]


def business_call_instances():
    return [
        instance
        for instance in FakeH2Connection.instances
        if any(call[1] != "https://show.bilibili.com/" for call in instance.calls)
    ]


def first_business_instance() -> FakeH2Connection:
    return business_call_instances()[0]


class RotatingIPJA3H2ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeH2Connection.fail_healthcheck_sources = set()
        FakeH2Connection.fail_request_sources = set()
        FakeH2Connection.status_by_source = {}
        FakeH2Connection.body_by_source = {}
        FakeH2Connection.delay_by_source = {}
        FakeH2Connection.instances = []
        FakeFallbackClient.instances = []

    def test_constructor_keeps_only_healthchecked_sources(self) -> None:
        FakeH2Connection.fail_healthcheck_sources = {"192.0.2.2"}

        client = RotatingIPJA3H2Client(
            headers={"user-agent": "ua"},
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        self.assertEqual(client.available_source_ips, ["192.0.2.1"])
        self.assertEqual(len(FakeH2Connection.instances), 3)
        self.assertTrue(FakeH2Connection.instances[0].closed)
        self.assertTrue(FakeH2Connection.instances[1].closed)
        self.assertFalse(FakeH2Connection.instances[2].closed)

        client.close()

    def test_get_uses_headers_cookies_and_params(self) -> None:
        client = RotatingIPJA3H2Client(
            headers={"user-agent": "ua"},
            source_ip_provider=lambda: [SourceAddress("192.0.2.1", "ipv4", "WLAN")],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )
        client.cookies.set("SESSDATA", "abc", domain=".bilibili.com")

        response = client.get(
            "https://show.bilibili.com/api/ticket",
            params={"project_id": "1"},
        )

        self.assertEqual(response.status_code, 200)
        connection = first_business_instance()
        method, url, headers, _ = connection.calls[-1]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://show.bilibili.com/api/ticket?project_id=1")
        self.assertEqual(headers["user-agent"], "ua")
        self.assertIn("SESSDATA=abc", headers["cookie"])

        client.close()

    def test_post_json_sets_body_and_content_type(self) -> None:
        client = RotatingIPJA3H2Client(
            source_ip_provider=lambda: [SourceAddress("192.0.2.1", "ipv4", "WLAN")],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.post(
            "https://show.bilibili.com/api/ticket/order/createV2",
            json={"project_id": 1},
        )

        self.assertEqual(response.text, '{"project_id":1}')
        _, _, headers, content = first_business_instance().calls[-1]
        self.assertEqual(headers["content-type"], "application/json")
        self.assertNotEqual(headers.get("content-length"), "0")
        self.assertEqual(content, b'{"project_id":1}')

        client.close()

    def test_request_failure_switches_to_next_source_ip(self) -> None:
        FakeH2Connection.fail_request_sources = {"192.0.2.1"}
        client = RotatingIPJA3H2Client(
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.get("https://show.bilibili.com/api/ticket")

        self.assertEqual(response.text, "192.0.2.2")
        failed_connection = next(
            instance
            for instance in business_call_instances()
            if instance.source_ip == "192.0.2.1"
        )
        self.assertTrue(failed_connection.closed)

        client.close()

    def test_non_show_get_delegates_to_fallback_httpx_client(self) -> None:
        client = RotatingIPJA3H2Client(
            headers={"user-agent": "ua"},
            source_ip_provider=lambda: [SourceAddress("192.0.2.1", "ipv4", "WLAN")],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
            fallback_client_factory=FakeFallbackClient,
        )
        client.headers["x-client-header"] = "yes"
        client.cookies.set("SESSDATA", "abc", domain=".bilibili.com")

        response = client.get(
            "https://api.bilibili.com/x/web-interface/nav",
            params={"foo": "bar"},
            headers={"accept": "application/json"},
        )

        fallback = FakeFallbackClient.instances[0]
        self.assertEqual(response.text, "fallback-get")
        self.assertEqual(business_call_instances(), [])
        self.assertEqual(
            fallback.calls,
            [
                (
                    "GET",
                    "https://api.bilibili.com/x/web-interface/nav",
                    {
                        "params": {"foo": "bar"},
                        "headers": {"accept": "application/json"},
                    },
                )
            ],
        )
        self.assertEqual(fallback.headers["x-client-header"], "yes")
        self.assertEqual(fallback.cookies.get("SESSDATA"), "abc")

        client.close()

    def test_non_show_post_delegates_to_fallback_httpx_client(self) -> None:
        client = RotatingIPJA3H2Client(
            source_ip_provider=lambda: [SourceAddress("192.0.2.1", "ipv4", "WLAN")],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
            fallback_client_factory=FakeFallbackClient,
        )

        response = client.post(
            "https://api.bilibili.com/x/test",
            json={"ok": True},
            headers={"accept": "application/json"},
        )

        fallback = FakeFallbackClient.instances[0]
        self.assertEqual(response.text, "fallback-post")
        self.assertEqual(
            fallback.calls,
            [
                (
                    "POST",
                    "https://api.bilibili.com/x/test",
                    {
                        "data": None,
                        "json": {"ok": True},
                        "content": None,
                        "headers": {"accept": "application/json"},
                    },
                )
            ],
        )

        client.close()

    def test_create_v2_fanout_returns_200_and_discards_412(self) -> None:
        FakeH2Connection.status_by_source = {
            "192.0.2.1": 412,
            "192.0.2.2": 429,
            "192.0.2.3": 200,
        }
        FakeH2Connection.body_by_source = {
            "192.0.2.3": b'{"errno":0,"data":{"orderId":"1"}}',
        }
        FakeH2Connection.delay_by_source = {"192.0.2.3": 0.02}
        client = CreateV2FanoutJA3H2Client(
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
                SourceAddress("192.0.2.3", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.post(
            "https://show.bilibili.com/api/ticket/order/createV2",
            json={"project_id": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["errno"], 0)
        rejected_connection = next(
            instance
            for instance in business_call_instances()
            if instance.source_ip == "192.0.2.1"
        )
        self.assertTrue(rejected_connection.closed)
        self.assertEqual(
            [instance.calls[-1][0] for instance in business_call_instances()],
            ["POST", "POST", "POST"],
        )

        client.close()

    def test_create_v2_fanout_recognizes_percent_encoded_path(self) -> None:
        FakeH2Connection.status_by_source = {
            "192.0.2.1": 412,
            "192.0.2.2": 200,
        }
        FakeH2Connection.body_by_source = {
            "192.0.2.2": b'{"errno":0,"data":{"orderId":"1"}}',
        }
        client = CreateV2FanoutJA3H2Client(
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.post(
            "https://show.bilibili.com/%61pi/ticket/order/create%562",
            json={"project_id": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["errno"], 0)
        self.assertEqual(len(business_call_instances()), 2)

        client.close()

    def test_create_v2_fanout_prefers_non_900001_http_200(self) -> None:
        FakeH2Connection.status_by_source = {
            "192.0.2.1": 200,
            "192.0.2.2": 200,
            "192.0.2.3": 429,
        }
        FakeH2Connection.body_by_source = {
            "192.0.2.1": b'{"errno":900001,"msg":"fast"}',
            "192.0.2.2": b'{"errno":0,"data":{"orderId":"1"}}',
        }
        FakeH2Connection.delay_by_source = {"192.0.2.2": 0.02}
        client = CreateV2FanoutJA3H2Client(
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
                SourceAddress("192.0.2.3", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.post(
            "https://show.bilibili.com/api/ticket/order/createV2",
            json={"project_id": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["errno"], 0)

        client.close()

    def test_create_v2_fanout_returns_900001_when_all_http_200_are_900001(
        self,
    ) -> None:
        FakeH2Connection.status_by_source = {
            "192.0.2.1": 200,
            "192.0.2.2": 200,
            "192.0.2.3": 429,
        }
        FakeH2Connection.body_by_source = {
            "192.0.2.1": b'{"errno":900001,"msg":"fast"}',
            "192.0.2.2": b'{"code":900001,"msg":"fast"}',
        }
        client = CreateV2FanoutJA3H2Client(
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
                SourceAddress("192.0.2.3", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.post(
            "https://show.bilibili.com/api/ticket/order/createV2",
            json={"project_id": 1},
        )

        self.assertEqual(response.status_code, 200)
        ret = response.json()
        self.assertEqual(ret.get("errno", ret.get("code")), 900001)

        client.close()

    def test_create_v2_fanout_returns_429_when_no_200(self) -> None:
        FakeH2Connection.status_by_source = {
            "192.0.2.1": 412,
            "192.0.2.2": 429,
        }
        client = CreateV2FanoutJA3H2Client(
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.post(
            "https://show.bilibili.com/api/ticket/order/createV2",
            json={"project_id": 1},
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.text, "192.0.2.2:429")

        client.close()

    def test_create_v2_fanout_matches_path_case_insensitively(self) -> None:
        client = CreateV2FanoutJA3H2Client(
            source_ip_provider=lambda: [SourceAddress("192.0.2.1", "ipv4", "WLAN")],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        self.assertTrue(
            client._should_fanout_create_v2(
                "https://show.bilibili.com/api/ticket/order/createv2"
            )
        )
        self.assertTrue(
            client._should_fanout_create_v2(
                "https://show.bilibili.com/api/ticket/order/create%56%32"
            )
        )
        self.assertFalse(
            client._should_fanout_create_v2(
                "https://show.bilibili.com/api/ticket/buyer/list"
            )
        )

        client.close()

    def test_create_v2_fanout_returns_412_when_all_are_412(self) -> None:
        FakeH2Connection.status_by_source = {
            "192.0.2.1": 412,
            "192.0.2.2": 412,
        }
        client = CreateV2FanoutJA3H2Client(
            source_ip_provider=lambda: [
                SourceAddress("192.0.2.1", "ipv4", "WLAN"),
                SourceAddress("192.0.2.2", "ipv4", "Ethernet"),
            ],
            connection_factory=FakeH2Connection,
            connections_per_source_ip=1,
            slot_chooser=first_slot,
        )

        response = client.post(
            "https://show.bilibili.com/api/ticket/order/createV2",
            json={"project_id": 1},
        )

        self.assertEqual(response.status_code, 412)

        client.close()


if __name__ == "__main__":
    unittest.main()
