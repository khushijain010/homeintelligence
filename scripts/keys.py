"""Create the RS256 private key that signs Alexa access tokens (`make keys`)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guardian.config import settings  # noqa: E402
from guardian.oauth.keys import generate_pem  # noqa: E402


def main() -> None:
    path = Path(settings.jwt_private_key_path)
    if path.exists() and "--force" not in sys.argv:
        sys.exit(f"{path} already exists (use --force to replace it; existing Alexa tokens will stop working)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(generate_pem())
    print(f"wrote {path}  (keep it secret; it is git-ignored)")


if __name__ == "__main__":
    main()
