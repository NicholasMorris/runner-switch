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

1. Make the repo public and `gh workflow enable switch.yml -R NicholasMorris/runner-switch`.
2. Credentials - see below.

## Credentials: GitHub App (preferred)

Each run mints two one-hour installation tokens (one per owner) with
`actions/create-github-app-token`, so nothing long-lived and broad is stored.

1. Create the app (pre-filled; check the permissions on the form):
   <https://github.com/settings/apps/new?name=runner-switch-nm&url=https://github.com/NicholasMorris/runner-switch&public=true&webhook_active=false&actions=write&checks=read&actions_variables=write&organization_actions_variables=write&organization_administration=read>
   - Repository: **Actions** read/write (list runs, re-run), **Checks** read
     (billing-refusal annotations), **Variables** read/write (`RUNNER_MODE` on
     personal repos), Metadata read (automatic).
   - Organization: **Variables** read/write (`RUNNER_MODE` org variable),
     **Administration** read (billing usage).
   - Webhook off. "Any account" can install - needed to install it on both
     your user and the geep-health org; only the private-key holder can mint
     tokens.
2. Install it on **NicholasMorris** (all repositories) and **geep-health**
   (all repositories).
3. Generate a private key, then:
   ```bash
   gh variable set APP_CLIENT_ID -R NicholasMorris/runner-switch --body <client id from the app page>
   gh secret set APP_PRIVATE_KEY -R NicholasMorris/runner-switch < ~/Downloads/runner-switch-nm.*.private-key.pem
   rm ~/Downloads/runner-switch-nm.*.private-key.pem
   gh workflow run switch.yml -R NicholasMorris/runner-switch   # check it goes green
   gh secret delete NM_TOKEN -R NicholasMorris/runner-switch
   gh secret delete GEEP_TOKEN -R NicholasMorris/runner-switch
   ```

The app cannot read a personal account's billing usage, so NicholasMorris
relies on the billing-refusal signal alone (as it already does).

### Fallback: stored tokens

While `APP_CLIENT_ID` is unset the workflow uses `NM_TOKEN` / `GEEP_TOKEN`:
NicholasMorris and geep-admin tokens with `repo`, `workflow` (+ `admin:org` for
geep). Broad - replace with the app.

## Local dry run

```bash
DRY_RUN=1 NM_TOKEN=$(gh auth token -u NicholasMorris) GEEP_TOKEN=$(gh auth token -u geep-admin) python3 switch.py
```
