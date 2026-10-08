from __future__ import annotations

from util.h2client.IPv6AliasTester import (
    IPv6AliasRecord,
    IPv6AliasTestResult,
    IPv6AliasTester,
    format_ipv6_alias_test_results,
)


def test_read_aliases_filters_interface(tmp_path):
    state_file = tmp_path / "aliases.tsv"
    state_file.write_text(
        "\n".join(
            [
                "en0\t2001:db8::1\t64\t2001:db8::/64\t2026-06-30T00:00:00Z",
                "en1\t2001:db8:1::1\t64\t2001:db8:1::/64\t2026-06-30T00:00:01Z",
            ]
        ),
        encoding="utf-8",
    )

    tester = IPv6AliasTester(state_file=state_file)

    assert tester.read_aliases(interface="en0") == [
        IPv6AliasRecord(
            interface="en0",
            address="2001:db8::1",
            prefixlen="64",
            network="2001:db8::/64",
            added_at="2026-06-30T00:00:00Z",
        )
    ]


def test_test_alias_reports_matching_observed_ip(monkeypatch):
    tester = IPv6AliasTester()
    record = IPv6AliasRecord("en0", "2001:db8::1", "64", "2001:db8::/64")
    monkeypatch.setattr(tester, "_fetch_observed_ip", lambda source_ip: source_ip)

    result = tester.test_alias(record)

    assert result.ok is True
    assert result.observed_ip == "2001:db8::1"
    assert result.error == ""


def test_test_alias_reports_mismatched_observed_ip(monkeypatch):
    tester = IPv6AliasTester()
    record = IPv6AliasRecord("en0", "2001:db8::1", "64", "2001:db8::/64")
    monkeypatch.setattr(tester, "_fetch_observed_ip", lambda _: "2001:db8::2")

    result = tester.test_alias(record)

    assert result.ok is False
    assert result.observed_ip == "2001:db8::2"
    assert "不一致" in result.error


def test_format_empty_results_mentions_setup(tmp_path):
    output = format_ipv6_alias_test_results([], state_file=tmp_path / "aliases.tsv")

    assert "没有找到已记录的 IPv6 alias" in output
    assert "configure_wifi_ipv6_aliases.sh" in output


def test_format_results_counts_successes():
    records = [
        IPv6AliasRecord("en0", "2001:db8::1", "64", "2001:db8::/64"),
        IPv6AliasRecord("en0", "2001:db8::2", "64", "2001:db8::/64"),
    ]
    output = format_ipv6_alias_test_results(
        [
            IPv6AliasTestResult(records[0], ok=True, observed_ip="2001:db8::1"),
            IPv6AliasTestResult(
                records[1],
                ok=False,
                observed_ip="2001:db8::3",
                error="出口 IP 与 alias 不一致",
            ),
        ]
    )

    assert "2001:db8::1" in output
    assert "2001:db8::3" in output
    assert "测试统计: 1/2 个 alias 出口匹配" in output


def test_state_file_and_echo_url_can_be_configured(monkeypatch, tmp_path):
    state_file = tmp_path / "custom.tsv"
    monkeypatch.setenv("BTB_IPV6_ALIAS_STATE_FILE", str(state_file))
    monkeypatch.setenv("BTB_IPV6_ECHO_URL", "https://echo.example/ip")
    tester = IPv6AliasTester()
    assert tester.state_file == state_file
    assert tester.echo_url == "https://echo.example/ip"
    assert str(state_file) in format_ipv6_alias_test_results([])

    explicit = IPv6AliasTester(
        state_file=tmp_path / "explicit.tsv", echo_url="https://explicit.example/ip"
    )
    assert explicit.state_file == tmp_path / "explicit.tsv"
    assert explicit.echo_url == "https://explicit.example/ip"


def test_connectivity_defaults_to_all_recorded_interfaces(monkeypatch, tmp_path):
    from util.h2client.IPv6AliasTester import test_ipv6_alias_connectivity

    state_file = tmp_path / "aliases.tsv"
    state_file.write_text("en7\t2001:db8::7\t64\t2001:db8::/64\n", encoding="utf-8")
    monkeypatch.setattr(IPv6AliasTester, "_fetch_observed_ip", lambda self, ip: ip)
    result = test_ipv6_alias_connectivity(state_file=state_file)
    assert "en7" in result
    assert "1/1" in result
