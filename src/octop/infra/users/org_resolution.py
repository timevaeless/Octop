"""JIT tenant resolution and role expansion (ADR D9~D14, T1-1).

Octop is the *only* decision point for "which tenant does this upstream
identity belong to". The upstream IdP (Keycloak in the old topology, the
portal's own OIDC in the direct topology) merely asserts raw facts; the
normalisation happens here, at the Octop boundary:

* **Namespace** — every external identifier is keyed by ``(idp_alias, value)``;
  the alias comes from ``sso_providers.idp_alias``. A bare value used as a
  global key would silently mis-assign tenants.
* **Upstream claim dialects** — Keycloak emitted the normalised claims
  (``org_ref`` / ``job_code``); the direct portal emits its own dialect
  (``tenant_id`` / ``roles``). Both are accepted here, in canonical-first
  order, so the day a new upstream appears only this mapping needs a row.
* **Five-level chain** (first match wins, never guess):
    1. canonical claim binding (``match_type='claim'``),
    2. static binding for the provider (``match_type='static'``),
    3. existing membership (idempotent re-entry),
    4. verified e-mail domain binding (``match_type='domain'``),
    5. nothing matched → *pending*: nothing is written here (``org_id`` is
       NOT NULL — there is no org to reference); the login is traced for
       the admin backlog by ``octop.infra.users.lifecycle`` (one
       ``user.jit_pending`` audit event + the derived pending list) and
       the user gets NO tenant (fail-closed).
* **Role expansion** — upstream job codes are looked up in
  ``org_role_mappings`` (org-specific rows plus the ``'*'`` defaults) to
  produce ability-package keys. Every login re-expands and corrects.
  🔴 The two sets are **unioned**, not "specific overrides default": when both
  ``('*', code, A)`` and ``(org, code, B)`` exist the user receives **A and B**.
  So a ``'*'`` row is a platform-wide grant, and trimming a role for a single
  tenant cannot be expressed by adding a tenant row — only by editing/removing
  the ``'*'`` row.
* **Auto-provisioning is tenant-scoped** — a role code the platform has never
  seen is mapped *for the tenant whose login revealed it*, never for ``'*'``.
  A role code is a fact about the upstream vocabulary (a role *name* means the
  same thing in every tenant of the realm), but the mapping decides which
  Octop role that name *grants*, and that is a policy. One tenant's first login
  must not set policy for tenants no admin has opened.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from octop.infra.db.repos.orgs import IdpOrgBindingRow, OrgRepo

logger = logging.getLogger(__name__)

# Canonical claim first, then known upstream dialects. A binding stores only
# the *value*; any of these claim names may carry it.
_ORG_CLAIM_NAMES = ("org_ref", "tenant_id", "org")

# Canonical job-code claim first, then the portal dialect.
_JOB_CODE_CLAIM_NAMES = ("job_code", "roles")

# Mapping scope meaning "every tenant" — the same sentinel the admin API and
# the seed data use. A ``'*'`` row is an *admin-authored* platform-wide grant
# and is deliberately **not** what auto-provisioning writes: a first login must
# never decide a policy for tenants nobody has reviewed (see
# :func:`provision_missing_mappings`).
ANY_ORG = "*"

# Shape an auto-provisioned role code must have. Auto-creation inserts a row
# per unknown upstream code, so a misconfigured (or hostile) IdP that emits a
# fresh value on every login would otherwise grow the table without bound.
# Codes outside this shape are left unmapped and logged, never guessed at.
# Mirrors the pattern the admin page enforces on a hand-typed role code.
_PROVISIONABLE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of the five-level chain for one login."""

    org_id: str | None = None
    role_keys: list[str] = field(default_factory=list)
    status: str = "active"  # 'active' | 'pending'
    matched_level: str | None = None  # 'claim' | 'static' | 'membership' | 'domain' | None
    matched_org: str | None = None  # org the binding/membership pointed at
    # Role codes that had no mapping at all and were auto-created this login
    # (see ``provision_missing_mappings``). Reported so the caller can audit
    # that a permission was granted without an admin asking for it.
    created_mappings: list[str] = field(default_factory=list)

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"


