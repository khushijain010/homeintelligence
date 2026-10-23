"""Print a bcrypt hash for the owner password (`make set-password`). Paste it into .env."""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guardian.oauth.owner import hash_password  # noqa: E402


def main() -> None:
    pw = getpass.getpass("Owner password: ")
    if len(pw) < 10:
        sys.exit("use at least 10 characters")
    if getpass.getpass("Repeat: ") != pw:
        sys.exit("passwords differ")
    # single quotes stop python-dotenv from expanding the $ signs in the hash
    print(f"\nAdd this line to .env:\nOWNER_PASSWORD_HASH='{hash_password(pw)}'")


if __name__ == "__main__":
    main()
