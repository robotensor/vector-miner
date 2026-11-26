"""The API paths of bin-picking-expert."""
from __future__ import annotations

from urllib.parse import quote

from vector_miner.api import Api, ApiError

BASE = "/v1/skills/bin-picking/expert"


class ExpertApi(Api):
    def expert(self) -> dict:
        return self.get(BASE)

    def nonce(self) -> str:
        j = self.get("/v1/expert/nonce")
        n = j.get("nonce") if isinstance(j, dict) else None
        if not isinstance(n, str) or not n:
            raise ApiError(200, "the nonce response has no nonce")
        return n

    def submissions(self, hotkey: str) -> list[dict]:
        j = self.get(f"{BASE}/submissions?hotkey={quote(hotkey, safe='')}")
        rows = j.get("submissions") if isinstance(j, dict) else j
        if not isinstance(rows, list):
            raise ApiError(200, "the submissions response has no submissions list")
        return [x for x in rows if isinstance(x, dict) and x.get("hotkey") in (None, hotkey)]

    def submission(self, sid: str) -> dict:
        return self.get(f"{BASE}/submissions/{quote(str(sid), safe='')}")

    def submission_verify(self, sid: str, max_bytes: int) -> bytes:
        return self.get_bytes(f"{BASE}/submissions/{quote(str(sid), safe='')}/verify", max_bytes)

    def fee(self, hotkey: str) -> dict:
        j = self.get(f"{BASE}/fee?hotkey={quote(hotkey, safe='')}")
        if not isinstance(j, dict) or "fee" not in j:
            raise ApiError(200, "the fee response has no fee")
        return j

    def dev_commit(self, image: str) -> dict:
        return self.post_signed("/v1/dev/commit", {"image": image})

    def dev_fee_pay(self, tao: float | None = None) -> dict:
        return self.post_signed("/v1/dev/fee/pay", {} if tao is None else {"tao": tao})
