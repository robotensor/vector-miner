from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

from .protocol import BadImage, parse_image


DEFAULT_WALLETS = os.path.expanduser("~/.bittensor/wallets")


def btcli_create(name: str, hotkey: str, path: str) -> str:
    where = "" if os.path.abspath(os.path.expanduser(path)) == os.path.abspath(DEFAULT_WALLETS) else f" --wallet-path {path}"
    have_cold = any(os.path.exists(os.path.join(os.path.expanduser(path), name, f)) for f in ("coldkey", "coldkeypub.txt"))
    cold = "" if have_cold else f".venv/bin/btcli wallet new-coldkey --wallet {name}{where}, then "
    return f"{cold}.venv/bin/btcli wallet new-hotkey --wallet {name} --wallet-hotkey {hotkey}{where}"


def _keypair(a, alt: str = ""):
    from bittensor_wallet import Wallet
    from bittensor_wallet.errors import KeyFileError
    w = Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey, path=a.wallet_path)
    try:
        return w.hotkey
    except KeyFileError:
        raise SystemExit(f"vector-miner: no wallet hotkey {a.wallet_name}/{a.wallet_hotkey} in {a.wallet_path}: "
                         f"{alt}pass --wallet.name/--wallet.hotkey (and --wallet.path), or create it with "
                         f"{btcli_create(a.wallet_name, a.wallet_hotkey, a.wallet_path)}") from None


def _api_or_exit(a, what: str) -> str:
    api = getattr(a, "api", None) or getattr(a, "dev_api", None)
    if not api:
        raise SystemExit(f"vector-miner: --api (or VECTOR_MINER_API) is required: {what}")
    return api


def check_no_submission(api: str, hotkey: str) -> None:
    import httpx

    from . import board
    from .api import Api, ApiError
    try:
        rows = Api(api).submissions(hotkey)
    except ApiError as e:
        raise SystemExit(f"vector-miner: cannot check this hotkey's submissions at {api}: {e} "
                         "(--no-api-check commits without the check)") from None
    except httpx.HTTPError as e:
        raise SystemExit(f"vector-miner: cannot reach {api} to check this hotkey's submissions: {e} "
                         "(--no-api-check commits without the check)") from None
    s = board.existing(rows)
    if s is not None:
        raise SystemExit(board.refusal(hotkey, s))


def pull_account(api: str | None) -> str | None:
    """The subnet's team account that must be able to pull a private image (`pull_account` of the expert view)."""
    if not api:
        return None
    import httpx

    from .api import Api, ApiError
    try:
        got = Api(api, timeout=20.0).expert().get("pull_account")
    except (ApiError, httpx.HTTPError, AttributeError):
        return None
    return got if isinstance(got, str) and got else None


def private_warning(ref: str, api: str | None) -> str:
    team = pull_account(api)
    who = team or "the subnet's team account (`pull_account` in GET /v1/skills/bin-picking/expert)"
    how = f"Package settings -> Manage access -> add {team or 'that account'}, role Read"
    return (f"vector-miner: warning: {ref} is private (anonymous pulls are refused; your docker login can pull it). "
            f"The subnet pulls it as {who}: share the package with it (Read) before you commit, or the commit is "
            f"invalid. On ghcr.io: the package's {how}.")


def cmd_commit(a) -> int:
    try:
        ref = parse_image(a.image)["ref"]
    except BadImage as e:
        raise SystemExit(f"bad image reference: {e}") from None
    if not a.dev_api and a.netuid is None:
        raise SystemExit("--netuid is required")
    if not a.no_check:
        from .chain import image_access
        state, why = image_access(ref)
        if state == "missing":
            raise SystemExit(f"the image is not pullable: {why} (--no-check to commit anyway)")
        if state == "private":
            print(private_warning(ref, getattr(a, "api", None) or getattr(a, "dev_api", None)), file=sys.stderr)
    kp = _keypair(a)
    if not a.no_api_check:
        check_no_submission(_api_or_exit(a, "commit first checks that this hotkey has no submission "
                                            "(one per hotkey, ever; --no-api-check skips it)"), kp.ss58_address)
    if a.dev_api:
        from .api import Api
        r = Api(a.dev_api, kp).dev_commit(ref)
        print(json.dumps(r))
        _next_steps(ref, a.dev_api)
        return 0
    from .chain import commit
    r = commit(a.network, a.netuid, kp, ref)
    print(json.dumps(r))
    if r["ok"]:
        _next_steps(ref, a.api)
    return 0 if r["ok"] else 1


