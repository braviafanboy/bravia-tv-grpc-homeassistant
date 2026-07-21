"""Nonce-gated, AES-256-GCM-encrypted ``system_setting.application_list`` read.

The full installed-app list is a nonce-gated ``GetStatesWithAuth`` read:

- Request: the ``GetNonce`` nonce goes into the embedded auth block's field-2
  slot (between ``session_random`` / field 1 and ``session_id`` / field 3) and
  is included in the signed HMAC preimage. Any other placement -> INVALID_ARGUMENT.
- Response: the payload is AES-256-GCM encrypted with the **session_key** (a
  distinct credential from the ``hmac_key`` used for signing). The response
  carries the ciphertext, a 12-byte GCM nonce and a 16-byte tag together:
  ``{ ciphertext, gcm_nonce[12], gcm_tag[16], session_id }``.

Plaintext is protobuf wrapping the JSON app list:
``[{"id": <package>, "label": <name>, "resources": [...]}, ...]``.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .wire import length_delimited, parse_fields

APPLICATION_LIST_PATH = "system_setting.application_list"


def _hmac_key_bytes(hmac_key_hex: str) -> bytes:
    if len(hmac_key_hex) == 64:
        return bytes.fromhex(hmac_key_hex)
    return hmac_key_hex.encode("utf-8")[:32].ljust(32, b"\x00")


def parse_get_nonce_response(raw: bytes) -> bytes:
    """Return the nonce (field 1) from a GetNonce response (64 bytes on TVs)."""
    fields = parse_fields(raw)
    if 1 not in fields or not fields[1]:
        raise ValueError("GetNonce response has no nonce")
    return fields[1][0]


def build_nonce_gated_request(
    path: str,
    *,
    session_random: bytes,
    session_id: str,
    hmac_key_hex: str,
    nonce: bytes,
) -> bytes:
    """Build a nonce-gated, signed request body for *path*.

    Shared by the ``GetStatesWithAuth`` application_list read and the
    ``GetResourcesWithAuth`` icon read: same wire shape (path + an embedded auth
    block carrying session_random / nonce / session_id, HMAC-signed), only the
    gRPC method and *path* differ.
    """
    nested = length_delimited(1, length_delimited(1, path.encode()))
    embedded = (
        length_delimited(1, session_random)
        + length_delimited(2, nonce)
        + length_delimited(3, session_id.encode())
    )
    preimage = nested + length_delimited(2, embedded)
    auth = hmac.new(_hmac_key_bytes(hmac_key_hex), preimage, hashlib.sha256).digest()
    return length_delimited(1, preimage) + length_delimited(2, auth)


def build_application_list_request(
    *, session_random: bytes, session_id: str, hmac_key_hex: str, nonce: bytes
) -> bytes:
    """Build the nonce-gated GetStatesWithAuth request for application_list."""
    return build_nonce_gated_request(
        APPLICATION_LIST_PATH,
        session_random=session_random,
        session_id=session_id,
        hmac_key_hex=hmac_key_hex,
        nonce=nonce,
    )


def _find_encrypted_block(raw: bytes) -> tuple[bytes, bytes, bytes]:
    """Locate (ciphertext, gcm_nonce[12], gcm_tag[16]) within the response."""
    for values in parse_fields(raw).values():
        for blob in values:
            inner = parse_fields(blob)
            flat = [v for vs in inner.values() for v in vs]
            nonce12 = next((v for v in flat if len(v) == 12), None)
            tag16 = next((v for v in flat if len(v) == 16), None)
            ciphertext = max((v for v in flat if len(v) > 32), key=len, default=None)
            if nonce12 and tag16 and ciphertext:
                return ciphertext, nonce12, tag16
    raise ValueError("encrypted application_list block not found in response")


def decrypt_gcm_response(raw: bytes, session_key_hex: str) -> bytes:
    """Locate and AES-256-GCM-decrypt the encrypted block in a nonce-gated
    response, returning the plaintext. Shared by the app_list read (plaintext is
    protobuf-wrapped JSON) and the icon read (plaintext is the raw image)."""
    ciphertext, gcm_nonce, gcm_tag = _find_encrypted_block(raw)
    key = bytes.fromhex(session_key_hex)
    return AESGCM(key).decrypt(gcm_nonce, ciphertext + gcm_tag, None)


def parse_application_list_response(raw: bytes, session_key_hex: str) -> list[dict]:
    """Decrypt and parse the app list -> ``[{"id", "label", "resources"}, ...]``."""
    plaintext = decrypt_gcm_response(raw, session_key_hex)
    idx = plaintext.find(b"[{")
    if idx < 0:
        return []
    obj, _ = json.JSONDecoder().raw_decode(
        plaintext[idx:].decode("utf-8", "surrogatepass")
    )
    return obj if isinstance(obj, list) else []
