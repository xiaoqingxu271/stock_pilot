"""Static bearer-token authentication with coarse roles.

This is the demo-grade identity layer: tokens are configured by environment
variable and map to one role. Production replaces it with OIDC, but the role
checks and audit behaviour stay the same.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from enum import Enum

from stock_pilot.config import Settings


class Role(str, Enum):
    AGENT = "agent"
    OPERATOR = "operator"
    ADMIN = "admin"


@dataclass(frozen=True)
class Principal:
    role: Role
    name: str


def configured_tokens(settings: Settings) -> dict[str, Principal]:
    """Token -> principal. A role with no token is simply unreachable."""
    candidates = (
        (settings.agent_token, Principal(Role.AGENT, "agent")),
        (settings.operator_token, Principal(Role.OPERATOR, "operator")),
        (settings.admin_token, Principal(Role.ADMIN, "admin")),
    )
    return {token: principal for token, principal in candidates if token}


def auth_enabled(settings: Settings) -> bool:
    """No tokens at all means unauthenticated dev mode; any token means auth."""
    return bool(configured_tokens(settings))


def _split_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def bearer_matches(expected: str, header: str | None) -> bool:
    token = _split_bearer(header)
    return token is not None and hmac.compare_digest(token, expected)


def principal_from_authorization(
    settings: Settings, header: str | None
) -> Principal | None:
    token = _split_bearer(header)
    if token is None:
        return None
    for expected, principal in configured_tokens(settings).items():
        if hmac.compare_digest(token, expected):
            return principal
    return None


def permits(principal: Principal, required: Role) -> bool:
    return principal.role is Role.ADMIN or principal.role is required
