from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx


DEFAULT_IPV6_ALIAS_STATE_FILE = (
    Path.home() / ".config" / "bilitickerbuy" / "wifi-ipv6-aliases.tsv"
)
DEFAULT_IPV6_ECHO_URL = "https://api64.ipify.org"


@dataclass(frozen=True)
class IPv6AliasRecord:
    interface: str
    address: str
    prefixlen: str
    network: str
    added_at: str = ""


@dataclass(frozen=True)
class IPv6AliasTestResult:
    record: IPv6AliasRecord
    ok: bool
    observed_ip: str = ""
    error: str = ""


def resolve_alias_state_file(state_file: str | os.PathLike[str] | None = None) -> Path:
    return Path(
        state_file
        or os.environ.get("BTB_IPV6_ALIAS_STATE_FILE")
        or DEFAULT_IPV6_ALIAS_STATE_FILE
    ).expanduser()


class IPv6AliasTester:
    def __init__(
        self,
        *,
        state_file: str | os.PathLike[str] | None = None,
        timeout: float = 10.0,
        echo_url: str | None = None,
    ) -> None:
        self.state_file = resolve_alias_state_file(state_file)
        self.timeout = float(timeout)
        self.echo_url = (
            echo_url or os.environ.get("BTB_IPV6_ECHO_URL") or DEFAULT_IPV6_ECHO_URL
        )

    def read_aliases(self, interface: str | None = None) -> list[IPv6AliasRecord]:
        if not self.state_file.is_file():
            return []

        records: list[IPv6AliasRecord] = []
        for raw_line in self.state_file.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            parts = raw_line.split("\t")
            if len(parts) < 4:
                continue
            record = IPv6AliasRecord(
                interface=parts[0],
                address=parts[1],
                prefixlen=parts[2],
                network=parts[3],
                added_at=parts[4] if len(parts) > 4 else "",
            )
            if interface and record.interface != interface:
                continue
            records.append(record)
        return records

    def test_alias(self, record: IPv6AliasRecord) -> IPv6AliasTestResult:
        try:
            observed_ip = self._fetch_observed_ip(record.address)
        except Exception as exc:
            return IPv6AliasTestResult(
                record=record,
                ok=False,
                error=f"{exc.__class__.__name__}: {exc}",
            )

        return IPv6AliasTestResult(
            record=record,
            ok=observed_ip == record.address,
            observed_ip=observed_ip,
            error="" if observed_ip == record.address else "出口 IP 与 alias 不一致",
        )

    def test_aliases(self, interface: str | None = None) -> list[IPv6AliasTestResult]:
        return [self.test_alias(record) for record in self.read_aliases(interface)]

    def _fetch_observed_ip(self, source_ip: str) -> str:
        transport = httpx.HTTPTransport(local_address=source_ip, retries=0)
        with httpx.Client(
            transport=transport,
            timeout=self.timeout,
            trust_env=False,
        ) as client:
            response = client.get(self.echo_url)
            response.raise_for_status()
            return response.text.strip()


def format_ipv6_alias_test_results(
    results: list[IPv6AliasTestResult],
    *,
    state_file: str | os.PathLike[str] | None = None,
) -> str:
    state_path = resolve_alias_state_file(state_file)
    output = [
        "Wi-Fi IPv6 alias 出口测试结果:",
        f"状态文件: {state_path}",
        "=" * 50,
    ]

    if not results:
        output.append("没有找到已记录的 IPv6 alias。")
        output.append("请先运行 scripts/configure_wifi_ipv6_aliases.sh --count N")
        return "\n".join(output)

    ok_count = 0
    for index, result in enumerate(results, 1):
        record = result.record
        if result.ok:
            ok_count += 1
            output.append(
                f"✅ [{index}] {record.interface} {record.address}/{record.prefixlen}"
            )
            output.append(f"    外部看到: {result.observed_ip}")
        else:
            output.append(
                f"❌ [{index}] {record.interface} {record.address}/{record.prefixlen}"
            )
            if result.observed_ip:
                output.append(f"    外部看到: {result.observed_ip}")
            if result.error:
                output.append(f"    错误: {result.error}")
        output.append("")

    output.append("=" * 50)
    output.append(f"测试统计: {ok_count}/{len(results)} 个 alias 出口匹配")
    return "\n".join(output)


def test_ipv6_alias_connectivity(
    interface: str = "",
    timeout: int = 10,
    state_file: str | os.PathLike[str] | None = None,
) -> str:
    tester = IPv6AliasTester(state_file=state_file, timeout=timeout)
    results = tester.test_aliases(interface=interface or None)
    return format_ipv6_alias_test_results(results, state_file=tester.state_file)
