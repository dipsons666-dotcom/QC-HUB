const fs = require("fs");
const path = require("path");

function inferCellType(value) {
  if (value === null || value === undefined) return "z";
  if (typeof value === "number") return "n";
  if (typeof value === "boolean") return "b";
  if (value instanceof Date) return "d";
  return "s";
}

function applyCellPatch(sheet, patch) {
  if (!sheet || !patch || !patch.address) return;
  const existing = sheet[patch.address] || {};
  const next = { ...existing };
  if (Object.prototype.hasOwnProperty.call(patch, "formula")) {
    next.f = patch.formula || "";
    if (Object.prototype.hasOwnProperty.call(patch, "value")) {
      next.v = patch.value;
      next.t = inferCellType(patch.value);
    } else if (!Object.prototype.hasOwnProperty.call(next, "v")) {
      next.v = 0;
      next.t = "n";
    }
  } else if (Object.prototype.hasOwnProperty.call(patch, "value")) {
    next.v = patch.value;
    next.t = inferCellType(patch.value);
    delete next.f;
  }
  sheet[patch.address] = next;
}

function applyTemplateCellPatches(workbook, templateCellPatches = {}) {
  for (const [sheetName, patches] of Object.entries(templateCellPatches || {})) {
    if (!Array.isArray(patches) || !patches.length) continue;
    const sheet = workbook.Sheets?.[sheetName];
    if (!sheet) continue;
    for (const patch of patches) applyCellPatch(sheet, patch);
  }
}

function writeTemplatePreservingWorkbook({ workbook, destinationPath, XLSX, templateCellPatches = {} }) {
  if (!workbook) throw new Error("Workbook is required.");
  if (!destinationPath) throw new Error("Destination path is required.");
  if (!XLSX || typeof XLSX.writeFile !== "function") throw new Error("XLSX writer is required.");
  fs.mkdirSync(path.dirname(destinationPath), { recursive: true });
  applyTemplateCellPatches(workbook, templateCellPatches);
  XLSX.writeFile(workbook, destinationPath, {
    bookType: "xlsx",
    cellStyles: true,
    compression: true,
  });
}

module.exports = {
  applyTemplateCellPatches,
  writeTemplatePreservingWorkbook,
};
