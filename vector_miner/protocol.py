from __future__ import annotations

import hashlib
import re
import time

REGISTRIES = ("ghcr.io",)
MAX_LEN = 128
REF = re.compile(r"^(?P<host>[a-z0-9.-]+(?::\d+)?)/(?P<repo>[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)"
                 r"@(?P<digest>sha256:[0-9a-f]{64})$")


class BadImage(ValueError):
    pass


def parse_image(ref: str) -> dict:
    ref = (ref or "").strip()
    if len(ref) > MAX_LEN:
        raise BadImage(f"longer than {MAX_LEN} characters")
    m = REF.match(ref)
    if not m:
        raise BadImage("not <registry>/<repo>@sha256:<digest> (pin the image by its digest)")
    if m["host"] not in REGISTRIES:
        raise BadImage(f"registry {m['host']} is not accepted (accepted: {', '.join(REGISTRIES)})")
    return {"ref": ref, "host": m["host"], "repo": m["repo"], "digest": m["digest"]}


def message(method: str, path: str, body: bytes, nonce: str) -> bytes:
    return "\n".join(["vector-v1", method.upper(), path, hashlib.sha256(body).hexdigest(), nonce]).encode()


def sign_headers(keypair, method: str, path: str, body: bytes, nonce: str | None = None) -> dict:
    nonce = nonce or str(int(time.time() * 1000))
    return {"x-vector-hotkey": keypair.ss58_address, "x-vector-nonce": nonce,
            "x-vector-signature": keypair.sign(message(method, path, body, nonce)).hex(),
            "content-type": "application/json"}


def commitment_info(data: str) -> dict:
    b = data.encode()
    if not 0 < len(b) <= MAX_LEN:
        raise BadImage(f"a commitment holds 1..{MAX_LEN} bytes")
    return {"fields": [{f"Raw{len(b)}": "0x" + b.hex()}]}


def connect_message(image: str, nonce: str) -> bytes:
    return f"vector-connect\n{image}\n{nonce}".encode()


def hello(keypair, image: str, nonce: str) -> dict:
    return {"type": "hello", "hotkey": keypair.ss58_address, "image": image, "nonce": nonce,
            "signature": keypair.sign(connect_message(image, nonce)).hex()}
