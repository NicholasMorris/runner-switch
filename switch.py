#!/usr/bin/env python3
"""Flip each owner's RUNNER_MODE between GitHub-hosted and self-hosted runners.

Workflows in every repo pick their runner with

    runs-on: ${{ vars.RUNNER_MODE == 'self-hosted' && fromJSON('["self-hosted","linux"]') || 'ubuntu-latest' }}

so an unset or "hosted" variable means GitHub-hosted, and this script is the
only thing that ever writes it. Per owner, each run:

  1. New month since we flipped? Go back to hosted - included minutes reset.
  2. Hosted and (a recent job was refused for billing, or the usage API says
     included minutes are nearly gone)? Flip to self-hosted.
  3. Self-hosted? Re-run recent runs whose jobs were refused for billing, so
     nothing that failed in the gap between exhaustion and the flip is lost.

The variable is an organization variable for orgs. Personal accounts have no
account-level variables, so for users it is written to every owned repo.
"""

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
VAR = "RUNNER_MODE"
SINCE_VAR = "RUNNER_MODE_SINCE"
BILLING_MSG = "recent account payments have failed or your spending limit"
LOOKBACK_HOURS = 3
RERUN_HOURS = 24
MAX_ATTEMPT = 3
DRY_RUN = os.environ.get("DRY_RUN") == "1"

NOW = dt.datetime.now(dt.timezone.utc)
MONTH = NOW.strftime("%Y-%m")


def api(token, method, path, body=None, ok404=False):
    url = path if path.startswith("http") else API + path
    if DRY_RUN and method != "GET":
        print(f"  [dry-run] {method} {url} {json.dumps(body) if body else ''}")
        return None
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if ok404 and e.code == 404:
            return None
        raise RuntimeError(f"{method} {url}: {e.code} {e.read()[:300]!r}") from e


def paged(token, path, key):
    sep = "&" if "?" in path else "?"
    page = 1
    while True:
        out = api(token, "GET", f"{path}{sep}per_page=100&page={page}")
        items = out[key] if key else out
        yield from items
        if len(items) < 100:
            return
        page += 1


