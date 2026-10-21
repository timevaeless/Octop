/**
 * T4-pluggable round layout for assistant turns (WorkBuddy-style layering).
 *
 * Splits a solo assistant ReAct turn into per-LLM-step rounds at completed
 * tool boundaries. Each round reuses the existing ``splitAssistantTurn`` so
 * its slice is shape-identical to the legacy turn split, then renders as its
 * own process fold + answer bubble: "think -> say -> work -> think -> say"
 * reads top-down instead of collapsing into one fold + one concatenated
 * answer.
 *
 * Fail-safe contract — the module returns/renders nothing on its own and the
 * caller falls back to the untouched legacy JSX whenever ``splitTurnRounds``
 * returns ``null``:
 *  - kill switch: the layout is ON by default; localStorage
 *    ``octop.chat.roundLayout = "off"`` falls back to legacy rendering;
 *  - team-stamped or wrapup turns stay legacy (member bubbles continue
 *    through tools by design);
 *  - turns that do not cut into >= 2 rounds stay legacy;
 *  - any unexpected shape raises into the catch-all and stays legacy;
 *  - lossless guard: every input text must survive into some round's answer
 *    or error, otherwise the whole split is rejected.
 */
import { Fragment, type ReactNode } from "react";
import type { ChatMessage } from "../hooks/useChat";
import {
  deriveMessageContent,
  splitAssistantTurn,
  toAnswerOnlyMessage,
  type AssistantTurnSplit,
} from "./messageContent";
import { messageSpeakerId } from "./messageGrouping";
import MessageBubble from "../components/MessageBubble";
import {
  TurnProcessBlocks,
  turnHasVisibleProcess,
  turnHasProcessSummary,
} from "../components/TurnProcessBlocks";
import styles from "../index.module.less";

export const ROUND_LAYOUT_STORAGE_KEY = "octop.chat.roundLayout";

/** On by default; localStorage ``"off"`` is the per-browser kill switch. */
export function isRoundLayoutEnabled(): boolean {
  try {
    return window.localStorage.getItem(ROUND_LAYOUT_STORAGE_KEY) !== "off";
  } catch {
    return true;
  }
}

export interface TurnRound {
  /** Stable React key — id of the round's first message. */
  key: string;
  messages: ChatMessage[];
  /** Legacy-shape split of exactly this round's slice. */
  split: AssistantTurnSplit;
}

interface TurnSplitOptions {
  joinAnswerFragments?: boolean;
}

/**
 * Split an assistant turn into rounds, or ``null`` to keep legacy rendering.
 * Never throws — any internal failure degrades to the legacy path.
 */
export function splitTurnRounds(
  messages: ChatMessage[],
  opts?: TurnSplitOptions,
): TurnRound[] | null {
  try {
    return computeTurnRounds(messages, opts);
  } catch {
    return null;
  }
}

/** Visible answer text of a message (tool rows never carry answer text). */
function visibleTextOf(message: ChatMessage): string {
  if (message.toolData) return "";
  const { textContent } = deriveMessageContent(message);
  return textContent.trim();
}

/** True when this assistant row carries thinking or answer text. */
function hasRoundContent(message: ChatMessage): boolean {
  if (message.toolData || message.role !== "assistant") return false;
  const { thinkingParts, textContent } = deriveMessageContent(message);
  return thinkingParts.length > 0 || Boolean(textContent.trim());
}

/** Completed tools cut rounds; in-flight ones never do. */
function isCompletedTool(message: ChatMessage): boolean {
  return Boolean(message.toolData && message.toolData.output);
}

function computeTurnRounds(
  messages: ChatMessage[],
  opts?: TurnSplitOptions,
): TurnRound[] | null {
  // The kill switch is enforced here too (not just at call sites) so a
  // future caller cannot bypass it accidentally.
  if (!isRoundLayoutEnabled()) return null;
  if (!messages || messages.length === 0) return null;

  // Team rooms own their bubble semantics (members continue through tools,
  // wrapup is a styled trailing bubble) — never re-layout those.
  for (const message of messages) {
    if (messageSpeakerId(message)) return null;
    if (message.teamWrapup) return null;
  }

  const slices: ChatMessage[][] = [];
  let current: ChatMessage[] = [];
  let sawCompletedTool = false;
  for (const message of messages) {
    if (
      current.length > 0 &&
      sawCompletedTool &&
      hasRoundContent(message)
    ) {
      slices.push(current);
      current = [];
      sawCompletedTool = false;
    }
    current.push(message);
    if (isCompletedTool(message)) sawCompletedTool = true;
  }
  if (current.length > 0) slices.push(current);

  // A turn that never cuts (simple Q&A, in-flight tool only) stays legacy.
  if (slices.length < 2) return null;

  const rounds: TurnRound[] = slices.map((slice, index) => ({
    key: slice[0]?.id || `round-${index}`,
    messages: slice,
    split: splitAssistantTurn(slice, opts),
  }));

  // Lossless guard: every answer/error text of the input must survive into
  // some round. joinAnswerParts only concatenates with optional separators
  // and dedups by substring, so per-part containment is a solid invariant.
  const roundTexts = rounds.map((round) =>
    [
      round.split.answerMessage ? round.split.answerMessage.content : "",
      ...(round.split.errorMessages ?? []).map(
        (message) => message.content || "",
      ),
    ].join("\n"),
  );
  for (const message of messages) {
    const text = visibleTextOf(message);
    if (!text) continue;
    if (!roundTexts.some((blob) => blob.includes(text))) return null;
  }

  return rounds;
}

