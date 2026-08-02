const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

const SCHEMA_VERSION = "2026-06-25-metadata-registry-v1";
const BRAND_STATUSES = new Set(["detected", "pending_review", "approved", "active", "inactive", "merged", "rejected"]);
const QUESTION_STATUSES = new Set(["detected", "pending_review", "approved", "active", "inactive", "replaced", "rejected"]);
const CHANGE_STATUSES = new Set(["pending_review", "approved", "rejected"]);
const RESERVED_BRAND_KEYS = new Set([
  "",
  "other",
  "others",
  "other specify",
  "specify",
  "dont know",
  "don't know",
  "dk",
  "none",
  "no response",
  "not applicable",
  "na",
  "n/a",
  "null",
  "undefined",
  "nan",
  "yes",
  "no",
]);

function nowIso() {
  return new Date().toISOString();
}

function normalizeText(value) {
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeKey(value) {
  return normalizeText(value)
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function stableId(prefix, parts) {
  const hash = crypto.createHash("sha1").update(parts.map((value) => normalizeKey(value)).join("|")).digest("hex").slice(0, 16);
  return `${prefix}_${hash}`;
}

function quoteSql(value) {
  if (value === null || value === undefined) return "NULL";
  return `'${String(value).replace(/'/g, "''")}'`;
}

function jsonSql(value) {
  return quoteSql(JSON.stringify(value ?? null));
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (_err) {
    return fallback;
  }
}

async function exec(dbApi, sql) {
  await dbApi.run(sql);
}

async function all(dbApi, sql) {
  return dbApi.all(sql);
}

async function tableExists(dbApi, tableName) {
  const rows = await all(dbApi, `
    SELECT table_name
    FROM information_schema.tables
    WHERE lower(table_name)=lower(${quoteSql(tableName)})
      AND table_schema='main'
    LIMIT 1
  `);
  return rows.length > 0;
}

async function ensureMetadataRegistrySchema(dbApi) {
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS registry_schema_migrations (
      id VARCHAR PRIMARY KEY,
      applied_at VARCHAR NOT NULL
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS brand_registry (
      id VARCHAR,
      code VARCHAR,
      label VARCHAR,
      category VARCHAR,
      display_order INTEGER,
      status VARCHAR,
      effective_from VARCHAR,
      effective_to VARCHAR,
      source VARCHAR,
      detected_at VARCHAR,
      approved_at VARCHAR,
      approved_by VARCHAR,
      replaces_brand_id VARCHAR,
      notes VARCHAR,
      record_json VARCHAR,
      created_at VARCHAR,
      updated_at VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS brand_aliases (
      brand_id VARCHAR,
      alias VARCHAR,
      alias_key VARCHAR,
      source VARCHAR,
      status VARCHAR,
      effective_from VARCHAR,
      effective_to VARCHAR,
      approved_at VARCHAR,
      approved_by VARCHAR,
      created_at VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS question_registry (
      id VARCHAR,
      variable VARCHAR,
      label VARCHAR,
      category VARCHAR,
      question_type VARCHAR,
      table_type VARCHAR,
      response_list_id VARCHAR,
      display_order INTEGER,
      status VARCHAR,
      effective_from VARCHAR,
      effective_to VARCHAR,
      filter_expression VARCHAR,
      base_rule VARCHAR,
      statistics_json VARCHAR,
      breaks_json VARCHAR,
      include_empty_categories BOOLEAN,
      source VARCHAR,
      detected_at VARCHAR,
      approved_at VARCHAR,
      approved_by VARCHAR,
      replaces_question_id VARCHAR,
      notes VARCHAR,
      record_json VARCHAR,
      created_at VARCHAR,
      updated_at VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS question_response_options (
      question_id VARCHAR,
      option_code VARCHAR,
      option_label VARCHAR,
      display_order INTEGER,
      status VARCHAR,
      effective_from VARCHAR,
      effective_to VARCHAR,
      source VARCHAR,
      record_json VARCHAR,
      created_at VARCHAR,
      updated_at VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS metadata_change_queue (
      id VARCHAR,
      entity_type VARCHAR,
      change_type VARCHAR,
      category VARCHAR,
      status VARCHAR,
      current_definition_json VARCHAR,
      detected_definition_json VARCHAR,
      first_observed_period VARCHAR,
      affected_tables_json VARCHAR,
      affected_export_types_json VARCHAR,
      potential_matches_json VARCHAR,
      confidence DOUBLE,
      recommendation VARCHAR,
      warnings_json VARCHAR,
      source VARCHAR,
      detected_at VARCHAR,
      decided_at VARCHAR,
      decided_by VARCHAR,
      decision_note VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS registry_audit_log (
      id VARCHAR,
      entity_type VARCHAR,
      entity_id VARCHAR,
      action VARCHAR,
      previous_value_json VARCHAR,
      new_value_json VARCHAR,
      effective_period VARCHAR,
      approved_by VARCHAR,
      approved_at VARCHAR,
      note VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS registry_snapshots (
      id VARCHAR,
      snapshot_type VARCHAR,
      category VARCHAR,
      period VARCHAR,
      snapshot_json VARCHAR,
      created_at VARCHAR,
      created_by VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS spss_autorecode_mapping_versions (
      id VARCHAR,
      source_variable VARCHAR,
      target_variable VARCHAR,
      effective_from VARCHAR,
      effective_to VARCHAR,
      status VARCHAR,
      source VARCHAR,
      created_at VARCHAR,
      created_by VARCHAR
    )
  `);
  await exec(dbApi, `
    CREATE TABLE IF NOT EXISTS spss_autorecode_mappings (
      version_id VARCHAR,
      source_variable VARCHAR,
      target_variable VARCHAR,
      source_value VARCHAR,
      source_label VARCHAR,
      target_code INTEGER,
      effective_from VARCHAR,
      effective_to VARCHAR,
      created_at VARCHAR
    )
  `);
  const rows = await all(dbApi, `SELECT id FROM registry_schema_migrations WHERE id=${quoteSql(SCHEMA_VERSION)} LIMIT 1`);
  if (!rows.length) {
    await exec(dbApi, `INSERT INTO registry_schema_migrations VALUES (${quoteSql(SCHEMA_VERSION)}, ${quoteSql(nowIso())})`);
  }
}

function categoryEntries(exportSpecs) {
  const categories = exportSpecs?.categories || {};
  return Object.entries(categories).map(([slug, spec]) => ({
    slug,
    category: normalizeText(spec?.label || slug.replace(/-/g, " ")),
    spec,
  }));
}

function questionTypeFromSpec(block) {
  const tableType = normalizeKey(block?.type || block?.tableType || "");
  if (tableType.includes("multi")) return "multiple_response";
  if (tableType.includes("mean") || tableType.includes("numeric")) return "numeric";
  if (tableType.includes("matrix") || tableType.includes("grid")) return "matrix";
  if (tableType.includes("rank")) return "ranking";
  if (tableType.includes("text") || tableType.includes("verbatim")) return "open_text";
  return "single_response";
}

function extractQuestions(exportSpecs, xlsformMetadata) {
  const rows = [];
  const seen = new Set();
  for (const { slug, category, spec } of categoryEntries(exportSpecs)) {
    const blocks = Array.isArray(spec?.blocks) ? spec.blocks : Array.isArray(spec?.tables) ? spec.tables : [];
    blocks.forEach((block, index) => {
      const variable = normalizeText(block.question || block.questionCode || block.variable || block.id || "");
      if (!variable) return;
      const key = `${slug}::${normalizeKey(variable)}`;
      if (seen.has(key)) return;
      seen.add(key);
      const metadata = xlsformMetadata?.questions?.[variable] || xlsformMetadata?.questions?.[variable.toLowerCase()] || {};
      const label = normalizeText(block.title || block.questionLabel || metadata.label || variable);
      const answers = Array.isArray(block.answers) ? block.answers : Array.isArray(block.rows) ? block.rows : [];
      rows.push({
        id: stableId("question", [slug, variable]),
        variable,
        label,
        category,
        question_type: questionTypeFromSpec(block),
        table_type: normalizeText(block.tableType || block.type || "column_percentage"),
        response_list_id: normalizeText(metadata.list_name || block.responseListId || ""),
        display_order: index + 1,
        status: "active",
        effective_from: "1900-01",
        effective_to: null,
        filter_expression: normalizeText(block.filter || block.filterExpression || ""),
        base_rule: normalizeText(block.baseRule || "valid_responses"),
        statistics: Array.isArray(block.statistics) ? block.statistics : ["count", "column_percentage"],
        breaks: Array.isArray(block.breaks) ? block.breaks : ["Total", "Region", "REGION2", "Gender", "Age", "SEC", "Week"],
        include_empty_categories: true,
        source: "export_specs",
        response_options: normalizeResponseOptions(answers.map((answer, answerIndex) => ({
          option_code: String(answerIndex + 1),
          option_label: normalizeText(typeof answer === "string" ? answer : answer?.label || answer?.answer || answer?.value),
          display_order: answerIndex + 1,
        })).filter((answer) => answer.option_label)),
      });
    });
  }
  return rows;
}

function inferBrandFromVariable(variable, label) {
  const source = normalizeText(label) || normalizeText(variable);
  const paren = source.match(/^\(([^)]+)\)/);
  if (paren) return normalizeText(paren[1]);
  const cleaned = source
    .replace(/\b(brand|advertising|awareness|media|source|usage|used|ever|prompted|spontaneous)\b/gi, " ")
    .replace(/\s+/g, " ")
    .trim();
  return cleaned && cleaned.length <= 60 ? cleaned : "";
}

function extractBrands(exportSpecs, spssRules, xlsformMetadata) {
  const brands = new Map();
  const addBrand = ({ category, label, code, source, order }) => {
    const cleanLabel = normalizeText(label);
    const key = normalizeKey(cleanLabel);
    if (!cleanLabel || RESERVED_BRAND_KEYS.has(key)) return;
    const cleanCategory = normalizeText(category || "Unassigned");
    const scoped = `${normalizeKey(cleanCategory)}::${key}`;
    if (!brands.has(scoped)) {
      brands.set(scoped, {
        id: stableId("brand", [cleanCategory, key]),
        code: normalizeText(code || key.replace(/\s+/g, "_")),
        label: cleanLabel,
        category: cleanCategory,
        aliases: Array.from(new Set([cleanLabel, code].map(normalizeText).filter(Boolean))),
        display_order: order || brands.size + 1,
        status: "active",
        effective_from: "1900-01",
        effective_to: null,
        source,
      });
    }
  };

  for (const { category, spec } of categoryEntries(exportSpecs)) {
    const blocks = Array.isArray(spec?.blocks) ? spec.blocks : Array.isArray(spec?.tables) ? spec.tables : [];
    blocks.forEach((block) => {
      const answers = Array.isArray(block.answers) ? block.answers : Array.isArray(block.rows) ? block.rows : [];
      answers.forEach((answer, index) => {
        const label = normalizeText(typeof answer === "string" ? answer : answer?.label || answer?.answer || answer?.value);
        if (/brand|bau|awareness|advert/i.test(`${block.question || ""} ${block.title || ""}`)) {
          addBrand({ category, label, source: "export_specs", order: index + 1 });
        }
      });
    });
  }

  Object.entries(spssRules?.variableLabels || {}).forEach(([variable, label], index) => {
    const brand = inferBrandFromVariable(variable, label);
    if (brand && /bau|brand|ad|advert|source/i.test(variable)) {
      const prefix = String(variable).split("_")[0] || "";
      const category = categoryEntries(exportSpecs).find((entry) => normalizeKey(entry.slug).startsWith(normalizeKey(prefix)))?.category || "Unassigned";
      addBrand({ category, label: brand, code: variable, source: "spss_rules", order: index + 1 });
    }
  });

  Object.values(xlsformMetadata?.lists || {}).forEach((choices) => {
    Object.entries(choices || {}).forEach(([code, label]) => {
      const cleanLabel = normalizeText(label);
      if (/\b(brand|bleach|noodle|toothpaste|oil|snack|malt)\b/i.test(cleanLabel)) {
        addBrand({ category: "Unassigned", label: cleanLabel, code, source: "xlsform" });
      }
    });
  });

  return Array.from(brands.values()).sort((a, b) => a.category.localeCompare(b.category) || a.display_order - b.display_order);
}

function buildRegistrySeed({ exportSpecsPath, spssRulesPath, xlsformMetadataPath }) {
  const exportSpecs = readJson(exportSpecsPath, { categories: {} });
  const spssRules = readJson(spssRulesPath, {});
  const xlsformMetadata = readJson(xlsformMetadataPath, { questions: {}, lists: {} });
  return {
    generatedAt: nowIso(),
    brands: extractBrands(exportSpecs, spssRules, xlsformMetadata),
    questions: extractQuestions(exportSpecs, xlsformMetadata),
  };
}

async function countRows(dbApi, table) {
  if (!(await tableExists(dbApi, table))) return 0;
  const rows = await all(dbApi, `SELECT COUNT(*) AS count FROM ${table}`);
  return Number(rows[0]?.count || 0);
}

async function seedInitialRegistry(dbApi, paths, options = {}) {
  await ensureMetadataRegistrySchema(dbApi);
  const seed = buildRegistrySeed(paths);
  const report = {
    dryRun: Boolean(options.dryRun),
    generatedAt: seed.generatedAt,
    brandsDetected: seed.brands.length,
    questionsDetected: seed.questions.length,
    inserted: { brands: 0, aliases: 0, questions: 0, responseOptions: 0, changes: 0 },
    warnings: [],
  };
  const existingBrands = await countRows(dbApi, "brand_registry");
  const existingQuestions = await countRows(dbApi, "question_registry");
  if (existingBrands || existingQuestions) {
    report.warnings.push("Registry already contains records; seed will only report counts unless forced manually.");
    return report;
  }
  if (options.dryRun) return report;

  const stamp = nowIso();
  for (const brand of seed.brands) {
    const record = { ...brand, detected_at: stamp, approved_at: stamp, approved_by: "migration" };
    await exec(dbApi, `
      INSERT INTO brand_registry VALUES (
        ${quoteSql(record.id)}, ${quoteSql(record.code)}, ${quoteSql(record.label)}, ${quoteSql(record.category)},
        ${Number(record.display_order) || 0}, ${quoteSql(record.status)}, ${quoteSql(record.effective_from)}, ${quoteSql(record.effective_to)},
        ${quoteSql(record.source)}, ${quoteSql(record.detected_at)}, ${quoteSql(record.approved_at)}, ${quoteSql(record.approved_by)},
        NULL, NULL, ${jsonSql(record)}, ${quoteSql(stamp)}, ${quoteSql(stamp)}
      )
    `);
    report.inserted.brands += 1;
    for (const alias of brand.aliases || []) {
      await exec(dbApi, `
        INSERT INTO brand_aliases VALUES (
          ${quoteSql(record.id)}, ${quoteSql(alias)}, ${quoteSql(normalizeKey(alias))}, ${quoteSql(record.source)},
          'active', ${quoteSql(record.effective_from)}, NULL, ${quoteSql(stamp)}, 'migration', ${quoteSql(stamp)}
        )
      `);
      report.inserted.aliases += 1;
    }
  }
  for (const question of seed.questions) {
    const record = { ...question, detected_at: stamp, approved_at: stamp, approved_by: "migration" };
    await exec(dbApi, `
      INSERT INTO question_registry VALUES (
        ${quoteSql(record.id)}, ${quoteSql(record.variable)}, ${quoteSql(record.label)}, ${quoteSql(record.category)},
        ${quoteSql(record.question_type)}, ${quoteSql(record.table_type)}, ${quoteSql(record.response_list_id)},
        ${Number(record.display_order) || 0}, ${quoteSql(record.status)}, ${quoteSql(record.effective_from)}, ${quoteSql(record.effective_to)},
        ${quoteSql(record.filter_expression)}, ${quoteSql(record.base_rule)}, ${jsonSql(record.statistics)}, ${jsonSql(record.breaks)},
        ${record.include_empty_categories ? "TRUE" : "FALSE"}, ${quoteSql(record.source)}, ${quoteSql(record.detected_at)},
        ${quoteSql(record.approved_at)}, ${quoteSql(record.approved_by)}, NULL, NULL, ${jsonSql(record)}, ${quoteSql(stamp)}, ${quoteSql(stamp)}
      )
    `);
    report.inserted.questions += 1;
    for (const option of record.response_options || []) {
      await exec(dbApi, `
        INSERT INTO question_response_options VALUES (
          ${quoteSql(record.id)}, ${quoteSql(option.option_code)}, ${quoteSql(option.option_label)}, ${Number(option.display_order) || 0},
          'active', ${quoteSql(record.effective_from)}, NULL, ${quoteSql(record.source)}, ${jsonSql(option)}, ${quoteSql(stamp)}, ${quoteSql(stamp)}
        )
      `);
      report.inserted.responseOptions += 1;
    }
  }
  await createRegistrySnapshot(dbApi, "migration", "all", "1900-01", "migration");
  return report;
}

function isPotentialBrandQuestion(variable, label) {
  return /(^|_)BAU|brand|advert|awareness|usage|media/i.test(`${variable} ${label}`);
}

function normalizeResponseOptions(options) {
  const rows = [];
  const seen = new Set();
  if (Array.isArray(options)) {
    options.forEach((option, index) => {
      const label = normalizeText(typeof option === "string" ? option : option?.option_label || option?.label || option?.name || option?.value);
      const code = normalizeText(typeof option === "string" ? String(index + 1) : option?.option_code || option?.code || option?.name || option?.value || String(index + 1));
      const key = normalizeKey(label || code);
      if (!label || RESERVED_BRAND_KEYS.has(key) || seen.has(key)) return;
      seen.add(key);
      rows.push({ option_code: code, option_label: label, display_order: Number(option?.display_order || option?.order || index + 1) || index + 1 });
    });
    return rows;
  }
  if (options && typeof options === "object") {
    Object.entries(options).forEach(([code, label], index) => {
      const cleanLabel = normalizeText(typeof label === "string" ? label : label?.label || label?.name || label?.value);
      const key = normalizeKey(cleanLabel || code);
      if (!cleanLabel || RESERVED_BRAND_KEYS.has(key) || seen.has(key)) return;
      seen.add(key);
      rows.push({ option_code: normalizeText(code), option_label: cleanLabel, display_order: index + 1 });
    });
  }
  return rows;
}

function responseOptionsForQuestion(metadata, entry) {
  const listName = normalizeText(entry?.list_name || "");
  if (!listName) return [];
  return normalizeResponseOptions(metadata?.lists?.[listName] || metadata?.choices?.[listName] || []);
}

function xlsformQuestionRows(metadata) {
  return Object.entries(metadata?.questions || {}).map(([variable, entry]) => ({
    variable,
    label: normalizeText(entry?.label || variable),
    type: normalizeText(entry?.type || entry?.kind || ""),
    list_name: normalizeText(entry?.list_name || ""),
    relevance: normalizeText(entry?.relevance || ""),
    required: normalizeText(entry?.required || ""),
    group: normalizeText(entry?.group || entry?.path || ""),
    response_options: responseOptionsForQuestion(metadata, entry),
    raw: entry || {},
  }));
}

async function enqueueChange(dbApi, change) {
  const id = stableId("change", [
    change.entity_type,
    change.change_type,
    change.category,
    JSON.stringify(change.detected_definition || {}),
    change.first_observed_period || "",
  ]);
  const existing = await all(dbApi, `SELECT id FROM metadata_change_queue WHERE id=${quoteSql(id)} LIMIT 1`);
  if (existing.length) return false;
  await exec(dbApi, `
    INSERT INTO metadata_change_queue VALUES (
      ${quoteSql(id)}, ${quoteSql(change.entity_type)}, ${quoteSql(change.change_type)}, ${quoteSql(change.category || "")},
      'pending_review', ${jsonSql(change.current_definition || null)}, ${jsonSql(change.detected_definition || null)},
      ${quoteSql(change.first_observed_period || "")}, ${jsonSql(change.affected_tables || [])}, ${jsonSql(change.affected_export_types || ["interim", "full", "rolling", "quarter"])},
      ${jsonSql(change.potential_matches || [])}, ${Number(change.confidence || 0)}, ${quoteSql(change.recommendation || "")},
      ${jsonSql(change.warnings || [])}, ${quoteSql(change.source || "xlsform")}, ${quoteSql(nowIso())}, NULL, NULL, NULL
    )
  `);
  return true;
}

async function detectMetadataChanges(dbApi, paths, options = {}) {
  await ensureMetadataRegistrySchema(dbApi);
  const metadata = readJson(paths.xlsformMetadataPath, { questions: {}, lists: {} });
  const period = options.period || "";
  const existingQuestions = new Map((await all(dbApi, `
    SELECT variable, label, question_type, response_list_id, category, record_json
    FROM question_registry
    WHERE status IN ('active', 'approved')
  `)).map((row) => [normalizeKey(row.variable), row]));
  const currentOptions = await all(dbApi, `
    SELECT q.variable, o.option_code, o.option_label, o.display_order
    FROM question_response_options o
    JOIN question_registry q ON q.id=o.question_id
    WHERE q.status IN ('active', 'approved') AND o.status IN ('active', 'approved')
    ORDER BY q.variable, o.display_order, o.option_label
  `);
  const optionsByVariable = new Map();
  for (const option of currentOptions) {
    const key = normalizeKey(option.variable);
    if (!optionsByVariable.has(key)) optionsByVariable.set(key, []);
    optionsByVariable.get(key).push(option);
  }
  const aliases = new Set((await all(dbApi, `
    SELECT alias_key FROM brand_aliases WHERE status IN ('active', 'approved')
  `)).map((row) => normalizeText(row.alias_key)));
  const brands = await all(dbApi, `
    SELECT id, code, label, category, record_json
    FROM brand_registry
    WHERE status IN ('active', 'approved')
  `);
  const brandByKey = new Map();
  brands.forEach((row) => {
    brandByKey.set(normalizeKey(row.label), row);
    brandByKey.set(normalizeKey(row.code), row);
  });

  let inserted = 0;
  for (const row of xlsformQuestionRows(metadata)) {
    const key = normalizeKey(row.variable);
    const current = existingQuestions.get(key);
    if (!current) {
      const change = {
        entity_type: "question",
        change_type: "new_field",
        category: "",
        current_definition: null,
        detected_definition: row,
        first_observed_period: period,
        recommendation: isPotentialBrandQuestion(row.variable, row.label) ? "Review as reportable question or brand driver." : "Review before including in data tables.",
        confidence: 0.7,
        warnings: [],
      };
      if (await enqueueChange(dbApi, change)) inserted += 1;
    } else {
      const changed = [];
      if (normalizeKey(current.label) !== normalizeKey(row.label)) changed.push("label");
      if (normalizeKey(current.response_list_id) !== normalizeKey(row.list_name)) changed.push("choice_list");
      const priorOptions = optionsByVariable.get(key) || [];
      const priorOptionKeys = priorOptions.map((option) => normalizeKey(option.option_label)).filter(Boolean).join("|");
      const detectedOptionKeys = (row.response_options || []).map((option) => normalizeKey(option.option_label)).filter(Boolean).join("|");
      if (priorOptionKeys !== detectedOptionKeys) changed.push("response_options");
      if (changed.length) {
        const change = {
          entity_type: "question",
          change_type: `changed_${changed.join("_")}`,
          category: current.category,
          current_definition: current,
          detected_definition: row,
          first_observed_period: period,
          recommendation: "Review as label/list update; do not create a replacement unless concept changed.",
          confidence: 0.8,
          warnings: changed,
        };
        if (await enqueueChange(dbApi, change)) inserted += 1;
      }
    }

    if (isPotentialBrandQuestion(row.variable, row.label)) {
      const brand = inferBrandFromVariable(row.variable, row.label);
      const brandKey = normalizeKey(brand);
      if (brand && !RESERVED_BRAND_KEYS.has(brandKey) && !brandByKey.has(brandKey) && !aliases.has(brandKey)) {
        const potential = brands
          .filter((candidate) => normalizeKey(candidate.label).split(" ").some((token) => token && brandKey.includes(token)))
          .slice(0, 5);
        const change = {
          entity_type: "brand",
          change_type: "unknown_brand_label",
          category: "",
          current_definition: null,
          detected_definition: { code: row.variable, label: brand, sourceQuestion: row.variable, sourceLabel: row.label },
          first_observed_period: period,
          recommendation: potential.length ? "Review as possible alias or renamed brand." : "Review as possible new brand.",
          potential_matches: potential,
          confidence: potential.length ? 0.55 : 0.7,
          warnings: potential.length ? ["possible_alias"] : [],
        };
        if (await enqueueChange(dbApi, change)) inserted += 1;
      }
    }
  }
  return { ok: true, scannedQuestions: Object.keys(metadata?.questions || {}).length, queuedChanges: inserted, period };
}

async function listReviewQueue(dbApi, status = "pending_review") {
  await ensureMetadataRegistrySchema(dbApi);
  const where = status === "all" ? "" : `WHERE status=${quoteSql(status)}`;
  const rows = await all(dbApi, `
    SELECT *
    FROM metadata_change_queue
    ${where}
    ORDER BY detected_at DESC
    LIMIT 500
  `);
  return rows.map(decodeChangeRow);
}

function parseJson(value, fallback) {
  try {
    return JSON.parse(value);
  } catch (_err) {
    return fallback;
  }
}

function decodeChangeRow(row) {
  return {
    ...row,
    current_definition: parseJson(row.current_definition_json, null),
    detected_definition: parseJson(row.detected_definition_json, null),
    affected_tables: parseJson(row.affected_tables_json, []),
    affected_export_types: parseJson(row.affected_export_types_json, []),
    potential_matches: parseJson(row.potential_matches_json, []),
    warnings: parseJson(row.warnings_json, []),
  };
}

function requireAdminToken(req) {
  const expected = process.env.ADMIN_REVIEW_TOKEN || "";
  if (!expected) return true;
  const actual = req.get("x-admin-review-token") || req.get("authorization")?.replace(/^Bearer\s+/i, "") || "";
  return actual === expected;
}

function validateBrandRecord(record) {
  const errors = [];
  if (!normalizeText(record.id)) errors.push("Brand id is required.");
  if (!normalizeText(record.code)) errors.push("Brand code is required.");
  if (!normalizeText(record.label)) errors.push("Brand label is required.");
  if (!normalizeText(record.category)) errors.push("Brand category is required.");
  if (!BRAND_STATUSES.has(record.status)) errors.push(`Invalid brand status: ${record.status}`);
  if (!/^\d{4}-\d{2}$/.test(String(record.effective_from || ""))) errors.push("Brand effective_from must be YYYY-MM.");
  return errors;
}

function validateQuestionRecord(record) {
  const errors = [];
  if (!normalizeText(record.id)) errors.push("Question id is required.");
  if (!normalizeText(record.variable)) errors.push("Question variable is required.");
  if (!normalizeText(record.label)) errors.push("Question label is required.");
  if (!normalizeText(record.category)) errors.push("Question category is required.");
  if (!QUESTION_STATUSES.has(record.status)) errors.push(`Invalid question status: ${record.status}`);
  if (!/^\d{4}-\d{2}$/.test(String(record.effective_from || ""))) errors.push("Question effective_from must be YYYY-MM.");
  const type = normalizeKey(record.question_type);
  const tableType = normalizeKey(record.table_type);
  if (!type) errors.push("Question type is required.");
  if (!tableType) errors.push("Question table type is required.");
  if (!Array.isArray(record.statistics) || !record.statistics.length) errors.push("At least one statistic is required.");
  if (!Array.isArray(record.breaks) || !record.breaks.length) errors.push("At least one break is required.");
  const options = normalizeResponseOptions(record.response_options || record.responseOptions || []);
  if (["single_response", "multiple_response", "matrix", "ranking"].includes(record.question_type) && !options.length) {
    errors.push(`${record.question_type} questions require approved response options.`);
  }
  return errors;
}

async function insertQuestionResponseOptions(dbApi, questionId, options, record, stamp) {
  const responseOptions = normalizeResponseOptions(options);
  for (const option of responseOptions) {
    await exec(dbApi, `
      INSERT INTO question_response_options VALUES (
        ${quoteSql(questionId)}, ${quoteSql(option.option_code)}, ${quoteSql(option.option_label)}, ${Number(option.display_order) || 0},
        'active', ${quoteSql(record.effective_from)}, NULL, ${quoteSql(record.source || "review")},
        ${jsonSql({ ...option, question_id: questionId })}, ${quoteSql(stamp)}, ${quoteSql(stamp)}
      )
    `);
  }
  return responseOptions.length;
}

async function createRegistrySnapshot(dbApi, snapshotType, category, period, createdBy) {
  const [brands, questions] = await Promise.all([
    all(dbApi, `SELECT * FROM brand_registry WHERE status IN ('active', 'approved') ORDER BY category, display_order, label`),
    all(dbApi, `SELECT * FROM question_registry WHERE status IN ('active', 'approved') ORDER BY category, display_order, variable`),
  ]);
  const snapshot = { brands, questions };
  const id = stableId("snapshot", [snapshotType, category, period, nowIso()]);
  await exec(dbApi, `
    INSERT INTO registry_snapshots VALUES (
      ${quoteSql(id)}, ${quoteSql(snapshotType)}, ${quoteSql(category)}, ${quoteSql(period)},
      ${jsonSql(snapshot)}, ${quoteSql(nowIso())}, ${quoteSql(createdBy || "system")}
    )
  `);
  return id;
}

async function updateQueueDecision(dbApi, id, status, decidedBy, note) {
  if (!CHANGE_STATUSES.has(status)) throw new Error(`Invalid queue status: ${status}`);
  await exec(dbApi, `
    UPDATE metadata_change_queue
    SET status=${quoteSql(status)}, decided_at=${quoteSql(nowIso())}, decided_by=${quoteSql(decidedBy)}, decision_note=${quoteSql(note || "")}
    WHERE id=${quoteSql(id)}
  `);
}

async function approveReviewChange(dbApi, id, payload) {
  await ensureMetadataRegistrySchema(dbApi);
  const rows = await all(dbApi, `SELECT * FROM metadata_change_queue WHERE id=${quoteSql(id)} LIMIT 1`);
  if (!rows.length) throw new Error("Review item not found.");
  const item = decodeChangeRow(rows[0]);
  const approvedBy = normalizeText(payload.approvedBy || payload.approved_by || "admin");
  const note = normalizeText(payload.note || "");
  const effectiveFrom = normalizeText(payload.effective_from || payload.effectiveFrom || item.first_observed_period || "1900-01");
  const stamp = nowIso();
  await exec(dbApi, "BEGIN TRANSACTION");
  try {
    let entityId = "";
    if (item.entity_type === "brand") {
      const detected = item.detected_definition || {};
      const record = {
        id: normalizeText(payload.id || detected.id || stableId("brand", [payload.category || detected.category || "Unassigned", payload.label || detected.label || detected.code])),
        code: normalizeText(payload.code || detected.code || detected.label),
        label: normalizeText(payload.label || detected.label || detected.code),
        category: normalizeText(payload.category || detected.category || item.category || "Unassigned"),
        display_order: Number(payload.display_order || payload.displayOrder || 999),
        status: "active",
        effective_from: effectiveFrom,
        effective_to: null,
        source: normalizeText(detected.source || item.source || "review"),
        detected_at: item.detected_at,
        approved_at: stamp,
        approved_by: approvedBy,
        replaces_brand_id: payload.replaces_brand_id || null,
        notes: note || null,
      };
      const errors = validateBrandRecord(record);
      if (errors.length) throw new Error(errors.join(" "));
      await exec(dbApi, `
        INSERT INTO brand_registry VALUES (
          ${quoteSql(record.id)}, ${quoteSql(record.code)}, ${quoteSql(record.label)}, ${quoteSql(record.category)},
          ${Number(record.display_order) || 0}, ${quoteSql(record.status)}, ${quoteSql(record.effective_from)}, NULL,
          ${quoteSql(record.source)}, ${quoteSql(record.detected_at)}, ${quoteSql(record.approved_at)}, ${quoteSql(record.approved_by)},
          ${quoteSql(record.replaces_brand_id)}, ${quoteSql(record.notes)}, ${jsonSql(record)}, ${quoteSql(stamp)}, ${quoteSql(stamp)}
        )
      `);
      const aliases = Array.from(new Set([record.label, record.code, ...(payload.aliases || [])].map(normalizeText).filter(Boolean)));
      for (const alias of aliases) {
        await exec(dbApi, `
          INSERT INTO brand_aliases VALUES (
            ${quoteSql(record.id)}, ${quoteSql(alias)}, ${quoteSql(normalizeKey(alias))}, 'review', 'active',
            ${quoteSql(record.effective_from)}, NULL, ${quoteSql(stamp)}, ${quoteSql(approvedBy)}, ${quoteSql(stamp)}
          )
        `);
      }
      entityId = record.id;
    } else if (item.entity_type === "question") {
      const detected = item.detected_definition || {};
      const record = {
        id: normalizeText(payload.id || detected.id || stableId("question", [payload.category || detected.category || "Unassigned", payload.variable || detected.variable])),
        variable: normalizeText(payload.variable || detected.variable),
        label: normalizeText(payload.label || detected.label || detected.variable),
        category: normalizeText(payload.category || detected.category || item.category || "Unassigned"),
        question_type: normalizeText(payload.question_type || payload.questionType || detected.question_type || "single_response"),
        table_type: normalizeText(payload.table_type || payload.tableType || "column_percentage"),
        response_list_id: normalizeText(payload.response_list_id || payload.responseListId || detected.list_name || ""),
        display_order: Number(payload.display_order || payload.displayOrder || 999),
        status: "active",
        effective_from: effectiveFrom,
        filter_expression: normalizeText(payload.filter_expression || payload.filterExpression || detected.relevance || ""),
        base_rule: normalizeText(payload.base_rule || payload.baseRule || "valid_responses"),
        statistics: Array.isArray(payload.statistics) ? payload.statistics : ["count", "column_percentage"],
        breaks: Array.isArray(payload.breaks) ? payload.breaks : ["Total", "Region", "REGION2", "Gender", "Age", "SEC", "Week"],
        include_empty_categories: payload.include_empty_categories !== false,
        response_options: normalizeResponseOptions(payload.response_options || payload.responseOptions || detected.response_options || []),
        source: normalizeText(detected.source || item.source || "review"),
        detected_at: item.detected_at,
        approved_at: stamp,
        approved_by: approvedBy,
        replaces_question_id: payload.replaces_question_id || null,
        notes: note || null,
      };
      const errors = validateQuestionRecord(record);
      if (errors.length) throw new Error(errors.join(" "));
      await exec(dbApi, `
        INSERT INTO question_registry VALUES (
          ${quoteSql(record.id)}, ${quoteSql(record.variable)}, ${quoteSql(record.label)}, ${quoteSql(record.category)},
          ${quoteSql(record.question_type)}, ${quoteSql(record.table_type)}, ${quoteSql(record.response_list_id)}, ${Number(record.display_order) || 0},
          ${quoteSql(record.status)}, ${quoteSql(record.effective_from)}, NULL, ${quoteSql(record.filter_expression)}, ${quoteSql(record.base_rule)},
          ${jsonSql(record.statistics)}, ${jsonSql(record.breaks)}, ${record.include_empty_categories ? "TRUE" : "FALSE"},
          ${quoteSql(record.source)}, ${quoteSql(record.detected_at)}, ${quoteSql(record.approved_at)}, ${quoteSql(record.approved_by)},
          ${quoteSql(record.replaces_question_id)}, ${quoteSql(record.notes)}, ${jsonSql(record)}, ${quoteSql(stamp)}, ${quoteSql(stamp)}
        )
      `);
      await insertQuestionResponseOptions(dbApi, record.id, record.response_options, record, stamp);
      entityId = record.id;
    } else {
      throw new Error(`Unsupported review entity type: ${item.entity_type}`);
    }
    await exec(dbApi, `
      INSERT INTO registry_audit_log VALUES (
        ${quoteSql(crypto.randomUUID())}, ${quoteSql(item.entity_type)}, ${quoteSql(entityId)}, 'approve',
        ${jsonSql(item.current_definition)}, ${jsonSql(item.detected_definition)}, ${quoteSql(effectiveFrom)},
        ${quoteSql(approvedBy)}, ${quoteSql(stamp)}, ${quoteSql(note)}
      )
    `);
    await updateQueueDecision(dbApi, id, "approved", approvedBy, note);
    await createRegistrySnapshot(dbApi, "approval", item.category || "all", effectiveFrom, approvedBy);
    await exec(dbApi, "COMMIT");
    return { ok: true, id, entityId, status: "approved" };
  } catch (err) {
    await exec(dbApi, "ROLLBACK");
    throw err;
  }
}

async function addAliasFromReview(dbApi, id, payload) {
  await ensureMetadataRegistrySchema(dbApi);
  const rows = await all(dbApi, `SELECT * FROM metadata_change_queue WHERE id=${quoteSql(id)} LIMIT 1`);
  if (!rows.length) throw new Error("Review item not found.");
  const item = decodeChangeRow(rows[0]);
  if (item.entity_type !== "brand") throw new Error("Alias actions are only valid for brand review items.");
  const targetBrandId = normalizeText(payload.targetBrandId || payload.target_brand_id || payload.brand_id);
  const detected = item.detected_definition || {};
  const alias = normalizeText(payload.alias || detected.label || detected.code);
  const approvedBy = normalizeText(payload.approvedBy || payload.approved_by || "admin");
  const effectiveFrom = normalizeText(payload.effective_from || payload.effectiveFrom || item.first_observed_period || "1900-01");
  const note = normalizeText(payload.note || "Approved as brand alias.");
  if (!targetBrandId) throw new Error("targetBrandId is required.");
  if (!alias) throw new Error("Alias is required.");

  await exec(dbApi, "BEGIN TRANSACTION");
  try {
    const target = await all(dbApi, `SELECT * FROM brand_registry WHERE id=${quoteSql(targetBrandId)} AND status IN ('active', 'approved') LIMIT 1`);
    if (!target.length) throw new Error("Target brand was not found.");
    await exec(dbApi, `
      INSERT INTO brand_aliases VALUES (
        ${quoteSql(targetBrandId)}, ${quoteSql(alias)}, ${quoteSql(normalizeKey(alias))}, 'review', 'active',
        ${quoteSql(effectiveFrom)}, NULL, ${quoteSql(nowIso())}, ${quoteSql(approvedBy)}, ${quoteSql(nowIso())}
      )
    `);
    await updateQueueDecision(dbApi, id, "approved", approvedBy, note);
    await exec(dbApi, `
      INSERT INTO registry_audit_log VALUES (
        ${quoteSql(crypto.randomUUID())}, 'brand', ${quoteSql(targetBrandId)}, 'add_alias',
        ${jsonSql(item.current_definition)}, ${jsonSql({ alias, targetBrandId, detected: item.detected_definition })},
        ${quoteSql(effectiveFrom)}, ${quoteSql(approvedBy)}, ${quoteSql(nowIso())}, ${quoteSql(note)}
      )
    `);
    await createRegistrySnapshot(dbApi, "approval", target[0].category || item.category || "all", effectiveFrom, approvedBy);
    await exec(dbApi, "COMMIT");
    return { ok: true, id, entityId: targetBrandId, alias, status: "approved" };
  } catch (err) {
    await exec(dbApi, "ROLLBACK");
    throw err;
  }
}

async function linkQuestionFromReview(dbApi, id, payload) {
  await ensureMetadataRegistrySchema(dbApi);
  const rows = await all(dbApi, `SELECT * FROM metadata_change_queue WHERE id=${quoteSql(id)} LIMIT 1`);
  if (!rows.length) throw new Error("Review item not found.");
  const item = decodeChangeRow(rows[0]);
  if (item.entity_type !== "question") throw new Error("Link actions are only valid for question review items.");
  const targetQuestionId = normalizeText(payload.targetQuestionId || payload.target_question_id || payload.question_id);
  const approvedBy = normalizeText(payload.approvedBy || payload.approved_by || "admin");
  const effectiveFrom = normalizeText(payload.effective_from || payload.effectiveFrom || item.first_observed_period || "1900-01");
  const note = normalizeText(payload.note || "Linked to existing question.");
  if (!targetQuestionId) throw new Error("targetQuestionId is required.");

  await exec(dbApi, "BEGIN TRANSACTION");
  try {
    const target = await all(dbApi, `SELECT * FROM question_registry WHERE id=${quoteSql(targetQuestionId)} AND status IN ('active', 'approved') LIMIT 1`);
    if (!target.length) throw new Error("Target question was not found.");
    await updateQueueDecision(dbApi, id, "approved", approvedBy, note);
    await exec(dbApi, `
      INSERT INTO registry_audit_log VALUES (
        ${quoteSql(crypto.randomUUID())}, 'question', ${quoteSql(targetQuestionId)}, 'link_question',
        ${jsonSql(item.current_definition)}, ${jsonSql({ targetQuestionId, detected: item.detected_definition })},
        ${quoteSql(effectiveFrom)}, ${quoteSql(approvedBy)}, ${quoteSql(nowIso())}, ${quoteSql(note)}
      )
    `);
    await createRegistrySnapshot(dbApi, "approval", target[0].category || item.category || "all", effectiveFrom, approvedBy);
    await exec(dbApi, "COMMIT");
    return { ok: true, id, entityId: targetQuestionId, status: "approved" };
  } catch (err) {
    await exec(dbApi, "ROLLBACK");
    throw err;
  }
}

async function markQuestionNonReportableFromReview(dbApi, id, payload) {
  await ensureMetadataRegistrySchema(dbApi);
  const rows = await all(dbApi, `SELECT * FROM metadata_change_queue WHERE id=${quoteSql(id)} LIMIT 1`);
  if (!rows.length) throw new Error("Review item not found.");
  const item = decodeChangeRow(rows[0]);
  if (item.entity_type !== "question") throw new Error("Non-reportable actions are only valid for question review items.");
  const approvedBy = normalizeText(payload.approvedBy || payload.approved_by || "admin");
  const effectiveFrom = normalizeText(payload.effective_from || payload.effectiveFrom || item.first_observed_period || "1900-01");
  const note = normalizeText(payload.note || "Marked as non-reportable.");

  await exec(dbApi, "BEGIN TRANSACTION");
  try {
    await updateQueueDecision(dbApi, id, "approved", approvedBy, note);
    await exec(dbApi, `
      INSERT INTO registry_audit_log VALUES (
        ${quoteSql(crypto.randomUUID())}, 'question', ${quoteSql(id)}, 'mark_non_reportable',
        ${jsonSql(item.current_definition)}, ${jsonSql(item.detected_definition)},
        ${quoteSql(effectiveFrom)}, ${quoteSql(approvedBy)}, ${quoteSql(nowIso())}, ${quoteSql(note)}
      )
    `);
    await createRegistrySnapshot(dbApi, "approval", item.category || "all", effectiveFrom, approvedBy);
    await exec(dbApi, "COMMIT");
    return { ok: true, id, status: "approved", reportable: false };
  } catch (err) {
    await exec(dbApi, "ROLLBACK");
    throw err;
  }
}

async function rejectReviewChange(dbApi, id, payload) {
  await ensureMetadataRegistrySchema(dbApi);
  const approvedBy = normalizeText(payload.approvedBy || payload.approved_by || "admin");
  const note = normalizeText(payload.note || "");
  await exec(dbApi, "BEGIN TRANSACTION");
  try {
    const rows = await all(dbApi, `SELECT * FROM metadata_change_queue WHERE id=${quoteSql(id)} LIMIT 1`);
    if (!rows.length) throw new Error("Review item not found.");
    const item = decodeChangeRow(rows[0]);
    await updateQueueDecision(dbApi, id, "rejected", approvedBy, note);
    await exec(dbApi, `
      INSERT INTO registry_audit_log VALUES (
        ${quoteSql(crypto.randomUUID())}, ${quoteSql(item.entity_type)}, ${quoteSql(id)}, 'reject',
        ${jsonSql(item.current_definition)}, ${jsonSql(item.detected_definition)}, ${quoteSql(item.first_observed_period || "")},
        ${quoteSql(approvedBy)}, ${quoteSql(nowIso())}, ${quoteSql(note)}
      )
    `);
    await exec(dbApi, "COMMIT");
    return { ok: true, id, status: "rejected" };
  } catch (err) {
    await exec(dbApi, "ROLLBACK");
    throw err;
  }
}

async function snapshotAutorecodeMappings(dbApi, { period = "1900-01", createdBy = "system" } = {}) {
  await ensureMetadataRegistrySchema(dbApi);
  if (!(await tableExists(dbApi, "spss_autorecode_maps"))) {
    return { ok: true, source: null, versions: 0, mappings: 0, message: "Runtime spss_autorecode_maps table is not available." };
  }
  const rules = await all(dbApi, `
    SELECT DISTINCT source_variable, target_variable
    FROM spss_autorecode_maps
    ORDER BY source_variable, target_variable
  `);
  let versionCount = 0;
  let mappingCount = 0;
  const stamp = nowIso();
  await exec(dbApi, "BEGIN TRANSACTION");
  try {
    for (const rule of rules) {
      const source = normalizeText(rule.source_variable);
      const target = normalizeText(rule.target_variable);
      if (!source || !target) continue;
      const versionId = stableId("autorecode", [source, target, period]);
      await exec(dbApi, `
        DELETE FROM spss_autorecode_mapping_versions
        WHERE id=${quoteSql(versionId)}
      `);
      await exec(dbApi, `
        DELETE FROM spss_autorecode_mappings
        WHERE version_id=${quoteSql(versionId)}
      `);
      await exec(dbApi, `
        INSERT INTO spss_autorecode_mapping_versions VALUES (
          ${quoteSql(versionId)}, ${quoteSql(source)}, ${quoteSql(target)}, ${quoteSql(period)}, NULL,
          'active', 'runtime_spss_autorecode_maps', ${quoteSql(stamp)}, ${quoteSql(createdBy)}
        )
      `);
      versionCount += 1;
      const mappings = await all(dbApi, `
        SELECT source_value, source_label, target_code
        FROM spss_autorecode_maps
        WHERE source_variable=${quoteSql(source)}
          AND target_variable=${quoteSql(target)}
        ORDER BY target_code
      `);
      for (const mapping of mappings) {
        await exec(dbApi, `
          INSERT INTO spss_autorecode_mappings VALUES (
            ${quoteSql(versionId)}, ${quoteSql(source)}, ${quoteSql(target)}, ${quoteSql(mapping.source_value)},
            ${quoteSql(mapping.source_label)}, ${Number(mapping.target_code) || 0}, ${quoteSql(period)}, NULL, ${quoteSql(stamp)}
          )
        `);
        mappingCount += 1;
      }
    }
    await exec(dbApi, "COMMIT");
    return { ok: true, source: "spss_autorecode_maps", period, versions: versionCount, mappings: mappingCount };
  } catch (err) {
    await exec(dbApi, "ROLLBACK");
    throw err;
  }
}

async function metadataDiagnostics(dbApi) {
  await ensureMetadataRegistrySchema(dbApi);
  const tables = {};
  for (const table of [
    "brand_registry",
    "brand_aliases",
    "question_registry",
    "question_response_options",
    "metadata_change_queue",
    "registry_audit_log",
    "registry_snapshots",
    "spss_autorecode_mapping_versions",
    "spss_autorecode_mappings",
  ]) {
    tables[table] = await countRows(dbApi, table);
  }
  const pending = await all(dbApi, `
    SELECT entity_type, change_type, COUNT(*) AS count
    FROM metadata_change_queue
    WHERE status='pending_review'
    GROUP BY entity_type, change_type
    ORDER BY entity_type, change_type
  `);
  return {
    ok: true,
    schemaVersion: SCHEMA_VERSION,
    generatedAt: nowIso(),
    tables,
    pendingReview: pending.map((row) => ({ ...row, count: Number(row.count || 0) })),
  };
}

async function registryBrands(dbApi) {
  await ensureMetadataRegistrySchema(dbApi);
  return all(dbApi, `SELECT * FROM brand_registry ORDER BY category, display_order, label LIMIT 2000`);
}

async function registryQuestions(dbApi) {
  await ensureMetadataRegistrySchema(dbApi);
  return all(dbApi, `SELECT * FROM question_registry ORDER BY category, display_order, variable LIMIT 3000`);
}

async function autorecodeDiagnostics(dbApi) {
  await ensureMetadataRegistrySchema(dbApi);
  const persistent = await tableExists(dbApi, "spss_autorecode_mappings");
  const runtime = await tableExists(dbApi, "spss_autorecode_maps");
  const table = persistent ? "spss_autorecode_mappings" : runtime ? "spss_autorecode_maps" : null;
  if (!table) return { ok: true, mappings: [], source: null };
  const rows = await all(dbApi, `
    SELECT *
    FROM ${table}
    ORDER BY source_variable, target_variable, target_code
    LIMIT 5000
  `);
  return { ok: true, source: table, mappings: rows };
}

function activeMonthsForRecord(record, months) {
  if (!Array.isArray(months) || !months.length) return [];
  const from = compactLabel(record?.effective_from);
  const to = compactLabel(record?.effective_to);
  return months.filter((month) => {
    const value = compactLabel(month);
    if (!value) return false;
    if (from && value < from) return false;
    if (to && value > to) return false;
    return true;
  });
}

async function generatedExportSpec(dbApi, { category = "", months = [], type = "" } = {}) {
  await ensureMetadataRegistrySchema(dbApi);
  const period = Array.isArray(months) && months.length ? months[months.length - 1] : "9999-99";
  const firstPeriod = Array.isArray(months) && months.length ? months[0] : "0000-00";
  const categoryFilter = normalizeText(category);
  const brands = await all(dbApi, `
    SELECT id, code, label, category, display_order, effective_from, effective_to, status, source
    FROM brand_registry
    WHERE status IN ('active', 'approved')
      AND (${quoteSql(categoryFilter)}='' OR lower(category)=lower(${quoteSql(categoryFilter)}))
      AND (effective_from IS NULL OR effective_from='' OR effective_from <= ${quoteSql(period)})
      AND (effective_to IS NULL OR effective_to='' OR effective_to >= ${quoteSql(firstPeriod)})
    ORDER BY category, display_order, label
  `);
  const questions = await all(dbApi, `
    SELECT *
    FROM question_registry
    WHERE status IN ('active', 'approved')
      AND (${quoteSql(categoryFilter)}='' OR lower(category)=lower(${quoteSql(categoryFilter)}))
      AND (effective_from IS NULL OR effective_from='' OR effective_from <= ${quoteSql(period)})
      AND (effective_to IS NULL OR effective_to='' OR effective_to >= ${quoteSql(firstPeriod)})
    ORDER BY category, display_order, variable
  `);
  const options = await all(dbApi, `
    SELECT *
    FROM question_response_options
    WHERE status IN ('active', 'approved')
      AND (effective_from IS NULL OR effective_from='' OR effective_from <= ${quoteSql(period)})
      AND (effective_to IS NULL OR effective_to='' OR effective_to >= ${quoteSql(firstPeriod)})
    ORDER BY question_id, display_order, option_label
  `);
  const optionsByQuestion = new Map();
  for (const option of options) {
    const enrichedOption = {
      ...option,
      activeMonths: activeMonthsForRecord(option, months),
    };
    if (!optionsByQuestion.has(option.question_id)) optionsByQuestion.set(option.question_id, []);
    optionsByQuestion.get(option.question_id).push(enrichedOption);
  }
  const tables = questions.map((question) => ({
    id: question.id,
    variable: question.variable,
    title: question.label,
    category: question.category,
    questionType: question.question_type,
    tableType: question.table_type,
    source: question.source,
    baseRule: question.base_rule,
    filterExpression: question.filter_expression || null,
    statistics: parseJson(question.statistics_json, []),
    breaks: parseJson(question.breaks_json, []),
    includeEmptyCategories: Boolean(question.include_empty_categories),
    effectiveFrom: question.effective_from || null,
    effectiveTo: question.effective_to || null,
    activeMonths: activeMonthsForRecord(question, months),
    responseOptions: optionsByQuestion.get(question.id) || [],
  }));
  const brandUniverse = brands.map((brand) => ({
    ...brand,
    activeMonths: activeMonthsForRecord(brand, months),
  }));
  return {
    ok: true,
    type,
    category: categoryFilter || null,
    months,
    registryPeriod: period === "9999-99" ? null : period,
    brandUniverse,
    questionUniverse: questions,
    generatedTables: tables,
    diagnostics: {
      brandUniverseCount: brandUniverse.length,
      questionUniverseCount: questions.length,
      tableCount: tables.length,
      firstPeriod: firstPeriod === "0000-00" ? null : firstPeriod,
      activeMonthCount: Array.isArray(months) ? months.length : 0,
    },
  };
}

async function exportRegistryDiagnostics(dbApi, { categorySlug, category, months, type }) {
  await ensureMetadataRegistrySchema(dbApi);
  const period = Array.isArray(months) && months.length ? months[months.length - 1] : "";
  const brands = await all(dbApi, `
    SELECT id, code, label, category, display_order, effective_from, effective_to, status, source
    FROM brand_registry
    WHERE status IN ('active', 'approved')
      AND (${quoteSql(category || "")}='' OR lower(category)=lower(${quoteSql(category || "")}))
      AND (effective_from IS NULL OR effective_from='' OR effective_from <= ${quoteSql(period || "9999-99")})
      AND (effective_to IS NULL OR effective_to='' OR effective_to >= ${quoteSql(months?.[0] || "0000-00")})
    ORDER BY display_order, label
  `);
  const questions = await all(dbApi, `
    SELECT id, variable, label, category, question_type, table_type, display_order, effective_from, effective_to, status, source
    FROM question_registry
    WHERE status IN ('active', 'approved')
      AND (${quoteSql(category || "")}='' OR lower(category)=lower(${quoteSql(category || "")}))
      AND (effective_from IS NULL OR effective_from='' OR effective_from <= ${quoteSql(period || "9999-99")})
      AND (effective_to IS NULL OR effective_to='' OR effective_to >= ${quoteSql(months?.[0] || "0000-00")})
    ORDER BY display_order, variable
  `);
  const warnings = await all(dbApi, `
    SELECT id, entity_type, change_type, recommendation, warnings_json
    FROM metadata_change_queue
    WHERE status='pending_review'
      AND (${quoteSql(category || "")}='' OR category='' OR lower(category)=lower(${quoteSql(category || "")}))
    ORDER BY detected_at DESC
    LIMIT 50
  `);
  return {
    categorySlug,
    category,
    type,
    months,
    registryPeriod: period,
    brandUniverseCount: brands.length,
    questionUniverseCount: questions.length,
    brands: brands.map((brand) => ({ ...brand, activeMonths: activeMonthsForRecord(brand, months) })),
    questions: questions.map((question) => ({ ...question, activeMonths: activeMonthsForRecord(question, months) })),
    generatedSpec: await generatedExportSpec(dbApi, { category, months, type }),
    unresolvedMetadata: warnings.map(decodeChangeRow),
  };
}

module.exports = {
  SCHEMA_VERSION,
  ensureMetadataRegistrySchema,
  seedInitialRegistry,
  detectMetadataChanges,
  listReviewQueue,
  requireAdminToken,
  approveReviewChange,
  addAliasFromReview,
  linkQuestionFromReview,
  markQuestionNonReportableFromReview,
  rejectReviewChange,
  snapshotAutorecodeMappings,
  metadataDiagnostics,
  registryBrands,
  registryQuestions,
  autorecodeDiagnostics,
  generatedExportSpec,
  exportRegistryDiagnostics,
  buildRegistrySeed,
  normalizeResponseOptions,
  normalizeKey,
  quoteSql,
};
