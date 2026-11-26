#!/usr/bin/env python3
"""用临时 Grafana 容器验证预置数据源与看板（T043）。

网络模式选择
------------
使用 ``-p 127.0.0.1:<端口>:3000`` 端口映射，不使用 host 网络。理由：
- 回环绑定由 Docker 的发布参数保证，容器内 Grafana 可以监听 0.0.0.0；
- host 网络会让容器共享宿主网络栈，若 Grafana 的 GF_SERVER_HTTP_ADDR
  配置不严，容易把 3000 暴露到非回环地址；
- 端口映射可在启动前检测宿主端口是否被占用，避免与已有观测栈冲突。

脚本用 ``docker port`` 和 ``ss``（或 ``/proc/net/tcp``）双重检查宿主监听
地址，要求 3000 只绑定回环地址。挂载本仓库的 provisioning 与 dashboards
为只读，并按 compose.observability.yaml 的口径设置匿名 Viewer、关闭
basic auth、登录表单和注册。

验证内容
--------
- ``/api/health`` 就绪；
- 预置数据源 ``Prometheus`` 存在且 ``isDefault`` 为 true；
- 预置看板 ``payment-hub-overview`` 可读且标题正确；
- 宿主端口只绑定回环地址。

本脚本只验证预置加载、匿名只读访问和端口绑定，不执行 PromQL。使用端口映射
时，数据源中的 ``http://127.0.0.1:9090`` 指向临时容器自身，不指向宿主
Prometheus；数据查证需在正式观测栈中完成。

退出码
------
0  全部通过；
2  环境或参数错误（docker 不可用、镜像不存在、端口/容器名冲突、
   Grafana 未就绪、HTTP 传输失败等）；
3  契约不满足（数据源、看板或回环绑定不符合预期）。

脚本不会拉取镜像，也不会删除已存在的同名容器；结束时只删除本次创建、
带唯一运行标签的临时容器。
"""

import argparse
import ipaddress
import json
import math
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROVISIONING = REPO / "deploy" / "observability" / "grafana" / "provisioning"
DASHBOARDS = REPO / "deploy" / "observability" / "grafana" / "dashboards"

DEFAULT_IMAGE = "grafana/grafana:13.2.7"
FALLBACK_IMAGE = "grafana/grafana:12.4.16"
DEFAULT_PLATFORM = "linux/amd64"
DEFAULT_NAME_PREFIX = "payment-hub-grafana-check-"
DASHBOARD_UID = "payment-hub-overview"
DASHBOARD_TITLE = "Payment Hub 压测总览"
DATASOURCE_NAME = "Prometheus"
CONTAINER_LABEL = "payment-hub.grafana-check"
RUN_LABEL = "payment-hub.grafana-check.run"
POLL_INTERVAL_SECONDS = 0.5

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SetupError(RuntimeError):
    """环境或参数不满足，对应退出码 2。"""


class ContractError(RuntimeError):
    """数据源、看板或回环绑定不符合预期，对应退出码 3。"""


class TransportError(RuntimeError):
    """HTTP 传输失败，由调用方决定重试或归入环境错误。"""


def _first_line(text, limit=400):
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def run_docker(args, timeout=60):
    """运行 docker 子命令；不经过 shell，避免路径与参数注入。"""
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SetupError("找不到 docker 命令，请先安装 Docker CLI。") from exc
    except subprocess.TimeoutExpired as exc:
        raise SetupError(
            f"docker {' '.join(args)} 在 {timeout:g}s 内未返回。"
        ) from exc


def check_docker():
    result = run_docker(["version", "--format", "{{.Server.Version}}"], timeout=20)
    if result.returncode != 0:
        raise SetupError(
            "无法连接 Docker daemon："
            + (_first_line(result.stderr) or "docker version 返回非零退出码")
        )
    return result.stdout.strip()


def check_image(image):
    result = run_docker(
        ["image", "inspect", image, "--format", "{{.Id}}"], timeout=30
    )
    if result.returncode != 0:
        raise SetupError(
            f"本地缺少镜像 {image}。脚本不会自动拉取；请先执行 "
            f"docker pull {image}，或用 --image {FALLBACK_IMAGE} 回退。"
        )


def check_container_name_free(name):
    result = run_docker(
        ["container", "inspect", name, "--format", "{{.Id}}"], timeout=20
    )
    if result.returncode == 0:
        raise SetupError(
            f"已存在同名容器 {name}；为避免影响已有环境，脚本拒绝启动。"
            "请用 --name 指定其他名称，或先手动处理该容器。"
        )
    stderr = (result.stderr or "").lower()
    if stderr and "no such" not in stderr and "not found" not in stderr:
        raise SetupError(
            f"检查容器名 {name} 时 Docker 返回错误：{_first_line(result.stderr)}"
        )


