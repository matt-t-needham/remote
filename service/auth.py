"""Access JWT and cross-site checks for control requests."""
import jwt
from fastapi import HTTPException, Request

import config

_jwks = None


def _jwks_client() -> jwt.PyJWKClient:
    """Cached client for the Access team's signing keys (rotated by Cloudflare;
    PyJWKClient refetches on an unknown key id)."""
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(
            f"https://{config.ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs",
            cache_keys=True, lifespan=3600, timeout=10,
        )
    return _jwks


def access_verified(request: Request) -> bool:
    """True only for a valid Access JWT for this app. The Host header and source
    IP are never trusted: tunnel and local requests both arrive via docker-proxy."""
    if config.ACCESS_DEV_BYPASS:
        return True
    if not config.ACCESS_AUD:
        return False
    token = request.headers.get("cf-access-jwt-assertion", "")
    if not token:
        return False
    try:
        key = _jwks_client().get_signing_key_from_jwt(token).key
        jwt.decode(
            token, key, algorithms=["RS256"],
            audience=config.ACCESS_AUD,
            issuer=f"https://{config.ACCESS_TEAM_DOMAIN}",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        return False
    return True


def same_origin(request: Request) -> bool:
    if request.headers.get("sec-fetch-site", "") in ("same-site", "cross-site"):
        return False
    origin = request.headers.get("origin")
    return origin is None or origin == config.PUBLIC_ORIGIN


def require_access(request: Request) -> None:
    if not access_verified(request):
        raise HTTPException(status_code=403, detail="forbidden")


def require_control(request: Request) -> None:
    require_access(request)
    if not same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
