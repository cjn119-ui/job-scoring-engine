# job-scoring-engine

[![tests](https://github.com/cjn119-ui/job-scoring-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/cjn119-ui/job-scoring-engine/actions/workflows/tests.yml)

**A job-search tool that reads public job boards, puts every posting into one format, and ranks them with rules you can check line by line.** Every point of score quotes the posting it came from. Anything the posting doesn't say, like pay, is reported as unknown instead of guessed. I designed it and wrote the requirements and tests, and AI coding agents wrote the code.

## What it does

1. **Collects postings** from the public job-board APIs of Greenhouse, Ashby and Lever, politely: read-only requests to a fixed list of hosts, a rate limit per host, and no workarounds when a board says no.
2. **Normalizes them** into one schema, whatever board they came from, and strips hidden page text so nothing a reader can't see can change a score.
3. **Scores each posting** against a profile: job family, responsibilities, ownership, gaps, location rules and pay. Each posting lands in a tier: strong, stretch, attractive but poor fit, poor fit, or excluded.
4. **Tracks changes over time.** A posting that disappears is marked closed, not deleted; duplicates collapse into one; changes since last run are described.
5. **Writes a shortlist report** in Markdown, with at most two roles per company.

## Sample output

From `make run`, which scores three fictional boards offline:

```markdown
### 1. globex-example - Analytics Engineer
- **Where:** Remote - United States - workplace: remote
- **Pay:** $140,000-$175,000 - verified (provider field) - no minimum configured: pay is reported, not compared
- **Score 69.0 (family data_platform):** pipelines: "Own our analytics engineering practice: dbt models,
  data quality tests and a Snowflake warehouse. ..."; ownership: "...Python and SQL required. End-to-end ownership."
- **Stated requirements (candidate attainment UNKNOWN):** none captured
- **Priority P1:** review qualifications first: strong profile alignment and recent published pay;
  candidate qualifications remain unverified

## Stretches (verify before investing)
- initech-example - Integrations Engineer - score 51.0 - pay: not published (unknown, not assumed)
- initech-example - Solutions Engineer - score 42.0 - pay: $180,000-$240,000 - on-target earnings
  (variable pay included); BASE NOT VERIFIED
```

Notice the last line: a sales-style pay range is flagged as commission-inclusive and never passed off as base salary.

## How it's tested

65 automated tests run on every push (see the badge above). They check behavior a user would care about, for example:

- Unpublished pay stays unknown; a pay range that includes commission doesn't count as a verified salary.
- Weekly and monthly pay is converted to a yearly figure; non-USD pay is never shown as dollars.
- A flashy title with no real substance scores as a poor fit, not a strong match.
- Instruction-like text planted in a posting is flagged and changes no decision.
- One company can't fill the whole shortlist; a posting that disappears is recorded as closed.
- Only the allowed public hosts are ever contacted, and a refusal from a board is never retried around.

The tests run against recorded responses, so they never touch the network.

## Run it

Requires Python 3.11+.

```bash
make setup
make run      # offline: three fictional boards served from recorded payloads in fixtures/
make test
```

`job-scoring run --boards <file>` without `--fixtures` fetches the real public APIs for the boards you list. `job-scoring evaluate postings.json` scores postings you already have. Replace the synthetic profile (`config/profile.example.json`) with your own via `JOB_SCORING_PROFILE`.

## How it works

```
boards file ──► collect.py ──► sources.py ──► evaluate.py ──► pipeline.py ──► report.py
 (companies,     fetch via       normalize     score, tier,     merge, dedupe,   shortlist
  board tokens)  allowlisted     to one        quote            mark closed,     markdown
                 GET-only        posting       evidence         shortlist
                 fetcher         schema
```

Standard library only. Every judgment is tagged `verified` (quoted from the posting), `inferred` (from a rule) or `unknown`.

## Design decisions

- **Unknown is a real answer.** Pay, requirements and qualifications each have an explicit "not stated" outcome, and the report repeats it.
- **Rules over model judgment.** A fixed rule set can be diffed, tested and argued with. A model-assigned "match %" can't.
- **Posting text is data, never instructions.** Scripts, hidden elements and comments are stripped before scoring, and the report escapes markup and links.
- **No pay floor by default.** Pay is reported but not compared unless you set `min_comp_usd`; a range that straddles your floor is flagged, not excluded.

## Provenance and authorship

I wrote the requirements, architecture and tests, and directed AI coding agents (Claude Code and Codex) to write the code. This repository is extracted and rewritten from a larger private system, not the running code itself: the scheduler, approval workflow, application-preparation features and personal profile data were left out, and the profile, boards and postings here are synthetic. It reads public data only and has no application or outreach code.

## License

MIT
