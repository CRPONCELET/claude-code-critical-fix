# Fix Critical
_Name: CRPONCELET_
_Date: 2026-03-19_
_Repository: anthropics/claude-code_
_Scope: GitHub Actions workflows, TypeScript scripts, hookify plugin, configuration_

---

## Overview

13 issues distributed across 3 severity levels:
- **4 high priority** -- security, massive duplication, missing tests
- **5 medium priority** -- maintainability, fragility, inconsistencies
- **4 low priority** -- hygiene, documentation, CI

---

## High Priority

### H1 -- githubRequest() duplicated across 3 TS scripts

**Description**
The `githubRequest()` function is copy-pasted across 3 files with minor variations:
- `scripts/auto-close-duplicates.ts` (lines 28-47) -- signature `(endpoint, token, method, body)`, User-Agent `"auto-close-duplicates-script"`
- `scripts/backfill-duplicate-comments.ts` (lines 26-45) -- identical signature, User-Agent `"backfill-duplicate-comments-script"`
- `scripts/sweep.ts` (lines 15-41) -- different signature `(endpoint, method, body)` (token read from env), User-Agent `"sweep"`, different 404 handling (returns `{} as T` instead of throw)

Approximately 150 lines of duplicated code in total. The `GitHubIssue` and `GitHubComment` interfaces are also duplicated between `auto-close-duplicates.ts` and `backfill-duplicate-comments.ts`.

**Impact**
- Bug fixes need to be applied 3 times
- Risk of behavioral divergence (already the case: `sweep.ts` handles 404 differently)
- Costly and error-prone maintenance

**Implemented Solution**
Extraction into a shared module `scripts/lib/github.ts`:
- Single `githubRequest<T>()` function with centralized token management
- Shared `GitHubIssue`, `GitHubComment`, `GitHubReaction` interfaces
- Unified error handling with `ignore404` option for the `sweep.ts` case
- All 3 scripts import from `./lib/github`

**Modified Files**
- `scripts/lib/github.ts` -- new shared module
- `scripts/auto-close-duplicates.ts` -- removal of githubRequest + interfaces, module import
- `scripts/backfill-duplicate-comments.ts` -- same
- `scripts/sweep.ts` -- same

---

### H2 -- No GitHub API rate-limiting handling

**Description**
None of the 3 TS scripts handle GitHub API rate-limiting headers (`X-RateLimit-Remaining`, `X-RateLimit-Reset`, `Retry-After`). The scripts perform requests in a loop (pagination of 100+ issues with comments for each), which can easily hit the 5000 requests/hour limit for standard tokens.

Concrete case: `backfill-duplicate-comments.ts` can iterate through 200 pages of 100 issues, then make one request per issue for its comments. That's potentially 20000+ requests in a single run.

**Impact**
- Silent script failure when the limit is reached (HTTP 403)
- The `auto-close-duplicates` script could close some issues and not others (partial execution)
- No backoff, no retry -- the script crashes and leaves an inconsistent state

**Implemented Solution**
Addition to `scripts/lib/github.ts`:
- Reading `X-RateLimit-Remaining` after each response
- When remaining < 100: warning log
- When remaining < 10: automatic pause until `X-RateLimit-Reset`
- Automatic retry with exponential backoff on HTTP 429 (Retry-After) and HTTP 403 (rate limit)
- Configurable limit on total requests per execution (safety cap)

**Modified Files**
- `scripts/lib/github.ts` -- rate-limiting logic in githubRequest

---

### H3 -- JSON injection vulnerability in log-issue-events.yml

**Description**
The workflow `.github/workflows/log-issue-events.yml` builds a JSON payload by concatenating shell variables into a raw JSON string via shell substitutions:

```yaml
-d '{
  "events": [{
    "metadata": {
      "title": "'"$(echo "$ISSUE_TITLE" | sed "s/\"/\\\\\"/g")"'",
```

Although values are passed via `env:` (no direct template injection `${{ }}` in the `run:`), building JSON by shell concatenation remains fragile:
- The `sed` only escapes double quotes, not backslashes, newlines, tabs, or other JSON control characters
- An issue title containing `\n`, `\t`, or backslashes would break the JSON or inject fields
- The `$AUTHOR` variable (GitHub login) has no escaping

