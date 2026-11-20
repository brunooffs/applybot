# applybot: architecture

A local, single-user, **review-first** job application assistant. It finds
postings, judges them against a verified profile, picks the right CV, drafts a
grounded cover letter, fact-checks it, and puts it in a queue for a human.
Nothing leaves the machine as an application unless you run `send --yes`.

## 1. The idea in one paragraph

An LLM is good at *reading* messy text and bad at being trusted with
decisions or facts. So the LLM only **extracts** (what does this posting
require, how well does it fit) and **drafts** (a letter). Plain Python code
**decides** (reject / human review / auto-eligible) and plain Python checks
the draft (numbers must exist in your facts). Every outgoing message passes
through a human approval step, and anything about money, dates or legal status
is always yours to answer.

## 2. End-to-end flow

```
 SOURCES                       JUDGEMENT                          OUTPUT
 -------                       ---------                          ------
 Greenhouse boards ─┐
 Lever boards      ─┤  ingest     prefilter      score (LLM extracts,
 Ashby boards      ─┼────────▶  (keywords,   ─▶  code decides)  ──▶ reject  (stored, no letter)
 Remotive feed     ─┘   dedupe)   cheap, no LLM)  + pick CV         human   ──┐
                                                                    auto_eligible ─┤
                                                                                   ▼
                                  draft letter (LLM, facts.yaml only)
                                           │
                                  guard 1: every number must be in facts/posting
                                  guard 2: second LLM fact-checks each claim
                                           │
                              ┌────────────┴────────────┐
                           drafted                  needs_edit
                              │                  (blocked until you edit)
                              ▼
                  YOU: queue → show → (edit) → approve
                              │
                     outbox/<job>/  letter.md, job.md, CV copy
                              │
                    send (dry-run by default; --yes to send)
                    SMTP over SSL → your mail server; copy to Sent via IMAP
                              │
                              ▼
          replies: read-only IMAP poll → match by Message-ID threading
                              │
              bounce ──▶ marked, application = bounced, no LLM
              reply  ──▶ classify (LLM)
                              │
        money/dates/legal/visa ──▶ needs_you (never drafted)
        safe question          ──▶ draft reply → same fact-check → draft_ready
                              │
                    YOU: inbox → reply-edit → reply --yes
```

## 3. Modules

