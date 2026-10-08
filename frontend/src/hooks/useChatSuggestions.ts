/**
 * useChatSuggestions — TanStack Query hook for a dataset's starter questions
 * (guardrailed-chat task 6.5, Requirement 1.4).
 *
 * Backs {@link SuggestionChips}: it fetches the 3–4 suggested questions from
 * `GET /datasets/{id}/chat/suggestions` (the API service's non-AI route,
 * `app/chat/suggestions_api.py`), which are theme-based with general fallbacks.
 * The component renders whatever strings come back; this hook is a thin,
 * read-only query, so there is no mutation or cache patching here.
 *
 * Hook style mirrors {@link useChatHistory} / {@link useDataset}: a stable
 * literal query key exported for reuse, the typed `src/api/chat.ts` client as
 * the query fn, and a `datasetId`-gated `enabled` so the panel can mount the
 * chips before the id is known. The key is exported as
 * {@link chatSuggestionsQueryKey} for symmetry with the other chat hooks (a
 * single source of truth should any later task want to prefetch or invalidate
 * it, e.g. after an `active_version` change swaps the themes).
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { getChatSuggestions } from "../api/chat";

/**
 * Literal query key for a dataset's chat suggestions.
 *
 * Keyed under `['chat-suggestions', id]`. Suggestions depend on the active
 * version's themes, so a future active-version-change handler could invalidate
 * this exact key to re-fetch the refreshed suggestions; imported rather than
 * re-declared so there is a single source of truth.
 */
export function chatSuggestionsQueryKey(id: string): readonly unknown[] {
  return ["chat-suggestions", id];
}

/**
 * Query a dataset's 3–4 suggested starter questions (Requirement 1.4).
 *
 * `enabled` is gated on `id` so the chips can mount before the id is known.
 * Returns the standard query result; {@link SuggestionChips} reads `data` (the
 * question strings) and `isPending`/`isError` to decide what to render.
 *
 * @param id - the dataset id, or null before it is known.
 */
export function useChatSuggestions(
  id: string | null,
): UseQueryResult<string[], Error> {
  return useQuery<string[], Error>({
    queryKey: chatSuggestionsQueryKey(id ?? "__none__"),
    queryFn: () => getChatSuggestions(id as string),
    enabled: id != null,
  });
}