def _split_host_port(address):
    address = address.strip()
    if address.startswith("["):
        host, _, rest = address.partition("]")
        return host[1:], rest.lstrip(":")
    host, _, port = address.rpartition(":")
    return host, port


def _listeners_from_ss(port):
    """从 ss -ltnH 读取监听地址；ss 不可用时返回 None。"""
    if not shutil.which("ss"):
        return None
    try:
        result = subprocess.run(
            ["ss", "-ltnH"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    hosts = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        host, listener_port = _split_host_port(parts[3])
        if listener_port == str(port):
            hosts.add(host)
    return hosts


def _decode_proc_address(hex_address):
    raw = bytes.fromhex(hex_address)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    if len(raw) == 16:
        # /proc/net/tcp6 以 4 个 32 位小端字存储 IPv6 地址。
        normalized = b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, normalized)
    return hex_address


def _listeners_from_proc(port):
    hosts = set()
    for path in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            hex_address, _, hex_port = parts[1].partition(":")
            try:
                listener_port = int(hex_port, 16)
            except ValueError:
                continue
            if listener_port != port:
                continue
            try:
                hosts.add(_decode_proc_address(hex_address))
            except (ValueError, OSError):
                hosts.add(hex_address)
    return hosts


def listening_hosts(port):
    ss_hosts = _listeners_from_ss(port)
    proc_hosts = _listeners_from_proc(port)
    if ss_hosts is None:
        return proc_hosts
    return ss_hosts | proc_hosts


def _is_loopback(host):
    if not host or host == "*":
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_port_free(port):
    hosts = listening_hosts(port)
    if hosts:
        raise SetupError(
            f"宿主端口 {port} 已被占用（监听地址：{', '.join(sorted(hosts))}）。"
            "请停止占用进程，或用 --port 指定其他端口；脚本不会停止已有服务。"
        )


def check_loopback_binding(container, port):
    """校验发布端口和宿主监听都只落在回环地址。"""
    result = run_docker(["port", container, "3000/tcp"], timeout=20)
    if result.returncode != 0:
        raise SetupError(
            f"无法读取容器端口映射：{_first_line(result.stderr)}"
        )
    mappings = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not mappings:
        raise ContractError("docker port 未返回 3000/tcp 的宿主绑定。")

    non_loopback = []
    for mapping in mappings:
        host, _, mapped_port = mapping.rpartition(":")
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if mapped_port != str(port):
            raise ContractError(
                f"docker port 返回的宿主端口不是 {port}：{mapping}"
            )
        if not _is_loopback(host):
            non_loopback.append(mapping)
    if non_loopback:
        raise ContractError(
            "3000 端口未只绑定回环地址：" + "、".join(non_loopback)
        )

    hosts = listening_hosts(port)
    wildcard_hosts = sorted(host for host in hosts if not _is_loopback(host))
    if wildcard_hosts:
        raise ContractError(
            "ss 或 /proc/net/tcp 发现 3000 端口存在非回环监听："
            + "、".join(wildcard_hosts)
        )
    if hosts:
        return "、".join(sorted(hosts))
    return (
        "未发现宿主监听（可能由 iptables DNAT 接管）；"
        "docker port 已确认 HostIp=127.0.0.1"
    )


def http_get_json(url, timeout):
    """GET 一个 JSON 端点；4xx/5xx 返回状态码，传输失败抛 TransportError。"""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"请求 {url} 失败：{exc}") from exc

    if not body:
        payload = None
    else:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            if status == 200:
                raise TransportError(f"{url} 返回的不是合法 JSON：{exc}") from exc
            payload = None
    return status, payload


def _sleep_before_retry(deadline):
    time.sleep(max(0.0, min(POLL_INTERVAL_SECONDS, deadline - time.monotonic())))


def wait_for_health(base_url, timeout):
    deadline = time.monotonic() + timeout
    last_detail = "尚未请求"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        request_timeout = max(0.2, min(5.0, remaining))
        try:
            status, payload = http_get_json(
                base_url + "/api/health", request_timeout
            )
        except TransportError as exc:
            last_detail = str(exc)
        else:
            database = payload.get("database") if isinstance(payload, dict) else None
            if status == 200 and database == "ok":
                return payload
            last_detail = f"HTTP {status}，database={database!r}"
        _sleep_before_retry(deadline)
    raise SetupError(
        f"Grafana 在 {timeout:g}s 内未通过 /api/health 就绪（{last_detail}）。"
        f"可回退镜像 {FALLBACK_IMAGE} 或查看容器日志。"
    )