| File | Responsibility |
|---|---|
| `cli.py` | All commands; thin glue only. |
| `config.py` | Loads `config/*.yaml` and `cvs/cvs.yaml` from your applybot folder (`$APPLYBOT_HOME`, else `~/.applybot`) into one `Config`; criteria and sources fall back to the packaged defaults in `defaults/`. Family → CV lookup. |
| `models.py` | `Job` dataclass; `dedupe_key` = company + title + location. |
| `sources/` | One adapter per board type. Each has a pure `parse_job` (unit-tested) and a `fetch`. `__init__.iter_jobs` runs them all and survives a failing board. |
| `text.py` | HTML → plain text. |
| `prefilter.py` | Keyword gate on the title before any LLM cost. |
| `llm.py` | Minimal client: Ollama (local) or Anthropic API, both over plain HTTP. Asks for JSON, retries once on malformed output. |
| `scoring.py` | The LLM prompt for extraction **and** `decide()`, the deterministic rules. |
| `drafting.py` | Letter prompt, `numeric_guard`, LLM fact-check. |
| `pipeline.py` | `ingest`, `score_pending`, `draft_pending`: idempotent, resumable stages. |
| `packets.py` | Builds `outbox/<job>/` when you approve. |
| `mailer.py` | SMTP send (returns the server's reply and queue id), IMAP connect, save to Sent. |
| `replies.py` | Poll, match, bounce detection, classify, escalate, draft replies. |
| `actions.py` | Approve, reject, edit, send an application, send a reply. Used by both the CLI and the dashboard so the safety checks live in one place. |
| `web.py` | `applybot serve`: local dashboard (stdlib only, 127.0.0.1, token + Host/Origin checks, confirmation page before every send). No decision logic. |
| `trial.py` | `applybot try`: judge one pasted posting and explain it. Stores nothing. |
| `db.py` | SQLite schema and small helpers. |

## 4. Data model (SQLite, `data/applybot.sqlite`)

- **jobs**: one row per posting. `status`: `new` → `scored`, or `filtered` (failed the prefilter). Unique on `id` and on `dedupe_key`, so the same role from two sources is stored once.
- **scores**: one row per scored job: `family`, `cv_id`, `overall`, `decision`, `reasons`, `language`, `contact_email`, and the raw LLM JSON (kept for auditing).
- **applications**: the draft letter and its life: `drafted` | `needs_edit` → `approved` → `sent`, or `rejected` | `bounced`.
- **messages**: every mail we sent (`out`) and every reply we matched (`in`), with `parent_id` linking a reply to what it answered. The `id` of an `in` row is the number you use in `inbox N` and `reply N`.

## 5. The decision rules (`scoring.decide`)

The LLM returns per-dimension scores, the job family, the posting's mandatory
requirements (each with a status and the *basis* on which you'd meet it), and
the work arrangement. Code then applies, in this order:

1. Family not one of your known families → **reject**.
2. Cannot be done from your home country (`work_eligibility`; the job's location string is checked by `eligibility.py` before the model's answer is used) → **reject**, only with a verbatim quote from the posting.
3. A **disqualifying** requirement is unmet → **reject** (a high score cannot hide it).
4. Weighted score below 55 → **reject**.
5. Otherwise collect reasons for human review: a non-blocking unmet requirement, an unconfirmable hard requirement, unclear location eligibility, on-site role, **no CV mapped for the family**, a selective/stretch family (Engineering Manager, Project Manager, Agile Coach, AI Engineer, AI Platform Engineer, Principal Engineer/Architect), or score below 85.
6. Any reasons → **human**. None → **auto_eligible**.

Weights (`config/criteria.yaml`): technical 30, seniority 20, responsibilities 20,
architecture/domain 10, geography 10, employment 10. Thresholds: 85 / 70 / 55.

Key subtlety: a requirement the model calls "met" only counts if its basis is
`production`, `project` or `working`. "Met on familiarity" is downgraded to
*unknown*. The prompt also tells the model that independent-project
experience does not satisfy "professional experience".

`auto_eligible` is only a label today. `auto_send_enabled` is false and nothing
reads it yet: every application needs `approve` and `send --yes`.

## 6. CV selection

The LLM picks one **job family** from your list. `cvs/cvs.yaml` then maps a
family to exactly one CV id. This is deliberately a lookup, not a second guess:
a family with no CV goes to human review with "no CV mapped". `approve`
copies that CV into the packet; `send` refuses to send if the file is missing.

## 7. Safety guards (what stops it embarrassing you)

| Risk | Guard |
|---|---|
| Invented experience | Letters may use only `config/facts.yaml`; skill levels (`production`/`project`/…) are in the prompt; `not_claimed` lists what you must never claim. |
| Invented numbers ("15 years") | `numeric_guard`: every number in a letter must appear in facts or the posting. |
| Subtle false claims | A second LLM call lists each claim and whether facts support it. A failing letter gets one automatic rewrite with the flagged claims fed back (`pipeline.max_revisions`, default 1) and is re-checked. If it still fails it becomes `needs_edit` and `approve` refuses it. |
| Wrong CV | Family → CV is a fixed table; unmapped families go to a human. |
| Answering money / legal questions | Replies mentioning salary, rates, notice, availability, visa/authorisation, relocation are `needs_you` and never drafted. |
| Bulk-sender behaviour | `send` is dry-run by default, capped at 15 per day, 3 minutes apart. |
| Reading your private mail | Only messages that thread back to something we sent are stored; polling is read-only (nothing is marked read). |
| Leaked credentials | Mail password and API key come only from environment variables, never from files. |

## 8. What leaves your machine

- Job boards: plain HTTPS GETs of public job data.
- Your mail server (host set in `settings.yaml`): SMTP (465) and IMAP (993), TLS.
- The LLM provider you choose in `config/settings.yaml`:
  - `ollama`: stays local.
  - `anthropic`: the posting text, your `facts.yaml` and the text of replies
    you process are sent to the API. Do not put anything in `facts.yaml` you
    would not send there.
  - `llm.stages` can route each stage (`score`, `draft`, `revise`, `verify`, `reply`; `revise` follows `draft` unless set) to a
    different provider. With `draft: ollama, verify: anthropic` the letter is written
    locally, but the letter and your `facts.yaml` are still sent to the API for the
    fact-check.
- A rejection needs a verbatim quote from the posting (`evidence`). Without one
  the job goes to human review instead of being discarded.

## 9. Configuration map

| File | What it controls |
|---|---|
| `config/settings.yaml` | LLM provider/model, mail server, caps, paths |
| `config/profile.yaml` | Families, preferences, positioning (used in prompts) |
| `config/facts.yaml` | The ONLY source letters may draw on (skills with levels, projects, not-claimed) |
| `config/criteria.yaml` | Weights, thresholds, prefilter keywords |
| `config/sources.yaml` | Board tokens and feeds to read |
| `cvs/cvs.yaml` + `cvs/*.pdf` | CV pool and family → CV mapping |

## 10. Commands

```
applybot try <file|->        judge ONE pasted posting; shows CV, score, letter. Stores/sends nothing
applybot run [--limit N]     ingest + score + draft. Never sends
applybot ingest|score|draft  the same stages one at a time
applybot queue | show ID     review drafts
applybot edit ID             fix a letter in $EDITOR
applybot approve ID          build outbox/<job>/ (refuses needs_edit; no override)
applybot reject ID
applybot send [--yes]        dry-run unless --yes
applybot replies             poll IMAP, match, classify, draft
applybot inbox [N] [--all]   replies waiting / read one
applybot reply-edit N | reply N [--yes]
applybot selftest [--to X] [--yes]    send one labelled test mail
applybot cvs | stats
```

## 11. Testing

`pytest` (46 tests) covers the decision rules, the guards, the parsers, mail
transport choice, threading/matching, escalation, bounces, and the full
pipeline with a fake LLM. Not covered by tests: the live job-board responses,
a real LLM's judgement quality, and ATS forms. Those need real runs.

## 12. Known limits

- Source adapters follow the vendors' documented formats and have not been verified against live data.
- Most postings have no email address; for those, `approve` prepares a packet and you apply on the company's site.
- Recruiters who write to you first are not handled (only replies to your sent mail).
- Weights and thresholds are an initial proposal, not calibrated.
- LLM judgement on a small local model will be noticeably weaker than on a strong hosted one.

## 13. Next steps (in order)

1. **Tidy the CV pool and mapping.** Make every `file:` in `cvs/cvs.yaml` match a real file, and map the families that currently have no CV (they all go to human review until you do). Any CV file not listed in `cvs.yaml` is never used.
2. **Finish `config/facts.yaml`.** Resolve the `TODO` and `CONFIRM` lines (employer dates, certifications, skill levels). This is the single biggest lever on letter quality and honesty.
3. **Calibrate with `applybot try`** on 10 to 15 real postings you know well: a clear fit, a clear miss, an "architect" that is really management, one requiring C#/.NET, one needing a visa. Read the CV choice and the reasons; adjust prompts, weights and thresholds until you agree with it.
4. **First real ingest.** Add 5 to 10 company board tokens to `config/sources.yaml`, run `applybot run --limit 20`, and fix whatever the live APIs do differently from the documented shape.
5. **Review loop for a week, no sending.** Use `queue`/`show`, note where you disagree, and tighten the rules.
6. **First real, hand-picked send** (one application, to a real contact email), watching the whole loop: send, reply, inbox.
7. **Scheduling and a daily digest** (cron/launchd running `run` and `replies`, one summary of what needs you).
8. **ATS form filling** with a headless browser (Playwright), one ATS at a time, only for the answers that come from verified facts.
9. **Inbound recruiter mail** (people who write to you first) and an HN "Who is hiring" source.
10. **Only then** consider letting high-confidence applications auto-send, and only after weeks of review showing it would have been right.
