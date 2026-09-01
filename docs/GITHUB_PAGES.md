# Enabling the Dashboard

The visual control center lives in `/dashboard`.

The repository is currently published through the legacy branch source:

1. Go to https://github.com/JaronKBragg7337/MachineManager/settings/pages
2. Source: Deploy from a branch
3. Branch: main / folder: /
4. Save

The repository root forwards visitors to the direct control-center path:
https://jaronkbragg7337.github.io/MachineManager/dashboard/

Once live the URL will be:
https://jaronkbragg7337.github.io/MachineManager/

(or with /dashboard if configured that way)

The dashboard is deliberately sanitized — no tokens, secrets, or private keys are ever published.

## Live machine updates

The public site is available at:

https://jaronkbragg7337.github.io/MachineManager/dashboard/

The page refreshes the public JSON files once per minute and labels a snapshot
stale after five minutes. The local manager writes those files every
observation. To move updates from the laptop to GitHub Pages, enable
public_upload in the local config and provide a dedicated fine-grained
MACHINE_MANAGER_GITHUB_TOKEN through an ignored env_file. The uploader batches
the sanitized latest, events, and scenarios files into one normal commit every
five minutes by default. A public manager, worker, job, or work-lane state
change publishes immediately, so recovery and lifecycle changes do not wait for
the regular cadence. It never uploads raw logs, command lines, private paths,
or credentials.

After a successful upload, the manager fast-forwards the local `main` reference
to the same remote commit when the checkout contains only the three generated
dashboard files as unstaged changes. Staged, unrelated, untracked, or diverged
local work causes the mirror step to defer; the remote upload still succeeds and
no local work is overwritten.

This makes the dashboard public and remotely viewable whenever the machine is
online. It is not a substitute for an always-on host: a sleeping or powered-off
laptop cannot supervise a GPU worker or publish fresh telemetry.
