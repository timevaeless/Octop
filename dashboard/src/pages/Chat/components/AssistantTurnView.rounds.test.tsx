import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChatMessage } from "../hooks/useChat";
import AssistantTurnView from "./AssistantTurnView";
import { ROUND_LAYOUT_STORAGE_KEY } from "../utils/turnRoundLayout";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, opts?: string | { defaultValue?: string }) => {
      if (typeof opts === "string") return opts;
      return opts?.defaultValue ?? key;
    },
  }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock("../../../hooks/useServerTimezone", () => ({
  useServerTimezone: () => "UTC",
}));

vi.mock("../../../hooks/useCurrentUser", () => ({
  useCurrentUser: () => ({ username: "ada", display_name: "Ada" }),
}));

vi.mock("../../../context/VoiceOutputContext", () => ({
  useVoiceOutputContext: () => ({ speakingId: null, speak: vi.fn() }),
}));

vi.mock("../../../context/AgentContext", () => ({
  useAgent: () => ({
    activeAgentId: "solo",
    activeAgent: {
      agent_id: "solo",
      name: "助手",
      kind: "assistant",
      icon_name: "bot",
    },
    agents: [
      {
        agent_id: "solo",
        name: "助手",
        kind: "assistant",
        icon_name: "bot",
      },
    ],
  }),
}));

function message(
  extra: Partial<ChatMessage> & Pick<ChatMessage, "id">,
): ChatMessage {
  return {
    role: "assistant",
    content: "",
    status: "done",
    timestamp: 1,
    ...extra,
  };
}

function thinkingText(id: string, thinking: string, text: string): ChatMessage {
  return message({
    id,
    contentBlocks: [
      { type: "thinking", content: thinking },
      { type: "text", content: text },
    ],
  });
}

function completedTool(id: string, name: string): ChatMessage {
  return message({
    id,
    toolData: { name, callId: `call-${id}`, arguments: "{}", output: "ok" },
  });
}

const MULTI_ROUND_TURN = [
  thinkingText("a1", "先查压测数据", "我先对比两个方案的吞吐"),
  completedTool("t1", "search_metrics"),
  thinkingText("a2", "再核对报价", "成本侧 B 更低，需要折算 QPS"),
  completedTool("t2", "calc_price"),
  message({ id: "a3", content: "结论：选 A。理由如下……" }),
];

// Live turn: one more thinking+text message still streaming after the tools.
const LIVE_ROUND_TURN = [
  ...MULTI_ROUND_TURN,
  {
    ...thinkingText("a4", "补充核对", "还有一点补充"),
    status: "streaming" as const,
  },
];

const COMBINED_SUMMARY = "已调用 {{tools}} 次工具，{{thinking}} 次深度思考";

describe("AssistantTurnView round layout", () => {
  afterEach(() => {
    window.localStorage.removeItem(ROUND_LAYOUT_STORAGE_KEY);
  });

  it("renders per-round folds with layered answers while streaming", () => {
    render(<AssistantTurnView messages={LIVE_ROUND_TURN} isTurnInProgress />);

    // Two settled rounds show their own summary folds (collapsed), and the
    // active round shows the live hint expanded — the WorkBuddy layering.
    expect(screen.getAllByText(COMBINED_SUMMARY)).toHaveLength(2);
    const liveFold = screen.getByText("整理结果中").closest("button");
    expect(liveFold?.getAttribute("aria-expanded")).toBe("true");
    // Intermediate narration is visible during the live run.
    expect(screen.getByText("我先对比两个方案的吞吐")).toBeInTheDocument();
    expect(screen.getByText("成本侧 B 更低，需要折算 QPS")).toBeInTheDocument();
  });

  it("settles back to the legacy single collapsed fold + final answer", () => {
    render(<AssistantTurnView messages={MULTI_ROUND_TURN} />);

    // One whole-turn fold (collapsed), one final answer — the pre-rounds form.
    const folds = screen.getAllByText(COMBINED_SUMMARY);
    expect(folds).toHaveLength(1);
    expect(folds[0].closest("button")?.getAttribute("aria-expanded")).toBe(
      "false",
    );
    expect(screen.getByText("结论：选 A。理由如下……")).toBeInTheDocument();
    // Intermediate narration is dropped in the settled state.
    expect(screen.queryByText("我先对比两个方案的吞吐")).toBeNull();
    expect(screen.queryByText("成本侧 B 更低，需要折算 QPS")).toBeNull();
  });

  it("stays legacy even while streaming when the kill switch is off", () => {
    window.localStorage.setItem(ROUND_LAYOUT_STORAGE_KEY, "off");
    render(<AssistantTurnView messages={LIVE_ROUND_TURN} isTurnInProgress />);

    // A single live fold, no per-round layering.
    expect(screen.getAllByText("整理结果中")).toHaveLength(1);
    expect(screen.queryByText(COMBINED_SUMMARY)).toBeNull();
  });

  it("keeps a single avatar when a later round carries only an error", () => {
    // Round 0 = think + completed tool (fold owns the avatar); round 1 =
    // an error message that cuts its own round (no fold, no answer).
    const ERRORED_ROUND_TURN = [
      thinkingText("a1", "先分析", "我先分析一下"),
      completedTool("t1", "search_metrics"),
      message({ id: "e1", status: "error" as const, content: "请求失败" }),
    ];
    render(<AssistantTurnView messages={ERRORED_ROUND_TURN} isTurnInProgress />);

    // Exactly one avatar — anchored on the round-0 fold; the trailing error
    // bubble must not add a second one.
    expect(screen.getAllByLabelText("助手")).toHaveLength(1);
    expect(screen.getByText("请求失败")).toBeInTheDocument();
  });

  it("renders tool file attachments below the final answer when settled", () => {
    const FILE_TURN = [
      completedTool("t1", "write_report"),
      message({ id: "a1", content: "结论：日报已生成。" }),
    ];
    // The completed tool carries a structured file attachment.
    FILE_TURN[0].attachments = [
      {
        url: "/api/agents/solo/files/report.xlsx",
        filename: "report.xlsx",
        kind: "file" as const,
      },
    ];
    const { container } = render(<AssistantTurnView messages={FILE_TURN} />);

    const answer = screen.getByText("结论：日报已生成。");
    const file = screen.getByText("report.xlsx");
    // The attachment pill must come after the answer bubble in document order.
    expect(
      answer.compareDocumentPosition(file) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(container).toBeTruthy();
  });

  it("transitions from layered streaming to the legacy settled form", () => {
    const { rerender } = render(
      <AssistantTurnView messages={LIVE_ROUND_TURN} isTurnInProgress />,
    );
    expect(screen.getAllByText(COMBINED_SUMMARY)).toHaveLength(2);
    expect(
      screen
        .getByText("整理结果中")
        .closest("button")
        ?.getAttribute("aria-expanded"),
    ).toBe("true");

    rerender(<AssistantTurnView messages={MULTI_ROUND_TURN} />);
    const folds = screen.getAllByText(COMBINED_SUMMARY);
    expect(folds).toHaveLength(1);
    expect(folds[0].closest("button")?.getAttribute("aria-expanded")).toBe(
      "false",
    );
    expect(screen.queryByText("我先对比两个方案的吞吐")).toBeNull();
  });
});
