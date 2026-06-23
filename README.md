# cli-tester

A lean CLI for testing **GitHub Copilot CLI skills** in a reproducible, isolated
environment. Point it at a skill directory; it spawns the real `copilot` agent
with **only that skill** loaded, captures the run transcript, scores it with
deterministic assertions **and** an LLM judge, and writes a report.

No adapters, lockfiles, or workflow YAML sprawl — a skill is just its `SKILL.md`
plus a small `tests.yaml` sidecar.

## How it works

```
cli-tester test ./skills/<name>
   │
   ├─ build a throwaway COPILOT_HOME containing ONLY this skill
   ├─ copy scenario fixtures into a sandbox working dir
   ├─ run: copilot -p "<prompt>" --output-format json --allow-all-tools
   │        → capture JSONL transcript (messages + tool calls)
   ├─ assertions: skill triggered? required/forbidden tools? output contains?
   ├─ LLM judge: grade against plain-English expected behavior (reuses copilot)
   └─ report → reports/<skill>-<timestamp>/{report.md, results.json, runs/…}
```

Isolation uses Copilot's `COPILOT_HOME` env var, so the test environment is
clean and reproducible and never touches your real skills/config. Skill
triggering is detected from the `tool.execution_start` event where
`toolName == "skill"`.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Requires the GitHub Copilot CLI (`copilot`) on your PATH and a working login.

> **Use a venv.** Install cli-tester into a virtual environment and run it as
> `.\.venv\Scripts\python.exe -m cli_tester …` (or activate the venv first).

## Getting started after cloning

The `skills/` folder is **intentionally untracked** (see `.gitignore`) — skills are
*your* input, not something this tool ships, and a live run can read real personal
data. So a fresh clone has **no `skills/` directory**, and the tool does not create
one for you. Author your own skill first:

```powershell
# 1) Create a skill directory + the skill you want to test
mkdir skills\my-skill
notepad skills\my-skill\SKILL.md     # write the skill (see "Skill layout" below)

# 2) Let the AI scaffold tests + synthetic fixtures, then run
cli-tester init my-skill
cli-tester test my-skill
```

You can also keep skills anywhere: every command accepts a **path** to a skill
directory, or `--skills-dir <root>` to resolve bare names from a different folder.
Each cloner authenticates with **their own** `copilot login` (or a token env var) —
no credentials or data travel in the repo.

## Usage

```powershell
# Validate a skill + its test spec
cli-tester validate greeter

# AI-generate a tests.yaml + synthetic fixtures for a skill that has none
cli-tester init email-parser

# Test it (runs every scenario in tests.yaml)
cli-tester test email-parser

# Useful flags
cli-tester test greeter --scenario basic-greeting   # one scenario
cli-tester test greeter --runs 5                     # reliability: 5 runs each
cli-tester test greeter --model claude-sonnet-4.6    # pin a model
cli-tester test greeter --no-judge                   # assertions only
```

The skill argument accepts a **bare name** (resolved under `skills/`) or a path.
`test` exits non-zero if any run fails — usable in CI.

## Skill layout

```
skills/
└─ email-parser/
   ├─ SKILL.md             # the skill under test (standard Copilot skill markdown)
   ├─ tests.yaml           # scenarios + expectations (this tool only)
   └─ fixtures/inbox.json  # synthetic test data (no real personal data)
```

## Two ways to run a skill

