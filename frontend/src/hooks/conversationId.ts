/**
 * Per-tab conversation id (guardrailed-chat task 6.4, Requirement 2.6).
 *
 * Each browser tab generates one random `conversation_id` and keeps it in
 * `sessionStorage`, so it persists across reloads of the same tab but is
 * distinct per tab (design "Overview"). It is sent with every chat request and
 * used server-side to scope follow-up context: only Exchanges with the asking
 * tab's conversation id are fed back to the model (Requirement 2.6, design
 * "Message layout").
 *
 * It identifies a conversation, not a person — it carries no visitor-identifying
 * data and is never shown in the UI (design "Overview"; Requirement 5.1: the
 * Exchange "SHALL NOT contain any visitor-identifying data"). Using a random
 * UUID keeps it opaque.
 *
 * `sessionStorage` is the right store: `localStorage` would share one id across
 * every tab (merging distinct conversations into one context), while an
 * in-memory value would be lost on reload. The helper tolerates environments
 * where `sessionStorage` is unavailable or throws (private-mode quirks, SSR) by
 * falling back to a fresh in-memory id, so the chat still works.
 */

/** The `sessionStorage` key under which the tab's conversation id is stored. */
export const CONVERSATION_ID_STORAGE_KEY = "reviewlens.chat.conversation_id";

/** A process-lifetime fallback id used when `sessionStorage` is unavailable. */
let inMemoryFallbackId: string | null = null;

/** Generate a random conversation id (UUID when available, else a random hex). */
function generateId(): string {
  const cryptoObj =
    typeof globalThis !== "undefined"
      ? (globalThis.crypto as Crypto | undefined)
      : undefined;
  if (cryptoObj?.randomUUID) {
    return cryptoObj.randomUUID();
  }
  // Fallback for environments without `crypto.randomUUID`.
  return `conv-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
}

/** Read `sessionStorage` safely, returning null on any access error. */
function readStored(): string | null {
  try {
    if (typeof window === "undefined" || window.sessionStorage == null) return null;
    return window.sessionStorage.getItem(CONVERSATION_ID_STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Write `sessionStorage` safely, swallowing any access error. */
function writeStored(value: string): boolean {
  try {
    if (typeof window === "undefined" || window.sessionStorage == null) return false;
    window.sessionStorage.setItem(CONVERSATION_ID_STORAGE_KEY, value);
    return true;
  } catch {
    return false;
  }
}

/**
 * Return this tab's conversation id, creating and persisting one on first use.
 *
 * Reads the id from `sessionStorage`; if none exists yet, generates a random
 * one, stores it, and returns it. Subsequent calls in the same tab return the
 * same id (persisted across reloads). When `sessionStorage` can't be used, a
 * single in-memory id is created and reused for the page's lifetime.
 */
export function getConversationId(): string {
  const existing = readStored();
  if (existing != null && existing.length > 0) return existing;

  const id = generateId();
  if (!writeStored(id)) {
    // Storage unavailable: keep one stable id in memory for this page load.
    if (inMemoryFallbackId == null) inMemoryFallbackId = id;
    return inMemoryFallbackId;
  }
  return id;
}
