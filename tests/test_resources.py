"""Unit tests for the app-icon (GetResourcesWithAuth) codec."""

from __future__ import annotations

import os

from btvgrpc import resources
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pb_helpers import ld

# A minimal valid-looking WEBP header (RIFF....WEBP) + payload.
_WEBP = b"RIFF" + (28).to_bytes(4, "little") + b"WEBP" + b"VP8X" + b"\x00" * 20
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _gcm_response(plaintext: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    combined = AESGCM(key).encrypt(nonce, plaintext, None)
    ciphertext, tag = combined[:-16], combined[-16:]
    return ld(3, ld(1, ciphertext) + ld(2, nonce) + ld(3, tag))


def test_image_content_type():
    assert resources.image_content_type(_WEBP) == "image/webp"
    assert resources.image_content_type(_PNG) == "image/png"
    assert resources.image_content_type(_JPEG) == "image/jpeg"
    assert resources.image_content_type(b"nope") == "application/octet-stream"


def test_find_image_field_extracts_exact_bytes():
    # Envelope: {1: {1: <uri>, 2: <image>}} with trailing junk after the image
    # field, as the TV sends — the exact image field must come back, no trailing.
    inner = ld(1, b"iot-resource://app-icon/abc") + ld(2, _WEBP) + ld(3, b"trail")
    envelope = ld(1, inner)
    assert resources._find_image_field(envelope) == _WEBP


def test_parse_get_resource_response_decrypts_and_extracts():
    key = os.urandom(32)
    inner = ld(1, b"iot-resource://app-icon/xyz") + ld(2, _WEBP)
    plaintext = ld(1, inner)
    raw = _gcm_response(plaintext, key)
    assert resources.parse_get_resource_response(raw, key.hex()) == _WEBP


def test_build_get_resource_request_uses_uri_as_path():
    kw = {
        "session_random": b"S" * 64,
        "session_id": "s",
        "hmac_key_hex": "ef" * 32,
        "nonce": b"N" * 64,
    }
    uri = "iot-resource://app-icon/deadbeef"
    from btvgrpc.application_list import build_nonce_gated_request

    assert resources.build_get_resource_request(uri=uri, **kw) == (
        build_nonce_gated_request(uri, **kw)
    )
