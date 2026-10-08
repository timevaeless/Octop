/**
 * useChatComposerResources.test.tsx — per-expert knowledge base selection.
 *
 * Regression for the "remember KB selection" bug: one global selection array
 * + a touched flag reset on expert switch meant Expert B's selection leaked
 * into (or replaced) Expert A's remembered selection when switching back.
 * Selection is now persisted per expert (chatStorage) and restored on switch.
 */

import { renderHook, waitFor, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../api/modules/connectors", () => ({
  connectorsApi: {
    // composer reads the ASSIGNABLE listing — the same name namespace as
    // expert.mcp_servers and turn validation (catalog rows are harness-keyed)
    listAssignable: vi.fn().mockResolvedValue([
      {
        status: "active",
        has_credentials: true,
        mcp_server_name: "c1",
        display_name: "C1",
        owner_user_id: 7,
        kind: "mcp",
      },
      {
        status: "active",
        has_credentials: true,
        mcp_server_name: "c2",
        display_name: "C2",
        owner_user_id: 7,
        kind: "mcp",
      },
    ]),
  },
}));
vi.mock("../../../api/modules/provider", () => ({
  providerApi: {
    listResolvedModels: vi.fn().mockResolvedValue([]),
    getActiveModel: vi.fn().mockResolvedValue(null),
  },
}));
vi.mock("../../../api/modules/preferences", () => ({
  preferencesApi: {
    get: vi.fn().mockResolvedValue(null),
    set: vi.fn().mockResolvedValue(undefined),
  },
}));
vi.mock("../../../api/modules/octopThreads", () => ({
  octopThreadsApi: { patch: vi.fn().mockResolvedValue({}) },
}));
vi.mock("../../../api/request", () => ({
  request: vi.fn().mockResolvedValue(null),
}));
vi.mock("../../../api/modules/knowledgeBases", () => ({
  knowledgeBasesApi: {
    getCapability: vi.fn().mockResolvedValue({ usable: true }),
    list: vi.fn().mockResolvedValue([
      { id: "k1", default_open: false, owner_user_id: 7 },
      { id: "k2", default_open: false, owner_user_id: 7 },
      { id: "k3", default_open: false, owner_user_id: 7 },
      { id: "k4", default_open: false, owner_user_id: 7 },
    ]),
  },
}));
vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ id: 7 }),
}));
vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({
    agents: [
      {
        agent_id: "expertA",
        knowledge_base_ids: ["k1"],
        mcp_servers: ["c1"],
      },
      {
        agent_id: "expertB",
        knowledge_base_ids: ["k3"],
        mcp_servers: ["c2"],
      },
    ],
  }),
}));

import { useChatComposerResources } from "./useChatComposerResources";

beforeEach(() => {
  localStorage.clear();
});

describe("useChatComposerResources — per-expert KB selection", () => {
  it("restores expert A's manual selection after switching A -> B -> A", async () => {
    const threadId = "thread-existing"; // existing session → saved prefs apply
    const { result, rerender } = renderHook(
      ({ agentId }: { agentId: string }) =>
        useChatComposerResources(agentId, threadId),
      { initialProps: { agentId: "expertA" } },
    );

    // A starts with its expert default (k1)
    await waitFor(() =>
      expect(result.current.selectedKnowledgeBaseIds).toEqual(["k1"]),
    );

    // user manually adds k2 in expert A
    act(() => result.current.handleKnowledgeBaseIdsChange(["k1", "k2"]));
    expect(result.current.selectedKnowledgeBaseIds).toEqual(["k1", "k2"]);

    // switch to expert B: B's own default, nothing leaked from A
    rerender({ agentId: "expertB" });
    await waitFor(() =>
      expect(result.current.selectedKnowledgeBaseIds).toEqual(["k3"]),
    );

    // user selects k3+k4 in expert B
    act(() => result.current.handleKnowledgeBaseIdsChange(["k3", "k4"]));

    // switch back to expert A: remembered k1+k2 (was: k3/k4 leak or reset)
    rerender({ agentId: "expertA" });
    await waitFor(() =>
      expect(result.current.selectedKnowledgeBaseIds).toEqual(["k1", "k2"]),
    );

    // and back to B: remembered k3+k4
    rerender({ agentId: "expertB" });
    await waitFor(() =>
      expect(result.current.selectedKnowledgeBaseIds).toEqual(["k3", "k4"]),
    );
  });

  it("does not leak a cleared selection across experts", async () => {
    const threadId = "thread-existing";
    const { result, rerender } = renderHook(
      ({ agentId }: { agentId: string }) =>
        useChatComposerResources(agentId, threadId),
      { initialProps: { agentId: "expertA" } },
    );
    await waitFor(() =>
      expect(result.current.selectedKnowledgeBaseIds).toEqual(["k1"]),
    );

    // user clears A's selection entirely (explicit empty is a choice)
    act(() => result.current.handleKnowledgeBaseIdsChange([]));

    rerender({ agentId: "expertB" });
    await waitFor(() =>
      expect(result.current.selectedKnowledgeBaseIds).toEqual(["k3"]),
    );
    act(() => result.current.handleKnowledgeBaseIdsChange(["k4"]));

    rerender({ agentId: "expertA" });
    await waitFor(() =>
      expect(result.current.selectedKnowledgeBaseIds).toEqual([]),
    );
  });

  it("connector picker is scoped to the expert's configured set; per-expert memory still applies", async () => {
    const threadId = "thread-existing";
    const { result, rerender } = renderHook(
      ({ agentId }: { agentId: string }) =>
        useChatComposerResources(agentId, threadId),
      { initialProps: { agentId: "expertA" } },
    );
    // A only sees its own configured connector (c1); c2 is invisible here.
    await waitFor(() =>
      expect(result.current.chatConnectors.map((c) => c.mcp_server_name)).toEqual([
        "c1",
      ]),
    );
    // expert default = configured connectors
    await waitFor(() => expect(result.current.selectedConnectors).toEqual(["c1"]));

    // explicit opt-out in A is remembered per expert
    act(() => result.current.handleConnectorsChange([]));

    // switch to B: only B's own connector visible, defaults to it
    rerender({ agentId: "expertB" });
    await waitFor(() =>
      expect(result.current.chatConnectors.map((c) => c.mcp_server_name)).toEqual([
        "c2",
      ]),
    );
    await waitFor(() => expect(result.current.selectedConnectors).toEqual(["c2"]));

    // back to A: opt-out remembered (not re-attached); B's c2 not leaked
    rerender({ agentId: "expertA" });
    await waitFor(() => expect(result.current.selectedConnectors).toEqual([]));
  });
});
