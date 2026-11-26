"""The vector-miner command: vector-miner [--leaf ID] <command> [args...], or vector-miner leaves."""
from __future__ import annotations

import importlib
import json
import sys

from . import skills

USAGE = "usage: vector-miner [--leaf ID] <command> [args...]\n       vector-miner leaves"


def _fail(msg: str) -> int:
    print(f"vector-miner: {msg}", file=sys.stderr)
    return 2


def _split(argv: list[str]) -> tuple[str | None, list[str], str | None]:
    """--leaf ID (only before the command), the rest, and an error."""
    if argv and argv[0] == "--leaf":
        if len(argv) < 2 or not argv[1] or argv[1].startswith("-"):
            return None, argv, "error: argument --leaf: expected one argument"
        return argv[1], argv[2:], None
    if argv and argv[0].startswith("--leaf="):
        got = argv[0][len("--leaf="):]
        return (got, argv[1:], None) if got else (None, argv, "error: argument --leaf: expected one argument")
    return None, argv, None


def _leaves_line() -> str:
    have = skills.leaves()
    return json.dumps({"leaves": [{"id": k, "skill": m.SKILL, "kind": m.KIND} for k, m in have.items()],
                       "default": skills.default()})


def _help(have: dict) -> str:
    rows = "\n".join(f"  {k}" for k in have) or "  (none)"
    return (f"{USAGE}\n\nleaves (give --leaf ID or set {skills.ENV}):\n{rows}\n\n"
            "vector-miner --leaf ID --help shows the commands of a leaf.")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    chosen, rest, err = _split(argv)
    if err:
        print(USAGE, file=sys.stderr)
        return _fail(err)
    if rest[:1] == ["leaves"]:
        print(_leaves_line())
        return 0
    have = skills.leaves()
    leaf_id = chosen or skills.default()
    if leaf_id is None:
        if rest[:1] in (["-h"], ["--help"]):
            print(_help(have))
            return 0
        if not have:
            return _fail("no leaf is installed")
        return _fail(f"several leaves are installed ({', '.join(have)}): give --leaf ID or set {skills.ENV}")
    try:
        pkg = skills.leaf(leaf_id)
    except KeyError as e:
        return _fail(str(e.args[0]))
    cli = importlib.import_module(f"{pkg.__name__}.cli")
    return cli.main(rest, prog="vector-miner")


if __name__ == "__main__":
    raise SystemExit(main())
