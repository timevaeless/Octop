/**
 * Template-install guard logic for ExpertComposerDefaultsFields.
 *
 * Extracted so the community composer component stays a thin caller: the
 * multi-tenant guard (dropping template-prefilled connector/KB references the
 * installer cannot use) lives here as pure functions.
 */

/** Counts reported via the "skipped" warning Alert. */
export interface SkippedTemplateRefs {
  connectors: number;
  knowledgeBases: number;
}

export interface TemplateInstallGuardInput {
  /** Currently selected connector server names (form value). */
  selectedConnectors: string[];
  /** Server names the installer is actually allowed to use. */
  assignableConnectorValues: string[];
  /** Currently selected knowledge-base ids (form value). */
  selectedKnowledgeBaseIds: string[];
  /** Knowledge-base ids the installer is actually allowed to use. */
  assignableKnowledgeBaseIds: string[];
}

export interface TemplateInstallGuardResult {
  nextConnectors: string[];
  nextKnowledgeBaseIds: string[];
  skipped: SkippedTemplateRefs;
}

/**
 * Drop selected values missing from the assignable lists. Callers must only
 * invoke this in create mode (editing an existing expert must never silently
 * alter its configuration) and only once both option lists have finished
 * loading — otherwise valid selections would be purged prematurely.
 *
 * Safe with a missing agentId: the assignable endpoints fall back to the
 * caller's own scope, so an empty result here reflects real data only after
 * loading completed.
 *
 * Returns null when nothing needs dropping — no form write, no warning.
 */
export function applyTemplateInstallGuard(
  input: TemplateInstallGuardInput,
): TemplateInstallGuardResult | null {
  const knownConnectors = new Set(input.assignableConnectorValues);
  const nextConnectors = input.selectedConnectors.filter((value) =>
    knownConnectors.has(value),
  );
  const knownKnowledgeBases = new Set(input.assignableKnowledgeBaseIds);
  const nextKnowledgeBaseIds = input.selectedKnowledgeBaseIds.filter((id) =>
    knownKnowledgeBases.has(id),
  );
  const skippedConnectors =
    input.selectedConnectors.length - nextConnectors.length;
  const skippedKnowledgeBases =
    input.selectedKnowledgeBaseIds.length - nextKnowledgeBaseIds.length;
  if (!skippedConnectors && !skippedKnowledgeBases) return null;
  return {
    nextConnectors,
    nextKnowledgeBaseIds,
    skipped: {
      connectors: skippedConnectors,
      knowledgeBases: skippedKnowledgeBases,
    },
  };
}
