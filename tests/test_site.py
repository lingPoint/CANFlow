"""Verify that fresh checkouts contain a usable standalone static site."""
import json
from pathlib import Path
import subprocess


def test_site_is_regular_tracked_content_and_exports_to_fresh_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]

    def git(*args):
        return subprocess.run(
            ["git", "-c", f"safe.directory={root.as_posix()}", *args],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout

    entries = git("ls-files", "--stage", "--", "site").splitlines()
    assert entries, "The website must be tracked by the parent repository"
    assert all(entry.startswith("100644 ") for entry in entries), entries
    paths = [entry.split("\t", 1)[1] for entry in entries]
    for path in paths:
        git("checkout-index", f"--prefix={tmp_path.as_posix()}/", "--", path)
    site = tmp_path / "site"
    config = json.loads((site / ".openai" / "hosting.json").read_text(encoding="utf-8"))
    dist = site / config["static"]["directory"]
    assert (dist / "index.html").is_file()
    assert (dist / "styles.css").is_file()
    for asset in ("canflow-logo.svg", "configuration.png", "dense-signals.png", "waveform.png"):
        assert (dist / "assets" / asset).stat().st_size > 0
