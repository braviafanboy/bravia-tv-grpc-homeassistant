"""Unit tests for the nonce-gated application_list request/response codec."""

from __future__ import annotations

import hashlib
import hmac
import os

from btvgrpc import application_list as al
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pb_helpers import ld


def _gcm_response(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt and wrap plaintext the way the TV frames a nonce-gated response."""
    nonce = os.urandom(12)
    combined = AESGCM(key).encrypt(nonce, plaintext, None)  # ciphertext + 16B tag
    ciphertext, tag = combined[:-16], combined[-16:]
    block = ld(1, ciphertext) + ld(2, nonce) + ld(3, tag)
    return ld(3, block)


def test_parse_get_nonce_response():
    nonce = b"N" * 64
    assert al.parse_get_nonce_response(ld(1, nonce)) == nonce


def test_build_nonce_gated_request_is_signed_correctly():
    sr, sid, nonce = b"S" * 64, "sess-id", b"N" * 64
    key_hex = "ab" * 32
    req = al.build_nonce_gated_request(
        "system_setting.application_list",
        session_random=sr,
        session_id=sid,
        hmac_key_hex=key_hex,
        nonce=nonce,
    )
    nested = ld(1, ld(1, b"system_setting.application_list"))
    embedded = ld(1, sr) + ld(2, nonce) + ld(3, sid.encode())
    preimage = nested + ld(2, embedded)
    auth = hmac.new(bytes.fromhex(key_hex), preimage, hashlib.sha256).digest()
    assert req == ld(1, preimage) + ld(2, auth)


def test_application_list_request_delegates_to_nonce_gated():
    kw = {
        "session_random": b"S" * 64,
        "session_id": "s",
        "hmac_key_hex": "cd" * 32,
        "nonce": b"N" * 64,
    }
    assert al.build_application_list_request(**kw) == al.build_nonce_gated_request(
        al.APPLICATION_LIST_PATH, **kw
    )


def test_decrypt_gcm_response_roundtrip():
    key = os.urandom(32)
    plaintext = b"the quick brown fox jumps over the lazy dog!!"  # > 32 bytes
    raw = _gcm_response(plaintext, key)
    assert al.decrypt_gcm_response(raw, key.hex()) == plaintext


def test_parse_application_list_response_extracts_json():
    key = os.urandom(32)
    # Protobuf-ish wrapper bytes followed by the JSON app list, as the TV sends.
    plaintext = b"\x0a\x05stuff" + (
        b'[{"id":"com.netflix.ninja","label":"Netflix"},'
        b'{"id":"tv.twitch.android.app","label":"Twitch"}]'
    )
    raw = _gcm_response(plaintext, key)
    apps = al.parse_application_list_response(raw, key.hex())
    assert apps == [
        {"id": "com.netflix.ninja", "label": "Netflix"},
        {"id": "tv.twitch.android.app", "label": "Twitch"},
    ]
