# Claude Code kickoff prompt: DRBR

Run `claude` from the root of the existing POP project, with `drbr-build-brief.md` placed alongside the existing code. Paste everything below the line.

Read `drbr-build-brief.md` in full before doing anything else. Then build it.

This extends an existing, working POP codebase. It is not a greenfield build. Preserve existing behaviour unless the brief explicitly changes it.

## Global operating rules

### 1. Front-load every user dependency

Do not ask me questions after Phase 0.

Everything requiring my input, credentials, files, paths, exports, screenshots, or confirmation must be identified and collected during Phase 0 in one batch.

From Phase 1 onward, if something is ambiguous:

1. Prefer the behaviour explicitly specified by `drbr-build-brief.md`.
2. If the brief is silent, prefer consistency with the existing POP architecture and behaviour.
3. If neither resolves it, make the smallest reasonable decision.
4. Record the decision and reasoning in `DECISIONS.md`.
5. Continue without asking me.

### 2. The codebase is authoritative about existing implementation

The brief was written without sight of the final POP code.

Its descriptions of existing schemas, modules, state transitions, authentication, file layout, naming, or control flow may therefore be wrong.

Phase 1 exists to resolve those discrepancies.

Where the brief describes what POP currently does and the actual code differs, the codebase wins. Document the discrepancy in `EXISTING.md`.

Where the brief specifies new desired behaviour, the brief wins unless implementing it would break an invariant that makes the system non-functional. If that happens, implement the closest compatible design and record the reason in `DECISIONS.md`.

### 3. Never infer value

Never add a heuristic that guesses whether something was worthwhile or wasteful.

Explicitly rejected approaches include:

- dwell-time scoring
- character-count proxies
- session-length classification
- engagement scoring
- behavioural inference
- multi-point value scales
- inferred usefulness from source, duration, folder, activity, or downstream behaviour

The governing principle is:

> The user labels. The system counts.

Source-label inheritance is allowed because it is an explicit user-defined label rule. Per-item overrides are allowed because they are explicit user labels.

If you find yourself about to infer worth from an indirect signal, do not implement it. Record the temptation and why it was rejected in `DECISIONS.md`.

### 4. Preserve raw evidence

For every imported source where parsing may later improve, retain enough raw source material to reproduce the parse.

Do not store only transformed totals when the underlying evidence can reasonably be retained.

Parsed records should expose their provenance.

### 5. No live external services in automated tests

Automated tests must never call live external APIs or services.

Use fixtures, mocks, fakes, frozen clocks, and local temporary databases.

The only intentional live credential checks occur during Phase 0, including one YouTube Data API request.

Runtime production code may call the services it is designed to use. Test paths may not.

### 6. Protect the existing installation

Before modifying data structures:

- identify the existing migration mechanism
- preserve existing user data
- prefer additive and backward-compatible migrations
- test migration from a representative pre-DRBR POP database, not only creation of a new empty database
- do not destructively rewrite existing records unless explicitly required

### 7. Report meaningful progress

Report progress to stdout throughout the build.

Report milestones, discoveries, discrepancies, completed components, integration failures, and test results.

Do not flood stdout with trivial file-by-file narration.

# Phase 0: collect every external dependency

Do not write implementation code until this phase is complete.

First inspect enough of the project root to identify its runtime, environment structure, and obvious credential conventions.

Then present me with a **single numbered list containing everything you need from me for the entire build**.

Do not request these items one at a time.

Collect:

1. **YouTube Data API key**
   - Tell me exactly how to create it.
   - Tell me exactly which Google API must be enabled.
   - Add it to POP's existing `.env`.
   - Add the corresponding placeholder to `.env.example`.
   - Validate it using exactly one small live YouTube Data API request.
2. **Obsidian vault**
   - Exact vault path.
   - Exact inbox folder name.
   - How the vault reaches, or could reach, the VPS.
3. **Vault architecture**
   - Determine whether the VPS can directly read the vault.
   - Based on the answer and the architecture described in the brief, choose:
     - server-side vault scanning, or
     - a small local agent that submits vault state.
   - Make the decision yourself.
   - Tell me which architecture you chose and why.
   - Do not ask me to choose between them.
4. **Real Cold Turkey export**
   - I will generate a real statistics export and place it in the project directory.
   - Locate and inspect the actual file.
   - The parser must be based on its real structure, not assumptions or documentation examples.
   - Preserve an anonymised fixture derived from this real export for tests.