**Impact**
- An attacker can create an issue with a crafted title to inject fields into the Statsig payload
- Silent logging breakage if the JSON is malformed (curl doesn't check the return)
- No code execution vulnerability, but telemetry data corruption

**Implemented Solution**
Replacement of manual JSON construction with `jq`:
```yaml
run: |
  jq -n \
    --arg num "$ISSUE_NUMBER" \
    --arg repo "$REPO" \
    --arg title "$ISSUE_TITLE" \
    --arg author "$AUTHOR" \
    --arg created "$CREATED_AT" \
    --arg time "$(date +%s)000" \
    '{events: [{eventName: "github_issue_created", metadata: {issue_number: $num, repository: $repo, title: $title, author: $author, created_at: $created}, time: ($time | tonumber)}]}' \
  | curl -X POST "https://events.statsigapi.net/v1/log_event" \
    -H "Content-Type: application/json" \
    -H "statsig-api-key: $STATSIG_API_KEY" \
    -d @-
```

`jq` automatically escapes all JSON special characters.

**Modified Files**
- `.github/workflows/log-issue-events.yml` -- replacement of JSON construction

---

### H4 -- Zero tests for the custom YAML parser and hookify rule engine

**Description**
The hookify plugin contains a 108-line custom YAML parser (`core/config_loader.py`, `extract_frontmatter` function) and a rule engine (`core/rule_engine.py`, `RuleEngine` class). No automated tests exist. The only "tests" are `if __name__ == '__main__'` blocks with one trivial case each.

The custom YAML parser is particularly critical: it manually handles indentation, lists, nested dictionaries, and boolean values. Multiple edge cases are not covered:
- Values with `:` (e.g., `pattern: https://example.com:8080`)
- Multiline values
- Lists of scalars mixed with dictionaries
- Inline comments after values
- Tabs vs spaces indentation

**Impact**
- Silent regressions during any parser or engine modification
- Hookify rules that don't match without visible explanation to the user
- False sense of security: a failing blocking hook lets dangerous operations through

**Implemented Solution**
Creation of a test suite `plugins/hookify/tests/`:
- `test_config_loader.py` -- YAML parser tests: simple frontmatter, multiple conditions, boolean values, values with special characters, files without frontmatter, invalid files
- `test_rule_engine.py` -- engine tests: basic matching, multiple conditions (AND), operators (regex, contains, equals, not_contains, starts_with, ends_with), warn vs block action, field extraction by tool type, invalid regex
- `test_hooks_integration.py` -- integration tests: all 4 hooks (pretooluse, posttooluse, stop, userpromptsubmit) with simulated payloads via stdin

**Modified Files**
- `plugins/hookify/tests/__init__.py` -- new
- `plugins/hookify/tests/test_config_loader.py` -- new
- `plugins/hookify/tests/test_rule_engine.py` -- new
- `plugins/hookify/tests/test_hooks_integration.py` -- new

---

## Medium Priority

### M1 -- 4 nearly identical hookify hooks -> single entry point

**Description**
The 4 hookify hook files are practically identical:
- `hooks/pretooluse.py` (74 lines)
- `hooks/posttooluse.py` (66 lines)
- `hooks/stop.py` (59 lines)
- `hooks/userpromptsubmit.py` (58 lines)

Each file contains the same boilerplate: sys.path configuration, import with fallback, stdin reading, RuleEngine creation, evaluation, JSON serialization, error handling. The only difference is the event type passed to `load_rules()`.

**Impact**
- 257 lines for work that requires 40
- Any correction (e.g., adding logging, changing output format) must be applied 4 times
- Risk of divergence between hooks

**Implemented Solution**
Creation of a single entry point `hooks/entrypoint.py`:
- Detects event type via `sys.argv[1]` or `HOOKIFY_EVENT` environment variable
- Contains all logic once
- The 4 old files become one-line wrappers that call the entry point
- Update to `hooks.json` to pass the event as an argument

**Modified Files**
- `plugins/hookify/hooks/entrypoint.py` -- new single entry point
- `plugins/hookify/hooks/pretooluse.py` -- reduced to a wrapper
- `plugins/hookify/hooks/posttooluse.py` -- same
- `plugins/hookify/hooks/stop.py` -- same
- `plugins/hookify/hooks/userpromptsubmit.py` -- same
- `plugins/hookify/hooks/hooks.json` -- command update

---

### M2 -- 108-line custom YAML parser (fragile)

**Description**
`plugins/hookify/core/config_loader.py` contains a homemade YAML parsing implementation (`extract_frontmatter` function, lines 87-195). This parser manually handles:
- `---` block detection
- Key-value parsing
- Lists (`-`)
- Dictionaries in lists
- Booleans (`true`/`false`)
- Indentation management

It silently fails on many valid YAML cases:
- Values containing `:` (cuts at the first `:`)
- Flow style `{key: value}` and `[item1, item2]`
- Anchors and aliases
- Multiline (`|` and `>`)
- Inline comments

**Impact**
- Users who write standard YAML in their `.local.md` files get unpredictable results
- Silent bugs: the parser doesn't raise errors, it produces incorrect data
- Large maintenance surface for a problem solved by PyYAML (indirect stdlib) or the standard `tomllib` library

**Implemented Solution**
Replacement with PyYAML (`yaml.safe_load`) with fallback to the custom parser if PyYAML is not installed:
- Conditional import: `try: import yaml` / `except ImportError: use_custom_parser()`
- Warning log when fallback is used to encourage the user to install PyYAML
- Preservation of the custom parser as fallback for constrained environments (README specifies "no external dependencies")

**Modified Files**
- `plugins/hookify/core/config_loader.py` -- conditional PyYAML import, fallback to existing parser

---

### M3 -- Inconsistent hook output conventions

**Description**
The rule engine (`rule_engine.py`) produces different output structures depending on the event type:
- For `Stop`: `{"decision": "block", "reason": "..."}`
- For `PreToolUse`/`PostToolUse`: `{"hookSpecificOutput": {"hookEventName": "...", "permissionDecision": "deny"}}`
- For others: `{"systemMessage": "..."}`

The `systemMessage` field is always present in blocking cases, but the main structure varies. No documentation on what format Claude Code actually expects.

**Impact**
- Difficult to test and reason about behavior
- Contributors must read the code to understand the output format
- Risk of error if the format expected by Claude Code changes

**Implemented Solution**
- Documentation of the output protocol in `plugins/hookify/PROTOCOL.md`
- Addition of comments in `rule_engine.py` referencing official Claude Code documentation
- Centralization of response construction in dedicated methods (`_build_block_response`, `_build_warn_response`)

**Modified Files**
- `plugins/hookify/PROTOCOL.md` -- new, documents the format expected by Claude Code
- `plugins/hookify/core/rule_engine.py` -- refactoring of response constructors

---

### M4 -- Insufficient .gitignore

**Description**
The current `.gitignore` contains only one line: `.DS_Store`. It's missing standard patterns for:
- `node_modules/`
- `*.js` / `*.d.ts` generated by TS compilation (if tsconfig added)
- `__pycache__/` / `*.pyc` (Python -- hookify)
- `.env` / `.env.*` (secrets)
- Temporary state files
- Logs (`*.log`)
- Coverage (`coverage/`, `.nyc_output/`)
- IDE (`.idea/`, `*.swp`, `*.swo`)
- OS (`Thumbs.db`)

**Impact**
- Undesirable files accidentally committed
- `.DS_Store` is already there, suggesting the problem is known but incompletely addressed
- `__pycache__/` from the hookify plugin could be committed

**Implemented Solution**
Extension of `.gitignore` with missing patterns, organized by category.

**Modified Files**
- `.gitignore` -- extension with missing patterns

---

### M5 -- State file leakage in security-guidance

**Description**
Note: this issue was identified in the analysis but the `security-guidance` file is not present in the current repository tree. It's possibly a runtime-generated file or an artifact from an unmerged branch.

The analysis indicates that temporary state files could be generated by workflows and not cleaned up, potentially exposed if they contain sensitive information.

**Impact**
- Potential exposure of state data between workflow runs
- Workspace pollution

**Implemented Solution**
- Addition of cleanup patterns in `.gitignore`
- Verification that workflows don't persist state files in the tracked tree

**Modified Files**
- `.gitignore` -- addition of patterns for temporary state files

---

## Low Priority

### B1 -- GitHub Actions not pinned by SHA

**Description**
Most workflows use tag references for third-party actions, except `claude.yml` which correctly pins `actions/checkout` by SHA:
```yaml
uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4
```

Other workflows use version references (`@v4`, `@v1`) or no third-party actions (inline scripts).

**Impact**
- Supply chain risk: a tag can be moved to a malicious commit
- GitHub security recommendation: always pin by full SHA
- Real impact is low in this repo since most workflows only use inline scripts

**Implemented Solution**
Verification and SHA pinning of all third-party action references in workflows. Addition of a comment with the readable tag after the SHA.

**Modified Files**
- `.github/workflows/claude.yml` -- already correct
- `.github/workflows/claude-issue-triage.yml` -- to verify
- `.github/workflows/claude-dedupe-issues.yml` -- to verify
- All workflows using `actions/*` -- SHA pinning

---

### B2 -- No CI validation of plugin structure

**Description**
No CI workflow validates that plugins respect the expected structure:
- Presence of `hooks/hooks.json`
- Valid JSON format
- Presence of referenced hook files
- Presence of a README.md
- Valid Python syntax for hooks

**Impact**
- A broken plugin can be merged without detection
- No automatic feedback for contributors

**Implemented Solution**
Creation of a CI workflow `.github/workflows/validate-plugins.yml` that:
- Iterates over subdirectories of `plugins/`
- Verifies the presence and validity of `hooks.json`
- Verifies that referenced files exist
- Executes `python3 -m py_compile` on Python files
- Runs hookify tests if present

**Modified Files**
- `.github/workflows/validate-plugins.yml` -- new CI workflow

---

### B3 -- No tsconfig.json for TS scripts

**Description**
The 5 TypeScript scripts in `scripts/` don't have a `tsconfig.json`. They use the shebang `#!/usr/bin/env bun` and depend on the Bun runtime for transpilation and execution. Without tsconfig:
- No type checking in CI
- No explicit configuration of target, module system, strict mode
- Imports between files (e.g., `sweep.ts` imports `issue-lifecycle.ts`) are not valid

**Impact**
- Undetected typing errors
- No static linting possible
- Implicit dependency on Bun's default behavior

**Implemented Solution**
Addition of a minimal `scripts/tsconfig.json` configured for Bun:
- `compilerOptions.strict: true`
- `compilerOptions.module: "esnext"`
- `compilerOptions.target: "esnext"`
- `compilerOptions.moduleResolution: "bundler"`
- `compilerOptions.types: ["bun-types"]`
- `compilerOptions.noEmit: true` (type-checking only)

**Modified Files**
- `scripts/tsconfig.json` -- new

---

### B4 -- Undocumented hook protocol

**Description**
The hookify plugin implements a communication protocol with Claude Code (JSON via stdin/stdout, exit codes, response structure) but this protocol is not documented anywhere. The README explains user configuration but not:
- The expected JSON format on input (stdin)
- The expected JSON format on output (stdout)
- The semantics of exit codes
- The available fields per event type
- The behavior on error

**Impact**
- Impossible for a contributor to create a new hook without reading the source code
- Impossible to test properly without knowing the contract
- Risk of hooks that work by accident

**Implemented Solution**
Creation of `plugins/hookify/PROTOCOL.md` documenting:
- The complete lifecycle of a hook call
- The input/output JSON schemas for each event type
- The semantics of `decision`, `hookSpecificOutput`, `systemMessage` fields
- Examples of stdin and stdout payloads for each event

**Modified Files**
- `plugins/hookify/PROTOCOL.md` -- new

---

## Tracking Matrix

| ID | Priority | Status | Assigned to | Short Description |
|----|----------|--------|-------------|-------------------|
| H1 | High | In progress | Fix agent | githubRequest deduplication |
| H2 | High | In progress | Fix agent | GitHub API rate-limiting |
| H3 | High | In progress | Fix agent | JSON injection log-issue-events |
| H4 | High | In progress | Fix agent | hookify tests |
| M1 | Medium | In progress | Fix agent | Hooks single entry point |
| M2 | Medium | In progress | Fix agent | PyYAML vs custom parser |
| M3 | Medium | In progress | Fix agent | Hook output conventions |
| M4 | Medium | In progress | Fix agent | .gitignore |
| M5 | Medium | In progress | Fix agent | State files |
| B1 | Low | In progress | Fix agent | Actions SHA pinning |
| B2 | Low | In progress | Fix agent | CI plugin validation |
| B3 | Low | In progress | Fix agent | tsconfig.json |
| B4 | Low | In progress | Fix agent | Hook protocol documentation |