def _claim_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def upstream_org_values(claims: dict[str, Any]) -> list[str]:
    """Ordered candidate tenant-identifier values from raw upstream claims."""
    out: list[str] = []
    for name in _ORG_CLAIM_NAMES:
        value = _claim_str(claims.get(name))
        if value is not None and value not in out:
            out.append(value)
    return out


def upstream_job_codes(claims: dict[str, Any]) -> list[str]:
    """Ordered candidate job codes from raw upstream claims."""
    out: list[str] = []
    for name in _JOB_CODE_CLAIM_NAMES:
        raw = claims.get(name)
        items: list[Any]
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, str):
            # A single string may be one code or a comma-separated list.
            items = raw.split(",") if "," in raw else [raw]
        else:
            items = []
        for item in items:
            code = _claim_str(item)
            if code is not None and code not in out:
                out.append(code)
    return out


def _verified_email(claims: dict[str, Any]) -> str | None:
    email = _claim_str(claims.get("email"))
    if email is None:
        return None
    if not claims.get("email_verified", False):
        return None
    return email.lower()


def expand_role_keys(org_repo: OrgRepo, org_id: str, job_codes: list[str]) -> list[str]:
    """Map upstream job codes to ability-package keys for ``org_id``.

    Org-specific rows win over ``'*'`` defaults only by virtue of being
    filtered per org; both sets are unioned. Unknown codes are simply absent
    from the result — never guessed.
    """
    if not job_codes:
        return []
    rows = org_repo.list_role_mappings(org_id=org_id, job_codes=job_codes)
    keys: list[str] = []
    for row in rows:
        if row.role_key not in keys:
            keys.append(row.role_key)
    return keys


def provision_missing_mappings(org_repo: OrgRepo, org_id: str, job_codes: list[str]) -> list[str]:
    """Auto-create a mapping for role codes the platform has never seen.

    A first-time user whose upstream role code has no mapping row resolves to a
    tenant but *zero* ability packages — they log in and can do nothing, with
    no signal to the admin that a role code needs attention. This closes that
    hole by mapping the unknown code to the default-minimal ability package
    (``roles.is_default``), so the login is at least usable and the admin can
    enrich the mapping afterwards from the 角色映射 page.

    Deliberately narrow, matching the agreed semantics:

    * Only codes with **no** row at all are provisioned — an admin who trims
      one package off a role code is never overridden, while deleting the whole
      mapping does get re-created on the next login. "No row" means no row
      *visible to this tenant*, i.e. neither its own nor a ``'*'`` one.
    * 🔴 Rows are scoped to **the tenant whose login revealed the code**, not to
      ``'*'``. A first login through cheryfs therefore creates
      ``(cheryfs, code, default)`` and nothing else: the dealers keep getting
      nothing until one of *their* users signs in. Writing ``'*'`` instead — as
      this function used to — made the very first tenant to log in set the
      default for every tenant on the platform, including tenants no admin had
      ever opened, which is a real least-privilege hole. The role *code* is a
      fact of the upstream vocabulary and may well be identical everywhere, but
      which Octop role it grants is a policy and policies start narrow.
    * ``origin='jit'`` + ``source_org`` record that the row was auto-created
      and by which tenant's login. ``source_org`` is kept even though it
      currently always equals ``org_id``: it is what tells an unattributed row
      apart from a machine-written one if a future operation ever moves a row's
      scope without re-stamping it.
    * If nothing is flagged ``is_default`` there is no such thing as a minimal
      package, so nothing is created (fail-closed, same as before).

    Returns the role codes it created, for the audit trail.
    """
    if not job_codes:
        return []
    covered = {
        row.job_code for row in org_repo.list_role_mappings(org_id=org_id, job_codes=job_codes)
    }
    missing = [code for code in job_codes if code not in covered]
    if not missing:
        return []
    provisionable = [code for code in missing if _PROVISIONABLE_CODE.match(code)]
    rejected = [code for code in missing if code not in provisionable]
    if rejected:
        logger.warning(
            "role code(s) not auto-provisioned (unexpected shape): %s",
            ", ".join(repr(code) for code in rejected),
        )
    if not provisionable:
        return []
    # Default packages are visibility-scoped to the triggering tenant
    # (platform packages plus its own) — a tenant-owned is_default row must
    # not leak into another tenant's provisioning, and platform defaults win
    # by ordering (TenantRoleRepo lists platform rows first).
    from octop.infra.db.repos.tenant_roles import tenant_role_repo_for

    default_keys = [
        r.role_key
        for r in tenant_role_repo_for(org_repo).list_visible_roles(org_id)
        if r.is_default
    ]
    if not default_keys:
        logger.info(
            "no default-minimal ability package (roles.is_default); leaving %d "
            "role code(s) unmapped: %s",
            len(provisionable),
            ", ".join(provisionable),
        )
        return []
    for code in provisionable:
        for role_key in default_keys:
            org_repo.add_role_mapping(org_id, code, role_key, origin="jit", source_org=org_id)
    logger.info(
        "auto-created role mappings scoped to tenant %s: %s → %s",
        org_id,
        ", ".join(provisionable),
        ", ".join(default_keys),
    )
    return provisionable


