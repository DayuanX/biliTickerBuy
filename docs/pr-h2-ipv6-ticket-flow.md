# macOS IPv6 地址配置与使用

这个脚本会在当前 Wi-Fi 网卡上添加 IPv6 地址，供程序建立多条本地连接。地址的网络前缀从网卡现有配置中读取，新增地址由脚本生成。

需要 macOS、Python 3，以及已分配公网 IPv6 地址的 Wi-Fi 网络。添加和删除地址时，系统会通过 `sudo` 要求输入密码。脚本只支持 macOS。

## 1. 进入项目并准备配置

在终端中进入你保存的 `biliTickerBuy` 项目目录。下文命令都从项目根目录运行。

按[安装指南](installation.md)准备 uv 后，启动界面：

```bash
uv run --python 3.12 python main.py ui
```

打开终端显示的地址，登录账号，选择项目、票档和购票人，生成购票配置。开抢时间填写实际场次的时间。

## 2. 添加 IPv6 地址

预览要添加的地址：

```bash
scripts/configure_wifi_ipv6_aliases.sh --count 4 --dry-run
```

添加地址：

```bash
scripts/configure_wifi_ipv6_aliases.sh --count 4
```

`4` 表示本次新增的地址数量，可以按需调整。再次执行会继续新增地址。脚本会自动查找 Wi-Fi 网卡，并输出网卡名称、新增地址、记录文件位置和启动程序需要的环境变量。

直接运行脚本即可，脚本内部会在修改网卡时调用 `sudo`。记录文件保存在当前用户目录中。

查看脚本记录的地址：

```bash
scripts/configure_wifi_ipv6_aliases.sh --list
```

默认记录文件为 `$HOME/.config/bilitickerbuy/wifi-ipv6-aliases.tsv`，每行记录网卡、地址、前缀长度、所属网段和添加时间。`--list` 显示的是这个文件中的记录；网卡当前是否仍有这些地址，可以用 `ifconfig` 查看。

## 3. 选择网卡并测试连接

获取本机的 Wi-Fi 网卡名，保存到变量：

```bash
WIFI_INTERFACE="$(networksetup -listallhardwareports | awk '/^Hardware Port: (Wi-Fi|AirPort)$/ { found=1; next } found && /^Device:/ { print $2; exit }')"
printf 'Wi-Fi 网卡：%s\n' "$WIFI_INTERFACE"
```

如果输出为空，运行 `networksetup -listallhardwareports` 查看网卡列表，再将实际设备名赋给 `WIFI_INTERFACE`。

需要指定网卡时，可以用：

```bash
scripts/configure_wifi_ipv6_aliases.sh --count 4 --interface "$WIFI_INTERFACE" --dry-run
```

测试刚添加的地址：

```bash
export BTB_H2_SOURCE_INTERFACE_ALIASES="$WIFI_INTERFACE"
uv run --python 3.12 python -c 'import os; from util.h2client.IPv6AliasTester import test_ipv6_alias_connectivity; print(test_ipv6_alias_connectivity(interface=os.environ["BTB_H2_SOURCE_INTERFACE_ALIASES"]))'
```

程序会逐个使用已记录的地址访问 IP 查询服务，并比较服务返回的地址和本地地址。结果显示“出口匹配”表示两者一致。

也可以在 Web UI 的“高级设置 → 代理”中点击“测试 IPv6 alias 出口”。网卡填写上面查到的设备名；留空会测试记录文件中的全部网卡。

## 4. 启动程序

在设置了网卡变量的同一个终端中启动界面：

```bash
export BTB_H2_SOURCE_INTERFACE_ALIASES="$WIFI_INTERFACE"
uv run --python 3.12 python main.py ui
```

已经打开的程序需要重新启动，才能读取新设置的环境变量。

打开“高级设置 → 代理 → 并发”，在“连接与任务分配方式”中选择“本机多 IP”。“每个地址的连接数”控制每个可用地址建立的连接数。普通连接仍使用默认的“均匀分配”；“代理池并发”使用代理列表。

使用命令行购票时，先设置配置文件的实际路径，再启动：

