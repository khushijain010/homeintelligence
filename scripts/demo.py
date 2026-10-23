"""`python scripts/demo.py` seeds the demo home; `python scripts/demo.py <scenario>` runs one."""

import sys

import httpx

BASE = "http://127.0.0.1:8000/api"


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else None
    path = f"/demo/run/{name}" if name else "/demo/reset"
    resp = httpx.post(BASE + path, timeout=30)
    print(resp.status_code, resp.text)
    resp.raise_for_status()


if __name__ == "__main__":
    main()
