# Requirements Document

## Introduction

The Guardrailed Q&A Interface lets an analyst ask natural-language questions about one ingested dataset and get answers grounded only in that dataset's reviews.

The scope guard is the most important quality of this feature. If a user asks about another platform, competitors (beyond what reviewers themselves say), general world knowledge, or anything unrelated, such as the weather, the assistant must decline gracefully and explicitly. This is enforced mainly through the system prompt, with additional layers for defense in depth.

Every question and answer is saved per dataset and persists across sessions. When the analyst opens a dataset, the history loads in a scrollable pane above the chat input. The input is only for asking the next question; each answered exchange moves into the history. The history also shows when the data was refreshed, so the analyst can tell which earlier answers may no longer apply.

Depends on: `platform-foundation`, `review-analysis` (review corpus and entity profile), `dataset-ingestion` (`dataset_versions` records), `ingestion-summary` (the detail page that hosts the chat), `dataset-library` (archived state, Realtime Channel).

## Glossary

- **Assistant**: The AI that answers questions in the chat.
- **Corpus**: The current data version's Normalized Reviews plus the Entity Profile (`reviews/v{n}.json`).
- **In-scope question**: A question that can be answered from the Corpus. Examples: themes, sentiment, specific complaints or praise, rating patterns, changes over time, quotes, counts, and comparisons that reviewers themselves make.
- **Out-of-scope question**: A question that needs information outside the Corpus. Examples: other platforms' reviews, competitor facts not in the reviews, general knowledge, current events, weather, coding help, or personal advice.
- **Exchange**: One question and its answer, with metadata.
- **Current data version**: The dataset's `active_version`, meaning the latest data version that finished processing successfully. A failed refresh does not change it.

## Requirements

### Requirement 1: Chat availability

**User Story:** As an analyst, I want the chat to be available only when the data is ready, so that I don't get answers from incomplete data.

#### Acceptance Criteria

