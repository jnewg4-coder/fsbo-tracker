"""
FSBO Tracker — Rate limiting utilities.

Real-IP extraction that works behind Railway/Cloudflare proxies,
and a limiter factory with global RATE_LIMIT_ENABLED kill-switch.

Ported from AVMLens api/utils/rate_limit.py.
"""

import ipaddress
import logging
import os

from starlette.requests import Request
from slowapi import Limiter

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
_logger = logging.getLogger("fsbo.rate_limit")
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def _parse_ip(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def _is_internal_ip(ip) -> bool:
    if ip.version == 4 and ip in _CGNAT_V4:
        return True
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def get_real_client_ip(request: Request) -> str:
    """Extract real client IP from proxy headers.

    Priority:
      1. CF-Connecting-IP (if CF-RAY present — Cloudflare)
      2. X-Forwarded-For rightmost public IP (skip Railway CGNAT)
      3. request.client.host (direct connection)
    """
    client_host = request.client.host if request.client else ""
    client_ip = _parse_ip(client_host)

    # 1. Cloudflare
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip and request.headers.get("cf-ray"):
        parsed = _parse_ip(cf_ip)
        if parsed:
            return str(parsed)

    # 2. X-Forwarded-For (only if behind proxy)
    if client_ip is None or _is_internal_ip(client_ip):
        xff = request.headers.get("x-forwarded-for")
        if xff:
            for ip in reversed([p.strip() for p in xff.split(",") if p.strip()]):
                parsed = _parse_ip(ip)
                if parsed and not _is_internal_ip(parsed):
                    return str(parsed)
            # Fallback: rightmost valid even if internal
            for ip in reversed([p.strip() for p in xff.split(",") if p.strip()]):
                parsed = _parse_ip(ip)
                if parsed:
                    return str(parsed)

    # 3. Direct connection
    if client_ip:
        return str(client_ip)

    return "unknown"


def create_limiter(**kwargs) -> Limiter:
    """Create a Limiter using real-IP extraction and the global enabled flag."""
    return Limiter(
        key_func=get_real_client_ip,
        enabled=RATE_LIMIT_ENABLED,
        **kwargs,
    )


# Shared instance — import this in routers and app.py
limiter = create_limiter()