Skills are **mode-agnostic** — a SKILL.md just says what to do (e.g. "read the
user's Outlook inbox"), never how the data is delivered. cli-tester supplies
that framing:

| Mode | Where the data comes from | What it proves |
|---|---|---|
| **Local** (default) | synthetic export files dropped into the agent's sandbox cwd; the harness tells the agent live services are unavailable and to read the local files | Can the agent reach the ideal result from the data? (reasoning + safety, fully reproducible) |
| **Live** (`--live`) | a real browser logged into your Microsoft account scrapes Outlook/Teams etc. | Can the skill actually obtain and use real data? (failure to access is a real signal) |

There are **no mock MCP servers**. In local mode the agent reads ordinary files
with its native file tools; in live mode it uses a real browser. The *same* skill
runs unmodified in both.

> **Local context note.** In local mode, when a scenario ships fixtures,
> cli-tester appends a short note to the prompt telling the agent that live
> services are unavailable and equivalent data has been exported to its working
> directory. This keeps the SKILL.md generic — the local-vs-live awareness lives
> in the harness, not the skill.

## `tests.yaml` reference

```yaml
model: claude-sonnet-4.6   # optional: pin a model for reproducibility
runs: 1                     # default runs per scenario

scenarios:
  - id: count-external
    prompt: "Parse my emails and tell me how many are external."
    files:                            # local data copied into the agent's sandbox cwd
      inbox.json: fixtures/inbox.json # <dest in sandbox>: <path relative to skill dir>
    expect:                           # deterministic assertions (all must pass)
      skill_invoked: true             # did the agent trigger THIS skill?
      output_contains: ["3"]          # substrings (case-insensitive) in the answer
      output_not_contains: [error]    # substrings that must be absent
    judge: >                          # optional plain-English rubric for the LLM judge
      The agent should report 3 emails marked [EXTERNAL] and send nothing.
```

Any field under `expect` is optional. Omit `judge` to skip the model judge for
that scenario (or pass `--no-judge` globally).

> **Safety assertions.** Because local mode has no mock write tool, you can't use
> `forbidden_tools` to *catch* an attempted send/delete. Encode "must not send"
> in the **judge** rubric instead (and verify in live mode). `required_tools` /
> `forbidden_tools` still work for real tool names (e.g. `view`, `browser_*`) but
> are rarely needed in local mode.

## `cli-tester init` — let the AI scaffold the tests

`init` reads a skill's `SKILL.md`, asks the Copilot agent to author a `tests.yaml`
plus **synthetic fixtures** (e.g. a fake inbox export), and writes them for you to
review:

```powershell
cli-tester init teams-unread        # writes tests.yaml + fixtures/*.json
# review/tweak the generated files, then:
cli-tester test teams-unread
```

⚠️ Generated tests are a **starting point a human reviews**, not ground truth —
an AI grading data it also invented can be circular, and it sometimes writes
over-strict assertions. Always sanity-check the fixtures and assertions.

## Live mode — test against real Outlook/Teams (gated)

Local mode is the default and is what you want for repeatable tests. For a check
against your **real** data, `--live` signs into your Microsoft account and gives the
agent a real browser (the [Playwright MCP](https://github.com/microsoft/playwright-mcp))
so it can read your Outlook, Teams, and other Microsoft 365 apps on the web.

```powershell
# 1) One-time: sign in once; the browser profile is saved to disk
cli-tester login

# 2) Run a scenario against real data (headed browser, asks for consent)
.\.venv\Scripts\python.exe -m cli_tester test email-parser --live

# Flags
cli-tester test email-parser --live --headless   # no visible window
cli-tester test email-parser --live --yes        # skip the consent prompt (CI)
cli-tester test email-parser --live --profile C:\path\to\profile
```

Requirements: Node/`npx` on PATH and the Playwright browser, installed once with:

```powershell
npx @playwright/mcp@latest install-browser chrome-for-testing
```

⚠️ **Live mode is NOT a reproducible test.** It reads real personal data and
results vary as your inbox changes. In live mode `files:` is ignored — the agent
uses generic `browser_*` tools against the real services, so a browser-based
"send" won't be caught by `forbidden_tools`. Prefer **read-only** scenarios in
live mode and keep local mode as your source of truth. The saved login profile
lives under `.cli-tester/live-profile` (git-ignored).

## Reports

Each run produces `reports/<skill>-<timestamp>/`:

- `report.md` — human-readable summary (pass rates, per-run assertions, judge
  verdicts, final answers).
- `results.json` — machine-readable results for CI / dashboards.
- `runs/<scenario>/run-NN/transcript.jsonl` — the raw Copilot event stream.

## Notes & roadmap

- The judge reuses `copilot` itself, so no extra API keys are needed.
- Local mode injects synthetic export files into the agent's sandbox cwd; the
  agent reads them with native file tools — no mock servers involved.
- Live M365 access (real email/Teams) is available behind the explicit,
  consent-gated `--live` mode (Playwright browser automation) — see above.