```bash
CONFIG_FILE="/path/to/your/config.json"
uv run --python 3.12 python main.py buy --config-file "$CONFIG_FILE" --create-request-proxy-strategy local_ipv6
```

这里的路径需要替换成你生成的配置文件路径。购票配置文件保存项目、票档和购票人信息。请求间隔、重试次数和开抢时间通过界面、CLI 参数或对应环境变量设置。默认请求间隔为 300 毫秒，重试上限为 100。

“本机多 IP”会发现所选网卡的可用 IPv4 和 IPv6 地址，包括脚本添加的地址，再检测哪些地址可以建立连接。未指定网卡时，macOS 会自动查找 Wi-Fi。指定网卡没有可用地址时，程序会提示网卡名称和原因。

本机多 IP 模式直接使用本地地址，代理列表和代理 API 配置不会用于这一模式。

## 5. 删除添加的地址

预览删除操作：

```bash
scripts/configure_wifi_ipv6_aliases.sh --remove --dry-run
```

删除记录文件中的全部地址：

```bash
scripts/configure_wifi_ipv6_aliases.sh --remove
```

只删除某张网卡上的记录：

```bash
scripts/configure_wifi_ipv6_aliases.sh --remove --interface "$WIFI_INTERFACE"
```

这些地址属于运行时网卡配置。重启电脑或重置网卡后，地址可能已经消失；再运行 `--remove` 会清理相应记录。更换 Wi-Fi 网络前先删除旧地址，连接新网络后重新添加。

## 可调整的设置

| 设置 | 用途 | 默认值 |
| --- | --- | --- |
| `--count N` | 本次新增的地址数 | 添加时必填 |
| `--interface IF` | 指定网卡 | 添加时自动查找 Wi-Fi；删除时处理全部记录 |
| `--dry-run` | 显示准备执行的命令 | 不启用 |
| `--state-file PATH` | 指定本次脚本使用的记录文件 | 使用环境变量或用户目录中的默认位置 |
| `BTB_IPV6_ALIAS_STATE_FILE` | 脚本和出口测试共用的记录文件 | `$HOME/.config/bilitickerbuy/wifi-ipv6-aliases.tsv` |
| `BTB_IPV6_ECHO_URL` | 出口测试使用的 IP 查询服务 | `https://api64.ipify.org` |
| `BTB_H2_SOURCE_INTERFACE_ALIASES` | 程序使用的网卡；多个名称用逗号分隔 | macOS 自动查找 Wi-Fi |
| `BTB_CREATE_REQUEST_PROXY_STRATEGY` | CLI 连接方式：`standard`、`local_fanout` 或 `local_ipv6` | `standard` |
| `BTB_H2_CONNECTIONS_PER_SOURCE_IP` | 每个地址建立的连接数 | `3` |

如果希望把记录文件放到其他位置，在添加地址和启动程序之前设置：

```bash
export BTB_IPV6_ALIAS_STATE_FILE="$HOME/.config/bilitickerbuy/my-ipv6-addresses.tsv"
scripts/configure_wifi_ipv6_aliases.sh --count 4
```

后续查看、测试和删除时使用同一个设置。`--state-file` 优先于环境变量，只影响该次脚本命令；要让 Web UI 读取同一文件，应在启动前设置 `BTB_IPV6_ALIAS_STATE_FILE`。

自定义 `BTB_IPV6_ECHO_URL` 时，查询服务应直接返回纯文本 IP 地址。

## 遇到问题时

- **提示没有公网 IPv6 地址**：当前网卡没有获得公网 IPv6。连接支持 IPv6 的网络后重新添加。
- **已记录地址，但连接测试失败**：用 `ifconfig "$WIFI_INTERFACE" inet6` 查看地址是否还在。网络变化后，删除旧记录并重新添加。
- **连接测试超时**：检查当前网络和 VPN 的 IPv6 连接情况，以及查询服务是否可达。出口测试直接使用指定的本地地址，不读取 HTTP 代理环境变量。
- **测试结果没有地址**：检查记录文件位置、网卡筛选值，以及添加地址时使用的 `--state-file` 或环境变量。
