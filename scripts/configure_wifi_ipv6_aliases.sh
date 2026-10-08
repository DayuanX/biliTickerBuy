#!/usr/bin/env bash
set -euo pipefail

default_state_file="${BTB_IPV6_ALIAS_STATE_FILE:-${HOME}/.config/bilitickerbuy/wifi-ipv6-aliases.tsv}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/configure_wifi_ipv6_aliases.sh --count N [--interface IF] [--dry-run]
  scripts/configure_wifi_ipv6_aliases.sh add --count N [--interface IF] [--dry-run]
  scripts/configure_wifi_ipv6_aliases.sh --remove [--interface IF] [--dry-run]
  scripts/configure_wifi_ipv6_aliases.sh --list

Add or remove IPv6 aliases on the macOS Wi-Fi interface for biliTickerBuy's
connections using local IPv6 addresses.

Options:
  -c, --count N        Number of new aliases to add.
  -i, --interface IF   Wi-Fi interface, for example en0. Auto-detected by default.
  -n, --dry-run        Print planned commands without changing network config.
      --remove         Remove aliases recorded by this script.
      --list           List aliases recorded by this script.
      --state-file P   State file used for reversible cleanup.
  -h, --help           Show this help.

Environment:
  BTB_IPV6_ALIAS_STATE_FILE  Default state file; --state-file overrides it.

After adding aliases, run biliTickerBuy with:
  export BTB_H2_SOURCE_INTERFACE_ALIASES=<interface>

Aliases added by this script are runtime interface aliases. They are not written
to macOS NetworkService settings, but can remain active until removed, the
interface is reset, or the machine restarts.
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

detect_wifi_interface() {
  networksetup -listallhardwareports 2>/dev/null |
    awk '
      /^Hardware Port: (Wi-Fi|AirPort)$/ { found=1; next }
      found && /^Device:/ { print $2; exit }
    '
}

ensure_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || die "this script is intended for macOS"
}

ensure_tools() {
  command -v ifconfig >/dev/null || die "ifconfig not found"
  command -v python3 >/dev/null || die "python3 not found"
}

resolve_interface() {
  local requested="$1"
  if [[ -n "$requested" ]]; then
    printf '%s\n' "$requested"
    return
  fi
  local detected
  detected="$(detect_wifi_interface)"
  [[ -n "$detected" ]] || die "could not auto-detect Wi-Fi interface; pass --interface with the device name from networksetup -listallhardwareports"
  printf '%s\n' "$detected"
}

ensure_state_parent() {
  mkdir -p "$(dirname "$state_file")"
}

record_alias() {
  local iface="$1"
  local address="$2"
  local prefixlen="$3"
  local network="$4"

  ensure_state_parent
  touch "$state_file"
  if awk -F '\t' -v iface="$iface" -v address="$address" '$1 == iface && $2 == address { found=1 } END { exit !found }' "$state_file"; then
    return
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$iface" "$address" "$prefixlen" "$network" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    >> "$state_file"
}

remove_record() {
  local iface="$1"
  local address="$2"
  local tmp_file

  [[ -f "$state_file" ]] || return
  tmp_file="$(mktemp "${TMPDIR:-/tmp}/btb-ipv6-state.XXXXXX")"
  awk -F '\t' -v iface="$iface" -v address="$address" \
    '!(($1 == iface) && ($2 == address))' "$state_file" > "$tmp_file"
  mv "$tmp_file" "$state_file"
}

list_aliases() {
  if [[ ! -s "$state_file" ]]; then
    echo "No aliases recorded in $state_file"
    return
  fi
  printf 'State file: %s\n' "$state_file"
  awk -F '\t' '
    BEGIN { printf "%-10s %-40s %-9s %-40s %s\n", "IFACE", "ADDRESS", "PREFIX", "NETWORK", "ADDED_AT" }
    { printf "%-10s %-40s %-9s %-40s %s\n", $1, $2, $3, $4, $5 }
  ' "$state_file"
}

generate_aliases() {
  local iface="$1"
  local count="$2"
  local output_file="$3"

  python3 - "$iface" "$count" > "$output_file" <<'PY'
import hashlib
import ipaddress
import re
import subprocess
import sys

iface = sys.argv[1]
count = int(sys.argv[2])

completed = subprocess.run(
    ["ifconfig", iface],
    check=False,
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
)
if completed.returncode != 0:
    print(f"ifconfig failed for {iface}", file=sys.stderr)
    sys.exit(2)

rows = []
for line in completed.stdout.splitlines():
    match = re.search(r"\binet6\s+([0-9a-fA-F:]+(?:%[^\s]+)?)\s+prefixlen\s+(\d+)", line)
    if not match:
        continue
    raw_ip, raw_prefixlen = match.groups()
    ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    prefixlen = int(raw_prefixlen)
    if ip.version != 6 or not ip.is_global:
        continue
    rows.append((ip, prefixlen, line))

if not rows:
    print(
        f"{iface} has no global IPv6 address; connect to an IPv6-enabled Wi-Fi network first",
        file=sys.stderr,
    )
    sys.exit(3)

stable_rows = [row for row in rows if "temporary" not in row[2] and "deprecated" not in row[2]]
base_ip, prefixlen, _ = stable_rows[0] if stable_rows else rows[0]
if prefixlen >= 128:
    print(f"{iface} has unusable IPv6 prefixlen {prefixlen}", file=sys.stderr)
    sys.exit(4)

network = ipaddress.IPv6Network((base_ip, prefixlen), strict=False)
host_bits = 128 - prefixlen
host_mask = (1 << host_bits) - 1
network_int = int(network.network_address)
existing = {
    ipaddress.ip_address(match.group(1).split("%", 1)[0])
    for match in re.finditer(r"\binet6\s+([0-9a-fA-F:]+(?:%[^\s]+)?)\b", completed.stdout)
}

generated = []
index = 1
while len(generated) < count:
    digest = hashlib.sha256(f"btb:{iface}:{network}:{index}".encode("utf-8")).digest()
    host = int.from_bytes(digest, "big") & host_mask
    if host == 0:
        index += 1
        continue
    candidate = ipaddress.IPv6Address(network_int | host)
    if candidate not in existing and candidate not in generated:
        generated.append(candidate)
    index += 1

for address in generated:
    print(f"{address}\t{prefixlen}\t{network}")
PY
}

