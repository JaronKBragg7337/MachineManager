# Revenue-lane research snapshot

Checked: 2026-08-30. This is a source-backed shortlist for future work, not a
guarantee of earnings and not an account, application, or submission.

## What the first scout found

### Superteam Earn for Agents

[Superteam Earn's agent interface](https://superteam.fun/earn/agents/) explicitly
describes agent registration, discovery of `AGENT_ALLOWED` and `AGENT_ONLY`
listings, and artifact submission. Its published flow keeps OAuth, wallet
signing, KYC, and payout claim with a human operator. That makes this an
interesting future integration candidate because the platform itself defines an
agent path rather than treating automated access as an accident.

Before any registration, Machine Manager should read the exact listing,
eligibility questions, and payout requirements. A human-owned claim/payout
handoff remains a real work item when the service requests it.

### IssueHunt

[IssueHunt's terms](https://oss.issuehunt.io/terms) describe funded open-source
issues where a contributor proposes a pull request and a maintainer determines
approval. They also say registration must be made by the actual individual or
corporation and not by proxy. That means a human-owned account is needed before
any contribution or payout action, while public issue/repository research can
remain read-only.

### Algora

[Algora's terms](https://algora.io/legal/terms) list coding bounties, but also
prohibit using a robot, spider, or other automatic device to access the service.
Machine Manager should not automate browsing, claiming, or submitting on Algora
unless the platform gives written permission. It can still treat a manually
selected, authorized repository issue as a normal code-delivery project.

### OnlyDust

[OnlyDust's terms](https://www.onlydust.com/terms) apply to its open-source
contributor platform. Identity and payment steps should be treated as
human-owned handoffs if this lane is explored later. No listing or payout claim
has been selected for the system.

## Immediate no-credential work

The system can now maintain a public research record, inspect a specific public
issue's scope and repository rules, build a clean branch/PR when the work is
clearly authorized, and record evidence of the result. It does not need to
invent a revenue claim or create an account just to look busy.

On the checked date, Superteam's public agent and development-listing pages did
not expose a specific active agent-eligible software task with a public scope,
reward, and deadline that could be assessed. The next listing check should be a
fresh observation, not an assumption that this result stays true.

The next real decision is not "turn on every platform." It is to choose one
specific, currently open, eligible task and run an ordinary engineering
assessment: scope, expected deliverable, terms, acceptance path, payout path,
and whether a human-owned sign-in or verification step is actually requested.

## Unattended public scout

Machine Manager can run this assessment as a recurring `research` task. The
worker reads only the listed public URLs, writes its detailed evidence artifact
locally, and publishes aggregate source and completion metrics. The public
dashboard shows the task lifecycle and next scheduled run; source excerpts and
model output remain local.

The scout is deliberately an opportunity-finding lane, not an account or
payout worker. A later objective may proceed only after a specific opportunity
has a clear deliverable, eligibility, acceptance path, and human-owned handoff
for any sign-in, identity, wallet, or payout step.

Example local configuration:

```json
{
  "id": "revenue-lane-scout",
  "kind": "research",
  "objective_id": "revenue-opportunities",
  "interval_s": 86400,
  "enabled": true,
  "payload": {
    "question": "Find one currently visible, publicly documented software-revenue opportunity or explain why none can yet be selected; compare eligibility, deliverable, terms, and the next human-owned handoff.",
    "sources": [
      {"title": "Superteam Earn agent listings", "url": "https://superteam.fun/earn/agents/"},
      {"title": "IssueHunt terms", "url": "https://oss.issuehunt.io/terms"},
      {"title": "Algora terms", "url": "https://algora.io/legal/terms"}
    ]
  }
}
```
