# applybot

<p align="center">
  <img src="docs/images/meat-proxy.webp" width="760" alt="applybot, a.k.a. The Meat Proxy Proxy: an AI applying for a human job with another AI">
</p>

<p align="center"><em>applybot, a.k.a. The Meat Proxy Proxy</em></p>

[![CI](https://github.com/brunooffs/applybot/actions/workflows/ci.yml/badge.svg)](https://github.com/brunooffs/applybot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Local, review-first job search assistant for one person. Finds postings,
scores them against a verified profile, picks the right CV, drafts a
grounded cover letter, fact-checks it, and puts it in a queue for you to
approve. Nothing is sent without `applybot send --yes`.

Everything runs on your machine. The only network calls are to the job boards
you list and to the LLM you choose: a fully local one via [Ollama](https://ollama.com)
(no data leaves your computer), or optionally a hosted API. Your personal data
(facts, profile, settings, CVs, database) stays in git-ignored files and is
never part of the repository.

```
sources -> prefilter -> LLM extraction -> deterministic decision -> CV pick
        -> letter draft -> numeric guard + LLM fact-check -> review queue
        -> approve (packet in outbox/) -> send (optional, SMTP)
```

Full design, data model, decision rules and roadmap: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Try it without sending anything:** `pbpaste | applybot try -` (macOS: copy a
job posting first; on Linux use `xclip -o | applybot try -`) shows which CV would be chosen, the score and reasons, and
the drafted letter. It stores and sends nothing.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest            # no network needed
applybot init               # creates your own config files from the *.example templates
```

The committed `*.example` files describe a fictional candidate. `applybot init`
copies them to `config/facts.yaml`, `config/profile.yaml`, `config/settings.yaml`
and `cvs/cvs.yaml`, which are git-ignored. Then:

1. Edit `config/facts.yaml` with YOUR details: resolve every `TODO` and
   `CONFIRM`. This file is the only thing letters may draw from.
2. Describe yourself and your target roles in `config/profile.yaml`.
3. Put your CVs in `cvs/` (names in `cvs/cvs.yaml`). `applybot cvs` shows what
   is missing and which job families have no CV.
4. Pick the LLM in `config/settings.yaml`: `ollama` (local, fully open source)
   or `anthropic` (hosted, optional, needs `ANTHROPIC_API_KEY`).
5. Add company board tokens to `config/sources.yaml`.

## Daily use

```bash
applybot run --limit 30     # ingest + score + draft
applybot queue              # drafts waiting, best first ("!" = failed verification)
applybot show <id-prefix>   # posting, reasons, letter, verification issues
applybot edit <id-prefix>   # fix the letter in $EDITOR
applybot approve <id-prefix># builds outbox/<job>/ with letter.md, job.md, CV copy
applybot send               # dry-run; add --yes to send (needs contact email in the posting)
applybot stats
```

## Dashboard (optional)

```
applybot serve --open        # http://127.0.0.1:8765, Ctrl-C to stop
```

Sources (add and check Greenhouse/Lever/Ashby tokens and Remotive categories), Run (fetch, score and draft in the background with progress, a stop button and a minimum score for drafting), paste-a-posting (`try`), the review queue with an editor, approve/reject, a
confirmation page before every send, and the inbox with reply drafts. It calls
the same code as the commands above, so the same checks apply (fact-check, CV
file must exist, daily cap). It only answers on your own computer.

Start it from the terminal where `APPLYBOT_MAIL_PASS` (and `ANTHROPIC_API_KEY`,
if used) are exported, otherwise sending and "Check replies" will fail. New
postings still come from `applybot run`.

## Mail (your own SMTP/IMAP account)

Settings are in `config/settings.yaml` under `mail`. The password is only ever
read from the environment:

```bash
export APPLYBOT_MAIL_PASS='...'     # never put it in a file in this repo
```

```bash
applybot send --yes         # SMTP over SSL (465); daily cap + spacing; copies to Sent via IMAP
applybot replies            # read-only IMAP poll -> match replies -> classify -> draft
applybot inbox              # replies waiting for you ("needs_you" = yours to answer)
applybot inbox 3            # read one, with its draft
applybot reply-edit 3       # write/fix the reply in $EDITOR
applybot reply 3 --yes      # send it, threaded (dry-run without --yes)
```

Replies are matched by Message-ID threading to mail applybot sent, so the rest
of your inbox is never read into the database. Anything mentioning salary,
rates, notice period, availability, visa/authorisation or relocation is
escalated to `needs_you` and never drafted. Recruiters who write to you first
are not handled yet.

## Design rules

- The LLM extracts; plain code decides (`scoring.decide`). A high score can
  never hide an unmet disqualifying requirement.
- "Met" only counts if the basis is real: independent-project experience does
  not satisfy "professional experience".
- Unmapped family, selective family, on-site, unclear location eligibility,
  unconfirmable hard requirement: always human review.
- Letters: only `facts.yaml`; every number must appear in facts or the posting;
  a second LLM pass checks each claim. Failing letters are `needs_edit`.
- Never auto-answers application-form questions on years, certifications,
  clearance or work authorisation.
- `auto_send_enabled` is false. `auto_eligible` is a label for now.

## Not built yet

- ATS form filling (headless Playwright), most postings have no email address.
- Reading recruiter replies and drafting responses.
- Source adapters beyond Greenhouse and Remotive are tested against documented shapes only (Greenhouse and Remotive were checked live).
- HN "Who is hiring" source.

## Known limitations

- Location eligibility is configured in `profile.yaml` (`work_eligibility`: home
  country, authorised regions, accepts relocation). The job's location string is
  read by plain code first (`eligibility.py`), the model only decides what that
  cannot. The country and region lists are not exhaustive; additions welcome.
- A letter with fact-check flags cannot be approved. The checks include a
  plain-code test for wording copied from the posting that your facts do not contain.
- Lever and Ashby adapters are tested against documented response shapes only,
  not yet verified against live endpoints.
- LinkedIn and Indeed are deliberately not supported (terms of service).

## Responsible use

applybot is a drafting and review tool. `auto_send_enabled` is off by default,
every application goes through your approval, and letters may only state facts
you wrote in `facts.yaml`. You are responsible for what you send and for
following each job site's terms of service.

## Contributing and license

Issues and pull requests are welcome, see [CONTRIBUTING.md](CONTRIBUTING.md).
Report security problems as described in [SECURITY.md](SECURITY.md).
Released under the [MIT License](LICENSE).