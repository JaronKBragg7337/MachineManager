"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const dashboard = fs.readFileSync(path.join(root, "dashboard", "index.html"), "utf8");
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "dashboard_active_zero_sample.json"), "utf8"));
const start = dashboard.indexOf("function escapeHtml(value)");
const end = dashboard.indexOf("function workProgress()", start);
const resourceStart = dashboard.indexOf("function resourceMeterLabel(value, percent)", end);
const resourceEnd = dashboard.indexOf("function renderWorkersTable", resourceStart);

assert.ok(start >= 0, "dashboard helper section must exist");
assert.ok(end > start, "dashboard helper section must have an end marker");
assert.ok(resourceStart > end, "dashboard resource helper section must exist");
assert.ok(resourceEnd > resourceStart, "dashboard resource helper section must have an end marker");

// Execute only the dashboard helper section in an isolated context; no DOM or
// browser globals are needed for these pure readout functions.
const executionContext = { fixture, output: null };
vm.createContext(executionContext);
vm.runInContext(
  dashboard.slice(start, end) +
    dashboard.slice(resourceStart, resourceEnd) +
    "\noutput = {" +
    "gpuText: gpuActivityText(fixture.gpu)," +
    "gpuCard: gpuActivityCard(fixture.gpu)," +
    "cpuText: cpuActivityText(fixture.system, fixture.gpu)," +
    "cpuCard: cpuActivityCard(fixture.system, fixture.gpu)," +
    "numericLabel: resourceMeterLabel(\"86\", 86)" +
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
assert.strictEqual(output.numericLabel, '<span class="muted">86%</span>');

console.log("dashboard readout fixture: OK");