def resolve_org_and_roles(
    org_repo: OrgRepo,
    *,
    idp_alias: str,
    claims: dict[str, Any],
    user_id: int,
) -> ResolutionResult:
    """Run the five-level chain. ``idp_alias`` empty ⇒ no resolution at all."""
    bindings: list[IdpOrgBindingRow] = []
    if idp_alias:
        bindings = org_repo.list_bindings(idp_alias)
    claim_bindings = [b for b in bindings if b.match_type == "claim"]
    static_bindings = [b for b in bindings if b.match_type == "static"]
    domain_bindings = [b for b in bindings if b.match_type == "domain"]

    org_values = upstream_org_values(claims)
    job_codes = upstream_job_codes(claims)

    # ① canonical claim binding (namespaced lookup: (idp_alias, 'claim', value))
    for value in org_values:
        for binding in claim_bindings:
            if binding.match_value == value:
                return _finish(org_repo, binding.org_id, job_codes, level="claim")

    # ② static binding for this provider
    for binding in static_bindings:
        return _finish(org_repo, binding.org_id, job_codes, level="static")

    # ③ existing membership — idempotent re-entry keeps the tenant stable
    memberships = org_repo.list_memberships(user_id)
    active = [m for m in memberships if m.status == "active"]
    if active:
        primary = next((m for m in active if m.is_primary), active[0])
        return _finish(org_repo, primary.org_id, job_codes, level="membership")

    # ④ verified e-mail domain binding
    email = _verified_email(claims)
    if email is not None:
        for binding in domain_bindings:
            domain = binding.match_value.lower().lstrip("@")
            if domain and email.endswith(f"@{domain}"):
                return _finish(org_repo, binding.org_id, job_codes, level="domain")

    # ⑤ pending — record nothing here (caller decides); fail closed.
    return ResolutionResult(status="pending")


def _finish(
    org_repo: OrgRepo,
    org_id: str,
    job_codes: list[str],
    *,
    level: str,
) -> ResolutionResult:
    # An org that no longer exists / is disabled must not resurrect itself.
    if org_repo.get_org(org_id) is None:
        logger.warning("resolved org %r is missing or inactive; failing closed", org_id)
        return ResolutionResult(status="pending")
    # Provision *before* expanding, so a brand-new role code receives its
    # minimal package on this very login rather than the next one. Only reached
    # once a tenant matched, so an unresolved (pending) user never seeds rows.
    created = provision_missing_mappings(org_repo, org_id, job_codes)
    role_keys = expand_role_keys(org_repo, org_id, job_codes)
    return ResolutionResult(
        org_id=org_id,
        role_keys=role_keys,
        status="active",
        matched_level=level,
        matched_org=org_id,
        created_mappings=created,
    )
