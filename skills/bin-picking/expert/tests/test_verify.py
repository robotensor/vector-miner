from __future__ import annotations

import pytest

from vector_miner.skills.bin_picking.expert import cli, verify


def test_the_round_option_is_gone(capsys):
    with pytest.raises(SystemExit):
        cli.main(["verify", "--round", "4", "--doc", "x.json"])
    assert "--round" in capsys.readouterr().err
    assert cli.main(["verify", "--doc", "x.json"]) == 2
    assert "give --submission ID" in capsys.readouterr().err


def test_documents_are_strict_json():
    with pytest.raises(verify.VerifyError, match="NaN"):
        verify.loads('{"a": NaN}')
    with pytest.raises(verify.VerifyError, match="not a verify document"):
        verify.loads("[1, 2]")
    with pytest.raises(verify.VerifyError, match="not JSON"):
        verify.loads("[" * 100000 + "]" * 100000)
