"""Check that the packaged application creates its main window and exits cleanly."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    exe = Path(sys.argv[1]).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="canflow-smoke-") as directory:
        marker = Path(directory) / "ready.txt"
        env = os.environ.copy()
        env["CANFLOW_SMOKE_TEST_FILE"] = str(marker)
        process = subprocess.Popen([str(exe)], env=env)
        try:
            code = process.wait(timeout=45)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            raise SystemExit("EXE startup timed out")
        if code != 0 or not marker.is_file() or marker.read_text(encoding="utf-8") != "ready":
            raise SystemExit(f"EXE startup failed (exit code {code}; ready={marker.is_file()})")
    print("EXE main window startup passed")


if __name__ == "__main__":
    main()