5. **Real iPhone Screen Time screenshot**
   - I will provide one captured exactly as specified in the brief:
     - per-device
     - iPhone selected
     - one week back
   - Build the parser against this real example.
   - Preserve a suitable test fixture derived from it.
6. **Real YouTube history HTML fixture**
   - Obtain or have me provide the real saved HTML needed to validate the history-page parser and selectors.
   - The fixture must reflect the actual current page structure.
   - Do not invent a DOM fixture and then design selectors around it.
   - Strip or replace unnecessary personal content in the committed test fixture while preserving the DOM structure required by the parser.
7. **Diagnostic configuration**
   - Weekly wasteful allowance.
   - Default proposal: `180 minutes`.
   - Diagnostic end date.
   - Default proposal: exactly eight weeks from the first valid data date.
   - Present those defaults and allow me to correct them during Phase 0.
   - Once Phase 0 ends, treat the resulting values as final configuration.

## Existing credentials

Reuse POP's existing `.env` for Spotify and Telegram.

Verify that the existing credentials still authenticate using the least invasive available calls.

Do not print secrets to stdout, logs, markdown files, fixtures, test snapshots, git history, or exceptions.

Add new secrets to the same `.env` convention and update `.env.example` with placeholders only.

## Environment validation

Create `check_env.py` covering every credential and external dependency required by the completed system.

Its output must identify failures without exposing secrets.

Run it before leaving Phase 0.

Do not proceed to Phase 1 while a required dependency is invalid.

If an external provider is temporarily unavailable but the credential itself cannot conclusively be shown invalid, distinguish that state from an authentication failure rather than misclassifying it.

# Phase 1: reconnaissance, architecture and contracts

**Single agent only. No implementation parallelism.**

Read the complete relevant POP codebase before designing the extension.

Exclude non-source material such as:

- `.git`
- dependency/vendor directories
- virtual environments
- generated build output
- caches
- coverage output
- binary assets that do not affect architecture

Inspect configuration, migrations, tests, deployment scripts, cron/systemd configuration, extensions, templates, and operational scripts where relevant.

## Produce `EXISTING.md`

Document the system as it actually exists.

Include:

### Database

- database technology
- migration mechanism
- every existing table
- columns and types
- keys and relationships
- relevant indexes and constraints
- which modules read and write each table

### Queue state machine

Document:

- every actual state
- every valid transition
- what triggers each transition
- lock behaviour
- allowance behaviour
- rollover/debt behaviour
- failure/retry behaviour where applicable

Express the resulting state machine clearly enough that Phase 4 can test it.

### Telegram bot

Document:

- entry point
- command routing
- callback routing
- capture creation
- message handling
- persistence boundaries
- where new capture types belong
- where new commands belong
- where inline labelling belongs

### Spotify

Document:

- authentication
- token refresh
- polling
- `resume_point`
- persistence
- error handling

### Gate and allowance

Identify exactly:

- where locking occurs
- where allowance eligibility is checked
- where allowance is spent
- where allowance resets
- where debt is created/carried
- every existing call site affected by making gating conditional on labels

### Deployment/runtime

Document:

- how POP currently starts
- background processes
- cron jobs
- ports
- environment loading
- relevant VPS assumptions

### Brief discrepancies

Create a dedicated section listing every meaningful place where the assumptions in `drbr-build-brief.md` differ from the real code.

## Produce `CONTRACTS.md`

Before parallel implementation begins, define the interfaces all Phase 2 agents must obey.

Include:

- schema additions
- schema changes
- migration ordering
- canonical enums/constants
- module boundaries
- function signatures
- return types
- error semantics
- provenance representation
- label-resolution semantics
- source identifiers
- capture identifiers
- week/date semantics
- timezone handling
- allowance interface
- vault-state interface
- reporting interface
- Telegram integration hooks

No Phase 2 agent may privately change a shared contract.

If implementation reveals that a contract must change, stop that affected integration, update `CONTRACTS.md` centrally, then apply the same revised contract to every affected component.

## Define Phase 2 file ownership

Before launching parallel agents, create a file ownership map in `CONTRACTS.md`.

No two parallel agents may modify the same file.

If several features need changes to a shared file such as the Telegram router, queue service, schema module, or central configuration, create integration hooks during Phase 1 or reserve that shared file for the later integration pass.

Do not rely on parallel agents making compatible edits to the same file.

## Apply the schema migration foundation

Create the migration required for the agreed schema, including:

