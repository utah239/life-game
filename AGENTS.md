# Context-efficient agent workflow

Keep the active context small without weakening correctness or verification.

## Read narrowly

- Start with `rg --files`, `rg -n`, and targeted `sed` ranges. Read a whole large
  file only when its full structure is required.
- Inspect `git status --short`, `git diff --stat`, or `git diff --name-only`
  before opening only the relevant diff hunks.
- Do not dump generated HTML/JSON, full checkpoints, full logs, or unchanged file
  bodies into the conversation. Extract the fields or lines that answer the current
  question.
- Reuse facts already established in the current turn. Do not repeat unchanged
  searches, file reads, or diagnostics.

## Bound tool output

- Set the smallest practical output limit for every command. Prefer quiet test modes
  and retain the command, exit status, test count, elapsed time, and failure details.
- For long-running commands, report only state changes or a one-sentence heartbeat;
  do not echo repetitive progress markers.
- Batch independent, read-only checks when safe. Return a compact aggregate instead
  of several raw outputs.
- Reduce large tool results locally with exact filters, counts, or summaries. Keep raw
  runtime artifacts under a temporary directory when they are needed for diagnosis.

## Preserve only durable state

- At each major phase boundary, retain a compact checkpoint containing: objective,
  decisions and invariants, changed files, verification results, external IDs,
  unresolved blockers, and the next concrete action.
- Before or after compaction, discard raw logs, completed exploration, superseded
  plans, and repeated narration. Preserve exact failures, user authorizations,
  unresolved choices, commit/PR IDs, and required next steps.
- Do not create ad-hoc handoff files in the repository unless the user asks for a
  durable artifact.

## Respond concisely

- Lead with the outcome. Default to at most five bullets covering changes,
  verification, risks, and next action; expand only when the task requires it.
- Never reduce requested scope, safety checks, tests, or review quality merely to save
  context.
