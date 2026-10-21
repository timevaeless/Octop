import { afterEach, describe, expect, it } from "vitest";
import {
  ROUND_LAYOUT_STORAGE_KEY,
  isRoundLayoutEnabled,
  splitTurnRounds,
} from "./turnRoundLayout";
import type { ChatMessage } from "../hooks/useChat";

let seq = 0;

function assistant(
  text: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage {
  seq += 1;
  return {
    id: `a${seq}`,
    role: "assistant",
    content: text,
    status: "done",
    timestamp: seq,
    ...extra,
  };
}

function thinkingAssistant(thinking: string): ChatMessage {
  return assistant("", {
    contentBlocks: [
      { type: "thinking", content: thinking },
    ],
  });
}

function tool(name: string, output: string | null): ChatMessage {
  seq += 1;
  return {
    id: `t${seq}`,
    role: "assistant",
    content: "",
    status: "done",
    timestamp: seq,
    toolData: {
      name,
      callId: `c${seq}`,
      arguments: "{}",
      ...(output ? { output } : {}),
    },
  };
}

describe("isRoundLayoutEnabled", () => {
  afterEach(() => {
    window.localStorage.removeItem(ROUND_LAYOUT_STORAGE_KEY);
  });

  it("defaults to on without any setup", () => {
    expect(isRoundLayoutEnabled()).toBe(true);
  });

  it("turns off only for the explicit kill-switch value", () => {
    window.localStorage.setItem(ROUND_LAYOUT_STORAGE_KEY, "off");
    expect(isRoundLayoutEnabled()).toBe(false);
    window.localStorage.removeItem(ROUND_LAYOUT_STORAGE_KEY);
    expect(isRoundLayoutEnabled()).toBe(true);
    window.localStorage.setItem(ROUND_LAYOUT_STORAGE_KEY, "on");
    expect(isRoundLayoutEnabled()).toBe(true);
  });
});

describe("splitTurnRounds", () => {
  afterEach(() => {
    window.localStorage.removeItem(ROUND_LAYOUT_STORAGE_KEY);
    seq = 0;
  });

  it("returns null when the kill switch is explicitly off", () => {
    window.localStorage.setItem(ROUND_LAYOUT_STORAGE_KEY, "off");
    const messages = [assistant("t1"), tool("search", "ok"), assistant("t2")];
    expect(splitTurnRounds(messages)).toBeNull();
  });

  it("returns null for empty input", () => {
    expect(splitTurnRounds([])).toBeNull();
  });

  it("returns null when the turn never cuts (no completed tool)", () => {
    expect(splitTurnRounds([assistant("你好"), assistant("世界")])).toBeNull();
  });

  it("returns null while the tool is still in flight", () => {
    const messages = [
      assistant("text1"),
      tool("search", null),
      assistant("text2"),
    ];
    expect(splitTurnRounds(messages)).toBeNull();
  });

  it("returns null for team-stamped turns", () => {
    const messages = [
      assistant("t1", { speakerAgentId: "host" }),
      tool("search", "ok"),
      assistant("t2", { speakerAgentId: "host" }),
    ];
    expect(splitTurnRounds(messages)).toBeNull();
  });

  it("returns null for wrapup turns", () => {
    const messages = [
      assistant("t1", { teamWrapup: true }),
      tool("search", "ok"),
      assistant("t2", { teamWrapup: true }),
    ];
    expect(splitTurnRounds(messages)).toBeNull();
  });

  it("cuts one round per completed tool boundary", () => {
    const messages = [
      assistant("我先看看数据"),
      tool("search", "ok"),
      assistant("数据如下：A 占优"),
      tool("calc", "42"),
      assistant("结论：选 A"),
    ];
    const rounds = splitTurnRounds(messages);
    expect(rounds).not.toBeNull();
    expect(rounds).toHaveLength(3);
    expect(rounds?.[0].split.answerMessage?.content).toBe("我先看看数据");
    expect(rounds?.[1].split.answerMessage?.content).toBe("数据如下：A 占优");
    expect(rounds?.[2].split.answerMessage?.content).toBe("结论：选 A");
    expect(rounds?.[0].split.tools).toHaveLength(1);
    expect(rounds?.[0].split.tools[0].toolData?.name).toBe("search");
    expect(rounds?.[1].split.tools[0].toolData?.name).toBe("calc");
    expect(rounds?.[2].split.tools).toHaveLength(0);
  });

  it("keeps consecutive completed tools inside one round", () => {
    const messages = [
      assistant("t1"),
      tool("search", "1"),
      tool("calc", "2"),
      assistant("t2"),
    ];
    const rounds = splitTurnRounds(messages);
    expect(rounds).toHaveLength(2);
    expect(rounds?.[0].split.tools).toHaveLength(2);
  });

  it("keeps a thinking-only message as the head of its own round", () => {
    const messages = [
      thinkingAssistant("想一下"),
      tool("search", "ok"),
      assistant("答案"),
    ];
    const rounds = splitTurnRounds(messages);
    expect(rounds).toHaveLength(2);
    expect(rounds?.[0].split.thinkings).toHaveLength(1);
    expect(rounds?.[0].split.thinkings[0].content).toBe("想一下");
    expect(rounds?.[0].split.answerMessage).toBeNull();
    expect(rounds?.[1].split.answerMessage?.content).toBe("答案");
  });

  it("keeps the error row and the recovery text in the same round", () => {
    const failure = assistant("查询失败", { status: "error" });
    const rounds = splitTurnRounds([
      assistant("t1"),
      tool("search", "ok"),
      failure,
      assistant("最终答案"),
    ]);
    expect(rounds).toHaveLength(2);
    expect(rounds?.[1].split.errorMessages?.[0].id).toBe(failure.id);
    expect(rounds?.[1].split.answerMessage?.content).toBe("最终答案");
  });

  it("never loses answer text (lossless guard holds)", () => {
    const texts = ["第一段叙述", "第二段叙述", "最终结论"];
    const messages = [
      assistant(texts[0]),
      tool("search", "ok"),
      assistant(texts[1]),
      tool("calc", "42"),
      assistant(texts[2]),
    ];
    const rounds = splitTurnRounds(messages);
    const joined = (rounds ?? [])
      .map(
        (round) =>
          [
            round.split.answerMessage ? round.split.answerMessage.content : "",
            ...(round.split.errorMessages ?? []).map((m) => m.content),
          ].join("\n"),
      )
      .join("\n");
    for (const text of texts) {
      expect(joined).toContain(text);
    }
  });
});
