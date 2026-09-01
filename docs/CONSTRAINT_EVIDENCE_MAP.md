# Constraint evidence map

This map turns audit categories into testable engineering questions. It does
not treat a phrase match as a command to remove a rule, and it does not publish
the local audit excerpts or file locations behind a finding.

## How to read an audit lead

An audit lead means only that a project contains language worth reviewing. The
review asks four things:

1. What concrete operation does the language affect?
2. Which layer already owns that responsibility?
3. What observable test proves that layer works?
4. Is there evidence of a gap that justifies another control?

The answer can be **keep**, **test**, **record as provider or service behavior**,
or **consolidate after evidence**. It is never automatically "remove."

Each completed audit now turns its known categories into a deterministic review
plan. The plan is stored locally and projected publicly as category counts,
stable test IDs, and a short recommended evidence test. Older audit records are
reconstructed from their sanitized category counts, so a manager restart does
not erase the next action.

## Data-boundary evidence

The current evidence audit reports data-boundary candidates as a category-level
signal. The following map covers the public data flow without exposing the
underlying local matches.

| Public boundary | Existing control | Evidence | Current decision |
| --- | --- | --- | --- |
| Local audit intake | Environment files, local logs, secret folders, dependency/build folders, and non-text files are excluded from audit input; sensitive excerpts are redacted. | `test_constraint_audit_skips_local_secret_and_environment_sources`; `test_constraint_audit_records_candidates_without_changing_source` | Keep. This is an audit-data boundary, not an autonomy restriction. |
| Telemetry projection | The publisher constructs compact public records from explicit field and metric allowlists, with sensitive text redaction. | `test_publisher_allowlists_public_fields`; `test_public_telemetry_allowlists_workstream_fields` | Keep. It protects the public dashboard from local runtime details. |
| Upload preflight | Public files are checked for prohibited field names and private paths before any GitHub API request. | `test_public_upload_rejects_pid_before_network_access`; `test_public_upload_rejects_sensitive_fields_and_private_paths_before_network_access` | Keep. This is the last local egress check before publication. |
| Browser rendering and freshness | Dashboard values are HTML-escaped before markup is built. Snapshot age is visibly classified as fresh, aging, stale, or unavailable instead of being presented as a direct live connection. | Code review of the dashboard render/freshness path; manual browser verification during dashboard work | Keep; add automated browser coverage when the dashboard gains a browser-test harness. |

## Autonomy evidence queue

The audit's autonomy-language category is a queue for bounded capability tests,
not a blanket list of prohibitions. Each test uses synthetic input and stops
before any external side effect unless the applicable authority, tool, and
service are present.

| Operation | Smallest useful evidence test | What the result records |
| --- | --- | --- |
| Public publishing | Validate a harmless telemetry artifact and verify any denial happens before network transport. | Whether the local publisher protects the public boundary. |
| Developer tooling | Run a bounded install/build test only when a real project objective needs it. | Worker/tool capability for the current runtime version. |
| Transparent outreach | Use the existing disclosure and opt-out workflow before a real recipient is contacted. | Whether the communication worker respects the delegated operating charter. |
| Account or payout handoff | Validate only synthetic handoff data until a specific service and human-owned sign-in step exist. | The precise service-owned handoff, not an invented system restriction. |

This is deliberately evidence-first: a current model or platform change is a
reason to rerun the relevant small test, not a reason to preserve an old
assumption forever.