add_aliases() {
  local iface="$1"
  local count="$2"
  local aliases_file

  [[ -n "$count" ]] || die "missing --count N"
  [[ "$count" =~ ^[0-9]+$ ]] || die "count must be a positive integer"
  (( count > 0 )) || die "count must be greater than zero"

  aliases_file="$(mktemp "${TMPDIR:-/tmp}/btb-ipv6-aliases.XXXXXX")"
  trap 'rm -f "$aliases_file"' RETURN
  generate_aliases "$iface" "$count" "$aliases_file"

  echo "Wi-Fi interface: $iface"
  echo "State file: $state_file"
  echo "Aliases to add: $(wc -l < "$aliases_file" | tr -d ' ')"

  while IFS=$'\t' read -r address prefixlen network; do
    [[ -n "$address" ]] || continue
    if ifconfig "$iface" inet6 | awk '{print $2}' | grep -Fxq "$address"; then
      echo "exists: $address/$prefixlen"
      record_alias "$iface" "$address" "$prefixlen" "$network"
      continue
    fi
    cmd=(ifconfig "$iface" inet6 "$address" prefixlen "$prefixlen" alias)
    if (( dry_run )); then
      printf 'dry-run: sudo'
      printf ' %q' "${cmd[@]}"
      printf '\n'
    else
      echo "add: $address/$prefixlen"
      sudo "${cmd[@]}"
      record_alias "$iface" "$address" "$prefixlen" "$network"
    fi
  done < "$aliases_file"

  cat <<EOF

Use this when starting biliTickerBuy so the H2 client only discovers this Wi-Fi interface:
  export BTB_H2_SOURCE_INTERFACE_ALIASES=$iface

To remove aliases recorded by this script:
  scripts/configure_wifi_ipv6_aliases.sh --remove --interface "$iface" --state-file "$state_file"
EOF
}

remove_aliases() {
  local requested_iface="$1"

  if [[ ! -s "$state_file" ]]; then
    echo "No aliases recorded in $state_file"
    return
  fi

  echo "State file: $state_file"
  while IFS=$'\t' read -r record_iface address prefixlen network added_at; do
    [[ -n "$record_iface" && -n "$address" ]] || continue
    if [[ -n "$requested_iface" && "$record_iface" != "$requested_iface" ]]; then
      continue
    fi

    cmd=(ifconfig "$record_iface" inet6 "$address" -alias)
    if (( dry_run )); then
      printf 'dry-run: sudo'
      printf ' %q' "${cmd[@]}"
      printf '\n'
      continue
    fi

    if ifconfig "$record_iface" inet6 2>/dev/null | awk '{print $2}' | grep -Fxq "$address"; then
      echo "remove: $record_iface $address/$prefixlen"
      sudo "${cmd[@]}"
    else
      echo "already absent: $record_iface $address/$prefixlen"
    fi
    remove_record "$record_iface" "$address"
  done < "$state_file"
}

action="add"
count=""
iface=""
dry_run=0
state_file="$default_state_file"

while [[ $# -gt 0 ]]; do
  case "$1" in
    add)
      action="add"
      shift
      ;;
    remove|--remove)
      action="remove"
      shift
      ;;
    list|--list)
      action="list"
      shift
      ;;
    -c|--count)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      count="$2"
      shift 2
      ;;
    -i|--interface)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      iface="$2"
      shift 2
      ;;
    -n|--dry-run)
      dry_run=1
      shift
      ;;
    --state-file)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      state_file="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    ''|*[!0-9]*)
      die "unknown argument: $1"
      ;;
    *)
      [[ -z "$count" ]] || die "count specified more than once"
      count="$1"
      shift
      ;;
  esac
done

ensure_macos

case "$action" in
  add)
    ensure_tools
    iface="$(resolve_interface "$iface")"
    add_aliases "$iface" "$count"
    ;;
  remove)
    command -v ifconfig >/dev/null || die "ifconfig not found"
    remove_aliases "$iface"
    ;;
  list)
    list_aliases
    ;;
  *)
    die "unknown action: $action"
    ;;
esac
