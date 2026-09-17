"""Windows IPv4 uplink binding. No system proxy, VPN route or administrator change."""

import ctypes
import ipaddress
import os
import socket
import struct
import threading
import time


class DirectError(Exception):
    pass


class Addr(ctypes.Structure):
    pass


Addr._fields_ = [
    ("next", ctypes.POINTER(Addr)),
    ("ip", ctypes.c_char * 16),
    ("mask", ctypes.c_char * 16),
    ("context", ctypes.c_uint32),
]


class Adapter(ctypes.Structure):
    pass


Adapter._fields_ = [
    ("next", ctypes.POINTER(Adapter)),
    ("combo", ctypes.c_uint32),
    ("name", ctypes.c_char * 260),
    ("description", ctypes.c_char * 132),
    ("address_length", ctypes.c_uint32),
    ("address", ctypes.c_ubyte * 8),
    ("index", ctypes.c_uint32),
    ("type", ctypes.c_uint32),
    ("dhcp", ctypes.c_uint32),
    ("current", ctypes.POINTER(Addr)),
    ("ips", Addr),
    ("gateways", Addr),
    ("dhcp_server", Addr),
    ("have_wins", ctypes.c_int32),
    ("primary_wins", Addr),
    ("secondary_wins", Addr),
    ("lease_obtained", ctypes.c_int64),
    ("lease_expires", ctypes.c_int64),
]
VIRTUAL = (
    "tunnel",
    "wintun",
    "wireguard",
    "openvpn",
    "tap-",
    "tap ",
    "vpn",
    "clash",
    "mihomo",
    "sing-box",
    "virtual",
    "hyper-v",
    "vmware",
    "virtualbox",
    "tailscale",
    "zerotier",
    "loopback",
)


def eligible(adapter):
    if adapter["type"] not in (6, 71) or any(v in adapter["description"].lower() for v in VIRTUAL):
        return False
    try:
        addr = ipaddress.IPv4Address(adapter["ip"])
        gateway = ipaddress.IPv4Address(adapter["gateway"])
        return (
            not (
                addr.is_unspecified
                or addr.is_loopback
                or addr.is_link_local
                or addr in ipaddress.ip_network("198.18.0.0/15")
            )
            and not gateway.is_unspecified
        )
    except ValueError:
        return False


def adapters():
    if os.name != "nt":
        raise DirectError("公网直连组件只支持 Windows")
    api = ctypes.WinDLL("iphlpapi")
    size = ctypes.c_uint32(0)
    result = api.GetAdaptersInfo(None, ctypes.byref(size))
    if result not in (0, 111):
        raise DirectError("无法读取本地网卡：" + str(result))
    for _ in range(3):
        buffer = ctypes.create_string_buffer(size.value)
        result = api.GetAdaptersInfo(buffer, ctypes.byref(size))
        if result != 111:
            break
    if result:
        raise DirectError("无法读取本地网卡：" + str(result))
    current = ctypes.cast(buffer, ctypes.POINTER(Adapter))
    items = []
    while current:
        a = current.contents
        current = a.next
        gateway = a.gateways.ip.decode("ascii")
        addr = ctypes.pointer(a.ips)
        while addr:
            ip = addr.contents.ip.decode("ascii")
            addr = addr.contents.next
            item = {
                "index": a.index,
                "type": a.type,
                "description": a.description.decode("mbcs", "replace"),
                "ip": ip,
                "gateway": gateway,
            }
            if eligible(item):
                items.append(item)
    # Prefer the physical adapter with the lowest default-route metric.
    size = ctypes.c_uint32(0)
    api.GetIpForwardTable(None, ctypes.byref(size), False)
    table = ctypes.create_string_buffer(size.value)
    if api.GetIpForwardTable(table, ctypes.byref(size), False) == 0:
        data = table.raw
        metrics = {}
        for pos in range(4, 4 + struct.unpack_from("<I", data)[0] * 56, 56):
            row = struct.unpack_from("<14I", data, pos)
            if row[0] == 0 and row[1] == 0:
                metrics[row[4]] = min(metrics.get(row[4], 2**32), row[9])
        items.sort(key=lambda a: metrics.get(a["index"], 2**32))
    return items


def clean_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not (
            key.lower().endswith("_proxy")
            or key.lower()
            in (
                "allproxy",
                "socks_proxy",
                "ssh_proxycommand",
                "git_ssh_command",
                "ssh_auth_sock",
                "ssh_agent_pid",
            )
        )
    }


def connect_direct(host, port, timeout=10):
    try:
        ip = ipaddress.IPv4Address(host)
        if not ip.is_global:
            raise ValueError()
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        raise DirectError("公网直连目标必须是公网 IPv4 地址和有效端口") from None
    candidates = adapters()
    if not candidates:
        raise DirectError("未找到带网关的真实有线/Wi-Fi网卡；已保留本地数据，不会改走代理")
    deadline = time.monotonic() + timeout
    errors = []
    for adapter in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Microsoft IP_UNICAST_IF requires the interface index in network byte order.
            connection.setsockopt(socket.IPPROTO_IP, 31, struct.pack("!I", adapter["index"]))
            connection.bind((adapter["ip"], 0))
            connection.settimeout(min(5, remaining))
            connection.connect((str(ip), port))
            connection.settimeout(None)
            return connection, {
                **adapter,
                "mode": "direct",
                "server": str(ip),
                "port": port,
                "local_address": connection.getsockname()[0],
            }
        except OSError as error:
            connection.close()
            errors.append(adapter["ip"] + "：" + str(error))
    raise DirectError("真实网卡直连失败，数据已保留，不会改走代理。" + "；".join(errors))


class DirectBridge:
    """Expose one connected, interface-bound socket to bundled SSH on loopback."""

    def __init__(self, host, port):
        self.remote, self.info = connect_direct(host, port)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
            self.listener.settimeout(12)
        except Exception:
            self.listener.close()
            self.remote.close()
            raise
        self.port = self.listener.getsockname()[1]
        self.local = None
        self.threads = []
        self.acceptor = threading.Thread(target=self._accept, daemon=True)
        self.acceptor.start()

    def _accept(self):
        try:
            self.local, _ = self.listener.accept()
            for source, destination in ((self.local, self.remote), (self.remote, self.local)):
                thread = threading.Thread(
                    target=self._copy, args=(source, destination), daemon=True
                )
                self.threads.append(thread)
                thread.start()
        except OSError:
            pass
        finally:
            self.listener.close()

    @staticmethod
    def _copy(source, destination):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                destination.sendall(data)
        except OSError:
            pass
        finally:
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        for channel in (self.listener, self.local, self.remote):
            if channel:
                try:
                    channel.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                channel.close()
        self.acceptor.join(1)
        for thread in self.threads:
            thread.join(1)
