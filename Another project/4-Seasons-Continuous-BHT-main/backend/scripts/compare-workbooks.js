#!/usr/bin/env node

const fs = require("fs");
const path = require("path");
const XLSX = require("xlsx");

function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 1) {
    const value = argv[index];
    if (!value.startsWith("--")) continue;
    const key = value.slice(2);
    const next = argv[index + 1];
    if (next && !next.startsWith("--")) {
      args[key] = next;
      index += 1;
    } else {
      args[key] = true;
    }
  }
  return args;
}

function stableJson(value) {
  if (value === undefined) return "";
  if (value === null) return "null";
  if (typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(",")}}`;
}

function workbookHiddenSheets(workbook) {
  const sheets = workbook.Workbook?.Sheets || [];
  return Object.fromEntries(sheets.map((sheet, index) => [
    workbook.SheetNames[index] || sheet.name || `sheet_${index + 1}`,
    sheet.Hidden || 0,
  ]));
}

function allCellAddresses(leftSheet = {}, rightSheet = {}) {
  const addresses = new Set();
  for (const key of Object.keys(leftSheet)) if (!key.startsWith("!")) addresses.add(key);
  for (const key of Object.keys(rightSheet)) if (!key.startsWith("!")) addresses.add(key);
  return Array.from(addresses).sort();
}

function comparableCell(cell) {
  if (!cell) return null;
  return {
    t: cell.t || "",
    v: Object.prototype.hasOwnProperty.call(cell, "v") ? cell.v : undefined,
    f: cell.f || "",
    z: cell.z || "",
    s: cell.s || null,
    l: cell.l || null,
  };
}

function pushDiff(diffs, section, detail, expected, actual) {
  diffs.push({
    section,
    detail,
    expected,
    actual,
  });
}

function compareWorkbookObjects(expectedWorkbook, actualWorkbook) {
  const diffs = [];
  const expectedSheets = expectedWorkbook.SheetNames || [];
  const actualSheets = actualWorkbook.SheetNames || [];
  if (stableJson(expectedSheets) !== stableJson(actualSheets)) {
    pushDiff(diffs, "sheetNames", "Workbook sheet order/names differ", expectedSheets, actualSheets);
  }

  const expectedHidden = workbookHiddenSheets(expectedWorkbook);
  const actualHidden = workbookHiddenSheets(actualWorkbook);
  if (stableJson(expectedHidden) !== stableJson(actualHidden)) {
    pushDiff(diffs, "hiddenSheets", "Workbook hidden-sheet state differs", expectedHidden, actualHidden);
  }

  const sheetNames = Array.from(new Set([...expectedSheets, ...actualSheets])).sort();
  for (const sheetName of sheetNames) {
    const expectedSheet = expectedWorkbook.Sheets?.[sheetName];
    const actualSheet = actualWorkbook.Sheets?.[sheetName];
    if (!expectedSheet || !actualSheet) {
      pushDiff(diffs, "sheet", sheetName, Boolean(expectedSheet), Boolean(actualSheet));
      continue;
    }

    for (const prop of ["!ref", "!merges", "!cols", "!rows"]) {
      if (stableJson(expectedSheet[prop]) !== stableJson(actualSheet[prop])) {
        pushDiff(diffs, prop, sheetName, expectedSheet[prop] || null, actualSheet[prop] || null);
      }
    }

    for (const address of allCellAddresses(expectedSheet, actualSheet)) {
      const expectedCell = comparableCell(expectedSheet[address]);
      const actualCell = comparableCell(actualSheet[address]);
      if (stableJson(expectedCell) !== stableJson(actualCell)) {
        pushDiff(diffs, "cell", `${sheetName}!${address}`, expectedCell, actualCell);
      }
    }
  }

  return {
    ok: diffs.length === 0,
    diffCount: diffs.length,
    diffs,
  };
}

function compareWorkbookFiles(expectedPath, actualPath) {
  const readOptions = { cellStyles: true, cellFormula: true, cellNF: true, cellHTML: false };
  const expectedWorkbook = XLSX.readFile(expectedPath, readOptions);
  const actualWorkbook = XLSX.readFile(actualPath, readOptions);
  return compareWorkbookObjects(expectedWorkbook, actualWorkbook);
}

function main() {
  const args = parseArgs(process.argv);
  if (!args.expected || !args.actual) {
    console.error("Usage: node scripts/compare-workbooks.js --expected <file.xlsx> --actual <file.xlsx> [--out report.json]");
    process.exitCode = 2;
    return;
  }
  const report = compareWorkbookFiles(args.expected, args.actual);
  const payload = `${JSON.stringify(report, null, 2)}\n`;
  if (args.out) {
    fs.mkdirSync(path.dirname(args.out), { recursive: true });
    fs.writeFileSync(args.out, payload, "utf8");
  } else {
    process.stdout.write(payload);
  }
  if (!report.ok) process.exitCode = 1;
}

if (require.main === module) main();

module.exports = {
  compareWorkbookObjects,
  compareWorkbookFiles,
};
