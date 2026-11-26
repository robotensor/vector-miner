from __future__ import annotations

import json

import httpx
import pytest
from bittensor_wallet import Keypair
from conftest import REF, D, _api, _subs

from vector_miner import chain, wallet
from vector_miner.skills.bin_picking.expert import cli
from vector_miner.skills.bin_picking.expert.api import ExpertApi


def test_commit_sends_the_checked_reference(monkeypatch, capsys):
    sent = {}
    monkeypatch.setattr(cli, "_keypair", lambda a: Keypair.create_from_uri("//Alice"))
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: sent.setdefault("image", image) and {"block": 1})
    _api(monkeypatch, _subs([{"hotkey": "5Other", "id": "s0", "status": "phase1"}]))
    assert cli.main(["commit", "--image", f"  {REF} ", "--no-check", "--dev-api", "http://api"]) == 0
    assert sent["image"] == REF
    assert f"vector-miner serve --image {REF} --api http://api;" in capsys.readouterr().err
    assert cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api", "--wallet.name", "cold",
                     "--wallet.hotkey", "hk1", "--wallet.path", "/w"]) == 0
    err = capsys.readouterr().err
    assert (f"vector-miner serve --image {REF} --api http://api --wallet.name cold --wallet.hotkey hk1 "
            "--wallet.path /w;") in err
    assert "vector-miner status --api http://api --wallet.name cold --wallet.hotkey hk1 --wallet.path /w" in err
    with pytest.raises(SystemExit, match="bad image reference"):
        cli.main(["commit", "--image", f"docker.io/alice/x@{D}", "--no-check", "--dev-api", "http://api"])


