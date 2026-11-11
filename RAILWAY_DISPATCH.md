# The dispatch cron service

`railway.dispatch.json` configures a **second** Railway service in the
same project as the web app. It runs `scripts/dispatch_workflows.py` on a
schedule, which POSTs a `workflow_dispatch` to every hazard workflow listed
in `pipeline.publish_schedule.HAZARD_WORKFLOWS` (IFR, MTN OBSC, TANGO).

It exists because GitHub delays `schedule:` runs — ~95 minutes on
2026-09-07, up to 4.5 hours by 2026-09-10, and growing. `workflow_dispatch`
is not queued that way. See [METHODS §8.9](docs/METHODS.md) for the
measurements and the reasoning.

## Setting it up

| Where | What |
|---|---|
| New Service → GitHub Repo | this repository |
| Settings → Config-as-code → Railway Config File | `railway.dispatch.json` |
| Variables → New Variable | `GITHUB_TOKEN` = a fine-grained PAT, scoped to this repo, permission **Actions: Read and write** |
| Settings → Cron Schedule | `20 4,10,16,22 * * *` (+1:20, timed to NBM's arrival) |

Then deploy and check the first run's logs for two `DISPATCH … status=204`
lines.

**Point the service at `railway.dispatch.json` explicitly.** Left on the
default `railway.json`, one config file would have to describe both this
cron service and the web app, and the web app's build is not this one.

The cron schedule is *also* in `railway.dispatch.json`, so a service that
reads the config file gets it either way; the UI field is what Railway
shows the forecaster, and `tests/test_publish_schedule.py` checks the file
against `pipeline/publish_schedule.py`. Run
`python3 scripts/dispatch_workflows.py --print-schedule` to print it.

## Checking it without deploying

```
python3 scripts/dispatch_workflows.py --dry-run      # prints URL + payload, sends nothing
python3 scripts/dispatch_workflows.py --print-schedule
```

## If it fails

The job exits non-zero, so Railway marks the run failed and the status
line says why (`status=401` is an expired or wrongly-scoped token,
`status=422` a bad ref or a workflow without a `workflow_dispatch`
trigger, `status=no-response` a network failure).

The cycle itself is still covered, twice: the backstop service below
dispatches again at +2:30, and behind it each workflow keeps one GitHub
`schedule:` entry at +3:37 as the last resort. Those are what publish the
package if this service is down; the non-zero exit is what tells somebody
to fix it.

# The backstop service (+2:30)

A **second** cron service, running the same script with
`--trigger railway-backstop`. It exists because GitHub's `schedule:` is
too unreliable to be the first backstop: over 2026-10-28 to 11-11 it fired
2–356 minutes late (median 83) and skipped about two slots in five. A
Railway cron fires on time.

On a normal day it costs nothing: every hazard's workflow sees the package
already published and exits in seconds, before installing anything. On a
bad day — the primary dispatch did not happen, or its token expired — it
starts the runs the primary should have. +2:30 is after the primary's poll
has closed (~+2:20) and before the viewer's staleness cutoff (+3:05). A run
it starts while a primary run is still publishing queues behind it: each
hazard has one `concurrency:` group, shared by every trigger.

## Setting it up

1. **Create the token.** GitHub → Settings → Developer settings →
   Fine-grained personal access tokens → Generate new token:
   - Resource owner: the repository's owner; Repository access: **Only
     select repositories** → this repository;
   - Permissions → Repository permissions → **Actions: Read and write**
     (nothing else);
   - an expiry **different from the primary token's**, so the two never
     lapse on the same day;
   - a name that says which service it belongs to, e.g.
     `gairmet-dispatch-backstop`.

2. **Create the service.** In the same Railway project:

| Where | What |
|---|---|
| New Service → GitHub Repo | this repository |
| Settings → Config-as-code → Railway Config File | `railway.backstop.json` |
| Variables → New Variable | `GITHUB_TOKEN` = the **new** token from step 1 |
| Settings → Cron Schedule | `30 5,11,17,23 * * *` (+2:30) |

   `GITHUB_TOKEN` is the variable name the script reads, the same as on
   the primary service. Railway variables are per service, so this one
   holds the backstop's own token. The optional `GITHUB_REPOSITORY` and
   `DISPATCH_REF` work here too and default correctly. The start command,
   `python3 scripts/dispatch_workflows.py --trigger railway-backstop`, is
   in the config file; don't retype it in the UI.

3. **Check it without waiting for 05:30Z.** Locally,
   `python3 scripts/dispatch_workflows.py --trigger railway-backstop --dry-run`
   prints the URLs and a payload with `"trigger": "railway-backstop"`, and
   `--print-schedule --trigger railway-backstop` prints the cron line.
   After the first real run, the Railway log should show one `DISPATCH …
   status=204` line per hazard, and each hazard's GitHub run should end
   with `SKIPPING PUBLISH [already-published]` (the normal case) or an
   `NBM-ARRIVAL … trigger=railway-backstop` line (it had work to do).