- sources
- labels
- captures
- vault state
- per-source measurements
- required modifications to existing tables

Test:

1. migration of a representative pre-DRBR database
2. clean database creation
3. preservation of existing POP records and behaviour

Do not begin Phase 2 until these pass.

# Phase 2: parallel source implementation

Launch four agents concurrently only after Phase 1 contracts and ownership boundaries are complete.

Use isolated branches or git worktrees so their changes cannot overwrite one another.

Each agent:

- owns only its assigned files
- imports shared interfaces from the Phase 1 contracts
- must not redesign another agent's interface
- writes unit tests for its own component
- uses fixtures rather than live services in tests
- records implementation assumptions in its own temporary notes for the integration agent

## Agent A: YouTube

Implement:

- Chrome extension
- `sources/youtube.py`

Requirements:

- Read watched fraction from the YouTube progress overlay using DOM state/value, not pixel analysis.
- Use cursor-based scrollback until reaching the newest video ID recorded by the previous successful run.
- Determine date from the page's date-group headers.
- Do not infer dates from relative ordering alone.
- Treat selector failure as an explicit parse failure.
- Never silently convert selector failure into `0`.
- Never silently discard an item because expected progress markup is absent.
- Handle Shorts separately.
- Shorts are treated as fully watched.
- Fetch true Short duration through the YouTube Data API.
- Retain enough raw ingestion metadata to diagnose parser failures.
- Build parser fixtures from the real Phase 0 saved HTML.
- Do not design fixtures around imagined YouTube markup.

Unit-test:

- ordinary videos
- partially watched videos
- fully watched videos
- Shorts
- missing progress element
- changed/broken selector
- date-group boundaries
- cursor stopping
- duplicate ingestion
- API duration mock
- API failure behaviour

## Agent B: Screen Time and Cold Turkey

Implement:

- `sources/screentime.py`
- `sources/coldturkey.py`
- source-specific Telegram ingestion handlers through the Phase 1 bot interfaces

### Screen Time

Parse the weekly iPhone screenshot into:

- per-app minutes
- per-category minutes
- source week/date
- parse metadata

Store:

- raw screenshot
- parsed result
- parser/version metadata
- provenance
- any parse warnings or unresolved fields

A later parser improvement must be able to re-run against the original image without asking the user for the screenshot again.

Do not silently invent values for unreadable fields.

### Cold Turkey

Write the parser against the actual Phase 0 export.

Preserve a sanitised fixture representing the real export structure.

Do not assume fields that were not observed in the real file.

Unit-test malformed, missing, duplicated, and unexpected records.

## Agent C: vault and labels

Implement:

- `sources/vault.py`
- `labels.py`
- source-specific bot handlers through the Phase 1 integration interfaces

### Capture IDs

Every newly created relevant note must contain its immutable capture ID in frontmatter.

The ID, not the note path, is the identity.

Renaming or moving a note must not break tracking.

### Weekly vault scan

Locate capture IDs and determine their current outcome exactly as defined in the brief.

Support exactly:

- the two defined outcomes
- exclusion

Do not create additional inferred states.

Deletions or exclusions must not be counted as failed outcomes unless the brief explicitly defines them that way.

### Labels

Implement:

- explicit item labels
- source-level labels
- source-label inheritance
- per-item override
- unlabelled defaulting to wasteful
- batch labelling via Telegram inline buttons

Resolve labels deterministically from explicit stored state.

Never infer them from behaviour.

Unit-test precedence exhaustively.

## Agent D: gate and reporting

Implement the label-aware gate using the Phase 1 interfaces.

Requirements:

- POP's existing lock behaviour remains intact for worthwhile items.
- Worthwhile items must never spend the wasteful allowance.
- Wasteful items may bypass the normal gate only according to the defined pooled weekly allowance rules.
- Maintain one pooled weekly allowance.
- Preserve existing rollover/debt semantics as specified by the brief and actual POP behaviour.
- Do not weaken the underlying gate globally in order to implement the bypass.

Implement `report.py` covering the brief's required:

- funnels
- cost per keeper
- gated vs ungated split
- source provenance

Never convert Obsidian captures into estimated minutes unless the brief explicitly defines a real measured duration source. Count them in their native unit instead.

Unit-test all label/gate combinations.

# Phase 2.5: integration

After all four agents finish, terminate parallel implementation.

Use a single integration agent.

Merge or cherry-pick each isolated implementation deliberately.

Resolve integration through the contracts rather than ad hoc coupling.

