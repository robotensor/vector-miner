from __future__ import annotations

import json

import httpx

from .protocol import sign_headers

SKILL = "bin-picking"
MAX_BYTES = 64 << 20


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


def make_client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout)


class Api:
    def __init__(self, base: str, keypair=None, client: httpx.Client | None = None, timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.kp = keypair
        self.http = client or make_client(timeout)

    def _check(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ApiError(r.status_code, str(detail))
        return r.json()

    def get(self, path: str, max_bytes: int = MAX_BYTES) -> dict:
        raw = self.get_bytes(path, max_bytes)
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            raise ApiError(200, "the response is not JSON") from None

    def post_signed(self, path: str, obj: dict | None = None) -> dict:
        body = json.dumps(obj or {}, separators=(",", ":")).encode()
        return self._check(self.http.post(self.base + path, content=body, headers=sign_headers(self.kp, "POST", path, body)))

    def expert(self) -> dict:
        return self.get(f"/v1/skills/{SKILL}/expert")

    def round(self, n: int) -> dict:
        return self.get(f"/v1/skills/{SKILL}/expert/rounds/{int(n)}")

    def get_bytes(self, path: str, max_bytes: int) -> bytes:
        body = bytearray()
        with self.http.stream("GET", self.base + path) as r:
            try:
                size = int(r.headers.get("content-length") or 0)
            except ValueError:
                size = 0
            if size > max_bytes:
                raise ApiError(r.status_code, f"the response is larger than {max_bytes} bytes")
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise ApiError(r.status_code, f"the response is larger than {max_bytes} bytes")
        if r.status_code >= 400:
            try:
                detail = json.loads(bytes(body)).get("detail", body.decode(errors="replace"))
            except (ValueError, AttributeError):
                detail = body.decode(errors="replace")
            raise ApiError(r.status_code, str(detail)[:2000])
        return bytes(body)

    def round_verify(self, n: int, max_bytes: int) -> bytes:
        return self.get_bytes(f"/v1/skills/{SKILL}/expert/rounds/{int(n)}/verify", max_bytes)

    def status(self, hotkey: str, rounds: int = 3) -> dict:
        ex = self.expert()
        cur = (ex.get("current") or {}).get("n")
        nums = [cur] if cur is not None else []
        nums += [r["n"] for r in ex.get("rounds") or [] if r.get("n") != cur]
        out = []
        for n in nums[:rounds]:
            rd = self.round(n)
            for s in rd.get("submissions") or []:
                if s.get("hotkey") == hotkey:
                    out.append({"round": n, **s})
        king = ex.get("king") or {}
        return {"hotkey": hotkey, "king": king.get("hotkey") == hotkey, "king_score": king.get("score"),
                "current": ex.get("current"), "submissions": out}

    def dev_commit(self, image: str) -> dict:
        return self.post_signed("/v1/dev/commit", {"image": image})
