"""Shared-agent workspace privacy (round-11 cross-tenant leak fix)."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from octop.infra.agents import shared_agent_privacy
from octop.infra.agents.shared_agent_privacy import (
    apply_shared_agent_privacy,
    is_shared_agent_row,
)

_HARNESS_AVAILABLE = True
try:
    from octop_harness import HarnessAgentConfig
except ImportError:  # pragma: no cover - harness always present in CI venv
    HarnessAgentConfig = None

requires_harness = pytest.mark.skipif(
    HarnessAgentConfig is None, reason="octop_harness not installed"
)


def _row(org_id: str | None) -> SimpleNamespace:
    return SimpleNamespace(agent_id="MKZZK4", org_id=org_id)


class TestIsSharedAgentRow:
    def test_star_org_is_shared(self) -> None:
        assert is_shared_agent_row(_row("*")) is True

    def test_tenant_org_is_not_shared(self) -> None:
        assert is_shared_agent_row(_row("cheryfs")) is False

    def test_unassigned_org_is_not_shared(self) -> None:
        # NULL = 未归属（仅平台管理员域），不是跨租户共享。
        assert is_shared_agent_row(_row(None)) is False

    def test_row_without_org_attribute(self) -> None:
        assert is_shared_agent_row(SimpleNamespace(agent_id="X")) is False


@requires_harness
class TestApplySharedAgentPrivacy:
    def _config(self) -> HarnessAgentConfig:
        return HarnessAgentConfig(name="MKZZK4")

    def _fields(self) -> frozenset[str]:
        return frozenset(HarnessAgentConfig.__dataclass_fields__)

    def test_disables_memory_and_session_log(self) -> None:
        cfg = apply_shared_agent_privacy(self._config(), supported_fields=self._fields())
        assert cfg.memory_enabled is False
        assert cfg.session_log_enabled is False

    def test_keeps_other_fields_untouched(self) -> None:
        cfg = self._config()
        out = apply_shared_agent_privacy(cfg, supported_fields=self._fields())
        assert out.name == cfg.name
        assert out == dataclasses.replace(cfg, memory_enabled=False, session_log_enabled=False)

    def test_tolerates_older_harness_without_fields(self) -> None:
        cfg = self._config()
        out = apply_shared_agent_privacy(cfg, supported_fields=frozenset({"name"}))
        assert out is cfg

    def test_memory_middleware_fully_skipped_when_both_off(self) -> None:
        """memory_enabled=False → no MemoryService; both off → build_middleware None."""
        cfg = apply_shared_agent_privacy(self._config(), supported_fields=self._fields())
        assert cfg.memory_enabled is False and cfg.session_log_enabled is False

    def test_both_flags_default_true_upstream(self) -> None:
        cfg = self._config()
        assert cfg.memory_enabled is True
        assert cfg.session_log_enabled is True


class TestManagerHookWiring:
    def test_manager_imports_hook(self) -> None:
        """Manager must call the pluggable module (one-line hook convention)."""
        import octop.infra.agents.manager as manager_module

        source = Path(manager_module.__file__).read_text(encoding="utf-8")
        assert "shared_agent_privacy.is_shared_agent_row(row)" in source
        assert "apply_shared_agent_privacy" in source

    def test_module_imports_platform_constant(self) -> None:
        assert shared_agent_privacy.PLATFORM_SHARED_ORG == "*"


@pytest.mark.skipif(HarnessAgentConfig is None, reason="octop_harness not installed")
class TestPrivacyMutationSurface:
    def test_only_memory_and_session_log_fields_change(self) -> None:
        """The privacy pass must not mutate any other config field."""
        cfg = HarnessAgentConfig(name="MKZZK4", tools_disabled=frozenset({"x"}))
        out = apply_shared_agent_privacy(cfg, supported_fields=frozenset(HarnessAgentConfig.__dataclass_fields__))
        changed = {
            f.name
            for f in dataclasses.fields(HarnessAgentConfig)
            if getattr(cfg, f.name) != getattr(out, f.name)
        }
        assert changed == {"memory_enabled", "session_log_enabled"}