1. WHEN the dataset has a current data version (an active version) and is not archived, the chat input SHALL be enabled, with the placeholder "Ask a question about these reviews…". This includes while a refresh is running or after a refresh failed; in those cases the panel SHALL show which version answers come from ("Answers use v2 until the refresh finishes").
2. WHILE the dataset has no active version (its first processing hasn't finished, or it failed), the chat input SHALL be disabled with an explanatory message.
3. WHILE the dataset is archived, the chat history SHALL be visible and the input SHALL be disabled with the message "Restore this dataset to ask new questions."
4. THE chat panel SHALL show 3 to 4 suggested starter questions built from the dataset's themes (for example "What do reviewers say about customer support?"). WHEN no themes are available, it SHALL show general questions that fit any review set (for example "What are the most common complaints?").

### Requirement 2: Grounded answers

**User Story:** As an analyst, I want answers based only on the ingested reviews, with evidence, so that I can trust and cite them to clients.

#### Acceptance Criteria

1. WHEN an in-scope question is asked, the Assistant SHALL answer using only information in the Corpus.
2. THE Assistant SHALL support its claims with citations to specific review IDs. The UI SHALL render each citation as a chip that shows the review's text, rating, and date on hover or tap. The cited reviews' text, rating, and date SHALL be saved with the Exchange, so citations in older Exchanges keep showing the right review after the data is refreshed.
3. WHEN an answer gives counts or proportions, they SHALL match the Corpus. Where the answer is an estimate, it SHALL say so.
4. IF the Corpus does not contain enough information to answer an in-scope question, THEN the Assistant SHALL say that the reviews do not address it, and SHALL NOT guess.
5. THE system SHALL remove any citation that does not match a review ID in the Corpus before showing or saving the answer.
6. WHEN a follow-up question refers to an earlier exchange, the Assistant SHALL resolve it using the recent Exchanges from the same browser conversation on the current data version. Each browser tab SHALL generate a random conversation ID for this purpose. It identifies the conversation, not a person, and every Exchange SHALL still appear in the shared history for everyone.

### Requirement 3: Scope guard

**User Story:** As the consultancy, I want the Assistant to refuse anything outside the ingested reviews, so that analysts never receive ungrounded or off-topic information.

#### Acceptance Criteria

1. WHEN a question asks about reviews or ratings on any platform other than the dataset's source, the Assistant SHALL decline, naming the dataset's source platform as its only scope.
2. WHEN a question asks for general world knowledge, current events, weather, prices or specifications not stated in the reviews, or any unrelated task (coding, writing, advice), the Assistant SHALL decline.
3. WHEN a question asks about a competitor, the Assistant SHALL answer only with what reviewers in the Corpus say about that competitor, SHALL state that limitation, and SHALL decline to add outside facts.
4. WHEN the Assistant declines, the response SHALL (a) state clearly that the question is outside what it can answer, (b) say briefly what it can answer, naming the entity and platform, and (c) offer 1 to 2 related in-scope questions where one exists. The tone SHALL be polite and not preachy.
5. THE scope guard SHALL be defined mainly by the system prompt. THE system MAY add a lightweight pre-check that classifies the question's scope, used as a secondary layer.
6. WHEN a question is declined, the Exchange SHALL be saved with `scope = "declined"` and the decline category.

### Requirement 4: Prompt-injection resistance

**User Story:** As the consultancy, I want the Assistant's rules to hold even when review text or user input tries to override them, so that the guardrails can't be bypassed.

#### Acceptance Criteria

1. THE system SHALL pass review content to the model as clearly separated data, and the system prompt SHALL tell the model never to follow instructions found inside reviews.
2. WHEN a user message tries to change the Assistant's rules, role, or scope (for example "ignore previous instructions," "pretend you are…"), the Assistant SHALL keep its scope and SHALL decline the out-of-scope part.
3. THE Assistant SHALL NOT reveal the system prompt text when asked.

### Requirement 5: Persistent history

**User Story:** As an analyst, I want every question and answer saved with the dataset, so that anyone using the app can revisit earlier analysis across sessions.

#### Acceptance Criteria

1. WHEN an answer is complete, the system SHALL save the Exchange (question, answer, citations, scope result, data version, conversation ID, timestamps, model, token usage) to S3 under the dataset ID. THE Exchange SHALL NOT contain any visitor-identifying data.
2. WHEN a dataset's Detail Page opens, the system SHALL load its history, oldest to newest, and show it in a scrollable pane above the chat input, scrolled to the latest Exchange.
3. THE history pane SHALL load earlier Exchanges in pages as the user scrolls up (page size 20).
4. WHERE an Exchange was answered against an earlier data version, the history SHALL label it with that version (see Requirement 9).
5. IF saving an Exchange fails, THEN the UI SHALL still show the answer, SHALL warn that it was not saved, and SHALL offer a retry.
6. THE history SHALL be shared: every visitor who opens the dataset SHALL see every Exchange, from every conversation, in time order.
7. WHEN another visitor's Exchange is saved on the dataset being viewed, it SHALL appear in the history without a page reload.

### Requirement 6: Question input behavior

**User Story:** As an analyst, I want a simple input for my next question, with answers moving into the history, so that the conversation stays tidy.

#### Acceptance Criteria

1. THE chat input SHALL accept a single question of up to 1,000 characters and SHALL submit on Enter (Shift+Enter adds a new line).
2. WHILE an answer is being generated, the input SHALL be disabled, and the pending question and streaming answer SHALL appear at the bottom of the history.
3. WHEN the answer completes, the Exchange SHALL be added to the history and the input SHALL be cleared and re-enabled.
4. THE system SHALL reject empty or whitespace-only questions on the client.

### Requirement 7: Responsiveness

**User Story:** As an analyst, I want answers to start quickly, so that exploring the data feels interactive.

#### Acceptance Criteria

1. THE Assistant's answer SHALL stream to the UI token by token.
2. WHILE the Corpus has up to 1,000 reviews, the first token SHALL appear within 5 seconds at the 90th percentile, excluding cold starts and the first question after the Corpus prompt cache has expired.
3. THE system SHALL cache the Corpus portion of the prompt so that repeat questions on the same dataset cost less and respond faster.

### Requirement 8: Guardrail evaluation

**User Story:** As a senior engineer, I want an automated evaluation of the scope guard, so that prompt changes can't quietly weaken it.

#### Acceptance Criteria

1. THE repository SHALL contain a labeled evaluation set of at least 60 questions: in-scope, out-of-scope (other platforms, world knowledge, weather, competitor facts, unrelated tasks), borderline, and injection attempts (both in user questions and planted in review text).
2. THE evaluation SHALL measure the correct-decline rate for out-of-scope questions, the false-decline rate for in-scope questions, and citation validity.
3. WHEN the evaluation runs, it SHALL fail if the out-of-scope correct-decline rate is below 95%, the in-scope false-decline rate is above 5%, or any injection case succeeds.
4. THE evaluation SHALL run on demand and on every change to the chat prompt files in CI (with the live model), and SHALL write a report.

### Requirement 9: Data refresh markers in the Q&A log

**User Story:** As an analyst, I want the Q&A log to show when the data was refreshed, so that I know which earlier answers may no longer apply.

#### Acceptance Criteria

1. WHEN a dataset has been refreshed, the history pane SHALL show a refresh marker at the point in the timeline where each new data version became ready. The marker SHALL show the refresh date and time, the version number, how it was started (manual refresh, duplicate URL submission, or replacement file), and the review count before and after (for example "Data refreshed Sep 28, 2026 8:20 PM · v3 · 212 reviews (was 180)").
2. WHILE a refresh is `requested` or `processing`, the history pane SHALL show a pending marker ("Refreshing data…") at the end of the history. It SHALL become a completed marker when the refresh finishes, or a failed marker when it fails.
3. WHEN a refresh fails, its marker SHALL say the refresh failed and that answers still reflect the previous version.
4. WHERE an Exchange was answered against a data version older than the current one, the Exchange SHALL be visually de-emphasized and labeled "Based on earlier data (v{n}, {date})", with a tooltip explaining that the reviews have changed since.
5. THE history pane SHALL let the analyst jump between refresh markers and collapse all Exchanges from earlier versions.
6. WHEN the analyst asks a question, the Assistant SHALL use only Exchanges from the current data version as conversation context, so that answers based on older data do not carry into new answers.
7. THE refresh markers SHALL come from `dataset_versions` records and SHALL update live through the Realtime Channel without a page reload.
8. WHEN a refresh attempt fails at the URL check (the page couldn't be read), no refresh marker SHALL be shown, because the data did not change and earlier answers still apply. The failure SHALL be visible in the summary's processing timeline instead.
