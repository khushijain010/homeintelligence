"""RS256 signing key for access tokens. Production requires the key file (`make keys`);
development generates an in-memory key so the server always starts."""

import logging
from pathlib import Path

from joserfc.jwk import RSAKey

from guardian.config import settings

log = logging.getLogger("guardian.oauth")
_key: RSAKey | None = None


def generate_pem() -> bytes:
    key = RSAKey.generate_key(2048, parameters={"kid": "guardian-1"})
    return key.as_pem(private=True)


def signing_key() -> RSAKey:
    global _key
    if _key is None:
        path = Path(settings.jwt_private_key_path)
        if path.exists():
            _key = RSAKey.import_key(path.read_bytes(), parameters={"kid": "guardian-1"})
        elif settings.is_production:
            raise RuntimeError(f"JWT key {path} missing; run `make keys`")
        else:
            log.warning("no JWT key at %s; using a temporary development key", path)
            _key = RSAKey.import_key(generate_pem(), parameters={"kid": "guardian-1"})
    return _key


def public_jwk() -> dict:
    return {**signing_key().as_dict(private=False), "use": "sig", "alg": "RS256"}


def reset() -> None:
    global _key
    _key = None
