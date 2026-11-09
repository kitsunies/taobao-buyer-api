"""Allowlisted public URLs; never return tracking parameters or credentials."""

import re
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .errors import SourcingError


def _parts(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return None
    try:
        parts = urlsplit("https:" + value if value.startswith("//") else value)
        if parts.scheme not in {"http", "https"} or parts.username or parts.password or parts.port:
            return None
        return parts
    except ValueError:
        return None


def canonical_product_url(value: str) -> str:
    parts = _parts(value)
    if parts:
        host = (parts.hostname or "").lower()
        if host == "pcdetail.taobao.com" and re.fullmatch(r"/[A-Za-z0-9_-]{8,200}\.html", parts.path):
            return urlunsplit(("https", host, parts.path, "", ""))
        if host in {"item.taobao.com", "detail.tmall.com"} and parts.path == "/item.htm":
            ids = parse_qs(parts.query, keep_blank_values=True).get("id", [])
            if len(ids) == 1 and re.fullmatch(r"[0-9]{1,30}", ids[0]):
                return f"https://{host}/item.htm?id={ids[0]}"
        if host == "h5.m.taobao.com" and parts.path == "/awp/core/detail.htm":
            ids = parse_qs(parts.query, keep_blank_values=True).get("id", [])
            if len(ids) == 1 and re.fullmatch(r"[0-9]{1,30}", ids[0]):
                return f"https://item.taobao.com/item.htm?id={ids[0]}"
    raise SourcingError("invalid_product_url", "Supply a supported Taobao or Tmall product detail URL.")


def image_url(value: str) -> str | None:
    parts = _parts(value)
    if not parts:
        return None
    host = (parts.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain)
               for domain in ("alicdn.com", "tbcdn.cn", "taobaocdn.com")):
        return None
    if not re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:_|$)", parts.path, re.I):
        return None
    if any(c in unquote(parts.path) for c in ("\\", "\r", "\n", "\x00")):
        return None
    return urlunsplit(("https", host, parts.path, "", ""))


def seller_url(value: str) -> str | None:
    parts = _parts(value)
    if parts:
        host = (parts.hostname or "").lower()
        if re.fullmatch(r"shop[0-9]+\.taobao\.com", host) or (
                host.endswith(".tmall.com") and re.fullmatch(r"[a-z0-9-]+\.tmall\.com", host)):
            return f"https://{host}/"
        ids = parse_qs(parts.query, keep_blank_values=True).get("shop_id", [])
        if host == "shop.m.taobao.com" and len(ids) == 1 and re.fullmatch(r"[0-9]{1,30}", ids[0]):
            return f"https://shop.m.taobao.com/shop/shop_index.htm?shop_id={ids[0]}"
    return None
