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
const auditStart = dashboard.indexOf("function auditCategoryText(categories)");
const auditEnd = dashboard.indexOf("function renderEvidence()", auditStart);

assert.ok(start >= 0, "dashboard helper section must exist");
assert.ok(end > start, "dashboard helper section must have an end marker");
assert.ok(resourceStart > end, "dashboard resource helper section must exist");
assert.ok(resourceEnd > resourceStart, "dashboard resource helper section must have an end marker");
assert.ok(auditStart > resourceEnd, "dashboard audit helper section must exist");
assert.ok(auditEnd > auditStart, "dashboard audit helper section must have an end marker");

// Execute only the dashboard helper section in an isolated context; no DOM or
// browser globals are needed for these pure readout functions.
const executionContext = { fixture, idleFixture, output: null };
vm.createContext(executionContext);
vm.runInContext(
    dashboard.slice(start, end) +
    dashboard.slice(resourceStart, resourceEnd) +
    dashboard.slice(auditStart, auditEnd) +
    "\noutput = {" +
    "gpuText: gpuActivityText(fixture.gpu)," +
    "gpuCard: gpuActivityCard(fixture.gpu)," +
    "cpuText: cpuActivityText(fixture.system, fixture.gpu)," +
    "cpuCard: cpuActivityCard(fixture.system, fixture.gpu)," +
    "idleGpuText: gpuActivityText(idleFixture.gpu)," +
    "idleGpuCard: gpuActivityCard(idleFixture.gpu)," +
    "idleCpuText: cpuActivityText(idleFixture.system, idleFixture.gpu)," +
    "idleCpuCard: cpuActivityCard(idleFixture.system, idleFixture.gpu)," +
    "numericLabel: resourceMeterLabel(\"86\", 86)," +
    "reviewPlan: auditReviewPlan([{category: \"approval_gate\", candidate_count: 2, recommended_test: \"Run a harmless delegated action.\"}])," +
    "reviewPlanSingular: auditReviewPlan([{category: \"scope_boundary\", candidate_count: 1, recommended_test: \"Run a bounded scope test.\"}])," +
    "plannedCount: auditReviewCount([{review_plan: [{category: \"approval_gate\"}, {category: \"scope_boundary\"}]}, {review_plan: []}])" +
    "};",
  executionContext,
  { filename: path.join(root, "dashboard", "index.html") },
);

const output = executionContext.output;
assert.strictEqual(output.gpuText, "ACTIVE");
assert.match(output.gpuCard, /Dedicated memory \+ power confirm work; raw driver sample 0%/);
assert.strictEqual(output.cpuText, "LOW");
assert.match(output.cpuCard, /Raw CPU sample 0\.2%/);
assert.ok(!output.gpuCard.includes('<span class="muted">0%</span>'));
assert.ok(!output.cpuCard.includes('<span class="muted">0%</span>'));
assert.strictEqual(output.idleGpuText, "IDLE");
assert.match(output.idleGpuCard, /Raw driver sample 0%; no independent activity evidence/);
assert.strictEqual(output.idleCpuText, "IDLE");
assert.match(output.idleCpuCard, /Raw CPU sample 0%; no active GPU evidence/);
assert.ok(!output.idleGpuCard.includes('<span class="muted">0%</span>'));
assert.ok(!output.idleCpuCard.includes('<span class="muted">0%</span>'));
assert.strictEqual(output.numericLabel, '<span class="muted">86%</span>');
assert.match(output.reviewPlan, /approval gate · 2 candidates/);
assert.match(output.reviewPlan, /Run a harmless delegated action/);
assert.match(output.reviewPlanSingular, /scope boundary · 1 candidate/);
assert.ok(!output.reviewPlanSingular.includes("1 candidates"));
assert.strictEqual(output.plannedCount, 2);

console.log("dashboard readout fixture: OK");
