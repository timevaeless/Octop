"""User identity primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Role(StrEnum):
    """Built-in role-template public ids (also stored on ``users.role``)."""

    ADMIN = "admin"
    USER = "user"


@dataclass
class User:
    id: int
    username: str
    role: str  # role-template public id: admin | user | custom ULID
    display_name: str | None
    locale: str = "zh"
    permissions: list[str] = field(default_factory=list)
    # Active business tenant. ``None`` means "no tenant context yet" (pending
    # onboarding, or a user that predates tenancy) -- never "all tenants":
    # consumers must treat it as "shared/platform resources only".
    org_id: str | None = None
    # Ability-package keys resolved from ``org_memberships.role_keys`` for the
    # active tenant. Authoritative source is the membership row (never stored
    # on ``users``); ``[]`` means "no ability packages" and therefore, under
    # resource ACL rules, only role-unrestricted (``allowed_roles = []``)
    # resources are visible. Platform admins bypass via ``is_admin``.
    role_keys: list[str] = field(default_factory=list)
    # Bumped whenever the membership / role mapping changes; embedded as the
    # ``ver`` token claim so old access tokens die on the next request.
    token_epoch: int = 0
    # SSO subject at the identity provider (e.g. the portal's
    # ``system_users.id``). ``None`` for local-password users. Carried into
    # the delegated-identity token (``ssu`` claim) so business MCP services
    # downstream of the gateway can attribute actions to the real operator.
    sso_subject: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.username

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN
