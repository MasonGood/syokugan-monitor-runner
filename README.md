# Public Product Monitor Runner

Monitors the public new-product, preorder, and restock lists on
`syokugan-ohkoku.com`. The runner uses Python's standard library, not a language
model. Notification credentials are supplied only through GitHub Actions Secrets.

This repository has an independent history and contains no legacy private
repository history. Product state contains only publicly listed product data.
Do not put personal contact information, credentials, or private notes in source,
state, issues, or workflow logs.

## Execution

The Product monitor workflow is manually dispatched with one of these modes:

- `check`: read lists and compare state; no SMTP or baseline writes.
- `monitor`: send notifications for pending products and save state only after
  the mail server accepts the message.
- `test_notification`: send one sample image email without updating state.

The default is `check`. All three modes share a concurrency group and check out
the current `main` baseline when the job actually starts.

For notification modes, add repository Secrets named `SMTP_USERNAME`,
`SMTP_AUTH_CODE`, and `EMAIL_TO`. The authorization code is a QQ SMTP app
authorization code, not the account password. Never paste it into chat or commit
it. GitHub cannot export the existing values from another repository.

```sh
python3 -B -m unittest discover -s tests -v
python3 tools/audit_public.py
python3 monitor.py --dry-run
node --test scheduler/worker.test.mjs
```

## Scheduler

Cloudflare runs `scheduler/worker.mjs` with a D1 binding named `DB`,
`GITHUB_REPOSITORY`, `DISPATCH_ENABLED`, and a `GITHUB_TOKEN` Secret.
Initialize the dedicated D1 database using `scheduler/schema.sql`.
The existing UTC schedules remain unchanged:

```text
2,17,32,47 0-12,23 * * *
2 13,15,17,19,21 * * *
```

These correspond to checks approximately every 15 minutes from 07:00 through
20:59 in Asia/Shanghai, and every two hours overnight.

Atomic database claims combine schedule identity and scheduled minute, merging
duplicate scheduled events whose timestamps differ by a few seconds. Requests
include a `slot_id`; workflow run names expose the same identifier for dispatch
reconciliation.

An accepted dispatch is not proof of successful monitoring or email delivery.
Each POST requires a current ownership check and a renewed lease; an in-flight
attempt is recorded as pending before contacting GitHub. Recovery validates run
identifiers and rejects incomplete or changing pagination.

Only a request explicitly rejected by GitHub, or a claim with no attempted POST,
can be retried automatically after reconciliation. A lost response or interrupted
POST remains uncertain even when the runs API returns no match, because run
visibility can be delayed. Later checks can reconcile an existing run, but do not
blindly resend an ambiguous attempt. Subsequent scheduled slots still run and
compare against the latest product baseline.

Recovery is bounded to three POST attempts and one day; exhausted rows are
retained as `needs_attention`. Even the last attempt is checked for an existing
run before being marked for attention. Accepted rows are retained for 30 days.

Use `DISPATCH_ENABLED=false` while staging. Do not enable or change the legacy
dispatch target until Secrets, a hosted check, a sample email, and real scheduled
deduplication have been verified. Retain the former Worker version for rollback.

Delivery still has practical limits: SMTP acceptance does not prove inbox
delivery, and a process crash after SMTP acceptance but before saving the
baseline can cause a repeat email. Workflow concurrency serializes jobs, but a
new queued run can replace an older pending run; dispatch acceptance alone is
not a successful check. Review failed Actions runs and D1
`needs_attention` rows; no separate failure-alert service is configured.
