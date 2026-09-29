import base64
import json
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import pytest

from config import settings
from services.horse_image_url import (
    ImageProxyConfigurationError, TOKEN_CONTEXT, _encrypted_token, image_url_for_game,
)

TEST_KEY = bytes(range(32)).hex()
SOURCE = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Gold_Ship.jpg"
JRA_SOURCE = "https://jra.jp/gallery/3minmeiba/horse9/img/pic_gallery_1.jpg"
JRA_HALL_OF_FAME_SOURCE = "https://jra.jp/gallery/dendo/horse13/img/pic_gallery_2.jpg"
NETKEIBA_CDN_SOURCE = "https://cdn.netkeiba.com/img.dir/keibamatome/image.php?id=3353"
NETKEIBA_PROFILE_SOURCE = (
    "https://cdnv2.netkeiba.com/img.db.sp/show_photo.php?"
    "horse_id=1985100743&no=spdb&tn=&tmp=no&default_image=netkeiba"
)
NETKEIBA_DIR_SOURCE = "https://dir.netkeiba.com/PhotoExhibition/photodetail/img/cate_02_detail_01.jpg"
NUMBER_SOURCE = "https://number.ismcdn.jp/mwimgs/c/d/750wm/img_cd0a2409e1309f002ba82de0a3e3896c4381320.jpg"
ARCHIVE_SOURCE = "https://assets.st-note.com/img/example.jpg?width=1200"


@pytest.fixture(autouse=True)
def proxy_config(monkeypatch):
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_URL", "https://photos.example.org")
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_KEY", TEST_KEY)
    monkeypatch.setattr(settings, "HORSE_IMAGE_ALLOWED_HOSTS", "upload.wikimedia.org")
    _encrypted_token.cache_clear()


def test_encrypted_url_is_opaque_authenticated_and_stable_in_memory():
    result = image_url_for_game(SOURCE)
    assert result.startswith("https://photos.example.org/h/v1/")
    assert result == image_url_for_game(SOURCE)
    assert "Gold" not in result and "wikimedia" not in result and "?" not in result
    token = result.rsplit("/", 1)[1][:-4]
    packed = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert SOURCE.encode() not in packed  # Not merely a base64-encoded source URL.
    assert AESGCM(bytes.fromhex(TEST_KEY)).decrypt(packed[:12], packed[12:], TOKEN_CONTEXT).decode() == SOURCE


def test_worker_interop_vector():
    vector = json.loads((Path(__file__).parent / "fixtures" / "horse-image-token.json").read_text())
    with patch("services.horse_image_url.secrets.token_bytes", return_value=bytes.fromhex(vector["nonce"])):
        assert _encrypted_token(vector["source"], vector["key"]) == vector["token"]


def test_disabled_proxy_keeps_existing_direct_url_mode(monkeypatch):
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_URL", "")
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_KEY", "")
    assert image_url_for_game(SOURCE) == SOURCE


@pytest.mark.parametrize("source", [
    JRA_SOURCE, JRA_HALL_OF_FAME_SOURCE, NETKEIBA_CDN_SOURCE,
    NETKEIBA_PROFILE_SOURCE, NETKEIBA_DIR_SOURCE, NUMBER_SOURCE,
])
def test_neutral_image_hosts_bypass_worker_with_exact_direct_url(source):
    assert image_url_for_game(source) == source


@pytest.mark.parametrize("base,key", [
    ("", TEST_KEY), ("https://photos.example.org", "invalid"),
])
def test_jra_bypass_precedes_worker_configuration_validation(monkeypatch, base, key):
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_URL", base)
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_KEY", key)
    for source in (
        JRA_SOURCE, NETKEIBA_CDN_SOURCE, NETKEIBA_PROFILE_SOURCE,
        NETKEIBA_DIR_SOURCE, NUMBER_SOURCE,
    ):
        assert image_url_for_game(source) == source


@pytest.mark.parametrize("source", [
    "https://images.jra.jp/photo.jpg",
    "https://example-jra.jp/photo.jpg",
    "https://jra.jp.example.org/photo.jpg",
    "https://jra.jp:444/photo.jpg",
    "https://jra.jp/photo.jpg#answer",
    "https://cdn.netkeiba.com.example.org/photo.jpg",
    "https://fakecdn.netkeiba.com/photo.jpg",
    "https://cdn.netkeiba.com/Gold_Ship.jpg",
    "https://cdn.netkeiba.com/img.dir/keibamatome/image.php?id=horse-name",
    "https://cdnv2.netkeiba.com/Gold_Ship.jpg",
    "https://cdnv2.netkeiba.com/img.db.sp/show_photo.php?horse_id=Gold_Ship&no=spdb&tn=&tmp=no&default_image=netkeiba",
    "https://cdnv2.netkeiba.com/img.db.sp/show_photo.php?horse_id=1985100743&no=spdb&tn=&tmp=no&default_image=other",
    "https://cdnv2.netkeiba.com.example.org/img.db.sp/show_photo.php?horse_id=1985100743&no=spdb&tn=&tmp=no&default_image=netkeiba",
    "https://dir.netkeiba.com:444/photo.jpg",
    "https://dir.netkeiba.com/photo.jpg#answer",
    "https://dir.netkeiba.com/Gold_Ship.jpg",
    "https://number.ismcdn.jp/mwimgs/c/d/750wm/Gold_Ship.jpg",
    "https://number.ismcdn.jp/mwimgs/c/d/750wm/img_not-a-hash.jpg",
    "https://number.ismcdn.jp.example.org/mwimgs/c/d/750wm/img_cd0a2409e1309f002ba82de0a3e3896c4381320.jpg",
])
def test_direct_host_lookalikes_and_unsafe_variants_do_not_bypass(source):
    with pytest.raises(ValueError):
        image_url_for_game(source)


def test_private_use_archive_host_can_be_enabled(monkeypatch):
    monkeypatch.setattr(settings, "HORSE_IMAGE_ALLOWED_HOSTS", "assets.st-note.com")
    result = image_url_for_game(ARCHIVE_SOURCE)
    assert result.startswith("https://photos.example.org/h/v1/")
    assert "st-note" not in result and "width" not in result


@pytest.mark.parametrize("base,key", [
    ("", TEST_KEY), ("https://photos.example.org", ""),
    ("http://photos.example.org", TEST_KEY), ("https://photos.example.org/path", TEST_KEY),
    ("https://photos.example.org?source=", TEST_KEY), ("https://photos.example.org", "invalid"),
])
def test_partial_or_invalid_configuration_never_falls_back_to_source(monkeypatch, base, key):
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_URL", base)
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_KEY", key)
    with pytest.raises(ImageProxyConfigurationError):
        image_url_for_game(SOURCE)


@pytest.mark.parametrize("source", [
    "https://127.0.0.1/photo.jpg", "https://example.org/photo.jpg",
    "https://upload.wikimedia.org:1234/photo.jpg", "https://upload.wikimedia.org/photo.jpg#secret",
    "https://upload.wikimedia.org/" + "x" * 1200,
])
def test_unsupported_source_is_rejected(source):
    with pytest.raises(ValueError):
        image_url_for_game(source)


def test_new_source_or_key_gets_a_new_token(monkeypatch):
    first = image_url_for_game(SOURCE)
    assert image_url_for_game(SOURCE.replace("Gold_Ship", "Oguri_Cap")) != first
    monkeypatch.setattr(settings, "HORSE_IMAGE_PROXY_KEY", "ff" * 32)
    assert image_url_for_game(SOURCE) != first
