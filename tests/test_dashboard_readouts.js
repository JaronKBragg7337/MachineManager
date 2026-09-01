"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const dashboard = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "dashboard_active_zero_sample.json"), "utf8"));
const idleFixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "dashboard_idle_zero_sample.json"), "utf8"));
const start = dashboard.indexOf("function escapeHtml(value)");
const end = dashboard.indexOf("function workProgress()", start);
const resourceStart = dashboard.indexOf("function resourceMeterLabel(value, percent)", end);
const resourceEnd = dashboard.indexOf("function renderWorkersTable", resourceStart);
const metricStart = dashboard.indexOf("function compactNumber(value)", end);
const metricEnd = dashboard.indexOf("function eventRow(event)", metricStart);
const queueStart = dashboard.indexOf("function taskKindLabel(value)");
const queueEnd = dashboard.indexOf("function renderRecurringSchedules", queueStart);
const auditStart = dashboard.indexOf("function auditCategoryText(categories)");
const auditEnd = dashboard.indexOf("function renderEvidence()", auditStart);

assert.ok(start >= 0, "dashboard helper section must exist");
assert.ok(end > start, "dashboard helper section must have an end marker");
assert.ok(resourceStart > end, "dashboard resource helper section must exist");
assert.ok(resourceEnd > resourceStart, "dashboard resource helper section must have an end marker");
assert.ok(metricStart > end, "dashboard metric helper section must exist");
assert.ok(metricEnd > metricStart, "dashboard metric helper section must have an end marker");
assert.ok(queueStart > resourceEnd, "dashboard queue helper section must exist");
assert.ok(queueEnd > queueStart, "dashboard queue helper section must have an end marker");
assert.ok(auditStart > resourceEnd, "dashboard audit helper section must exist");
assert.ok(auditEnd > auditStart, "dashboard audit helper section must have an end marker");

// Execute only the dashboard helper section in an isolated context; no DOM or
// browser globals are needed for these pure readout functions.
const executionContext = { fixture, idleFixture, output: null };
vm.createContext(executionContext);
vm.runInContext(
    dashboard.slice(start, end) +
    dashboard.slice(resourceStart, resourceEnd) +
    dashboard.slice(metricStart, metricEnd) +
    dashboard.slice(queueStart, queueEnd) +
    dashboard.slice(auditStart, auditEnd) +
    "\noutput = {" +
    "gpuText: gpuActivityText(fixture.gpu)," +
    "gpuBasis: gpuActivityBasisText(fixture.gpu)," +
    "gpuBasisFallback: gpuActivityBasisText({resource_active: true, mem_used_mib: 2367, power_w: 80})," +
    "gpuRecentBasisFallback: gpuActivityBasisText({util_pct: 0, util_pct_recent_max: 82, util_pct_sample_count: 3, mem_used_mib: 0, power_w: 3})," +
    "gpuRecentCardFallback: gpuActivityCard({util_pct: 0, util_pct_recent_max: 82, util_pct_sample_count: 3, mem_used_mib: 0, power_w: 3})," +
    "gpuObservation: gpuObservationText(fixture.gpu)," +
    "gpuHighObservation: gpuObservationText(Object.assign({}, fixture.gpu, {util_pct: 70, activity_basis: \"driver_utilization\"}))," +
    "gpuHighCard: gpuActivityCard(Object.assign({}, fixture.gpu, {util_pct: 70}))," +
    "gpuCard: gpuActivityCard(fixture.gpu)," +
    "cpuText: cpuActivityText(fixture.system, fixture.gpu)," +
    "cpuBasis: cpuLoadBasisText(fixture.system, fixture.gpu)," +
    "cpuIdleBasis: cpuLoadBasisText(idleFixture.system, idleFixture.gpu)," +
    "cpuHighText: cpuActivityText(Object.assign({}, fixture.system, {cpu_pct: 5.0, cpu_pct_recent_max: 9.2}), fixture.gpu)," +
    "cpuHighCard: cpuActivityCard(Object.assign({}, fixture.system, {cpu_pct: 5.0, cpu_pct_recent_max: 9.2, load_state: \"ACTIVE\", load_basis: \"host_counter\"}), fixture.gpu)," +
    "cpuCard: cpuActivityCard(fixture.system, fixture.gpu)," +
    "idleGpuText: gpuActivityText(idleFixture.gpu)," +
    "idleGpuObservation: gpuObservationText(idleFixture.gpu)," +
    "idleGpuCard: gpuActivityCard(idleFixture.gpu)," +
    "idleCpuText: cpuActivityText(idleFixture.system, idleFixture.gpu)," +
    "idleCpuCard: cpuActivityCard(idleFixture.system, idleFixture.gpu)," +
    "unavailableGpuCard: gpuActivityCard({})," +
    "unavailableCpuCard: cpuActivityCard({}, {})," +
    "numericLabel: resourceMeterLabel(\"86\", 86)," +
    "zeroGpuMetric: eventMetricChips({metrics: {util_pct: 0, mem_used_mib: 2367, power_w: 80, resource_active: true}})," +
    "zeroGpuMetricWithHistory: eventMetricChips({metrics: {util_pct: 0, util_pct_recent_max: 77, util_pct_sample_count: 3, util_pct_zero_samples: 1, mem_used_mib: 2367, power_w: 80, resource_active: true}})," +
    "zeroGpuMetricRecentOnly: eventMetricChips({metrics: {util_pct: 0, util_pct_recent_max: 77, util_pct_sample_count: 3, util_pct_zero_samples: 1, mem_used_mib: 0, power_w: 3}})," +
    "queueEvidence: taskEvidenceLabel({message: \"Result summary\", outcome: \"handler_completed\"})," +
    "queueEvidenceFallback: taskEvidenceLabel({outcome: \"handler_completed\"})," +
    "queueTable: renderQueueActivity([{task_id: \"task-demo\", kind: \"research\", objective_id: \"demo\", status: \"COMPLETE\", attempts: 1, updated: \"2026-09-01T12:34:56Z\", message: \"Recorded\"}])," +
    "reviewPlan: auditReviewPlan([{category: \"approval_gate\", candidate_count: 2, recommended_test: \"Run a harmless delegated action.\"}])," +
    "reviewPlanSingular: auditReviewPlan([{category: \"scope_boundary\", candidate_count: 1, recommended_test: \"Run a bounded scope test.\"}])," +
    "plannedCount: auditReviewCount([{review_plan: [{category: \"approval_gate\"}, {category: \"scope_boundary\"}]}, {review_plan: []}])" +
    "};",
  executionContext,
  { filename: path.join(root, "dashboard", "index.html") },
);

