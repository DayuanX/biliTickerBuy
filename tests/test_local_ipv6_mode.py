from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import gradio as gr
import pytest

from app_cmd.config.BuyConfig import BuyConfig
from interface.config import build_runtime_options
from task.buy import Buy, _build_buy_request
from util.h2client import local_sources
from util.h2client.ja_h2_client import (
    CreateV2FanoutJA3H2Client,
    ProxyPoolCreateV2FanoutJA3H2Client,
    SourceAddress,
)
from test_buy_env_vars import _assemble
from test_local_fanout_proxy_strategy import FakeH2Connection
from test_notifier_ui_advanced_params import _FakeConfigDB


@pytest.mark.parametrize(
    "strategy, client_type",
    [
        ("standard", None),
        ("local_fanout", ProxyPoolCreateV2FanoutJA3H2Client),
        ("local_ipv6", CreateV2FanoutJA3H2Client),
    ],
)
def test_buy_selects_transport_and_keeps_local_mode_direct(strategy, client_type):
    config = BuyConfig(
        create_request_proxy_strategy=strategy,
        https_proxys="http://proxy.example:8080",
        h2_connections_per_source_ip=2,
    )
    request = _build_buy_request(config, [])
    try:
        assert request._h2_client_type is client_type
        assert request.proxy_manager.proxy_list == (
            ["none"] if strategy == "local_ipv6" else ["http://proxy.example:8080"]
        )
        if strategy != "standard":
            assert request._h2_client_options["connections_per_source_ip"] == 2
    finally:
        request.session.close()


def test_local_mode_survives_cli_env_runtime_and_ticket_file(monkeypatch, tmp_path):
    monkeypatch.setenv("BTB_CREATE_REQUEST_PROXY_STRATEGY", "local_ipv6")
    assert _assemble(["buy"]).create_request_proxy_strategy == "local_ipv6"
    assert (
        _assemble(
            ["buy", "--create-request-proxy-strategy", "standard"]
        ).create_request_proxy_strategy
        == "standard"
    )
    runtime = build_runtime_options(create_request_proxy_strategy="local_ipv6")
    config = BuyConfig.from_runtime_options("{}", runtime)
    assert (
        _assemble(["buy", *config.to_cli_args()]).create_request_proxy_strategy
        == "local_ipv6"
    )
    ticket_file = tmp_path / "ticket.json"
    ticket_file.write_text(json.dumps({"detail": "fixture"}))
    resolved = Buy(
        config.with_overrides(config_file=str(ticket_file))
    ).resolved_config()
    assert resolved.create_request_proxy_strategy == "local_ipv6"
    assert json.loads(resolved.tickets_info) == {"detail": "fixture"}
    assert config.interval == 300
    assert config.create_retry_limit == 100


def test_macos_detects_wifi_and_only_reads_its_addresses(monkeypatch):
    monkeypatch.delenv("BTB_H2_SOURCE_INTERFACE_ALIASES", raising=False)
    monkeypatch.setattr(local_sources.platform, "system", lambda: "Darwin")

    def run(command, **kwargs):
        if command[0] == "networksetup":
            output = "Hardware Port: Wi-Fi\nDevice: en7\nEthernet Address: 00:00:00:00:00:00\n"
        else:
            assert command == ["ifconfig"]
            output = (
                "utun4: flags=8051<UP> mtu 1380\n    inet6 2001:db8:1::1 prefixlen 64\n"
                "en7: flags=8863<UP> mtu 1500\n    inet6 2001:db8::7 prefixlen 64\n"
                "    inet6 fe80::7%en7 prefixlen 64\n"
            )
        return subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(local_sources.subprocess, "run", run)
    assert local_sources.discover_interface_source_ips() == [
        SourceAddress("2001:db8::7", "ipv6", "en7")
    ]
    monkeypatch.setenv("BTB_H2_SOURCE_INTERFACE_ALIASES", "en9")
    with pytest.raises(RuntimeError, match="en9"):
        local_sources.discover_interface_source_ips()


def test_local_pool_binds_discovered_addresses(monkeypatch):
    FakeH2Connection.instances = []
    monkeypatch.setattr(
        local_sources,
        "discover_interface_source_ips",
        lambda _: [SourceAddress("2001:db8::7", "ipv6", "en7")],
    )
    client = CreateV2FanoutJA3H2Client(
        connection_factory=FakeH2Connection,
        connections_per_source_ip=2,
    )
    try:
        assert (
            len([item for item in FakeH2Connection.instances if not item.closed]) == 2
        )
        assert all(
            item.source_ip == "2001:db8::7" for item in FakeH2Connection.instances
        )
        assert all(item.proxy_url is None for item in FakeH2Connection.instances)
    finally:
        client.close()


def test_ui_local_mode_launches_without_proxy_pool(monkeypatch, tmp_path):
    import tab.go as go_tab
    import tab.config as config_tab

    fake = _FakeConfigDB()
    fake.store.update(
        {
            "proxyAssignmentStrategy": "local_ipv6",
            "proxyIncludeDirect": False,
            "autoCleanupLogs": False,
        }
    )
    monkeypatch.setattr(go_tab, "ConfigDB", fake)
    monkeypatch.setattr(config_tab, "ConfigDB", fake)
    monkeypatch.setattr(
        BuyConfig,
        "from_config_db",
        classmethod(
            lambda cls, **kwargs: cls(h2_connections_per_source_ip=2, **kwargs)
        ),
    )
    launched = []
    monkeypatch.setattr(
        go_tab,
        "buy_new_terminal",
        lambda **kwargs: (
            launched.append(kwargs["config"]) or SimpleNamespace(pid=123)
        ),
    )
    monkeypatch.setattr(
        go_tab.GlobalStatusInstance, "register_task_log", lambda **_: None
    )
    monkeypatch.setattr(go_tab.gr, "Info", lambda *_: None)
    with gr.Blocks() as blocks:
        config_tab.go_settings_tab(gr.Markdown("header"))
        go_tab.go_start_tab()
    choices = [
        getattr(component, "choices", []) for component in blocks.blocks.values()
    ]
    assert any(("本机多 IP", "local_ipv6") in group for group in choices)
    handlers = {fn.fn.__name__: fn.fn for fn in blocks.fns.values() if fn.fn}
    import importlib

    tester_module = importlib.import_module("util.h2client.IPv6AliasTester")
    tested_interfaces = []
    monkeypatch.setattr(
        tester_module,
        "test_ipv6_alias_connectivity",
        lambda interface, timeout: (tested_interfaces.append(interface) or "ok"),
    )
    handlers["test_ipv6_alias_connectivity"]("", 10)
    assert tested_interfaces == [""]
    ticket = tmp_path / "ticket.json"
    ticket.write_text("{}")
    handlers["start_go"]([str(ticket)], "", 300)
    assert len(launched) == 1
    assert launched[0].create_request_proxy_strategy == "local_ipv6"
    assert launched[0].https_proxys == "none"
    assert launched[0].h2_connections_per_source_ip == 2
