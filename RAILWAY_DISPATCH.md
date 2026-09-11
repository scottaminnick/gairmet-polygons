# The dispatch cron service

`railway.dispatch.json` configures a **second** Railway service in the
same project as the web app. It runs `scripts/dispatch_workflows.py` on a
schedule, which POSTs a `workflow_dispatch` to both hazard workflows.

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
| Settings → Cron Schedule | `45 3,9,15,21 * * *` |

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

The cycle itself is still covered: each workflow keeps one `schedule:`
backstop at +3:37 after the synoptic hour. That is what publishes the
package if this service is down; the non-zero exit is what tells somebody
to fix it.
