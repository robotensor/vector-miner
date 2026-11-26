from __future__ import annotations

import hashlib

import pytest
from bittensor_wallet import Keypair

from vector_miner import protocol


REF = "ghcr.io/alice/bin-expert@sha256:" + "a" * 64
D = "sha256:" + "a" * 64


def test_image_rules_match_the_subnet():
    assert protocol.REGISTRIES == ("ghcr.io",) and protocol.MAX_LEN == 128
    img = protocol.parse_image(f"ghcr.io/alice/bin-expert@{D}")
    assert img == {"ref": f"ghcr.io/alice/bin-expert@{D}", "host": "ghcr.io", "repo": "alice/bin-expert", "digest": D}
    assert protocol.parse_image(f"  ghcr.io/a/b/c@{D} ")["ref"] == f"ghcr.io/a/b/c@{D}"
    for bad in ("ghcr.io/alice/bin-expert:latest", "ghcr.io/alice/bin-expert", f"alice/bin-expert@{D}",
                f"ghcr.io/Alice/x@{D}", "ghcr.io/a/x@sha256:abc", f"ghcr.io/a/x:v1@{D}", f"ghcr.io/a/x@sha256:{'A' * 64}",
                f"ghcr.io/a//x@{D}", f"ghcr.io/a/x-@{D}", ""):
        with pytest.raises(protocol.BadImage):
            protocol.parse_image(bad)
    for host in ("docker.io", "ghcr.io:443", "GHCR.io", "quay.io"):
        with pytest.raises(protocol.BadImage):
            protocol.parse_image(f"{host}/alice/x@{D}")
    with pytest.raises(protocol.BadImage, match="not accepted"):
        protocol.parse_image(f"docker.io/alice/x@{D}")
    fill = 128 - len(f"ghcr.io//x@{D}")
    assert len(protocol.parse_image(f"ghcr.io/{'a' * fill}/x@{D}")["ref"]) == 128
    with pytest.raises(protocol.BadImage, match="longer than 128"):
        protocol.parse_image(f"ghcr.io/{'a' * (fill + 1)}/x@{D}")


def test_signed_request():
    kp = Keypair.create_from_uri("//Alice")
    body = b'{"x":1}'
    h = protocol.sign_headers(kp, "post", "/v1/expert/work", body, nonce="1700000000000")
    msg = f"vector-v1\nPOST\n/v1/expert/work\n{hashlib.sha256(body).hexdigest()}\n1700000000000".encode()
    assert protocol.message("POST", "/v1/expert/work", body, "1700000000000") == msg
    assert h["x-vector-hotkey"] == kp.ss58_address and h["x-vector-nonce"] == "1700000000000"
    assert kp.verify(msg, bytes.fromhex(h["x-vector-signature"]))


def test_commitment_payload():
    info = protocol.commitment_info(REF)
    n = len(REF.encode())
    assert info == {"fields": [{f"Raw{n}": "0x" + REF.encode().hex()}]}
    with pytest.raises(protocol.BadImage):
        protocol.commitment_info("x" * 129)
    bt = pytest.importorskip("bittensor")
    if not hasattr(bt, "intents"):
        pytest.skip("bittensor < 11")
    from vector_miner.chain import commit_call
    c = commit_call(7, REF)
    assert (c.module, c.function) == ("Commitments", "set_commitment") and c.params == {"netuid": 7, "info": info}


def test_the_connect_hello_is_signed():
    kp = Keypair.create_from_uri("//Alice")
    assert protocol.connect_message(REF, "n-1") == f"vector-connect\n{REF}\nn-1".encode()
    h = protocol.hello(kp, REF, "n-1")
    assert {k: h[k] for k in ("type", "hotkey", "image", "nonce")} == {"type": "hello", "hotkey": kp.ss58_address,
                                                                       "image": REF, "nonce": "n-1"}
    assert kp.verify(protocol.connect_message(REF, "n-1"), bytes.fromhex(h["signature"]))
    assert not kp.verify(protocol.connect_message(REF, "n-2"), bytes.fromhex(h["signature"]))
