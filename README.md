# job-scoring-engine

Ingests postings from public applicant-tracking-system (ATS) job boards, normalizes them into one schema, and scores each against a profile with **deterministic, explainable rules**: every point of score, every exclusion and every gap carries a quoted excerpt from the posting, and anything the posting does not state is reported as unknown rather than guessed.

This is research tooling only. It reads public board APIs; it has no application, outreach or submission code.

## Problem

Job boards are noisy and the usual shortcuts are opaque: a model-assigned "match %" you can't audit, or keyword filters that treat a pay blurb for sales roles as the salary of an engineering role. This engine makes each judgment inspectable and refuses to invent facts: unpublished pay stays "not published", an on-target-earnings range doesn't "verify" a base salary, and your qualifications are always `UNKNOWN` until you check them.

## How it works

```
boards file ──► collect.py ──► sources.py ──► evaluate.py ──► pipeline.py ──► report.py
 (companies,     fetch via       normalize     score, tier,     state: merge,    shortlist
  ATS tokens)    allowlisted     to one        quote           dedupe, close,    markdown
                 GET-only        posting       evidence        shortlist
                 fetcher         schema
```

- **`sources.py`**: Greenhouse, Ashby and Lever adapters. HTTPS GET only, fixed host allowlist, no redirects or cookies, size and time caps, per-host rate limit. A 401/403/429 marks the board blocked and is never retried around. HTML is reduced to visible text *before* scoring, so hidden or script text can't influence a score. The transport is injectable, so tests never touch the network.
- **`evaluate.py`**: the rules. Title family (weighted patterns), responsibility and technology signals, ownership signals, gap penalties, hard exclusions, US eligibility, compensation assessment (structured vs. stated-in-text, annualization, non-USD handling, OTE detection), and instruction-like text flagged as `injection_suspect`. Output is a tier (`strong`, `stretch`, `attractive_poor_fit`, `poor_fit`, `excluded`) plus an evidence trail. A `title_only` variant exists for controlled comparison.
- **`pipeline.py`**: merges evaluations into a JSON-serializable state. A posting missing from a successful listing is recorded as closed, not deleted; changes since last seen are described; duplicates collapse; the shortlist allows at most two entries per company.
- **`report.py`**: Markdown report with all untrusted text neutralized.
- Standard library only. The profile (`config/profile.example.json`) is synthetic; replace it with your own via `JOB_SCORING_PROFILE`.

## Run it

Requires Python 3.11+.

```bash
make setup
make run      # offline: three fictional boards served from recorded payloads in fixtures/
make test
```

`job-scoring run --boards <file>` without `--fixtures` fetches the real public APIs for whatever tokens your boards file lists. `job-scoring evaluate postings.json` scores postings you already have. `config/boards.example.json` and every posting in `fixtures/` are fictional.

## Design decisions

- **Unknown is a first-class value.** Pay, requirements and qualifications each have an explicit "not stated / UNKNOWN" outcome, and the report repeats it.
- **Rules over model judgment for scoring.** A fixed rule set can be diffed, unit-tested and argued with; every claim is tagged `verified` (quoted), `inferred` (rule) or `unknown`.
- **Posting text is data, never instructions.** Scripts, hidden elements and comments are stripped, instruction-like text is flagged, and the report escapes control characters, markup and link syntax.
- **No pay floor by default.** The shipped profile sets none, so pay is reported but never compared; set `min_comp_usd` to enable below-minimum exclusion (a range that straddles the floor is flagged, not excluded).
- **Politeness is enforced in code**: host allowlist, rate limit, no workaround of refusals.

## Provenance and authorship

I wrote the requirements, architecture and tests, and directed AI coding agents (Claude Code and Codex) to write the code. This repository is extracted and rewritten from a larger private production system, not the running code itself: the scheduler, approval workflow, application-preparation features and personal profile data were left out, and the profile, boards and postings here are synthetic.

## License

MIT