const output = executionContext.output;
assert.strictEqual(output.gpuText, "ACTIVE");
assert.strictEqual(output.gpuBasis, "dedicated memory + power");
assert.strictEqual(output.gpuBasisFallback, "dedicated memory + power");
assert.strictEqual(output.gpuRecentBasisFallback, "recent driver utilization");
assert.match(output.gpuRecentCardFallback, /Confirmed by recent driver utilization; diagnostic driver sample 0%/);
assert.strictEqual(output.gpuObservation, "Diagnostic 0%; activity confirmed by dedicated memory + power; recent peak 86% over 5 samples; 1 transient zero sample");
assert.strictEqual(output.gpuHighObservation, "70%; activity confirmed by driver utilization; recent peak 86% over 5 samples; 1 transient zero sample");
assert.match(output.gpuHighCard, /Current accelerator activity; recent peak 86% over 5 samples/);
assert.match(output.gpuCard, /Confirmed by dedicated memory \+ power; diagnostic driver sample 0%/);
assert.strictEqual(output.cpuText, "LOW");
assert.strictEqual(output.cpuBasis, "GPU worker offload");
assert.strictEqual(output.cpuIdleBasis, "host CPU counter");
assert.strictEqual(output.cpuHighText, "5%; recent host peak 9.2% over 5 samples; 2 transient zero samples");
assert.match(output.cpuHighCard, /Current host activity; source host CPU counter; recent host peak 9\.2% over 5 samples/);
assert.match(output.cpuCard, /Confirmed by GPU worker offload; diagnostic CPU sample 0\.2%/);
assert.match(output.cpuCard, /recent host peak 1\.2% over 5 samples/);
assert.ok(!output.gpuCard.includes('<span class="muted">0%</span>'));
assert.ok(!output.cpuCard.includes('<span class="muted">0%</span>'));
assert.strictEqual(output.idleGpuText, "IDLE");
assert.strictEqual(output.idleGpuObservation, "0%; no independent activity evidence");
assert.match(output.idleGpuCard, /Raw driver sample 0%; no independent activity evidence/);
assert.strictEqual(output.idleCpuText, "IDLE");
assert.match(output.idleCpuCard, /Observed by host CPU counter; raw CPU sample 0%; no active GPU evidence/);
assert.ok(!output.idleGpuCard.includes('<span class="muted">0%</span>'));
assert.ok(!output.idleCpuCard.includes('<span class="muted">0%</span>'));
assert.match(output.unavailableGpuCard, /No verified live reading/);
assert.match(output.unavailableCpuCard, /No verified live reading/);
assert.match(output.unavailableGpuCard, /<div class="resource-value">--<small>%<\/small><\/div>/);
assert.match(output.unavailableCpuCard, /<div class="resource-value">--<small><\/small><\/div>/);
assert.strictEqual(output.numericLabel, '<span class="muted">86%</span>');
assert.match(output.zeroGpuMetric, /GPU ACTIVE \(diagnostic raw sample 0%; activity confirmed by dedicated memory \+ power\)/);
assert.ok(!output.zeroGpuMetric.includes('GPU ACTIVE (raw 0%)'));
assert.match(output.zeroGpuMetricWithHistory, /GPU ACTIVE \(diagnostic raw sample 0%; recent peak 77% over 3 samples; 1 transient zero sample; activity confirmed by dedicated memory \+ power\)/);
assert.match(output.zeroGpuMetricRecentOnly, /GPU ACTIVE \(diagnostic raw sample 0%; recent peak 77% over 3 samples; 1 transient zero sample; activity confirmed by recent driver utilization\)/);
assert.strictEqual(output.queueEvidence, "Result summary");
assert.strictEqual(output.queueEvidenceFallback, "handler completed");
assert.match(output.queueTable, /<th>Updated<\/th>/);
assert.match(output.queueTable, /task-demo/);
assert.match(output.queueTable, /Recorded/);
assert.match(output.reviewPlan, /approval gate · 2 candidates/);
assert.match(output.reviewPlan, /Run a harmless delegated action/);
assert.match(output.reviewPlanSingular, /scope boundary · 1 candidate/);
assert.ok(!output.reviewPlanSingular.includes("1 candidates"));
assert.strictEqual(output.plannedCount, 2);

console.log("dashboard readout fixture: OK");
