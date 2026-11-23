from __future__ import annotations

import re
import subprocess

import httpx

from .protocol import commitment_info, parse_image

ACCEPT = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json"])


def _token(client: httpx.Client, challenge: str) -> str | None:
    kv = dict(re.findall(r'(\w+)="([^"]*)"', challenge or ""))
    if "realm" not in kv:
        return None
    r = client.get(kv["realm"], params={k: v for k, v in kv.items() if k in ("service", "scope")})
    if r.status_code != 200:
        return None
    j = r.json()
    return j.get("token") or j.get("access_token")


ANON_REFUSED = "the registry refused an anonymous pull token (the image is private, or there is no such image)"


def image_exists(ref: str, timeout: float = 20.0) -> tuple[bool, str]:
    img = parse_image(ref)
    url = f"https://{img['host']}/v2/{img['repo']}/manifests/{img['digest']}"
    with httpx.Client(timeout=timeout, follow_redirects=True) as c:
        try:
            r = c.head(url, headers={"accept": ACCEPT})
            if r.status_code == 401:
                tok = _token(c, r.headers.get("www-authenticate", ""))
                if not tok:
                    return False, ANON_REFUSED
                r = c.head(url, headers={"accept": ACCEPT, "authorization": f"Bearer {tok}"})
        except httpx.HTTPError as e:
            return False, f"registry unreachable: {e}"
    if r.status_code == 200:
        got = r.headers.get("docker-content-digest")
        if got and got != img["digest"]:
            return False, f"the registry answered another digest ({got})"
        return True, ""
    if r.status_code == 404:
        return False, "no such image at that digest"
    return False, f"registry answered HTTP {r.status_code}"


def own_pull(ref: str, docker: str = "docker", timeout: float = 60.0) -> bool:
    """Whether this machine's docker login (DOCKER_CONFIG) can read the image's manifest."""
    try:
        p = subprocess.run([docker, "manifest", "inspect", ref], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0


def image_access(ref: str, timeout: float = 20.0) -> tuple[str, str]:
    """'public' (anyone can pull it), 'private' (anonymous pulls refused, your own docker login can pull it: the
    subnet can pull it only once the package is shared with its team account) or 'missing' (with the reason)."""
    ok, why = image_exists(ref, timeout)
    if ok:
        return "public", ""
    if (why == ANON_REFUSED or why.startswith("no such image") or why in ("registry answered HTTP 401",
                                                                          "registry answered HTTP 403")) \
            and own_pull(parse_image(ref)["ref"]):
        return "private", why
    return "missing", why


def commit_call(netuid: int, ref: str):
    from bittensor._generated.calls import Commitments
    return Commitments.set_commitment(netuid=int(netuid), info=commitment_info(parse_image(ref)["ref"]))


def commit(network: str, netuid: int, hotkey_keypair, ref: str) -> dict:
    import bittensor as bt
    sub = bt.Subtensor(network=network)
    res = sub.submit_call(commit_call(netuid, ref), hotkey_keypair)
    ok = getattr(res, "success", None)
    return {"ok": bool(res) if ok is None else bool(ok), "result": str(res)[:500]}


def read_commitment(network: str, netuid: int, hotkey: str):
    import bittensor as bt
    c = bt.Subtensor(network=network).read("commitment", netuid=int(netuid), hotkey_ss58=hotkey)
    return None if c is None else {"data": getattr(c, "data", None), "block": getattr(c, "block", None)}
