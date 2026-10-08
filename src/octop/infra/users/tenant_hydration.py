"""JIT tenant resolution persistence + User hydration (T1-1, pluggable
module).

User convention 2026-09-26 (extract, don't weave). ``org_resolution.py``
owns the five-level *decision*; this module owns the *persistence
orchestration* around it, which used to live inside
``infra/users/manager.py``:

* :func:`apply_jit_resolution` — run the chain on every SSO login and
  persist the outcome: membership upsert (role keys re-expanded from
  ``org_role_mappings``, never merged), ``default_org_id`` claimed only
  while NULL (an admin-assigned default wins), ``token_epoch`` bumped when
  the visible context changed, the level-⑤ pending trace, the audit echo;
* :func:`active_role_keys_for` — the membership read behind every
  ``(re)hydration`` path (boot, password login, SSO resolve/bind, enable):
  the membership row is the authority for ``role_keys``, never ``users``.

Upstream touch points (``manager.py``): one lazy hook line replacing the
whole ``_apply_jit_resolution`` body, and the hydration branch inside
``_user_from_row``. Deleting this module together with those hooks
restores community behaviour verbatim: SSO logins would no longer persist
a tenant context at all.
"""

from __future__ import annotations

from typing import Any

__all__ = ["active_role_keys_for", "apply_jit_resolution"]


def active_role_keys_for(services: Any, user_id: int, default_org_id: str | None) -> list[str]:
    """Active membership role keys for the user's default tenant ([] when none).

    Review P1-3: the result feeds the fresh token's ``rls`` claim, and the
    password-login hydration path has no JIT pass to prune with — so keys
    whose package was deleted after the membership JSON was written are
    dropped here instead of riding in the claim until the next SSO login.
    """
    if default_org_id is None:
        return []
    membership = services.org_repo.get_membership(user_id, default_org_id)
    if membership is not None and membership.status == "active":
        keys = list(membership.role_keys)
        if not keys:
            return []
        from octop.infra.db.repos.tenant_roles import tenant_role_repo_for

        stale = set(
            tenant_role_repo_for(services.org_repo).validate_keys_visible(
                default_org_id, keys
            )
        )
        if not stale:
            return keys
        return [k for k in keys if k not in stale]
    return []


def apply_jit_resolution(services: Any, user_id: int, provider_id: int, claims: dict[str, Any]) -> None:
    """Run the five-level tenant chain and persist the outcome (T1-1).

    Idempotent on every login: membership role keys are re-expanded from
    ``org_role_mappings`` and corrected (never merged); ``default_org_id``
    is only claimed while it is still NULL (an admin-assigned default
    wins); ``token_epoch`` is bumped whenever the visible context changed
    so stale tokens die on their next request.

    028: mapped keys are *unioned* with ``org_member_grants`` — admin
    grants made through the management faces (member roster, user
    membership, pending assign) survive the re-expansion instead of being
    wiped. Grant-derived keys are revoked by the next admin write that
    omits them; mapping-derived keys keep following the mapping table
    (including the 018 auto-provision of unknown codes to the default
    package). Pluggable: deleting the union below (and the grants table +
    write hooks) restores the pre-028 replace-wholesale behaviour.
    """
    from octop.infra.users.lifecycle import record_pending_resolution
    from octop.infra.users.org_resolution import resolve_org_and_roles

    provider = services.sso_repo.get_by_id(provider_id)
    idp_alias = provider.idp_alias if provider is not None else ""
    row = services.user_repo.get(user_id)
    if row is None:
        return
    result = resolve_org_and_roles(
        services.org_repo,
        idp_alias=idp_alias,
        claims=claims,
        user_id=user_id,
    )
    audit_payload = f"level={result.matched_level or 'none'} org={result.org_id or '-'}"
    if result.created_mappings:
        # A permission was granted without an admin asking for it — never
        # do that silently. (The packages themselves land in the ``roles=``
        # part below, which is written whenever the membership changed.)
        audit_payload += f" auto_created={','.join(result.created_mappings)}"

    if result.org_id is not None and not result.is_pending:
        # 028: mapped ∪ granted — the grant rows carry what admins gave
        # this specific user; mapped keys alone would wipe them.
        effective_keys = list(result.role_keys)
        for granted in services.org_repo.list_member_grants(user_id, result.org_id):
            if granted not in effective_keys:
                effective_keys.append(granted)
        # Packages can be deleted after a grant/mapping referenced them (the
        # FK cascades the rows, but the membership ``role_keys`` JSON above
        # survives). Drop keys that no longer resolve to a visible package —
        # the recompute below then sees a change and bumps the epoch, so the
        # stale claim dies on the member's next login.
        from octop.infra.db.repos.tenant_roles import tenant_role_repo_for

        stale = set(
            tenant_role_repo_for(services.org_repo).validate_keys_visible(
                result.org_id, effective_keys
            )
        )
        if stale:
            effective_keys = [k for k in effective_keys if k not in stale]
        existing = services.org_repo.get_membership(user_id, result.org_id)
        membership_changed = (
            existing is None
            or existing.status != "active"
            or existing.role_keys != effective_keys
        )
        services.org_repo.upsert_membership(
            user_id,
            result.org_id,
            role_keys=effective_keys,
            status="active",
            source="jit",
        )
        org_claimed = False
        if row.default_org_id is None:
            services.user_repo.set_default_org_id(user_id, result.org_id)
            org_claimed = True
        if membership_changed or org_claimed:
            services.user_repo.bump_token_epoch(user_id)
            if membership_changed:
                audit_payload += f" roles={','.join(effective_keys) or '-'}"
    else:
        # T4-3: level-⑤ logins get no membership row (org_id is NOT NULL
        # and there is no org to reference) — leave the discoverable
        # trace for the admin backlog instead.
        record_pending_resolution(services, row, idp_alias=idp_alias)

    services.audit_repo.write(
        actor=row.username,
        action="user.jit_resolve",
        target=row.username,
        payload=audit_payload,
        org_id=row.default_org_id,
    )