class Owner:
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.kind = cfg["type"]
        self.quota = cfg.get("included_minutes")
        self.threshold = cfg.get("threshold", 0.95)
        self.token = os.environ.get(cfg["token_env"], "")
        self.skip = set(cfg.get("skip_repos", []))

    def repos(self):
        path = f"/orgs/{self.name}/repos?type=all" if self.kind == "org" \
            else "/user/repos?affiliation=owner"
        return [r["name"] for r in paged(self.token, path, None)
                if not r["archived"] and r["owner"]["login"] == self.name
                and r["name"] not in self.skip]

    # Variables -----------------------------------------------------------

    def _var_path(self, repo):
        return f"/orgs/{self.name}/actions/variables" if self.kind == "org" \
            else f"/repos/{self.name}/{repo}/actions/variables"

    def get_var(self, name, repo=None):
        out = api(self.token, "GET", f"{self._var_path(repo)}/{name}", ok404=True)
        return out["value"] if out else None

    def set_var(self, name, value, repo=None, exists=False):
        base = self._var_path(repo)
        body = {"name": name, "value": value}
        if self.kind == "org":
            body["visibility"] = "all"
        if not exists:
            api(self.token, "POST", base, body)
        else:
            api(self.token, "PATCH", f"{base}/{name}", body)

    def state(self, repos):
        """Current (mode, since). For users the newest self-hosted repo wins, and
        set_state rewrites any repo that disagrees, so a new repo converges."""
        targets = [None] if self.kind == "org" else repos
        self._seen = {r: (self.get_var(VAR, r), self.get_var(SINCE_VAR, r)) for r in targets}
        self_hosted = [(m, s or "") for m, s in self._seen.values() if m == "self-hosted"]
        return max(self_hosted, key=lambda m: m[1]) if self_hosted else ("hosted", "")

    def set_state(self, mode, since):
        for r, (cur_mode, cur_since) in self._seen.items():
            if cur_mode != mode:
                self.set_var(VAR, mode, r, exists=cur_mode is not None)
            if cur_since != since:
                self.set_var(SINCE_VAR, since, r, exists=cur_since is not None)

    # Signals -------------------------------------------------------------

    def billing_failures(self, repos, hours):
        """Runs (repo, run) in the last `hours` whose latest attempt had a job
        GitHub refused to start for billing reasons."""
        since = (NOW - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hits = []
        for repo in repos:
            runs = api(self.token, "GET",
                       f"/repos/{self.name}/{repo}/actions/runs?status=failure"
                       f"&created=%3E%3D{since}&per_page=100")["workflow_runs"]
            for run in runs:
                jobs = api(self.token, "GET", run["jobs_url"] + "?per_page=100")["jobs"]
                # Refused jobs never got a runner; skip annotation calls for the rest.
                for job in jobs:
                    if job["conclusion"] != "failure" or job.get("runner_name"):
                        continue
                    notes = api(self.token, "GET",
                                f"{job['check_run_url']}/annotations", ok404=True) or []
                    if any(BILLING_MSG in (n.get("message") or "") for n in notes):
                        hits.append((repo, run))
                        break
        return hits

    def usage_exhausted(self):
        """Proactive check from the billing usage API. Included minutes show up
        as discountAmount, priced per SKU, so summing discounts handles the
        macOS/Windows multipliers for free. Returns None if unavailable."""
        if not self.quota:
            return None
        path = (f"/organizations/{self.name}/settings/billing/usage" if self.kind == "org"
                else f"/users/{self.name}/settings/billing/usage")
        try:
            out = api(self.token, "GET", f"{path}?year={NOW.year}&month={NOW.month}")
        except RuntimeError as e:
            print(f"  usage API unavailable: {e}")
            return None
        used = sum(i["discountAmount"] + i["netAmount"] for i in out.get("usageItems", [])
                   if i["product"] == "actions" and i["unitType"] == "Minutes")
        budget = self.quota * 0.006  # included minutes, priced at the Linux rate
        print(f"  usage: ${used:.2f} of ${budget:.2f} included ({used / budget:.0%})")
        return used >= budget * self.threshold


def run_owner(owner, force):
    print(f"== {owner.name} ({owner.kind})")
    if not owner.token:
        print("  no token, skipping")
        return
    repos = owner.repos()
    mode, since = owner.state(repos)
    print(f"  state: {mode} since {since or '-'}; {len(repos)} repos")
    new_mode, reason = mode, None

    if force in ("hosted", "self-hosted"):
        new_mode, reason = force, "forced by workflow_dispatch"
    elif mode == "self-hosted" and since != MONTH:
        new_mode, reason = "hosted", f"new month ({since} -> {MONTH})"
    elif mode == "hosted":
        hits = owner.billing_failures(repos, LOOKBACK_HOURS)
        if hits:
            new_mode = "self-hosted"
            reason = "billing refusals: " + ", ".join(f"{r}#{run['id']}" for r, run in hits[:5])
        elif owner.usage_exhausted():
            new_mode, reason = "self-hosted", "included minutes nearly exhausted"

    # Always rewrite: keeps new user repos in sync with everyone else.
    owner.set_state(new_mode, MONTH if new_mode == "self-hosted" else "-")
    if new_mode != mode:
        print(f"  FLIP {mode} -> {new_mode}: {reason}")
        summary(f"**{owner.name}**: `{mode}` -> `{new_mode}` ({reason})")
    else:
        summary(f"**{owner.name}**: `{mode}` (no change)")

    if new_mode == "self-hosted":
        for repo, run in owner.billing_failures(repos, RERUN_HOURS):
            if run["run_attempt"] >= MAX_ATTEMPT:
                print(f"  not re-running {repo}#{run['id']}: attempt {run['run_attempt']}")
                continue
            api(owner.token, "POST", f"/repos/{owner.name}/{repo}/actions/runs/{run['id']}/rerun-failed-jobs")
            print(f"  re-ran {repo}#{run['id']} ({run['name']})")
            summary(f"- re-ran {repo} [{run['name']}]({run['html_url']})")


def summary(line):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(line + "\n\n")


def main():
    with open(os.path.join(os.path.dirname(__file__), "owners.json")) as f:
        owners = [Owner(c) for c in json.load(f)]
    force = os.environ.get("FORCE_MODE", "auto")
    only = os.environ.get("ONLY_OWNER", "")
    failed = False
    for owner in owners:
        if only and owner.name != only:
            continue
        try:
            run_owner(owner, force)
        except Exception as e:  # one owner's outage must not block the other
            print(f"  ERROR: {e}")
            failed = True
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
