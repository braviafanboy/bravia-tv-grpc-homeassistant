"""Nonce-gated, AES-256-GCM-encrypted ``GetResourcesWithAuth`` read (app icons).

App icons are referenced from the application_list entries as
``iot-resource://app-icon/<hash>`` URIs. Fetching one reuses the application_list
crack exactly: the full URI is the request path, the ``GetNonce`` nonce goes in
the embedded auth block's field-2 slot (signed), and the response is AES-256-GCM
encrypted with the ``session_key``. The decrypted plaintext is a small protobuf
wrapper — ``{1: {1: <uri>, 2: <image bytes>}}`` — so the actual image (WEBP on
the BRAVIA 8 II) is a nested field, extracted below and served to Home Assistant.
"""

from __future__ import annotations

from .application_list import build_nonce_gated_request, decrypt_gcm_response
from .wire import parse_fields

# Leading magic bytes of the image formats an icon may arrive as.
_IMAGE_MAGICS = (b"RIFF", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8")


def build_get_resource_request(
    *, uri: str, session_random: bytes, session_id: str, hmac_key_hex: str, nonce: bytes
) -> bytes:
    """Build the nonce-gated GetResourcesWithAuth request for a resource *uri*."""
    return build_nonce_gated_request(
        uri,
        session_random=session_random,
        session_id=session_id,
        hmac_key_hex=hmac_key_hex,
        nonce=nonce,
    )


def _looks_like_image(value: bytes) -> bool:
    return any(value.startswith(magic) for magic in _IMAGE_MAGICS)


def _find_image_field(raw: bytes, depth: int = 0) -> bytes | None:
    """Recursively locate the length-delimited protobuf field holding the image.

    The image field's value is returned with exact bounds (no trailing wrapper
    fields). Image bytes themselves are never recursed into — the magic check
    happens first — so their non-protobuf content is not misparsed.
    """
    if depth > 5:
        return None
    for values in parse_fields(raw).values():
        for value in values:
            if _looks_like_image(value):
                return value
            found = _find_image_field(value, depth + 1)
            if found is not None:
                return found
    return None


def parse_get_resource_response(raw: bytes, session_key_hex: str) -> bytes:
    """Decrypt a GetResourcesWithAuth response and return the exact image bytes.

    The decrypted plaintext wraps the image in a protobuf envelope alongside its
    URI (``{1: {1: <uri>, 2: <image>}}``); extract the image field precisely.
    Falls back to slicing from the image's magic bytes if the envelope shape is
    unexpected.
    """
    plaintext = decrypt_gcm_response(raw, session_key_hex)
    image = _find_image_field(plaintext)
    if image is not None:
        return image
    for magic in _IMAGE_MAGICS:
        start = plaintext.find(magic)
        if start >= 0:
            return plaintext[start:]
    return plaintext


def image_content_type(data: bytes) -> str:
    """Best-effort image MIME type from magic bytes (icons are WEBP in practice)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "application/octet-stream"
