from __future__ import annotations

import argparse
import json
import os
import sys

from .protocol import BadImage, parse_image


def _keypair(a):
    from bittensor_wallet import Wallet
    w = Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey, path=a.wallet_path)
    return w.hotkey


def cmd_commit(a) -> int:
    try:
        ref = parse_image(a.image)["ref"]
    except BadImage as e:
        raise SystemExit(f"bad image reference: {e}") from None
    if not a.no_check:
        from .chain import image_exists
        ok, why = image_exists(ref)
        if not ok:
            raise SystemExit(f"the image is not pullable: {why} (--no-check to commit anyway)")
    kp = _keypair(a)
    if a.dev_api:
        from .api import Api
        print(json.dumps(Api(a.dev_api, kp).dev_commit(ref)))
        return 0
    if a.netuid is None:
        raise SystemExit("--netuid is required")
    from .chain import commit
    r = commit(a.network, a.netuid, kp, ref)
    print(json.dumps(r))
    return 0 if r["ok"] else 1


def cmd_status(a) -> int:
    from .api import Api
    hotkey = a.hotkey or _keypair(a).ss58_address
    print(json.dumps(Api(a.api).status(hotkey, a.rounds), indent=1))
    return 0


def cmd_eval(a) -> int:
    try:
        from vector_env import harness
        from vector_env.cli import evaluate
    except ImportError:
        raise SystemExit("vector-miner eval needs vector-env (scripts/setup.sh installs it)") from None
    if a.command:
        import shlex
        expert = {"command": shlex.split(a.command)}
    elif a.image:
        expert = {"image": a.image}
    else:
        raise SystemExit("give --image REF or --command \"...\"")
    docker = {k: v for k, v in (("cpus", a.cpus), ("memory", a.memory), ("binary", a.docker)) if v is not None}
    if a.image and a.pull:
        harness.pull(a.image, docker)
    from vector_env.assets import AssetsError
    try:
        r = evaluate(expert, a.scenes, a.seed, workers=a.workers, time_limit_s=a.time_limit, wall_s=a.wall, docker=docker)
    except AssetsError as e:
        print(f"vector-miner: {e}", file=sys.stderr)
        return 2
    if a.results:
        with open(a.results, "w") as f:
            for x in r["results"]:
                f.write(json.dumps(x, separators=(",", ":")) + "\n")
    print(json.dumps(r["summary"]))
    return 0


def cmd_verify(a) -> int:
    from .verify import VerifyError, verify
    if a.command and not a.image:
        raise SystemExit("--command replaces an evaluated image: give --image REF too")
    if a.doc:
        with open(a.doc) as f:
            doc = json.load(f)
        if int(doc.get("round", -1)) != a.round:
            raise SystemExit(f"{a.doc} is round {doc.get('round')}, not {a.round}")
    else:
        if not a.api:
            raise SystemExit("--api (or VECTOR_MINER_API) is required without --doc")
        from .api import Api, ApiError
        try:
            doc = Api(a.api).round_verify(a.round)
        except ApiError as e:
            print(f"vector-miner: round {a.round}: {e.detail}" if e.status == 404 else f"vector-miner: {e}", file=sys.stderr)
            return 2
    if a.save:
        with open(a.save, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
    expert = None
    if a.command:
        import shlex
        expert = {"command": shlex.split(a.command)}
    docker = {k: v for k, v in (("cpus", a.cpus), ("memory", a.memory), ("binary", a.docker)) if v is not None}
    try:
        if a.pull and a.image and not a.command:
            from .verify import find_image, load_env
            ref = find_image(doc, a.image).get("run_image")
            if ref:
                load_env().harness.pull(ref, docker)
        out = verify(doc, hash_scenes=a.hash_scenes, image=a.image, expert=expert, rerun_scenes=a.scenes,
                     sample_seed=a.sample_seed, wall_s=a.wall, workers=a.workers, docker=docker)
    except VerifyError as e:
        print(f"vector-miner: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    return 0 if out["ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vector-miner")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--wallet.name", dest="wallet_name", default="default")
    common.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    common.add_argument("--wallet.path", dest="wallet_path", default=os.path.expanduser("~/.bittensor/wallets"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("commit", parents=[common], help="commit your image on chain")
    c.add_argument("--image", required=True)
    c.add_argument("--network", default="finney")
    c.add_argument("--netuid", type=int)
    c.add_argument("--no-check", action="store_true", help="do not ask the registry whether the image exists")
    c.add_argument("--dev-api", help="a local dev API (no chain): commit there instead")
    s = sub.add_parser("status", parents=[common], help="your submissions, their scores and the king")
    s.add_argument("--api", default=os.environ.get("VECTOR_MINER_API"), required=not os.environ.get("VECTOR_MINER_API"))
    s.add_argument("--hotkey", help="an ss58 address instead of the wallet's hotkey")
    s.add_argument("--rounds", type=int, default=3)
    e = sub.add_parser("eval", help="local evaluation: random scenes through the same step harness the subnet uses")
    e.add_argument("--image", help="your image (any local tag or ghcr.io/<you>/<repo>@sha256:<digest>)")
    e.add_argument("--command", help="run a local program instead of the image (while developing)")
    e.add_argument("--scenes", type=int, default=20)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--workers", type=int, default=1)
    e.add_argument("--time-limit", type=float)
    e.add_argument("--wall", type=float)
    e.add_argument("--cpus", type=float)
    e.add_argument("--memory")
    e.add_argument("--docker", help="the docker binary")
    e.add_argument("--pull", action="store_true")
    e.add_argument("--results", help="write the per-scene results here (JSON lines)")
    v = sub.add_parser("verify", help="re-check a final round: regenerate its scenes from the published salt, "
                                      "check the results, optionally re-run an evaluated image")
    v.add_argument("--round", type=int, required=True)
    v.add_argument("--api", default=os.environ.get("VECTOR_MINER_API"))
    v.add_argument("--doc", help="read a saved verify document instead of fetching it")
    v.add_argument("--save", help="write the fetched document here")
    v.add_argument("--image", help="re-run this evaluated image (its reference, digest or hotkey) on --scenes scenes")
    v.add_argument("--command", help="with --image: run a local program in its place (no docker)")
    v.add_argument("--scenes", type=int, default=20, help="scenes to re-run the image on (sampled)")
    v.add_argument("--hash-scenes", type=int, help="regenerate only this many sampled scenes (default: all)")
    v.add_argument("--sample-seed", type=int, default=0)
    v.add_argument("--workers", type=int, default=1)
    v.add_argument("--wall", type=float)
    v.add_argument("--cpus", type=float)
    v.add_argument("--memory")
    v.add_argument("--docker", help="the docker binary")
    v.add_argument("--pull", action="store_true")
    a = ap.parse_args(argv)
    return {"commit": cmd_commit, "status": cmd_status, "eval": cmd_eval, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
