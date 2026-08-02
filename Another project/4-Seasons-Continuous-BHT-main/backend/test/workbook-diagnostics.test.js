const assert = require("node:assert/strict");
const test = require("node:test");
const XLSX = require("xlsx");

const { compareWorkbookObjects } = require("../scripts/compare-workbooks");
const { applyTemplateCellPatches } = require("../src/template-workbook-writer");

function makeWorkbook(rows) {
  const workbook = XLSX.utils.book_new();
  const sheet = XLSX.utils.aoa_to_sheet(rows);
  sheet["!cols"] = [{ wch: 12 }, { wch: 18 }];
  XLSX.utils.book_append_sheet(workbook, sheet, "Sheet1");
  return workbook;
}

test("workbook comparer reports identical workbook structures as ok", () => {
  const left = makeWorkbook([["Question", "Base"], ["N_BAU1A", 100]]);
  const right = makeWorkbook([["Question", "Base"], ["N_BAU1A", 100]]);
  const report = compareWorkbookObjects(left, right);
  assert.equal(report.ok, true);
  assert.equal(report.diffCount, 0);
});

test("workbook comparer catches value and dimension differences", () => {
  const left = makeWorkbook([["Question", "Base"], ["N_BAU1A", 100]]);
  const right = makeWorkbook([["Question", "Base"], ["N_BAU1A", 101]]);
  right.Sheets.Sheet1["!cols"] = [{ wch: 8 }, { wch: 18 }];
  const report = compareWorkbookObjects(left, right);
  assert.equal(report.ok, false);
  assert.ok(report.diffs.some((diff) => diff.section === "cell" && diff.detail === "Sheet1!B2"));
  assert.ok(report.diffs.some((diff) => diff.section === "!cols"));
});

test("template workbook patcher preserves styles while updating values and formulas", () => {
  const workbook = makeWorkbook([["Question", "Base"], ["N_BAU1A", 100]]);
  workbook.Sheets.Sheet1.B2.s = { font: { bold: true } };
  applyTemplateCellPatches(workbook, {
    Sheet1: [
      { address: "B2", value: 120 },
      { address: "C2", formula: "B2*2" },
    ],
  });
  assert.equal(workbook.Sheets.Sheet1.B2.v, 120);
  assert.deepEqual(workbook.Sheets.Sheet1.B2.s, { font: { bold: true } });
  assert.equal(workbook.Sheets.Sheet1.C2.f, "B2*2");
});