@pytest.mark.parametrize("status", ["waiting_connection", "phase1", "review", "below_king", "passed", "ranked"])
def test_commit_refuses_a_hotkey_that_already_has_a_submission(monkeypatch, status):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: pytest.fail("committed"))
    monkeypatch.setattr(chain, "commit", lambda *a: pytest.fail("committed on chain"))
    rows = [{"hotkey": alice.ss58_address, "id": "sub-7", "image": REF, "status": status,
             "committed_at": "2026-11-23T01:00:00+00:00"},
            {"hotkey": alice.ss58_address, "id": "sub-9", "image": REF, "status": "duplicate_hotkey"}]
    _api(monkeypatch, _subs(rows))
    for argv in (["--dev-api", "http://api"], ["--netuid", "1", "--api", "http://api"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["commit", "--image", REF, "--no-check", *argv])
        msg = str(e.value.code)
        assert f"already has submission sub-7 ({REF}, status {status}" in msg
        assert "one submission per hotkey, ever" in msg and "duplicate_hotkey" in msg and "new hotkey" in msg
        assert (f"a submission in {status} uses it (only invalid, copy, duplicate_hotkey, abandoned and unpaid commits "
                "leave it free)") in msg


def test_commit_goes_ahead_when_no_earlier_commit_used_the_slot(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    sent = []
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: sent.append(image) or {"block": 9})
    rows = [{"hotkey": alice.ss58_address, "id": f"sub-{k}", "image": REF, "status": st, "commit_block": k}
            for k, st in enumerate(("invalid", "copy", "duplicate_hotkey", "abandoned", "unpaid"))]
    _api(monkeypatch, _subs(rows))
    assert cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"]) == 0 and sent == [REF]
    rows += [{"hotkey": alice.ss58_address, "id": "sub-6", "image": REF, "status": "below_king", "commit_block": 6}]
    with pytest.raises(SystemExit, match="already has submission sub-6 .*status below_king"):
        cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"])
    assert sent == [REF]


def test_commit_needs_the_api_check_or_its_opt_out(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.delenv("VECTOR_MINER_API", raising=False)
    with pytest.raises(SystemExit, match="--api .* is required: commit first checks"):
        cli.main(["commit", "--image", REF, "--no-check", "--netuid", "1"])

    def down(req):
        raise httpx.ConnectError("refused")
    _api(monkeypatch, down)
    with pytest.raises(SystemExit, match="cannot reach http://api .*--no-api-check"):
        cli.main(["commit", "--image", REF, "--no-check", "--netuid", "1", "--api", "http://api"])
    sent = []
    monkeypatch.setattr(chain, "commit", lambda net, netuid, kp, ref: sent.append((net, netuid, ref)) or {"ok": True})
    assert cli.main(["commit", "--image", REF, "--no-check", "--netuid", "1", "--no-api-check", "--network", "test"]) == 0
    assert sent == [("test", 1, REF)]


def test_status_and_commit_without_a_wallet(tmp_path):
    for argv in (["status", "--api", "http://api"], ["commit", "--image", REF, "--no-check", "--netuid", "1"]):
        with pytest.raises(SystemExit) as e:
            cli.main([*argv, "--wallet.path", str(tmp_path), "--wallet.name", "miner"])
        msg = str(e.value.code)
        assert msg.startswith("vector-miner: no wallet hotkey miner/default in ")
        assert f".venv/bin/btcli wallet new-coldkey --wallet miner --wallet-path {tmp_path}, then " in msg
        assert msg.endswith(f".venv/bin/btcli wallet new-hotkey --wallet miner --wallet-hotkey default --wallet-path {tmp_path}")
        assert "new_" not in msg and "--wallet.name/--wallet.hotkey" in msg
        assert ("--hotkey <ss58>" in msg) == (argv[0] == "status")
    (tmp_path / "miner").mkdir()
    (tmp_path / "miner" / "coldkeypub.txt").write_text("{}")
    msg = wallet.btcli_create("miner", "hk", str(tmp_path))
    assert msg == f".venv/bin/btcli wallet new-hotkey --wallet miner --wallet-hotkey hk --wallet-path {tmp_path}"
    assert wallet.btcli_create("nobody-x", "hk", wallet.DEFAULT_WALLETS).endswith("new-hotkey --wallet nobody-x --wallet-hotkey hk")


def test_commit_warns_for_a_private_image_and_names_the_team_account(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(cli, "_keypair", lambda a: Keypair.create_from_uri("//Alice"))
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: sent.append(image) or {"block": 1})
    monkeypatch.setattr(chain, "image_access", lambda ref: ("private", chain.ANON_REFUSED))

    def h(req):
        if req.url.path == "/v1/skills/bin-picking/expert":
            return httpx.Response(200, json={"pull_account": "teamacct", "registries": ["ghcr.io"]})
        return _subs([])(req)
    _api(monkeypatch, h)
    assert cli.main(["commit", "--image", REF, "--dev-api", "http://api"]) == 0 and sent == [REF]
    err = capsys.readouterr().err
    assert "is private" in err and "share the package with it (Read)" in err and "add teamacct, role Read" in err
    _api(monkeypatch, _subs([]))
    assert cli.main(["commit", "--image", REF, "--dev-api", "http://api"]) == 0
    assert "the subnet's team account (`pull_account`" in capsys.readouterr().err
    monkeypatch.setattr(chain, "image_access", lambda ref: ("missing", "no such image at that digest"))
    with pytest.raises(SystemExit, match="not pullable: no such image"):
        cli.main(["commit", "--image", REF, "--dev-api", "http://api"])
    assert len(sent) == 2


VAULT = Keypair.create_from_uri("//VectorVault").ss58_address


COLD = Keypair.create_from_uri("//ColdA").ss58_address


def _fee_api(rows, fee, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/skills/bin-picking/expert/fee":
            return httpx.Response(200, json={"hotkey": req.url.params["hotkey"], **fee})
        if req.url.path == "/v1/dev/fee/pay":
            (seen if seen is not None else []).append(json.loads(req.content))
            return httpx.Response(200, json={"block": 1002, "extrinsic": 1, "from": COLD, "to": VAULT,
                                             "amount_rao": 10 ** 8})
        return _subs(rows)(req)
    return handler


UNPAID = {"fee": {"tao": 0.1, "vault": VAULT}, "coldkey": COLD, "paid": False, "payments": [], "scanned_to": 1001}


PAID = {**UNPAID, "paid": True, "payments": [{"id": "1000-1", "block": 1000, "extrinsic": 1, "tao": 0.1}]}


def test_commit_refuses_without_a_fee_payment(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: pytest.fail("committed"))
    monkeypatch.setattr(chain, "commit", lambda *a: pytest.fail("committed on chain"))
    _api(monkeypatch, _fee_api([], UNPAID))
    for argv in (["--dev-api", "http://api"], ["--netuid", "1", "--api", "http://api"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["commit", "--image", REF, "--no-check", *argv])
        msg = str(e.value.code)
        assert f"no unused fee payment of 0.1 TAO from {COLD} to the vault {VAULT}" in msg
        assert "vector-miner fee pay --api http://api" in msg and "never evaluated" in msg


def test_commit_goes_ahead_with_a_payment_or_without_a_fee(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    sent = []
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: sent.append(image) or {"block": 9})
    for fee in (PAID, {"fee": None, "paid": True, "payments": []}):
        _api(monkeypatch, _fee_api([], fee))
        assert cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"]) == 0
    assert sent == [REF, REF]


def test_fee_status_and_pay(monkeypatch, capsys):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a, alt="": alice)
    monkeypatch.setattr(cli, "_coldkey_address", lambda a: COLD)
    seen = []
    _api(monkeypatch, _fee_api([], UNPAID, seen))
    assert cli.main(["fee", "status", "--api", "http://api"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["fee"] == {"tao": 0.1, "vault": VAULT}
    assert f"not paid: send 0.1 TAO from {COLD} to the vault {VAULT} before you commit" in out.err
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["fee", "pay", "--dev-api", "http://api"]) == 1 and seen == []
    assert cli.main(["fee", "pay", "--dev-api", "http://api", "--yes"]) == 0 and seen == [{"tao": 0.1}]
    assert f"pay 0.1 TAO from {COLD} to the vault {VAULT}" in capsys.readouterr().err
    _api(monkeypatch, _fee_api([], PAID, seen))
    assert cli.main(["fee", "pay", "--dev-api", "http://api", "--yes"]) == 0 and len(seen) == 1
    assert "--again pays one more" in capsys.readouterr().err
    monkeypatch.setattr(cli, "_coldkey_address", lambda a: VAULT)
    _api(monkeypatch, _fee_api([], UNPAID, seen))
    with pytest.raises(SystemExit, match=f"is owned by the coldkey {COLD}, not by this wallet's coldkey"):
        cli.main(["fee", "pay", "--dev-api", "http://api", "--yes"])


def test_fee_pay_on_chain_sends_a_transfer(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a, alt="": alice)
    monkeypatch.setattr(cli, "_coldkey_address", lambda a: COLD)
    sent = []
    monkeypatch.setattr(chain, "pay_fee", lambda net, w, vault, tao: sent.append((net, vault, tao)) or {"ok": True})
    _api(monkeypatch, _fee_api([], UNPAID))
    assert cli.main(["fee", "pay", "--api", "http://api", "--network", "test", "--yes"]) == 0
    assert sent == [("test", VAULT, 0.1)]


def _leaf_api(status, rows=(), fee=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/skills/bin-picking/expert":
            body = {"ranks": []}
            if status is not None:
                body["leaf"] = {"id": "bin-picking-expert", "skill": "bin-picking", "kind": "expert", "status": status,
                                "round": None, "next": None}
            return httpx.Response(200, json=body)
        if fee is not None and req.url.path == "/v1/skills/bin-picking/expert/fee":
            return httpx.Response(200, json={"hotkey": req.url.params["hotkey"], **fee})
        return _subs(list(rows))(req)
    return handler


def test_commit_refuses_a_finished_leaf_before_anything_is_sent(monkeypatch):
    alice = Keypair.create_from_uri("//Alice")
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: pytest.fail("committed"))
    monkeypatch.setattr(chain, "commit", lambda *a: pytest.fail("committed on chain"))
    _api(monkeypatch, _leaf_api("finished"))
    for argv in (["--dev-api", "http://api"], ["--netuid", "1", "--api", "http://api"]):
        with pytest.raises(SystemExit) as e:
            cli.main(["commit", "--image", REF, "--no-check", *argv])
        assert str(e.value.code) == ("vector-miner: bin-picking-expert is finished: it takes no commits "
                                     "(--no-api-check skips this check)")
    sent = []
    monkeypatch.setattr(chain, "commit", lambda net, netuid, kp, ref: sent.append(ref) or {"ok": True})
    assert cli.main(["commit", "--image", REF, "--no-check", "--netuid", "1", "--no-api-check"]) == 0 and sent == [REF]


@pytest.mark.parametrize("status", ["planned", "paused"])
def test_commit_warns_for_a_planned_or_paused_leaf_and_goes_on(monkeypatch, capsys, status):
    alice = Keypair.create_from_uri("//Alice")
    sent = []
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: sent.append(image) or {"block": 9})
    _api(monkeypatch, _leaf_api(status))
    assert cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"]) == 0 and sent == [REF]
    err = capsys.readouterr().err
    assert (f"vector-miner: warning: bin-picking-expert is {status}: the commit is picked up when its next round "
            "starts\n") in err
    assert "vector-miner: committed." in err


@pytest.mark.parametrize("status", ["ongoing", None])
def test_commit_is_unchanged_while_ongoing_or_without_the_leaf_object(monkeypatch, capsys, status):
    alice = Keypair.create_from_uri("//Alice")
    sent = []
    monkeypatch.setattr(cli, "_keypair", lambda a: alice)
    monkeypatch.setattr(ExpertApi, "dev_commit", lambda self, image: sent.append(image) or {"block": 9})
    _api(monkeypatch, _leaf_api(status))
    assert cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"]) == 0 and sent == [REF]
    assert "bin-picking-expert is" not in capsys.readouterr().err
    rows = [{"hotkey": alice.ss58_address, "id": "sub-7", "image": REF, "status": "phase1"}]
    _api(monkeypatch, _leaf_api(status, rows))
    with pytest.raises(SystemExit, match="already has submission sub-7"):
        cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"])
    _api(monkeypatch, _leaf_api(status, fee=UNPAID))
    with pytest.raises(SystemExit, match="no unused fee payment"):
        cli.main(["commit", "--image", REF, "--no-check", "--dev-api", "http://api"])
    assert sent == [REF]
