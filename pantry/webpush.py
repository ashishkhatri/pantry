"""Web Push without extra packages: VAPID (RFC 8292) + aes128gcm payload encryption (RFC 8291).

The browser gives us a subscription {endpoint, keys: {p256dh, auth}}. To notify it we
encrypt the JSON payload for that browser's key and POST it to the endpoint (Google's
FCM for Chrome on Android), signed with our VAPID key so the push service knows the
message came from this server.

VAPID keys are generated once and kept in data/vapid_private.pem.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class PushError(Exception):
    def __init__(self, message: str, gone: bool = False):
        super().__init__(message)
        self.gone = gone      # the subscription no longer exists; delete it


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, ua_public_b64: str, auth_b64: str, salt: Optional[bytes] = None,
            as_private: Optional[ec.EllipticCurvePrivateKey] = None, record_size: int = 4096) -> bytes:
    """RFC 8291 message encryption. ``salt`` and ``as_private`` are only passed by tests."""
    ua_public = b64u_decode(ua_public_b64)
    auth = b64u_decode(auth_b64)
    as_private = as_private or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(as_private.public_key())
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    ecdh = as_private.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth, ecdh, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) + 17 > record_size:
        raise PushError("Notification payload too large.")
    ct = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!IB", record_size, len(as_public)) + as_public
    return header + ct


# ---------------- VAPID ----------------

class Vapid:
    def __init__(self, key: ec.EllipticCurvePrivateKey, subject: str):
        self.key = key
        self.subject = subject

    @classmethod
    def load_or_create(cls, path: str, subject: str = "mailto:pantry@localhost") -> "Vapid":
        p = Path(path)
        if p.exists():
            key = serialization.load_pem_private_key(p.read_bytes(), password=None)
        else:
            key = ec.generate_private_key(ec.SECP256R1())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        return cls(key, subject)

    @property
    def public_key_b64(self) -> str:
        """The applicationServerKey the browser needs to subscribe."""
        return b64u(_raw_public(self.key.public_key()))

    def auth_header(self, endpoint: str, ttl: int = 12 * 3600) -> str:
        u = urllib.parse.urlsplit(endpoint)
        claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl, "sub": self.subject}
        signing_input = (b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()) + "." +
                         b64u(json.dumps(claims, separators=(",", ":")).encode()))
        der = self.key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"vapid t={signing_input}.{b64u(sig)}, k={self.public_key_b64}"


def send(vapid: Vapid, sub: dict, data: dict, ttl: int = 24 * 3600, urgency: str = "normal",
         timeout: float = 15) -> int:
    """Send one notification. ``sub`` has endpoint, p256dh, auth. Returns the HTTP status."""
    body = encrypt(json.dumps(data).encode("utf-8"), sub["p256dh"], sub["auth"])
    req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "Authorization": vapid.auth_header(sub["endpoint"]),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": urgency,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise PushError(f"Push service answered {e.code}: {detail}", gone=e.code in (404, 410)) from e
    except (urllib.error.URLError, OSError) as e:
        raise PushError(f"Couldn't reach the push service: {getattr(e, 'reason', e)}") from e
