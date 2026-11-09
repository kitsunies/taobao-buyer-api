import io
import json
import os
import subprocess
import sys
from email.utils import formatdate
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from taobao_buyer.errors import SourcingError
from taobao_buyer.extract import extract_product, extract_search
from taobao_buyer.images import MAX_IMAGE_BYTES, normalize_image
from taobao_buyer.policy import Policy, retry_after_seconds
from taobao_buyer.profile import Profile
from taobao_buyer.urls import canonical_product_url, image_url, seller_url


@pytest.mark.parametrize("url,expected", [
    ("http://item.taobao.com/item.htm?id=123&spm=tracking#private", "https://item.taobao.com/item.htm?id=123"),
    ("https://detail.tmall.com/item.htm?id=456&user_id=private", "https://detail.tmall.com/item.htm?id=456"),
    ("https://h5.m.taobao.com/awp/core/detail.htm?id=123&token=private", "https://item.taobao.com/item.htm?id=123"),
    ("https://pcdetail.taobao.com/SyntheticOpaqueItem123.html?spm=tracking", "https://pcdetail.taobao.com/SyntheticOpaqueItem123.html"),
])
def test_canonical_urls(url, expected):
    assert canonical_product_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://item.taobao.com.evil.test/item.htm?id=1", "https://evil.test/?id=1",
    "https://item.taobao.com@evil.test/item.htm?id=1", "https://user:pass@item.taobao.com/item.htm?id=1",
    "https://item.taobao.com:443/item.htm?id=1", "file:///item.htm?id=1",
    "https://item.taobao.com/item.htm?id=1&id=2", "https://item.taobao.com/item.htm?id=abc",
    "https://item.taobao.com/item.htm?id=１２", "https://item.taobao.com/other?id=1",
    "https://pcdetail.taobao.com/too.html", "https://pcdetail.taobao.com/a/bbbbbbbbb.html",
    "https://pcdetail.taobao.com/../SyntheticOpaqueItem123.html", "https://item.taobao.com/item.htm?id=1\n",
])
def test_invalid_product_urls(url):
    with pytest.raises(SourcingError, match="supported Taobao"):
        canonical_product_url(url)


def test_public_image_and_seller_urls():
    assert image_url("//img.alicdn.com/imgextra/synthetic.jpg?token=private#fragment") == "https://img.alicdn.com/imgextra/synthetic.jpg"
    assert image_url("https://img.alicdn.com/a.jpg_400x400.webp?spm=track") == "https://img.alicdn.com/a.jpg_400x400.webp"
    for url in ("https://evil.test/a.jpg", "https://alicdn.com.evil.test/a.jpg", "data:image/png,abc",
                "https://img.alicdn.com/a.svg", "https://user@img.alicdn.com/a.jpg",
                "https://img.alicdn.com/a%0A.jpg"):
        assert image_url(url) is None
    assert seller_url("https://shop123.taobao.com/index.htm?user_id=private") == "https://shop123.taobao.com/"
    assert seller_url("https://synthetic.tmall.com/?spm=private") == "https://synthetic.tmall.com/"
    assert seller_url("https://shop.m.taobao.com/shop/shop_index.htm?shop_id=123&user_id=private") == "https://shop.m.taobao.com/shop/shop_index.htm?shop_id=123"
    assert seller_url("https://evil.test/?shop_id=123") is None


def test_product_evidence_and_privacy():
    snapshot = {
        "title": "Synthetic cotton tee", "price_text": "券后 ¥89 起", "price_qualifications": ["领券后", "新人优惠"],
        "variants": [{"name": "尺码", "options": ["0", "1"]}],
        "seller": {"name": "Synthetic shop", "url": "https://shop123.taobao.com/?user_id=private", "account_id": "private"},
        "attributes": [{"name": "面料", "value": "棉"}, {"name": "收货地址", "value": "private address"}],
        "gallery": ["https://img.alicdn.com/synthetic-front.jpg?token=private"],
        "detail_images": ["https://img.alicdn.com/synthetic-chart.png?spm=private"],
        "material_claims": ["衣身：100%棉", "领口：Sorona混纺"],
        "size_chart_images": ["https://img.alicdn.com/synthetic-chart.png?spm=private"],
        "cookies": [{"name": "private", "value": "secret"}], "raw_html": "private address",
    }
    result = extract_product(snapshot, "https://item.taobao.com/item.htm?id=123&token=private")
    assert result["material_claims"] == ["衣身：100%棉", "领口：Sorona混纺"]
    assert result["size_chart_images"] == ["https://img.alicdn.com/synthetic-chart.png"]
    assert result["detail_images"] == result["size_chart_images"]
    assert result["price"] == {"displayed": "券后 ¥89 起", "qualifications": ["领券后", "新人优惠"], "checkout_price_verified": False}
    assert result["attributes"] == [{"name": "面料", "value": "棉"}]
    assert result["observed_at"].endswith("Z")
    serialized = json.dumps(result)
    for private in ("private", "cookies", "raw_html", "account_id", "user_id", "token", "spm", "measurements"):
        assert private not in serialized


