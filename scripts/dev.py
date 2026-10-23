"""Run the ring-sandbox emulator (:8787) and Guardian (:8000) together. Ctrl+C stops both."""

import subprocess
import sys
import time

PROCS = [
    [sys.executable, "-m", "ring_sandbox.cli", "serve", "--port", "8787"],
    [sys.executable, "-m", "uvicorn", "guardian.main:app", "--host", "127.0.0.1", "--port", "8000"],
]


def main() -> None:
    procs = [subprocess.Popen(cmd) for cmd in PROCS]
    print("emulator http://127.0.0.1:8787/_sandbox/docs  ·  dashboard http://127.0.0.1:8000")
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait()


if __name__ == "__main__":
    main()
