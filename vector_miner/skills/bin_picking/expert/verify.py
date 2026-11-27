from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import random
import sys
from types import SimpleNamespace

MAX_DOC_BYTES = 64 << 20
MAX_SCENES = 100_000
MAX_TRIES = 64
MAX_STR = 256
ENV_KEYS = ("version", "assets_sha256")


class VerifyError(Exception):
    pass


def load_env() -> SimpleNamespace:
    try:
        from vector_env import harness, scenes
        from vector_env.settings import env_config
    except ImportError:
        raise VerifyError("regenerating scenes needs vector-env (scripts/setup.sh installs it)") from None
    try:
        wall_s = float(env_config()["harness"]["wall_s"])
    except Exception as e:
        raise VerifyError(f"vector-env's settings do not load: {e}") from None
    return SimpleNamespace(scenes=scenes, harness=harness, wall_s=wall_s)


def _no_constant(name: str):
    raise ValueError(f"{name} is not a JSON number")


def loads(raw: bytes | str) -> dict:
    if len(raw) > MAX_DOC_BYTES:
        raise VerifyError(f"the document is larger than {MAX_DOC_BYTES} bytes")
    try:
        doc = json.loads(raw, parse_constant=_no_constant)
    except (ValueError, RecursionError) as e:
        raise VerifyError(f"the document is not JSON: {e}") from None
    if not isinstance(doc, dict):
        raise VerifyError("the document is not a verify document (not a JSON object)")
    return doc


def read_file(path: str) -> dict:
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_DOC_BYTES + 1)
    except OSError as e:
        raise VerifyError(f"cannot read {path}: {e}") from None
    try:
        return loads(raw)
    except VerifyError as e:
        raise VerifyError(f"cannot read {path}: {e}") from None


def _bad(where: str, why: str) -> VerifyError:
    return VerifyError(f"malformed verify document: {where} {why}")


def _int(v, where: str, lo: int = 0, hi: int = 1 << 63, null: bool = False) -> None:
    if v is None and null:
        return
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise _bad(where, f"is not an integer in {lo}..{hi}")


def _num(v, where: str) -> None:
    if v is None:
        return
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
        raise _bad(where, "is not a finite number")


def _str(v, where: str, n: int = MAX_STR, null: bool = True) -> None:
    if v is None and null:
        return
    if not isinstance(v, str) or len(v) > n:
        raise _bad(where, f"is not a string of at most {n} characters")


def _flag(v, where: str) -> None:
    if v is not None and not isinstance(v, bool):
        raise _bad(where, "is not true or false")


def _obj(v, where: str, null: bool = True) -> dict:
    if v is None and null:
        return {}
    if not isinstance(v, dict):
        raise _bad(where, "is not an object")
    return v


def _arr(v, where: str, n: int, null: bool = False) -> list:
    if v is None and null:
        return []
    if not isinstance(v, list) or len(v) > n:
        raise _bad(where, f"is not a list of at most {n} items")
    return v


def _time(v, where: str) -> None:
    _str(v, where, 64)
    if v is not None:
        try:
            dt.datetime.fromisoformat(v)
        except ValueError:
            raise _bad(where, "is not an ISO time") from None


def _env(v, where: str) -> None:
    for k, x in _obj(v, where).items():
        _str(x, f"{where}.{k}")


def _log(*x) -> None:
    print(*x, file=sys.stderr, flush=True)


def base_seed(salt: str, n: int, index: int) -> int:
    return int.from_bytes(hashlib.sha256(f"{salt}:{int(n)}:{int(index)}".encode()).digest()[:6], "big")


def scenes_digest(rows: list[dict]) -> str:
    return hashlib.sha256("\n".join(f"{x['index']}:{x['seed']}:{x['state_hash']}" for x in rows).encode()).hexdigest()


def sample_order(count: int, seed: int) -> list[int]:
    return random.Random(int(seed)).sample(range(count), count)


def env_info(e: SimpleNamespace) -> dict:
    try:
        got = e.scenes.env_info()
    except Exception as ex:
        raise VerifyError(f"vector-env cannot describe its asset bundle: {ex}") from None
    return got if isinstance(got, dict) else {}


