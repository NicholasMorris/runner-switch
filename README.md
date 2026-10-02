# runner-switch

Defaults every repo in `NicholasMorris` and `geep-health` to GitHub-hosted
runners, and flips to the self-hosted ghrp pool on the Mac mini only once the
account's Actions minutes run out. Flips back at the start of each month.

## How workflows opt in

```yaml
runs-on: ${{ vars.RUNNER_MODE == 'self-hosted' && fromJSON('["self-hosted","linux"]') || 'ubuntu-latest' }}
```

An unset variable means hosted, so a repo that predates the switch still works.
Claude Code jobs (`claude.yml`) are always `[self-hosted, linux]` and ignore the switch.

## What the switch does (every 15 min, `switch.yml`)

Per owner in `owners.json`:

1. `self-hosted` but flipped in a previous month -> back to `hosted`.
2. `hosted` and a job in the last 3h was refused with *"recent account payments
   have failed or your spending limit needs to be increased"* (or, where the
   token can read the billing usage API, included minutes are >=95% used)
   -> `self-hosted`.
3. While `self-hosted`, re-runs failed jobs of this month's runs that were
   refused for billing in the last 24h (max 3 attempts).

Variables written: `RUNNER_MODE` (`hosted`|`self-hosted`) and
`RUNNER_MODE_SINCE` (`YYYY-MM` or `-`). Org variable for `geep-health`; repo
variables on every owned repo for `NicholasMorris` (personal accounts have no
account-level variables).

## Manual override

```bash
gh workflow run switch.yml -R NicholasMorris/runner-switch -f mode=self-hosted -f owner=geep-health
gh workflow run switch.yml -R NicholasMorris/runner-switch -f mode=hosted
```

A forced `self-hosted` lasts until the month rolls over; `auto` resumes detection.

## Going live

This repo must be **public**: public repos get unlimited standard GitHub-hosted
minutes, so the switch keeps running when the accounts it manages are out.
(Private, it would cost ~2,900 minutes a month on its own.) There are no
`pull_request` triggers, so secrets never reach fork code.

1. Fill in the secrets (created empty):
   - `NM_TOKEN` - NicholasMorris token: `repo`, `workflow` (read runs, write repo
     variables, re-run jobs). Add `user` scope (or fine-grained "Plan: read") to
     enable the proactive usage check.
   - `GEEP_TOKEN` - geep-admin token: `repo`, `admin:org` (org variables, billing usage).
2. Make the repo public.
3. `gh workflow enable switch.yml -R NicholasMorris/runner-switch` (disabled until then).

## Local dry run

```bash
DRY_RUN=1 NM_TOKEN=$(gh auth token -u NicholasMorris) GEEP_TOKEN=$(gh auth token -u geep-admin) python3 switch.py
```