The integration agent may modify shared files that were intentionally reserved during Phase 1.

Run the complete test suite after integrating each source, not only after all four have been combined.

Verify specifically that:

- Telegram handlers coexist
- schema expectations match
- source IDs match
- label precedence matches
- gate callers use the same label resolver
- provenance survives from ingestion through reporting
- no source silently swallows parse failures

Update `CONTRACTS.md` if the final integrated implementation differs from the original contract, and record why.

# Phase 3: dashboard

**Single agent. Begins only after Phase 2.5 passes.**

Implement the dashboard using:

- Flask
- HTMX
- server-side rendering
- binding to `127.0.0.1`

Build exactly the surfaces specified in `drbr-build-brief.md`.

Do not add speculative dashboard features.

The diagnostic countdown must appear on every dashboard page.

## Provenance

The origin and quality of every displayed measurement must remain visible.

For example, data derived from:

- an API
- DOM extraction
- a screenshot parse
- an exported statistics file
- a vault scan
- explicit user labels

must not be visually or semantically represented as though they have identical provenance.

Screenshot-derived figures must not appear to have API-level certainty.

Expose parse warnings or incomplete source state where relevant.

# Phase 4: independent verification

Use an agent that did not implement the code being reviewed.

Its purpose is adversarial verification, not confirmation.

## Integration test

Build an end-to-end integration test using:

- frozen clock
- fixtures
- mocked external services
- representative migrated POP database
- all five source paths

Test at minimum:

1. capture creation
2. source-label inheritance
3. per-item override
4. unlabelled defaults to wasteful
5. worthwhile gate behaviour
6. wasteful allowance bypass
7. pooled allowance spending
8. week rollover
9. debt behaviour
10. vault scan outcome 1
11. vault scan outcome 2
12. vault exclusion
13. note move/rename while retaining identity
14. report generation
15. provenance propagation
16. duplicate ingestion/idempotency
17. parser failure behaviour

## Targeted audit

Audit the entire relevant codebase specifically for:

- any heuristic that infers value instead of reading explicit label state
- vault tracking by path instead of capture ID
- Shorts silently dropped when no progress element exists
- selector failures converted into zero values
- deletions/exclusions counted as failures
- Obsidian captures converted into estimated minutes
- worthwhile items spending wasteful allowance
- worthwhile items accidentally bypassing the existing gate
- screenshot-derived figures represented with API-level confidence
- live external API/service calls reachable from automated test paths
- secrets committed or logged
- incompatible duplicate source identifiers
- timezone inconsistencies at week boundaries
- migration paths that lose existing POP data

## `REVIEW.md`

Write:

- defects found
- severity
- affected files
- reproduction/test
- fix applied
- regression test added

Fix every defect found that is within the brief's scope.

After fixes, run the independent audit again.

The final `REVIEW.md` must distinguish:

- findings discovered and fixed
- findings deliberately accepted, with justification
- checks performed with no defect found

Do not leave a known in-scope defect merely documented if it can reasonably be fixed.

# Phase 5: final verification and deliverables

Produce or update:

- `README.md`
- `DECISIONS.md`
- `EXISTING.md`
- `CONTRACTS.md`
- `REVIEW.md`
- `.env.example`

## README

Include:

- prerequisites
- installation
- migration/setup
- environment configuration
- credential verification
- Telegram setup
- Chrome extension installation
- YouTube ingestion behaviour
- Screen Time weekly procedure
- Cold Turkey import procedure
- Obsidian/vault procedure
- dashboard access
- cron/systemd lines as applicable
- backup/recovery considerations
- weekly user routine
- troubleshooting for parse failures

## Single startup command

Create one canonical command that starts the complete DRBR-enabled POP system in its intended local/VPS configuration.

Prefer the convention already used by POP.

If no suitable single launcher exists, add one, for example through the project's existing task runner, `Makefile`, or a small checked-in script.

Do not merely describe several commands and call them one command.

## Final checks

Run the project's applicable:

- tests
- integration tests
- migration tests
- linting
- formatting checks
- type checks
- environment/config validation that can safely run at this stage

Do not claim a check passed unless you actually ran it.

Ensure:

- no secret is committed
- no temporary fixture containing personal data remains
- no accidental build artefacts are staged
- no debugging code remains
- repository status contains only intentional changes

Then report:

1. what was built
2. significant deviations from the original brief
3. final test/check results
4. any known limitations
5. the single command to start everything

Then stop.

Do not add features beyond the brief.