def _datasource_entries(payload):
    if isinstance(payload, list):
        return [value for value in payload if isinstance(value, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("datasources", payload)
    if isinstance(data, dict):
        return [value for value in data.values() if isinstance(value, dict)]
    if isinstance(data, list):
        return [value for value in data if isinstance(value, dict)]
    return []


def _find_datasource(payload):
    if (
        isinstance(payload, dict)
        and payload.get("name") == DATASOURCE_NAME
        and "isDefault" in payload
    ):
        return payload
    for entry in _datasource_entries(payload):
        if entry.get("name") == DATASOURCE_NAME:
            return entry
    return None


def _fetch_datasource_once(base_url, timeout):
    saw_auth_failure = False
    saw_success = False
    for path in (
        "/api/datasources",
        "/api/frontend/settings",
        f"/api/datasources/name/{DATASOURCE_NAME}",
    ):
        status, payload = http_get_json(base_url + path, timeout)
        if status == 200:
            saw_success = True
            entry = _find_datasource(payload)
            if entry is not None:
                return entry
        elif status in (401, 403):
            saw_auth_failure = True
    if not saw_success and saw_auth_failure:
        raise ContractError(
            "匿名 Viewer 无法读取数据源信息（HTTP 401/403）。"
            "请检查 GF_AUTH_ANONYMOUS_ENABLED=true、"
            "GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer。"
        )
    return None


def wait_for_datasource(base_url, timeout):
    deadline = time.monotonic() + timeout
    last_detail = "数据源尚未加载"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        request_timeout = max(0.2, min(5.0, remaining))
        try:
            entry = _fetch_datasource_once(base_url, request_timeout)
        except TransportError as exc:
            last_detail = str(exc)
            entry = None
        if entry is not None:
            if bool(entry.get("isDefault")):
                return entry
            raise ContractError(
                f"数据源 {DATASOURCE_NAME} 已加载，但 isDefault 不是 true"
                f"（当前值 {entry.get('isDefault')!r}）。"
                "检查 provisioning/datasources/prometheus.yml 的 isDefault: true。"
            )
        _sleep_before_retry(deadline)
    raise ContractError(
        f"数据源 {DATASOURCE_NAME} 在 {timeout:g}s 内未出现（{last_detail}）。"
        "检查 provisioning/datasources 挂载与 datasource 名称。"
    )


def _dashboard_snapshot(base_url, timeout):
    status, payload = http_get_json(
        base_url + f"/api/dashboards/uid/{DASHBOARD_UID}", timeout
    )
    if status == 200:
        dashboard = payload.get("dashboard") if isinstance(payload, dict) else None
        if not isinstance(dashboard, dict):
            raise ContractError("看板 API 返回 200，但缺少 dashboard 对象。")
        if dashboard.get("uid") != DASHBOARD_UID:
            raise ContractError(
                f"看板 UID 不是 {DASHBOARD_UID}：{dashboard.get('uid')!r}"
            )
        if dashboard.get("title") != DASHBOARD_TITLE:
            raise ContractError(
                f"看板标题不是“{DASHBOARD_TITLE}”：{dashboard.get('title')!r}"
            )
        panels = dashboard.get("panels")
        if not isinstance(panels, list) or not panels:
            raise ContractError(
                "看板已加载，但 panel 为空；若使用 V2 格式请相应调整验证脚本。"
            )
        return dashboard
    if status in (401, 403):
        raise ContractError(
            "匿名 Viewer 无法读取看板（HTTP 401/403）。"
            "检查匿名 Viewer 与看板 provider 的 allowUiUpdates/disableDeletion。"
        )
    if status == 404:
        return None
    raise TransportError(f"看板 API 返回 HTTP {status}")


def wait_for_dashboard(base_url, timeout):
    deadline = time.monotonic() + timeout
    last_detail = "看板尚未加载"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        request_timeout = max(0.2, min(5.0, remaining))
        try:
            dashboard = _dashboard_snapshot(base_url, request_timeout)
        except TransportError as exc:
            last_detail = str(exc)
            dashboard = None
        if dashboard is not None:
            return dashboard
        _sleep_before_retry(deadline)
    raise ContractError(
        f"看板 {DASHBOARD_UID} 在 {timeout:g}s 内不可读（{last_detail}）。"
        "检查 provisioning/dashboards/provider.yml、dashboards 挂载和 JSON 结构。"
    )


def start_container(args, token):
    if not PROVISIONING.is_dir():
        raise SetupError(f"缺少 provisioning 目录：{PROVISIONING}")
    if not DASHBOARDS.is_dir():
        raise SetupError(f"缺少 dashboards 目录：{DASHBOARDS}")

    command = [
        "run",
        "-d",
        "--name",
        args.name,
        "--label",
        f"{CONTAINER_LABEL}=1",
        "--label",
        f"{RUN_LABEL}={token}",
        "--pull=never",
        "--platform",
        args.platform,
        "-p",
        f"127.0.0.1:{args.port}:3000",
        "-e",
        "GF_SERVER_HTTP_ADDR=0.0.0.0",
        "-e",
        "GF_SERVER_HTTP_PORT=3000",
        "-e",
        "GF_AUTH_ANONYMOUS_ENABLED=true",
        "-e",
        "GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer",
        "-e",
        "GF_AUTH_BASIC_ENABLED=false",
        "-e",
        "GF_AUTH_DISABLE_LOGIN_FORM=true",
        "-e",
        "GF_USERS_ALLOW_SIGN_UP=false",
        "-e",
        "GF_ANALYTICS_REPORTING_ENABLED=false",
        "-e",
        "GF_ANALYTICS_CHECK_FOR_UPDATES=false",
        "-e",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES=false",
        "-v",
        f"{PROVISIONING}:/etc/grafana/provisioning:ro",
        "-v",
        f"{DASHBOARDS}:/var/lib/grafana/dashboards:ro",
        "--mount",
        "type=tmpfs,destination=/var/lib/grafana,tmpfs-size=134217728,tmpfs-mode=1777",
        args.image,
    ]
    result = run_docker(command, timeout=60)
    if result.returncode != 0:
        raise SetupError(
            "启动 Grafana 容器失败："
            + (_first_line(result.stderr) or "docker run 返回非零退出码")
            + f"。可回退镜像 {FALLBACK_IMAGE}。"
        )


def container_logs(name, tail=80):
    try:
        result = run_docker(
            ["logs", "--tail", str(tail), name], timeout=20
        )
    except SetupError as exc:
        return str(exc)
    return ((result.stdout or "") + (result.stderr or "")).strip()


def container_logs_if_exists(name):
    try:
        inspect = run_docker(
            ["container", "inspect", name, "--format", "{{.Id}}"], timeout=10
        )
    except SetupError:
        return ""
    if inspect.returncode != 0:
        return ""
    return container_logs(name)


def cleanup_container(token):
    """只删除带本次唯一运行标签的容器；不触碰同名但非本次创建的容器。

    返回 ``(错误信息, 实际删除数量)``；错误信息为 None 表示清理命令成功。
    """
    try:
        listed = run_docker(
            ["ps", "-aq", "--filter", f"label={RUN_LABEL}={token}"],
            timeout=20,
        )
    except SetupError as exc:
        return str(exc), 0
    if listed.returncode != 0:
        return "无法列出临时容器：" + _first_line(listed.stderr), 0
    container_ids = [
        line.strip() for line in listed.stdout.splitlines() if line.strip()
    ]
    for container_id in container_ids:
        try:
            removed = run_docker(
                ["rm", "-f", "-v", container_id], timeout=30
            )
        except SetupError as exc:
            return str(exc), 0
        if removed.returncode != 0:
            return (
                f"删除临时容器 {container_id[:12]} 失败："
                + _first_line(removed.stderr),
                0,
            )
    return None, len(container_ids)


def print_fallback_hint():
    print("可操作提示：", file=sys.stderr)
    print(
        f"  - 镜像启动失败时，可回退：python3 tests/benchmark/verify_grafana.py "
        f"--image {FALLBACK_IMAGE}",
        file=sys.stderr,
    )
    print(
        "  - 若预置经典 JSON 看板未被加载，核对 provider.yml 的 path 与挂载目录，"
        "并评估改用 Grafana V2 看板格式。",
        file=sys.stderr,
    )
    print(
        "  - 若匿名访问被拒，核对 GF_AUTH_ANONYMOUS_* 与 "
        "GF_AUTH_DISABLE_LOGIN_FORM 是否与 compose.observability.yaml 一致。",
        file=sys.stderr,
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "用临时 Grafana 容器验证 Payment Hub 预置数据源与看板。"
            "使用 127.0.0.1 端口映射而非 host 网络，确保 3000 只绑定回环。"
        ),
        epilog=(
            "退出码：0 全部通过；2 环境或参数错误；3 契约不满足。"
            "脚本不会拉取镜像，也不会删除已存在的同名容器；"
            "结束时只删除本次创建并带唯一运行标签的临时容器。"
        ),
    )
    parser.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help=(
            f"Grafana 镜像（默认 {DEFAULT_IMAGE}；失败时可回退 "
            f"{FALLBACK_IMAGE}）"
        ),
    )
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "临时容器名（默认 payment-hub-grafana-check-<随机>）；"
            "同名容器已存在时脚本拒绝启动且不会删除它"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=3000,
        help=(
            "宿主回环端口（默认 3000；正式部署必须使用 3000，"
            "仅在本机临时冲突时改用其他端口）"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="等待 Grafana 就绪、数据源和看板加载的总超时秒数（默认 90）",
    )
    parser.add_argument(
        "--platform",
        default=DEFAULT_PLATFORM,
        help=f"容器平台（默认 {DEFAULT_PLATFORM}，与 compose 保持一致）",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 < args.port < 65536:
        parser.error("--port 必须在 1-65535 之间")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是大于 0 的有限数值")
    if not args.image.strip():
        parser.error("--image 不能为空")
    if not args.platform.strip():
        parser.error("--platform 不能为空")

    if args.name is None:
        args.name = DEFAULT_NAME_PREFIX + uuid.uuid4().hex[:8]
    elif not args.name or not (
        args.name[0].isalnum()
        and all(
            character.isalnum() or character in "_.-"
            for character in args.name
        )
    ):
        parser.error(
            "--name 必须以字母或数字开头，且只能包含字母、数字、下划线、点和连字符"
        )

    token = uuid.uuid4().hex
    cleanup_needed = False
    exit_code = 0

    try:
        docker_version = check_docker()
        print(f"Docker daemon 可用（版本 {docker_version}）")
        check_image(args.image)
        print(f"使用本地镜像 {args.image}（未拉取）")
        check_container_name_free(args.name)
        check_port_free(args.port)

        cleanup_needed = True
        start_container(args, token)
        base_url = f"http://127.0.0.1:{args.port}"
        print(f"临时容器 {args.name} 已启动，等待 {base_url}/api/health 就绪")

        health = wait_for_health(base_url, args.timeout)
        print(
            "Grafana 已就绪：database={}，version={}".format(
                health.get("database"), health.get("version", "unknown")
            )
        )

        datasource = wait_for_datasource(base_url, args.timeout)
        print(
            "数据源检查通过：name={}，isDefault={}，uid={}".format(
                datasource.get("name"),
                datasource.get("isDefault"),
                datasource.get("uid", "unknown"),
            )
        )

        dashboard = wait_for_dashboard(base_url, args.timeout)
        print(
            "看板检查通过：uid={}，title={}，panel 数={}".format(
                dashboard.get("uid"),
                dashboard.get("title"),
                len(dashboard.get("panels", [])),
            )
        )

        binding = check_loopback_binding(args.name, args.port)
        print(f"回环绑定检查通过：{binding}")
    except SetupError as exc:
        print(f"错误：环境或参数不满足：{exc}", file=sys.stderr)
        logs = container_logs_if_exists(args.name) if cleanup_needed else ""
        if logs:
            print("容器日志（末尾）：", file=sys.stderr)
            print(logs, file=sys.stderr)
        print_fallback_hint()
        exit_code = 2
    except ContractError as exc:
        print(f"错误：契约不满足：{exc}", file=sys.stderr)
        logs = container_logs_if_exists(args.name) if cleanup_needed else ""
        if logs:
            print("容器日志（末尾）：", file=sys.stderr)
            print(logs, file=sys.stderr)
        print_fallback_hint()
        exit_code = 3
    finally:
        if cleanup_needed:
            cleanup_error, removed_count = cleanup_container(token)
            if cleanup_error is not None:
                print(
                    f"警告：清理临时容器失败：{cleanup_error}", file=sys.stderr
                )
                print(
                    "请手动检查：docker ps -a --filter "
                    f"label={RUN_LABEL}={token}",
                    file=sys.stderr,
                )
                if exit_code == 0:
                    exit_code = 2
            elif removed_count:
                print(f"已删除临时容器 {args.name}")
            else:
                print("未发现需要清理的临时容器")

    if exit_code == 0:
        print("结果：全部通过")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