def test_search_canonical_deduplication():
    result = extract_search({"links": [
        {"url": "https://item.taobao.com/item.htm?id=123&spm=one", "title": "tee", "price_text": "¥10"},
        {"url": "https://item.taobao.com/item.htm?id=123&spm=two", "title": "tee duplicate"},
        {"url": "https://evil.test/item.htm?id=456", "title": "invalid"},
    ], "account": "private"})
    assert result["page"] == 1
    assert len(result["products"]) == 1
    assert result["products"][0]["url"] == "https://item.taobao.com/item.htm?id=123"


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_images_convert_and_strip_metadata(image_format):
    image = Image.new("RGB", (20, 30), "red")
    metadata = Image.Exif()
    metadata[315] = "PRIVATE ACCOUNT"
    source = io.BytesIO()
    extra = {"exif": metadata.tobytes()}
    if image_format == "PNG":
        png_metadata = PngImagePlugin.PngInfo()
        png_metadata.add_text("Comment", "PRIVATE TOKEN")
        extra["pnginfo"] = png_metadata
    image.save(source, image_format, **extra)
    normalized, mime = normalize_image(source.getvalue())
    with Image.open(io.BytesIO(normalized)) as decoded:
        assert decoded.size == (20, 30)
        assert not decoded.getexif()
        assert "Comment" not in decoded.info
        assert decoded.format in {"JPEG", "PNG"}
    assert mime in {"image/jpeg", "image/png"}
    assert b"PRIVATE" not in normalized


def test_alpha_and_orientation():
    rgba = Image.new("RGBA", (20, 30), (255, 0, 0, 40))
    source = io.BytesIO()
    rgba.save(source, "PNG")
    normalized, mime = normalize_image(source.getvalue())
    assert mime == "image/png"
    with Image.open(io.BytesIO(normalized)) as decoded:
        assert decoded.getpixel((0, 0))[-1] == 40
    rgb = Image.new("RGB", (20, 30))
    exif = Image.Exif()
    exif[274] = 6
    source = io.BytesIO()
    rgb.save(source, "JPEG", exif=exif)
    normalized, _ = normalize_image(source.getvalue())
    with Image.open(io.BytesIO(normalized)) as decoded:
        assert decoded.size == (30, 20)


def test_invalid_oversized_animated_images():
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    huge = io.BytesIO()
    Image.new("RGB", (8193, 1)).save(huge, "PNG")
    animation = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(animation, "WEBP", save_all=True,
                                            append_images=[Image.new("RGB", (2, 2), "blue")])
    for source in (b"", b"not an image", b"x" * (MAX_IMAGE_BYTES + 1), gif.getvalue(), huge.getvalue(), animation.getvalue()):
        with pytest.raises(SourcingError) as error:
            normalize_image(source)
        assert error.value.code == "invalid_image"


def test_private_profile_lock_survives_release(tmp_path):
    profile = Profile(tmp_path / "private")
    profile.acquire()
    assert profile.path.stat().st_mode & 0o777 == 0o700
    assert (profile.path / ".lock").stat().st_mode & 0o777 == 0o600
    another = Profile(profile.path)
    with pytest.raises(SourcingError) as error:
        another.acquire()
    assert error.value.code == "profile_busy"
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run([sys.executable, "-c", "from taobao_buyer.profile import Profile; from pathlib import Path; import sys; Profile(Path(sys.argv[1])).acquire()", str(profile.path)],
                            capture_output=True, env=environment)
    assert result.returncode != 0
    assert b"profile is in use" in result.stderr
    inode = (profile.path / ".lock").stat().st_ino
    profile.release()
    another.acquire()
    assert (profile.path / ".lock").stat().st_ino == inode
    another.release()


def test_profile_rejects_checkout_and_symlink(tmp_path):
    with pytest.raises(SourcingError):
        Profile(Path(__file__).resolve().parents[1] / "secrets")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(SourcingError):
        Profile(link / "profile")


def test_spacing_and_persisted_cooldown(tmp_path):
    policy = Policy(tmp_path)
    policy.record(now=1000)
    with pytest.raises(SourcingError) as error:
        Policy(tmp_path).check(now=1001)
    assert error.value.retry_after == 29
    assert (tmp_path / "policy.json").stat().st_mode & 0o777 == 0o600
    assert policy.cooldown("60", now=1001) == 60
    with pytest.raises(SourcingError) as error:
        Policy(tmp_path).check(now=1030)
    assert error.value.code == "cooldown"
    assert error.value.retry_after == 31
    Policy(tmp_path).check(now=1061)
    assert policy.summary(now=1061)["operations_last_hour"] == 1


@pytest.mark.parametrize("header,expected", [("10", 10), (" 60 ", 60), ("0", 1), (None, 1800),
    ("", 1800), ("invalid", 1800), ("1.5", 1800), ("-1", 1800), ("9" * 100, 1800),
    (formatdate(1120, usegmt=True), 120), (formatdate(999, usegmt=True), 1800),
    ("Thu, 01 Jan 1970 00:18:40", 1800)])
def test_retry_after(header, expected):
    assert retry_after_seconds(header, now=1000) == expected


def test_hourly_and_daily_budgets(tmp_path):
    policy = Policy(tmp_path)
    for index in range(20):
        policy.record(now=100000 + index * 30)
    with pytest.raises(SourcingError) as error:
        policy.record(now=100600)
    assert error.value.retry_after == 3000
    policy.check(now=103600)
    state = {"operations": [100000 + index * 600 for index in range(100)], "cooldown_until": 0}
    (tmp_path / "policy.json").write_text(json.dumps(state))
    with pytest.raises(SourcingError) as error:
        policy.check(now=160000)
    assert error.value.retry_after == 26400
    policy.check(now=186400)


def test_fallback_cooldown_and_corrupt_state(tmp_path):
    policy = Policy(tmp_path)
    assert policy.cooldown(None, now=1000) == 1800
    assert policy.cooldown("1", now=1001) == 1799
    with pytest.raises(SourcingError):
        Policy(tmp_path).check(now=2799)
    Policy(tmp_path).check(now=2800)
    for bad in ("not json", '{"operations": [], "cooldown_until": NaN}', '{"operations": [true], "cooldown_until": 0}'):
        (tmp_path / "policy.json").write_text(bad)
        with pytest.raises(SourcingError) as error:
            policy.check(now=3000)
        assert error.value.code == "policy_state_invalid"
