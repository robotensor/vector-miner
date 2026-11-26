"""Wallet flags and keys shared by every leaf's commands."""
from __future__ import annotations

import argparse
import os

DEFAULT_WALLETS = os.path.expanduser("~/.bittensor/wallets")


def common() -> argparse.ArgumentParser:
    """The parent parser of the --wallet.name, --wallet.hotkey and --wallet.path flags."""
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--wallet.name", dest="wallet_name", default="default")
    p.add_argument("--wallet.hotkey", dest="wallet_hotkey", default="default")
    p.add_argument("--wallet.path", dest="wallet_path", default=DEFAULT_WALLETS)
    return p


def btcli_create(name: str, hotkey: str, path: str) -> str:
    """The btcli commands that create this wallet's missing keys."""
    where = "" if os.path.abspath(os.path.expanduser(path)) == os.path.abspath(DEFAULT_WALLETS) else f" --wallet-path {path}"
    have_cold = any(os.path.exists(os.path.join(os.path.expanduser(path), name, f)) for f in ("coldkey", "coldkeypub.txt"))
    cold = "" if have_cold else f".venv/bin/btcli wallet new-coldkey --wallet {name}{where}, then "
    return f"{cold}.venv/bin/btcli wallet new-hotkey --wallet {name} --wallet-hotkey {hotkey}{where}"


def keypair(a, alt: str = ""):
    """The wallet's hotkey keypair; SystemExit with the btcli commands that create it when it is missing."""
    from bittensor_wallet import Wallet
    from bittensor_wallet.errors import KeyFileError
    w = Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey, path=a.wallet_path)
    try:
        return w.hotkey
    except KeyFileError:
        raise SystemExit(f"vector-miner: no wallet hotkey {a.wallet_name}/{a.wallet_hotkey} in {a.wallet_path}: "
                         f"{alt}pass --wallet.name/--wallet.hotkey (and --wallet.path), or create it with "
                         f"{btcli_create(a.wallet_name, a.wallet_hotkey, a.wallet_path)}") from None


def wallet_flags(a) -> str:
    """The --wallet.* flags of this call that differ from the defaults, to repeat in the commands we print."""
    out = []
    if getattr(a, "wallet_name", "default") != "default":
        out.append(f" --wallet.name {a.wallet_name}")
    if getattr(a, "wallet_hotkey", "default") != "default":
        out.append(f" --wallet.hotkey {a.wallet_hotkey}")
    path = getattr(a, "wallet_path", DEFAULT_WALLETS)
    if os.path.abspath(os.path.expanduser(path)) != os.path.abspath(DEFAULT_WALLETS):
        out.append(f" --wallet.path {path}")
    return "".join(out)


def coldkey_address(a) -> str | None:
    """The wallet's coldkey ss58 address; None when it cannot be read."""
    from bittensor_wallet import Wallet
    try:
        return Wallet(name=a.wallet_name, hotkey=a.wallet_hotkey, path=a.wallet_path).coldkeypub.ss58_address
    except Exception:
        return None
