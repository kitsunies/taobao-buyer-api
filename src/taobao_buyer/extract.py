"""Allowlisted public observations from scoped product/search DOM snapshots."""

import re
from datetime import datetime, timezone

from .errors import SourcingError
from .urls import canonical_product_url, image_url, seller_url


_PRIVATE_ATTRIBUTE = re.compile(r"cookie|token|authorization|account|user.?id|address|phone|email|账号|账户|收货|地址|电话|手机|昵称", re.I)


def _text(value, limit=2000):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c for c in value if ord(c) >= 32 or c in "\n\t").split())[:limit]


def _texts(values, limit=2000, count=100):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(text for value in values[:count] if (text := _text(value, limit))))


def _images(values):
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(url for value in values[:300] if (url := image_url(value))))


def _price(snapshot):
    return {"displayed": _text(snapshot.get("price_text"), 1000) or None,
            "qualifications": _texts(snapshot.get("price_qualifications", [])),
            "checkout_price_verified": False}


def _observed_at():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def extract_product(snapshot: dict, url: str) -> dict:
    canonical = canonical_product_url(url)
    title = _text(snapshot.get("title"), 500)
    if not title:
        raise SourcingError("extraction_failed", "Product content could not be identified.", "operator", 502)
    variants = []
    for variant in snapshot.get("variants", [])[:100]:
        if isinstance(variant, dict) and (name := _text(variant.get("name"), 300)):
            variants.append({"name": name, "options": _texts(variant.get("options", []), 500)})
    attributes = []
    for attribute in snapshot.get("attributes", [])[:300]:
        if not isinstance(attribute, dict):
            continue
        name, value = _text(attribute.get("name"), 300), _text(attribute.get("value"))
        if name and value and not _PRIVATE_ATTRIBUTE.search(name):
            attributes.append({"name": name, "value": value})
    raw_seller = snapshot.get("seller", {})
    seller = {}
    if isinstance(raw_seller, dict):
        if name := _text(raw_seller.get("name"), 500):
            seller["name"] = name
        if shop := seller_url(raw_seller.get("url")):
            seller["url"] = shop
    return {"url": canonical, "observed_at": _observed_at(), "title": title,
            "price": _price(snapshot), "variants": variants, "seller": seller,
            "attributes": attributes, "gallery": _images(snapshot.get("gallery", [])),
            "detail_images": _images(snapshot.get("detail_images", [])),
            "material_claims": [claim for claim in _texts(snapshot.get("material_claims", []), 8000)
                                if not _PRIVATE_ATTRIBUTE.search(claim)],
            "size_chart_images": _images(snapshot.get("size_chart_images", []))}


def extract_search(snapshot: dict) -> dict:
    products, seen = [], set()
    for link in snapshot.get("links", [])[:500]:
        if not isinstance(link, dict):
            continue
        try:
            url = canonical_product_url(link.get("url"))
        except SourcingError:
            continue
        if url in seen:
            continue
        seen.add(url)
        product = {"url": url, "title": _text(link.get("title"), 500), "price": _price(link)}
        if image := image_url(link.get("image")):
            product["image"] = image
        products.append(product)
    return {"observed_at": _observed_at(), "page": 1, "products": products}
