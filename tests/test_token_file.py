from pathlib import Path

from mcp_server.server import resolve_token


def test_env_token_wins(tmp_path: Path):
    assert resolve_token("x" * 20, tmp_path) == ("x" * 20, False)
    assert not (tmp_path / "token").exists()


def test_generates_once_and_reuses(tmp_path: Path):
    t1, new1 = resolve_token("", tmp_path / "data")
    t2, new2 = resolve_token("", tmp_path / "data")
    assert new1 and not new2 and t1 == t2 and len(t1) == 64
    assert oct((tmp_path / "data" / "token").stat().st_mode & 0o777) == "0o600"
