"""绕过本机对 Cursor 域名的 DNS 劫持。

有些本地网关工具（如 cgw）会把 cursor.com / api2.cursor.sh 的 DNS 指向本机中转 IP，
导致我们的请求被拦截或用错账号。这里用 DoH（DNS over HTTPS）解析这两个主机的真实 IP，
仅对它们覆盖 socket.getaddrinfo；TLS 的 SNI 与证书校验仍用原域名，安全不受影响
（就算某家 DoH 答错 IP，证书对不上域名也会直接握手失败，不会静默连到假站）。

多家 DoH 并发竞速、谁先答谁赢：1.1.1.1 / dns.google 在国内不少网络直接超时，
阿里 223.5.5.5 与腾讯 1.12.12.12 则能 0.3s 内返回。全部失败时记一次负缓存，
在 NEGATIVE_TTL 内直接回落系统 DNS，避免每个请求都白等一轮超时（之前每次多等 8s）。
"""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

CURSOR_HOSTS = {"cursor.com", "api2.cursor.sh"}
# 全部按 IP 直连（证书 SAN 含该 IP，校验照常开着），DoH 自身不依赖系统 DNS，也不会递归进本覆盖。
DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://223.5.5.5/resolve",
    "https://1.12.12.12/dns-query",
    "https://223.6.6.6/resolve",
    "https://120.53.53.53/dns-query",
)
DOH_TIMEOUT = 4
NEGATIVE_TTL = 120

_orig_getaddrinfo = socket.getaddrinfo
_cache: dict[str, str] = {}
_failed_at: dict[str, float] = {}
_lock = threading.Lock()


def _doh_one(endpoint: str, host: str) -> str | None:
    resp = requests.get(
        endpoint,
        params={"name": host, "type": "A"},
        headers={"accept": "application/dns-json"},
        timeout=DOH_TIMEOUT,
    )
    for answer in resp.json().get("Answer", []):
        if answer.get("type") == 1 and answer.get("data"):
            return str(answer["data"])
    return None


def _doh(host: str) -> str | None:
    """并发问所有 DoH，拿到第一个 A 记录就返回；其余请求在后台自行超时结束。"""
    pool = ThreadPoolExecutor(max_workers=len(DOH_ENDPOINTS))
    try:
        futures = [pool.submit(_doh_one, endpoint, host) for endpoint in DOH_ENDPOINTS]
        for fut in as_completed(futures):
            try:
                ip = fut.result()
            except Exception:
                continue
            if ip:
                return ip
        return None
    finally:
        pool.shutdown(wait=False)


def _resolve(host: str) -> str | None:
    ip = _cache.get(host)
    if ip:
        return ip
    # 同一主机同时只跑一轮 DoH：并发验证多个账号时，其余线程等这一轮的结果，不重复发问。
    with _lock:
        ip = _cache.get(host)
        if ip:
            return ip
        if time.monotonic() - _failed_at.get(host, float("-inf")) < NEGATIVE_TTL:
            return None
        ip = _doh(host)
        if ip:
            _cache[host] = ip
            _failed_at.pop(host, None)
        else:
            _failed_at[host] = time.monotonic()
        return ip


def _patched_getaddrinfo(host, *args, **kwargs):
    if host in CURSOR_HOSTS:
        ip = _resolve(host)
        if ip:
            # 用真实 IP 连接，但上层仍以原域名做 TLS SNI 与证书校验。
            return _orig_getaddrinfo(ip, *args, **kwargs)
    return _orig_getaddrinfo(host, *args, **kwargs)


def install() -> None:
    """安装 DNS 覆盖。DoH 端点全是 IP、不在覆盖名单内，故不会自我递归。"""
    socket.getaddrinfo = _patched_getaddrinfo
