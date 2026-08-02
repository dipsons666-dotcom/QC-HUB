const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const registry = require("../src/metadata-registry");

function writeJson(dir, name, payload) {
  const filePath = path.join(dir, name);
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  return filePath;
}

test("buildRegistrySeed extracts stable brands and questions from current metadata sources", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "metadata-registry-"));
  const exportSpecsPath = writeJson(dir, "export_table_specs.json", {
    categories: {
      noodles: {
        label: "Noodles",
        blocks: [
          {
            question: "N_BAU1A",
            title: "When you think of Instant Noodles, what brand comes to your mind first?",
            type: "single_response",
            answers: ["Indomie", "Chikki", "Other"],
          },
        ],
      },
    },
  });
  const spssRulesPath = writeJson(dir, "spss_export_rules.json", {
    variableLabels: {
      N_BAU4_1: "(Indomie) Where did you see the advert?",
    },
  });
  const xlsformMetadataPath = writeJson(dir, "xlsform_metadata.json", {
    questions: {
      N_BAU1A: { label: "Top of mind noodles", type: "select_one brands", list_name: "brands" },
    },
    lists: {
      brands: {
        "1": "Indomie",
        "2": "Chikki",
      },
    },
  });

  const seed = registry.buildRegistrySeed({ exportSpecsPath, spssRulesPath, xlsformMetadataPath });
  assert.equal(seed.questions.length, 1);
  assert.equal(seed.questions[0].variable, "N_BAU1A");
  assert.deepEqual(seed.questions[0].response_options.map((option) => option.option_label), ["Indomie", "Chikki"]);
  assert.ok(seed.brands.some((brand) => brand.label === "Indomie"));
  assert.ok(seed.brands.every((brand) => brand.label !== "Other"));
});

test("normalizeKey collapses brand aliases into comparable keys", () => {
  assert.equal(registry.normalizeKey("HYPO bleach"), "hypo bleach");
  assert.equal(registry.normalizeKey("Hypo-Bleach"), "hypo bleach");
  assert.equal(registry.normalizeKey("Hypo   Bleach"), "hypo bleach");
});

test("normalizeResponseOptions removes reserved and duplicate choices while preserving order", () => {
  const options = registry.normalizeResponseOptions({
    "1": "Indomie",
    "2": "Other",
    "3": "Chikki",
    "4": "INDOMIE",
  });
  assert.deepEqual(options.map((option) => option.option_label), ["Indomie", "Chikki"]);
  assert.deepEqual(options.map((option) => option.option_code), ["1", "3"]);
});