interface RoundBubbleHandlers {
  onRegenerate?: (messageId: string) => void;
  onDisableStreamUsage?: () => Promise<boolean>;
  onEditUserMessage?: (messageId: string, newText: string) => void;
  onForkAssistantMessage?: (messageId: string) => void;
  forkDisabled?: boolean;
  forkDisabledHint?: string;
  onRunShellCommand?: (code: string) => void;
  shellCommandDisabled?: boolean;
  shellCommandDisabledTitle?: string;
}

interface TurnRoundsBlockProps extends RoundBubbleHandlers {
  rounds: TurnRound[];
  /** Already gated upstream (turn streaming minus pending-HITL freeze). */
  isStreaming: boolean;
  onAcpPermissionSelect?: (message: string) => void;
  hideToolMedia: boolean;
  /** Turn-level speaker for the fold avatar column. */
  foldAgentId: string | null;
  agentId: string | null;
  /** Rendered once, after the first round that owns a process summary. */
  todoPanel?: ReactNode;
}

function RoundErrorBubbles({
  messages,
  agentId,
  showAvatar,
  onRegenerate,
  onDisableStreamUsage,
  onEditUserMessage,
  onForkAssistantMessage,
  forkDisabled,
  forkDisabledHint,
  onRunShellCommand,
  shellCommandDisabled,
  shellCommandDisabledTitle,
}: {
  messages: ChatMessage[] | undefined;
  agentId: string | null;
  showAvatar: boolean;
} & RoundBubbleHandlers) {
  if (!messages?.length) return null;
  return (
    <>
      {messages.map((message, index) => (
        <div key={message.id} className={styles.assistantTurnAnswer}>
          <MessageBubble
            message={message}
            agentId={agentId}
            showAvatar={showAvatar && index === 0}
            groupPosition="only"
            onRegenerate={onRegenerate}
            onDisableStreamUsage={onDisableStreamUsage}
            onEditUserMessage={onEditUserMessage}
            onForkAssistantMessage={onForkAssistantMessage}
            forkDisabled={forkDisabled}
            forkDisabledHint={forkDisabledHint}
            onRunShellCommand={onRunShellCommand}
            shellCommandDisabled={shellCommandDisabled}
            shellCommandDisabledTitle={shellCommandDisabledTitle}
          />
        </div>
      ))}
    </>
  );
}

/** Per-round fold + answer + errors, top-down in arrival order. */
export function TurnRoundsBlock({
  rounds,
  isStreaming,
  onAcpPermissionSelect,
  hideToolMedia,
  foldAgentId,
  agentId,
  todoPanel,
  onRegenerate,
  onDisableStreamUsage,
  onEditUserMessage,
  onForkAssistantMessage,
  forkDisabled,
  forkDisabledHint,
  onRunShellCommand,
  shellCommandDisabled,
  shellCommandDisabledTitle,
}: TurnRoundsBlockProps) {
  const lastIdx = rounds.length - 1;
  // Rounds >= 2 always contain at least one completed tool, so some round
  // owns a fold; the avatar anchors there exactly once per turn.
  const firstSummaryIdx = rounds.findIndex((round) =>
    turnHasProcessSummary(round.split),
  );
  // Legacy avatar fallback: when no round owns a fold (all tools rendered as
  // pinned rich-UI blocks), the avatar anchors to the first rendered answer
  // or error bubble instead of disappearing.
  const fallbackAvatarIdx =
    firstSummaryIdx < 0
      ? rounds.findIndex(
          (round) =>
            Boolean(round.split.answerMessage) ||
            (round.split.errorMessages?.length ?? 0) > 0,
        )
      : -1;
  return (
    <>
      {rounds.map((round, idx) => {
        const isLast = idx === lastIdx;
        const showFold = turnHasVisibleProcess(round.split);
        // Regenerate/fork stay on the final answer only — one actionable
        // bubble per turn, matching the legacy single-answer semantics.
        const roundHandlers: RoundBubbleHandlers = {
          onRegenerate: isLast ? onRegenerate : undefined,
          onDisableStreamUsage,
          onEditUserMessage,
          onForkAssistantMessage: isLast ? onForkAssistantMessage : undefined,
          forkDisabled,
          forkDisabledHint,
          onRunShellCommand,
          shellCommandDisabled,
          shellCommandDisabledTitle,
        };
        return (
          <Fragment key={round.key}>
            {showFold ? (
              <TurnProcessBlocks
                split={round.split}
                isStreaming={isStreaming && isLast}
                onAcpPermissionSelect={onAcpPermissionSelect}
                hideToolMedia={hideToolMedia}
                agentId={foldAgentId}
                showAvatar={idx === firstSummaryIdx}
              />
            ) : null}
            {todoPanel &&
            (idx === firstSummaryIdx || (firstSummaryIdx < 0 && isLast)) ? (
              <div className={styles.turnInset}>{todoPanel}</div>
            ) : null}
            {round.split.answerMessage ? (
              <div className={styles.assistantTurnAnswer}>
                <MessageBubble
                  message={toAnswerOnlyMessage(round.split.answerMessage)}
                  agentId={agentId}
                  showAvatar={firstSummaryIdx < 0 && idx === fallbackAvatarIdx}
                  groupPosition="only"
                  {...roundHandlers}
                />
              </div>
            ) : null}
            <RoundErrorBubbles
              messages={round.split.errorMessages}
              agentId={agentId}
              showAvatar={
                firstSummaryIdx < 0
                  ? idx === fallbackAvatarIdx && !round.split.answerMessage
                  : false
              }
              {...roundHandlers}
            />
          </Fragment>
        );
      })}
    </>
  );
}
