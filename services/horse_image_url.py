"""Opaque image URLs for the independently deployed Cloudflare image Worker."""
import base64
from functools import lru_cache
import re
import secrets
from urllib.parse import SplitResult, parse_qsl, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import settings
from models.horse_trivia_question import validate_https_url

TOKEN_CONTEXT = b"umacore-horse-image-v1"


class ImageProxyConfigurationError(RuntimeError):
    pass


def image_url_for_game(source_url: str) -> str:
    """Return a safe game image URL, proxying sources that can reveal answers."""
    source_url = validate_https_url(source_url)
    source = urlsplit(source_url)
    if _is_direct_image_source(source):
        return source_url

    base_url = settings.HORSE_IMAGE_PROXY_URL.strip().rstrip("/")
    key_hex = settings.HORSE_IMAGE_PROXY_KEY.strip()
    if not base_url and not key_hex:
        return source_url
    try:
        validate_https_url(base_url)
        parsed = urlsplit(base_url)
        if parsed.path or parsed.query or parsed.fragment or parsed.port not in (None, 443):
            raise ValueError("Expected an HTTPS origin")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key_hex):
            raise ValueError("Expected a 32-byte hex key")
    except ValueError as exc:
        raise ImageProxyConfigurationError(
            "Set HORSE_IMAGE_PROXY_URL to an HTTPS origin and HORSE_IMAGE_PROXY_KEY "
            "to the Worker's 64-character hex secret."
        ) from exc

    allowed_hosts = {
        host.strip().lower() for host in settings.HORSE_IMAGE_ALLOWED_HOSTS.split(",")
        if host.strip()
    }
    if source.hostname not in allowed_hosts or source.port not in (None, 443) or source.fragment:
        raise ValueError("The image host must be enabled in HORSE_IMAGE_ALLOWED_HOSTS on bot and Worker.")
    if len(source_url.encode("utf-8")) > 1200:
        raise ValueError("The image URL is too long for the image proxy (maximum 1,200 UTF-8 bytes).")
    return f"{base_url}/h/v1/{_encrypted_token(source_url, key_hex)}.jpg"


def _is_direct_image_source(source: SplitResult) -> bool:
    if source.port not in (None, 443) or source.fragment:
        return False
    if source.hostname == "jra.jp":
        return True
    if source.hostname == "cdn.netkeiba.com":
        return (
            source.path == "/img.dir/keibamatome/image.php"
            and re.fullmatch(r"id=\d+", source.query) is not None
        )
    if source.hostname == "cdnv2.netkeiba.com":
        query = parse_qsl(source.query, keep_blank_values=True)
        values = dict(query)
        return (
            source.path == "/img.db.sp/show_photo.php"
            and len(query) == 5
            and set(values) == {"horse_id", "no", "tn", "tmp", "default_image"}
            and re.fullmatch(r"\d+", values["horse_id"]) is not None
            and values["no"] == "spdb"
            and values["tn"] == ""
            and values["tmp"] == "no"
            and values["default_image"] == "netkeiba"
        )
    if source.hostname == "dir.netkeiba.com":
        return (
            not source.query
            and re.fullmatch(
                r"/PhotoExhibition/photodetail/img/cate_\d+_detail_\d+\.jpg",
                source.path,
            ) is not None
        )
    if source.hostname == "number.ismcdn.jp":
        return (
            not source.query
            and re.fullmatch(
                r"/mwimgs/[0-9a-f]/[0-9a-f]/\d+(?:wm|mw|w)/"
                r"img_[0-9a-f]{32,}\.(?:jpe?g|png|webp)",
                source.path,
            ) is not None
        )
    return False


@lru_cache(maxsize=512)
def _encrypted_token(source_url: str, key_hex: str) -> str:
    # A random nonce is mandatory for AES-GCM. Keep recent URLs stable in memory;
    # the Worker also caches upstream images by source URL across bot restarts.
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(bytes.fromhex(key_hex)).encrypt(
        nonce, source_url.encode("utf-8"), TOKEN_CONTEXT
    )
    return base64.urlsafe_b64encode(nonce + encrypted).rstrip(b"=").decode("ascii")