def same_env(local: dict, want: dict) -> bool:
    return all(local.get(k) == want.get(k) for k in ENV_KEYS if want.get(k) is not None)


def _draw(e: SimpleNamespace, seed0: int, tries: int, draw) -> tuple[dict | None, str | None]:
    why = None
    for k in range(tries):
        try:
            return e.scenes.generate(seed0 + k, draw), None
        except Exception as ex:
            why = f"{type(ex).__name__}: {ex}"[:500]
    return None, why


def _without_bury(e: SimpleNamespace, seed: int, draw) -> tuple[dict | None, str | None]:
    """A scene the subnet made again from the same seed without the bury (its row says `bury: false`)."""
    try:
        st = dict(e.scenes.settings_for(int(seed), draw))
        st.pop("bury", None)
        return e.scenes.generate(int(seed), draw, settings=st), None
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"[:500]


def _generator_works(e: SimpleNamespace, rows: dict, failed: set, draw) -> tuple[bool, str | None]:
    probe = next((x for i, x in sorted(rows.items()) if i not in failed), None)
    if probe is None:
        return False, "no other published scene to test the generator on"
    try:
        e.scenes.generate(int(probe["seed"]), draw)
        return True, None
    except Exception as ex:
        return False, f"{type(ex).__name__}: {ex}"[:500]


def regenerate(doc: dict, e: SimpleNamespace, indexes: list[int], log=_log, local: dict | None = None,
               skipped: list[int] = (), n: int = 0) -> dict:
    salt, draw = doc["salt"], doc.get("draw")
    tries = doc.get("seed_tries") or 6
    rows = {x["index"]: x for x in doc["scenes"]}
    top = doc.get("env") or {}
    local = env_info(e) if local is None else local
    made, bad, other, failed, works = {}, [], [], set(), False

    def check_works(why):
        nonlocal works
        if works or made:
            works = True
            return
        works, err = _generator_works(e, rows, failed, draw)
        if not works:
            raise VerifyError(f"the scene generator does not work here ({why}; a published scene fails too: {err}): "
                              "check vector-env, node and the asset bundle")

    for j, i in enumerate(indexes):
        x = rows[i]
        want = x.get("env") or top
        if x.get("bury") is False:
            sc, why = _without_bury(e, x["seed"], draw)
        else:
            sc, why = _draw(e, base_seed(salt, n, i), tries, draw)
        row = {"index": i, "seed": x["seed"], "state_hash": x["state_hash"]}
        if sc is None:
            failed.add(i)
            check_works(why)
            row["error"] = why
        elif not isinstance(sc, dict) or sc.get("seed") != x["seed"] or sc.get("state_hash") != x["state_hash"]:
            row.update(got_seed=sc.get("seed") if isinstance(sc, dict) else None,
                       got_state_hash=sc.get("state_hash") if isinstance(sc, dict) else None)
        else:
            made[i] = sc
            row = None
        tag = "ok"
        if row is not None and same_env(local, want):
            bad.append(row)
            tag = "MISMATCH"
        elif row is not None:
            other.append({**row, "env": want})
            tag = "differs (made with another vector-env)"
        log(f"scene {j + 1}/{len(indexes)}: index {i} {tag}")
    for i in skipped:
        sc, _ = _draw(e, base_seed(salt, n, i), tries, draw)
        if sc is None:
            continue
        row = {"index": i, "skipped": True, "got_seed": sc.get("seed"), "got_state_hash": sc.get("state_hash")}
        (bad if same_env(local, top) else other).append(row)
        log(f"skipped scene {i}: composes a valid scene here")
    return {"checked": len(indexes), "matched": len(made), "mismatched": bad, "unverified": other,
            "skipped_checked": len(skipped), "scenes": made}


def exit_code(out: dict) -> int:
    if out["document"]["problems"] or (out.get("scenes") or {}).get("mismatched"):
        return 1
    return 0 if out["ok"] else 2
