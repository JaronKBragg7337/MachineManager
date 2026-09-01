# GitHub Pages dashboard

The visual control center lives in `/dashboard` and is deployed by the
repository's GitHub Actions workflow at [`.github/workflows/pages.yml`](../.github/workflows/pages.yml).

## One-time Pages setup

If Pages has not been enabled for the repository yet:

1. Open the repository's [Pages settings](https://github.com/JaronKBragg7337/MachineManager/settings/pages).
2. Under **Build and deployment**, set **Source** to **GitHub Actions**.
3. Save. A push to `main` that changes `dashboard/` or the Pages workflow starts the deployment.

The public URLs are:

- [Mission Control](https://jaronkbragg7337.github.io/MachineManager/dashboard/)
- [Repository landing page](https://jaronkbragg7337.github.io/MachineManager/), which forwards visitors to Mission Control

The dashboard is deliberately sanitized: no tokens, secrets, private keys,
raw logs, or unrestricted model output are published.

## Live machine updates

The public site is available at:

https://jaronkbragg7337.github.io/MachineManager/dashboard/

The page refreshes the public JSON files once per minute and labels a snapshot
stale after five minutes. The local manager writes those files every
observation. To move updates from the laptop to GitHub Pages, enable
public_upload in the local config and provide a dedicated fine-grained
MACHINE_MANAGER_GITHUB_TOKEN through an ignored env_file. The uploader batches
the sanitized latest, events, and scenarios files into one normal commit every
two minutes by default. A public manager, worker, job, or work-lane state
change requests a publication, but the same cooldown still coalesces rapid
changes into the next due snapshot; this prevents overlapping GitHub Pages
deployments while keeping recovery and lifecycle evidence fresh. It never
uploads raw logs, command lines, private paths, or credentials.

After a successful upload, the manager fast-forwards the local `main` reference
to the same remote commit when the checkout contains only the three generated
dashboard files as unstaged changes. Staged, unrelated, untracked, or diverged
local work causes the mirror step to defer; the remote upload still succeeds and
no local work is overwritten.

The Pages workflow cancels obsolete overlapping deployments and retries a
transient deployment collision once. The public page is considered updated only
after the Pages deployment itself succeeds.

This makes the dashboard public and remotely viewable whenever the machine is
online. It is not a substitute for an always-on host: a sleeping or powered-off
laptop cannot supervise a GPU worker or publish fresh telemetry.
