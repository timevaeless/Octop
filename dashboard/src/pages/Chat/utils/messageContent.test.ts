import { describe, expect, it } from "vitest";
import { splitAssistantTurn } from "./messageContent";
import type { ChatMessage } from "../hooks/useChat";

let seq = 0;
function step(thinking: string, text: string): ChatMessage {
  seq += 1;
  return {
    id: `a${seq}`,
    role: "assistant",
    content: text,
    status: "done",
    timestamp: seq,
    contentBlocks: [
      { type: "thinking", content: thinking },
      { type: "text", content: text },
    ],
  };
}
function tool(name: string): ChatMessage {
  seq += 1;
  return {
    id: `t${seq}`,
    role: "assistant",
    content: "",
    status: "done",
    timestamp: seq,
    toolData: { name, callId: `c${seq}`, arguments: "{}", output: "ok" },
  };
}

describe("settle answer picks last text only", () => {
  it("multi-round turn with joinAnswerFragments=false", () => {
    const messages = [
      step("想1", "我来处理这个查询。先确认当前时间和表结构口径。"),
      tool("read_file"),
      step("想2", "先按 skill 规程自检 SQL、口径明确，开始写 SQL 并自检。"),
      tool("run_sql"),
      step("想3", "自检通过。执行查询，查询成功。"),
      tool("query"),
      step("想4", "按规程沉淀这条可复用查询，然后汇报。"),
      tool("save_query"),
      step("想5", "结论：本月共发生垫款流水 18 笔、合计 84.65 万元。"),
    ];
    const split = splitAssistantTurn(messages, { joinAnswerFragments: false });
    expect(split.answerMessage?.content).toBe(
      "结论：本月共发生垫款流水 18 笔、合计 84.65 万元。",
    );
  });

  it("single final message holding all narration (data-shape suspect)", () => {
    // If the LAST row itself carries the whole recap, the settle answer
    // faithfully contains it — frontend cannot tell narration from conclusion.
    const messages = [
      tool("read_file"),
      tool("run_sql"),
      step(
        "想5",
        "我来处理这个查询。先确认当前时间和表结构口径。 自检通过。 执行查询，查询成功。 按规程沉淀这条可复用查询，然后汇报。结论：本月共发生垫款流水 18 笔。",
      ),
    ];
    const split = splitAssistantTurn(messages, { joinAnswerFragments: false });
    expect(split.answerMessage?.content).toContain("我来处理这个查询");
    expect(split.answerMessage?.content).toContain("结论：");
  });
});