def _next_steps(ref: str, api: str | None) -> None:
    where = f" --api {api}" if api else ""
    print(f"vector-miner: committed. Phase 1 starts when your miner connects (within 48 h): "
          f"vector-miner serve --image {ref}{where}; follow it with vector-miner status{where}", file=sys.stderr)


def cmd_status(a) -> int:
    import httpx

    from . import board
    from .api import Api, ApiError
    hotkey = a.hotkey or _keypair(a, "pass --hotkey <ss58>, or ").ss58_address
    try:
        out = board.status(Api(a.api), hotkey)
    except ApiError as e:
        print(f"vector-miner: {a.api}: {e}", file=sys.stderr)
        return 2
    except httpx.HTTPError as e:
        print(f"vector-miner: cannot reach {a.api}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=1))
    for s in out["submissions"]:
        print(f"vector-miner: {s['id']}: {s['verdict']}", file=sys.stderr)
    for k in ("king_note", "note"):
        if out.get(k):
            print(f"vector-miner: {out[k]}", file=sys.stderr)
    return 0


def _bundle():
    try:
        from vector_env import assets
    except ImportError:
        return None
    try:
        return assets.find()
    except Exception:
        return None


def cmd_serve(a) -> int:
    from . import serve as sv
    try:
        ref = parse_image(a.image)["ref"]
    except BadImage as e:
        raise SystemExit(f"bad image reference: {e} (serve the image you committed)") from None
    if a.concurrency < 1 or (a.warm is not None and a.warm < 0):
        raise SystemExit("--concurrency must be at least 1 and --warm at least 0")
    try:
        api_url = sv.ws_url(_api_or_exit(a, "serve connects to the subnet's API"))
    except sv.ServeError as e:
        raise SystemExit(f"vector-miner: {e}") from None
    docker = {k: v for k, v in (("cpus", a.cpus), ("memory", a.memory), ("binary", a.docker)) if v is not None}
    if a.command:
        import shlex
        try:
            expert = {"command": shlex.split(a.command)}
        except ValueError as e:
            raise SystemExit(f"vector-miner: --command: {e}") from None
    else:
        try:
            from vector_env import harness
        except ImportError:
            raise SystemExit("vector-miner serve --image needs vector-env (scripts/setup.sh installs it)") from None
        try:
            harness.ensure_image(ref, docker, pull_missing=a.pull)
        except harness.HarnessError as e:
            hint = "; pull it first (--pull)" if e.outcome == "image_missing" else ""
            print(f"vector-miner: cannot run {ref}: {e}{hint}", file=sys.stderr)
            return 2
        expert = {"image": ref}
    bundle = _bundle()
    if bundle is None:
        print("vector-miner: warning: no asset bundle here, so the expert runs without /bundle "
              "(VECTOR_SIM_BUNDLE); see vector-env assets", file=sys.stderr)
    kp = _keypair(a)
    launcher = sv.Launcher(expert, docker, bundle, a.stderr)
    print(f"vector-miner: serving {ref} as {kp.ss58_address} to {api_url} ({a.concurrency} scenes at a time, "
          f"{a.concurrency if a.warm is None else a.warm} warm)", file=sys.stderr)
    return sv.run(a.api, ref, kp, launcher, concurrency=a.concurrency, warm=a.warm)


def cmd_eval(a) -> int:
    try:
        from vector_env import harness
        from vector_env.cli import EvalError, report
        from vector_env.compose import SceneError
        from vector_env.node import SimError
    except ImportError:
        raise SystemExit("vector-miner eval needs vector-env (scripts/setup.sh installs it)") from None
    from . import local
    if a.command:
        import shlex
        expert = {"command": shlex.split(a.command)}
    elif a.image:
        expert = {"image": a.image}
    else:
        raise SystemExit("give --image REF or --command \"...\"")
    for k in ("step_deadline", "first_step_deadline"):
        v = getattr(a, k)
        if v is not None and not (math.isfinite(v) and v > 0):
            raise SystemExit(f"--{k.replace('_', '-')} must be a positive number of seconds")
    docker = {k: v for k, v in (("cpus", a.cpus), ("memory", a.memory), ("binary", a.docker)) if v is not None}
    if a.image and a.pull:
        try:
            harness.docker_check(docker)
            harness.pull(a.image, docker)
        except harness.HarnessError as e:
            hint = "; --pull and --image need a running docker daemon" if e.outcome == "docker_unavailable" else ""
            print(f"vector-miner: cannot pull {a.image}: {e}{hint}", file=sys.stderr)
            return 2
    from vector_env.assets import AssetsError
    n = a.determinism if a.determinism is not None else a.scenes
    if n < 1:
        raise SystemExit("--scenes and --determinism must be at least 1")
    limits = local.deadlines(a.first_step_deadline, a.step_deadline)
    try:
        r = local.evaluate(expert, n, a.seed, workers=a.workers, time_limit_s=a.time_limit, wall_s=a.wall,
                           docker=docker, stderr_dir=a.stderr, trace_dir=a.trace,
                           scene_cache_dir=False if a.no_scene_cache else a.scene_cache, limits=limits,
                           again=1 if a.determinism is not None else 0)
    except (AssetsError, EvalError, SceneError, SimError, harness.HarnessError, ValueError) as e:
        print(f"vector-miner: {e}", file=sys.stderr)
        return 2
    if a.determinism is None:
        code = report(r, a.results, "vector-miner")
        return _deadline_verdict(a, r, code)
    return _deadline_verdict(a, r, _determinism(a, r, harness))


def _deadline_verdict(a, r: dict, code: int) -> int:
    from . import local
    if code == 2:
        return code
    lat = r["latency"]
    for line in local.report_lines(lat):
        print(f"vector-miner: {line}", file=sys.stderr)
    if a.strict and lat["late_scenes"] and code == 0:
        return 1
    return code


def _determinism(a, r: dict, harness) -> int:
    from vector_env.cli import infra_problem
    why = infra_problem(r["summary"], r["results"])
    if why:
        print(f"vector-miner: {why}", file=sys.stderr)
        return 2
    s = r["summary"]
    again = r["runs"][1]
    c = harness.compare(r["results"], again)
    if a.results:
        with open(a.results, "w") as f:
            for x in r["results"] + again:
                f.write(json.dumps(x, separators=(",", ":")) + "\n")
    out = {"scenes": s["scenes"], "seed": s["seed"], "score": s["score"], "score_again": harness.summary(again)["score"],
           "determinism": {k: c[k] for k in ("compared", "equal", "all_equal", "mismatched", "infra")},
           "deadlines": s.get("deadlines")}
    print(json.dumps(out))
    if c["all_equal"]:
        print(f"vector-miner: deterministic on {c['compared']} scenes: every action hash matched", file=sys.stderr)
        return 0
    real = [x for x in c["scenes"] if not x["equal"] and not x["infra"]]
    if not real:
        print(f"vector-miner: {c['infra']} scenes did not run here both times; nothing differed otherwise: "
              "not a determinism result", file=sys.stderr)
        return 2
    print(f"vector-miner: NOT deterministic: {len(real)} of {c['compared']} scenes sent other rows the second time "
          f"({', '.join(x['scene_id'] for x in real[:5])}{' ...' if len(real) > 5 else ''}). Phase 2 would disqualify "
          "this image: seed every random draw from the scene, read no clock, keep threads from racing on results",
          file=sys.stderr)
    return 1


def _verify_args(a) -> str | None:
    if a.command and not a.image:
        return "--command replaces an evaluated image: give --image REF too"
    if a.workers < 1:
        return "--workers must be at least 1"
    if a.scenes < 0 or (a.hash_scenes is not None and a.hash_scenes < 0):
        return "--scenes and --hash-scenes cannot be negative"
    for k in ("wall", "cpus"):
        v = getattr(a, k)
        if v is not None and not (math.isfinite(v) and v > 0):
            return f"--{k} must be a positive number"
    if a.salt_sha256 is not None:
        a.salt_sha256 = a.salt_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", a.salt_sha256):
            return "--salt-sha256 takes the 64 hex characters of the salt commitment"
    return None


def _verify(a) -> int:
    def bad(msg: str) -> int:
        print(f"vector-miner: {msg}", file=sys.stderr)
        return 2

    why = _verify_args(a)
    if why:
        return bad(why)
    if a.submission is None:
        return bad("give --submission ID")
    return _verify_submission(a, bad)


def _verify_submission(a, bad) -> int:
    from . import subverify as sv
    from . import verify as vf
    salt_expect, salt_source = a.salt_sha256, "you gave"
    sid = a.submission
    try:
        if a.doc:
            doc = vf.read_file(a.doc)
            got = (doc.get("submission") or {}).get("id") if isinstance(doc.get("submission"), dict) else None
            if got is not None and str(got) != sid:
                return bad(f"{a.doc} is submission {got}, not {sid}")
        else:
            if not a.api:
                return bad("--api (or VECTOR_MINER_API) is required without --doc")
            import httpx

            from .api import Api, ApiError
            try:
                raw = Api(a.api).submission_verify(sid, vf.MAX_DOC_BYTES)
            except ApiError as e:
                return bad(f"submission {sid}: {e.detail}" if e.status == 404 else str(e))
            except httpx.HTTPError as e:
                return bad(f"cannot reach {a.api}: {e}")
            doc = vf.loads(raw)
            if not a.salt_sha256:
                try:
                    v = Api(a.api).submission(sid)
                    seen = (v.get("submission") if isinstance(v.get("submission"), dict) else v).get("salt_sha256")
                except (ApiError, httpx.HTTPError, ValueError, AttributeError) as e:
                    print(f"vector-miner: warning: the submission view did not load ({e}); checking salt_sha256 "
                          "against the document only", file=sys.stderr)
                    seen = None
                if isinstance(seen, str) and seen:
                    salt_expect, salt_source = seen.strip().lower()[:64], "of the submission view"
        if a.save:
            try:
                with open(a.save, "w") as f:
                    json.dump(doc, f, separators=(",", ":"))
            except OSError as e:
                return bad(f"cannot write {a.save}: {e}")
        expert = None
        if a.command:
            import shlex
            try:
                expert = {"command": shlex.split(a.command)}
            except ValueError as e:
                return bad(f"--command: {e}")
        docker = {k: v for k, v in (("cpus", a.cpus), ("memory", a.memory), ("binary", a.docker)) if v is not None}
        out = sv.verify(doc, hash_scenes=a.hash_scenes, image=a.image, expert=expert, rerun_scenes=a.scenes,
                        sample_seed=a.sample_seed, wall_s=a.wall, workers=a.workers, docker=docker,
                        salt_sha256=salt_expect, salt_source=salt_source, pull=a.pull,
                        allow_uncommitted=a.allow_uncommitted)
    except vf.VerifyError as e:
        return bad(str(e))
    print(json.dumps(out, indent=1))
    return vf.exit_code(out)


def cmd_verify(a) -> int:
    try:
        return _verify(a)
    except Exception as e:
        print(f"vector-miner: cannot check: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vector-miner")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--wallet.name", dest="wallet_name", default="default")
    common.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    common.add_argument("--wallet.path", dest="wallet_path", default=DEFAULT_WALLETS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    api_default = os.environ.get("VECTOR_MINER_API")
    c = sub.add_parser("commit", parents=[common], help="commit your image on chain (once: one submission per hotkey, ever)")
    c.add_argument("--image", required=True)
    c.add_argument("--network", default="finney")
    c.add_argument("--netuid", type=int)
    c.add_argument("--api", default=api_default, help="the public API that tells whether this hotkey already has a submission")
    c.add_argument("--no-check", action="store_true", help="do not ask the registry whether the image exists")
    c.add_argument("--no-api-check", action="store_true", help="do not ask the API whether this hotkey already has a submission")
    c.add_argument("--dev-api", help="a local dev API (no chain): commit there instead")
    s = sub.add_parser("status", parents=[common], help="your submission: phase-1 progress, queue position, phase 2, "
                                                        "the king on the same scenes, the verdict")
    s.add_argument("--api", default=api_default, required=not api_default)
    s.add_argument("--hotkey", help="an ss58 address instead of the wallet's hotkey")
    sv = sub.add_parser("serve", parents=[common], help="phase 1: connect to the subnet and run your committed image "
                                                        "locally on the scenes it sends")
    sv.add_argument("--image", required=True, help="the image you committed (ghcr.io/<you>/<repo>@sha256:<digest>)")
    sv.add_argument("--api", default=api_default)
    sv.add_argument("--concurrency", type=int, default=8, help="scenes run at once (the server sends up to 8)")
    sv.add_argument("--warm", type=int, help="containers started ahead, waiting for a scene (default: --concurrency)")
    sv.add_argument("--command", help="run a local program per scene instead of the image (local development)")
    sv.add_argument("--cpus", type=float)
    sv.add_argument("--memory")
    sv.add_argument("--docker", help="the docker binary")
    sv.add_argument("--pull", action="store_true", help="pull the image first when it is not local")
    sv.add_argument("--stderr", metavar="DIR", help="write each container's stderr to DIR/<container name>.log")
    e = sub.add_parser("eval", help="local evaluation: random scenes played the way phase 1 plays them, with its "
                                    "step deadlines")
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
    e.add_argument("--stderr", metavar="DIR", help="write the expert's stderr of every scene to DIR/<index, 4 digits>-<scene_id>.log")
    e.add_argument("--trace", metavar="DIR", help="after the run, replay every scene and write DIR/<index, 4 digits>-<scene_id>.json "
                   "(per tick: row, joints, grip, tcp pose, contacts; why it ended)")
    e.add_argument("--scene-cache", metavar="DIR", help="keep generated scenes here (default $VECTOR_ENV_SCENE_CACHE, "
                   "else ~/.cache/vector-env/scenes)")
    e.add_argument("--no-scene-cache", action="store_true", help="generate every scene afresh and keep none")
    e.add_argument("--determinism", type=int, metavar="N", help="run N scenes twice and compare every action hash "
                   "(phase 2 needs them equal)")
    e.add_argument("--first-step-deadline", type=float, metavar="S", help="seconds the expert has to answer start "
                   "(phase 1: 10)")
    e.add_argument("--step-deadline", type=float, metavar="S", help="seconds the expert has to answer each later state "
                   "(phase 1: 2)")
    e.add_argument("--strict", action="store_true", help="exit 1 when a scene missed a phase-1 deadline")
    v = sub.add_parser("verify", help="re-check a final submission: regenerate its scenes from the published salt, "
                                      "check phase 1, phase 2 and the king run, optionally re-run an image")
    v.add_argument("--submission", help="the submission id")
    v.add_argument("--api", default=os.environ.get("VECTOR_MINER_API"))
    v.add_argument("--doc", help="read a saved verify document instead of fetching it")
    v.add_argument("--save", help="write the fetched document here")
    v.add_argument("--image", help="re-run this image ('submission', 'king', or its reference, digest or hotkey) "
                                   "on --scenes scenes")
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
    v.add_argument("--salt-sha256", help="the salt commitment shown at commit")
    v.add_argument("--allow-uncommitted", action="store_true",
                   help="no salt commitment time (salt_committed_at null) is a warning, not a problem")
    a = ap.parse_args(argv)
    return {"commit": cmd_commit, "status": cmd_status, "serve": cmd_serve, "eval": cmd_eval,
            "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
