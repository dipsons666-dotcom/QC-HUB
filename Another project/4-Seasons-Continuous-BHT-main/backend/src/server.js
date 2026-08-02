const express = require("express");
const cors = require("cors");
const duckdb = require("duckdb");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { spawn, spawnSync } = require("child_process");
const { writeTemplatePreservingWorkbook } = require("./template-workbook-writer");
const metadataRegistry = require("./metadata-registry");

let XLSX = null;
try {
  XLSX = require("xlsx-js-style");
} catch (_err) {
  try {
    XLSX = require("xlsx");
  } catch (_fallbackErr) {
    XLSX = null;
  }
}

const app = express();
const port = Number(process.env.PORT || 4000);

const DEFAULT_RENDER_DATA_ROOT = "/var/data";
const DATA_ROOT =
  process.env.DATA_ROOT ||
  (process.env.RENDER
    ? DEFAULT_RENDER_DATA_ROOT
    : "C:/Users/Adesina Adeyemo/Downloads/Restructured/Restructured");
const LONG_GLOB =
  process.env.LONG_PARQUET_PATH ||
  `${DATA_ROOT}/responses_long_parquet/*/*/*.parquet`;
const BASE_GLOB =
  process.env.BASE_PARQUET_PATH ||
  `${DATA_ROOT}/responses_base_parquet/*/*/*.parquet`;
const DEFAULT_DB_PATH = process.env.RENDER
  ? path.join(DATA_ROOT, "current.duckdb")
  : path.join(process.cwd(), "current.duckdb");
const CONFIGURED_DB_PATH =
  path.resolve(process.env.DUCKDB_PATH || DEFAULT_DB_PATH);
const BUNDLED_DB_PATH = path.resolve(
  process.env.BUNDLED_DUCKDB_PATH || path.join(__dirname, "../market_insights.duckdb"),
);
const CONFIGURED_INSPECT_DB_PATH =
  path.resolve(
    process.env.INSPECT_DB_PATH ||
    path.join(path.dirname(CONFIGURED_DB_PATH), "market_insights_inspect.duckdb"),
  );

function resolveMarketDbPath(configuredDbPath = CONFIGURED_DB_PATH) {
  if (process.env.MARKET_DB_PATH) return path.resolve(process.env.MARKET_DB_PATH);

  const candidates = [
    path.join(path.dirname(path.resolve(configuredDbPath)), "market_insights.duckdb"),
    path.join(path.dirname(path.resolve(configuredDbPath)), "market_insights_1.duckdb"),
    path.resolve(process.cwd(), "market_insights.duckdb"),
    path.resolve(process.cwd(), "market_insights_1.duckdb"),
    path.resolve(process.cwd(), "../market_insights.duckdb"),
    path.resolve(process.cwd(), "../market_insights_1.duckdb"),
    path.resolve(__dirname, "../../market_insights.duckdb"),
    path.resolve(__dirname, "../../market_insights_1.duckdb"),
    path.resolve(__dirname, "../market_insights.duckdb"),
    path.resolve(__dirname, "../market_insights_1.duckdb"),
  ];

  const found = candidates.find((candidate) => fs.existsSync(candidate));
  return path.resolve(found || candidates[0]);
}

// Secondary DB: market_insights.duckdb. It stays separate from current.duckdb;
// startup attaches it read-only and exposes both files through combined views.
const MARKET_DB_PATH = resolveMarketDbPath(CONFIGURED_DB_PATH);
// Optional manual cap for market_insights.duckdb rows. When unset, the cap is
// read from the market DB's latest respondent_dims month.
const MARKET_INSIGHTS_CURRENT_MONTH = process.env.MARKET_INSIGHTS_CURRENT_MONTH || "";
const DUCKDB_MEMORY_LIMIT = process.env.DUCKDB_MEMORY_LIMIT || "1536MB";
const DUCKDB_TEMP_DIRECTORY = process.env.DUCKDB_TEMP_DIRECTORY || path.join(DATA_ROOT, "duckdb_tmp");
const DUCKDB_MAX_TEMP_DIRECTORY_SIZE = process.env.DUCKDB_MAX_TEMP_DIRECTORY_SIZE || "7GB";
const DUCKDB_THREADS = Math.max(1, Number(process.env.DUCKDB_THREADS || 2));
const SYNC_STATE_PATH = path.join(DATA_ROOT, "state", "sync_state.json");
const SYNC_RESULT_PATH = path.join(DATA_ROOT, "state", "sync_result.json");
const EXPORTS_ROOT = path.join(DATA_ROOT, "exports");
const EXPORT_MANIFEST_PATH = path.join(EXPORTS_ROOT, "export_manifest.json");
const EXPORT_TEMPLATE_PATH = path.resolve(__dirname, "../templates/BHT_TRACKER_DATA_TABLES_TEMPLATE.xlsx");
const EXPORT_TABLE_SPECS_PATH = path.resolve(__dirname, "../data/export_table_specs.json");
const SPSS_EXPORT_RULES_PATH = path.resolve(__dirname, "../data/spss_export_rules.json");
const XLSFORM_EXPORT_METADATA_PATH = path.resolve(__dirname, "../data/xlsform_metadata.json");
const SYNC_SCRIPT_PATH = path.resolve(__dirname, "../scripts/surveycto_bht_sync.py");
const BHT_DATAMAP_PATH = path.resolve(
  process.env.BHT_DATAMAP_PATH ||
    [
      path.resolve(__dirname, "../data/Datamap.xlsx"),
      path.resolve(__dirname, "../../Datamap.xlsx"),
      path.resolve(__dirname, "../../../Datamap.xlsx"),
    ].find((candidate) => fs.existsSync(candidate)) ||
    path.resolve(__dirname, "../data/Datamap.xlsx"),
);
const MARKET_HEADER_AUDIT_METADATA_PATH = path.resolve(
  __dirname,
  "../data/market_insights_header_audit_metadata.json",
);
const CURRENT_HEADER_AUDIT_METADATA_PATH = path.resolve(
  __dirname,
  "../data/current_header_audit_metadata.json",
);
const CURRENT_HEADER_AUDIT_MONTH_CUTOFF = process.env.CURRENT_HEADER_AUDIT_MONTH_CUTOFF || "2026-04";
const HISTORICAL_DATAMAP_AUDIT_MONTHS = String(
  process.env.HISTORICAL_DATAMAP_AUDIT_MONTHS || "2026-04,2026-05",
)
  .split(",")
  .map((value) => value.trim())
  .filter((value) => /^\d{4}-\d{2}$/.test(value));
const PYTHON_BIN = process.env.PYTHON_BIN || "python";
const SYNC_INTERVAL_MS = Math.max(60_000, Number(process.env.SYNC_INTERVAL_MS || 3_600_000));
const RUN_SYNC_ON_START = /^(1|true|yes|on)$/i.test(String(process.env.RUN_SYNC_ON_START || "true"));
const DUCKDB_BACKUP_RETENTION = Math.max(0, Number(process.env.DUCKDB_BACKUP_RETENTION || 3));

function resolveFrontendDistPath() {
  const candidates = [
    process.env.FRONTEND_DIST_PATH,
    path.resolve(process.cwd(), "frontend", "dist"),
    path.resolve(__dirname, "../../frontend/dist"),
    path.resolve(process.cwd(), "dist"),
    path.resolve(__dirname, "../dist"),
  ].filter(Boolean);

  for (const candidate of candidates) {
    const resolved = path.resolve(candidate);
    if (fs.existsSync(path.join(resolved, "index.html"))) {
      return resolved;
    }
  }

  return null;
}

const FRONTEND_DIST_PATH = resolveFrontendDistPath();
const FRONTEND_INDEX_PATH = FRONTEND_DIST_PATH
  ? path.join(FRONTEND_DIST_PATH, "index.html")
  : null;

function listDuckDbCandidates(configuredDbPath) {
  const configuredPath = path.resolve(configuredDbPath);
  const marketPath = resolveMarketDbPath(configuredPath);
  const configuredStats = fs.existsSync(configuredPath)
    ? fs.statSync(configuredPath)
    : null;
  const candidates = [{
    path: configuredPath,
    size: configuredStats ? Number(configuredStats.size || 0) : 0,
    mtimeMs: configuredStats ? Number(configuredStats.mtimeMs || 0) : 0,
  }];
  if (fs.existsSync(marketPath) && path.resolve(marketPath) !== configuredPath) {
    const stats = fs.statSync(marketPath);
    candidates.push({
      path: marketPath,
      size: Number(stats.size || 0),
      mtimeMs: Number(stats.mtimeMs || 0),
    });
  }
  return candidates;
}
let DUCKDB_CANDIDATES = listDuckDbCandidates(CONFIGURED_DB_PATH);
let DB_PATH = DUCKDB_CANDIDATES[0]?.path || CONFIGURED_DB_PATH;
const INSPECT_DB_PATH = CONFIGURED_INSPECT_DB_PATH;

let db = null;
let conn = null;
let connectionPromise = null;
let loggedDbPath = false;
let loggedDuckDbCandidates = false;
let combinedMarketViewsReady = false;
let combinedMarketMonthCutoff = "";
let datamapRawQuestionCodesByOldKey = new Map();
let syncTimer = null;
let exportTimer = null;
let syncInFlight = null;
let syncDatabaseMaintenance = false;
let syncStatus = {
  enabled: false,
  running: false,
  lastRunStartedAt: null,
  lastRunFinishedAt: null,
  lastSuccessAt: null,
  lastError: null,
  lastResult: null,
};

function activeDbIsReadOnlyMarketDb() {
  return path.resolve(DB_PATH) === path.resolve(MARKET_DB_PATH);
}
let headerAuditMetadataCache = null;

let initialized = false;
let initPromise = null;
let schemaCache = null;
let questionCacheByCategory = new Map();
let respondentDimColumns = ["category", "respondent_id", "month"];
const RESPONDENT_DIM_FIELDS = ["Region", "D3", "Gender", "Age", "SEC", "Week"];
const RESPONDENT_DIM_FIELD_CANDIDATES = new Map([
  ["region", ["Region", "City_1"]],
  ["city_1", ["City_1", "Region"]],
  ["d3", ["D3", "Income"]],
  ["gender", ["Gender"]],
  ["age", ["Age", "Age_cal"]],
  ["sec", ["SEC"]],
  ["week", ["Week"]],
]);
const REQUIRED_NATIVE_TABLES = ["respondent_dims", "responses_fact"];
const AWARENESS_QUERY_CACHE_TTL_MS = 300_000;
const awarenessQueryCache = new Map();
const awarenessQueryInFlight = new Map();
const OVERVIEW_QUERY_CACHE_TTL_MS = 3_600_000;
const overviewQueryCache = new Map();
const overviewQueryInFlight = new Map();
const FILTERS_QUERY_CACHE_TTL_MS = 3_600_000;
const filtersQueryCache = new Map();
const PAGE_DATA_QUERY_CACHE_TTL_MS = 600_000;
const pageDataQueryCache = new Map();
const pageDataQueryInFlight = new Map();
const PAGE_DATA_MONTHLY_QUERY_CACHE_TTL_MS = 600_000;
const pageDataMonthlyQueryCache = new Map();
const pageDataMonthlyQueryInFlight = new Map();
const VERBATIM_STOP_WORDS = new Set([
  "a",
  "about",
  "after",
  "again",
  "all",
  "also",
  "am",
  "an",
  "and",
  "any",
  "are",
  "as",
  "at",
  "be",
  "because",
  "been",
  "before",
  "being",
  "between",
  "both",
  "but",
  "by",
  "can",
  "could",
  "did",
  "do",
  "does",
  "doing",
  "done",
  "for",
  "from",
  "get",
  "got",
  "had",
  "has",
  "have",
  "having",
  "he",
  "her",
  "here",
  "hers",
  "him",
  "his",
  "how",
  "i",
  "if",
  "in",
  "into",
  "is",
  "it",
  "its",
  "itself",
  "just",
  "me",
  "more",
  "most",
  "my",
  "no",
  "not",
  "of",
  "on",
  "or",
  "our",
  "out",
  "really",
  "same",
  "she",
  "so",
  "some",
  "such",
  "than",
  "that",
  "the",
  "their",
  "them",
  "then",
  "there",
  "these",
  "they",
  "this",
  "those",
  "to",
  "too",
  "very",
  "was",
  "we",
  "were",
  "what",
  "when",
  "where",
  "which",
  "while",
  "who",
  "why",
  "will",
  "with",
  "would",
  "you",
  "your",
]);
const POSITIVE_SENTIMENT_TOKENS = new Set([
  "affordable",
  "amazing",
  "appealing",
  "aroma",
  "attractive",
  "available",
  "beautiful",
  "best",
  "convenient",
  "creamy",
  "crispy",
  "delicious",
  "easy",
  "enjoy",
  "enjoyable",
  "excellent",
  "fantastic",
  "fast",
  "favorite",
  "filling",
  "flavor",
  "flavour",
  "fresh",
  "good",
  "great",
  "healthy",
  "hygienic",
  "interesting",
  "like",
  "love",
  "lovely",
  "nice",
  "nutritious",
  "pleasant",
  "quality",
  "refreshing",
  "reliable",
  "rich",
  "savory",
  "satisfy",
  "satisfied",
  "satisfying",
  "smooth",
  "special",
  "strong",
  "sweet",
  "tasty",
  "unique",
  "value",
  "wonderful",
  "yummy",
]);
const NEGATIVE_SENTIMENT_TOKENS = new Set([
  "annoying",
  "awful",
  "bad",
  "bitter",
  "bland",
  "boring",
  "confusing",
  "costly",
  "delay",
  "dirty",
  "difficult",
  "dislike",
  "disappoint",
  "disgust",
  "dry",
  "expensive",
  "fake",
  "greasy",
  "hard",
  "hate",
  "horrible",
  "low",
  "messy",
  "ordinary",
  "poor",
  "salty",
  "slow",
  "small",
  "smell",
  "sour",
  "spicy",
  "terrible",
  "tough",
  "ugly",
  "unavailable",
  "unpleasant",
  "weak",
  "worst",
]);

const CATEGORY_SLUG_MAP = {
  "breakfast-cereals": "Breakfast_Cereal",
  noodles: "Noodles",
  toothpaste: "Toothpaste",
  bleach: "Bleach",
  "wet-hair": "Wet_Hair_Care",
  "dry-hair": "Dry_Hair_Care",
  "condiment-mixes": "Condiment_Mixes",
  malt: "Malt_Beverage",
  snacks: "Snacks_Products",
  "edible-oil": "Edible_Oil",
  "toilet-cleaner": "Toilet_Cleaner",
};

// Derived from questionnaire sections:
// SCREENER, CATEGORY QUESTIONS, PURCHASE BEHAVIOR, BRAND IMAGERY, CAMPAIGN CHECK, VIDEO AD SECTION.
const PAGE_DEFINITIONS = [
  {
    id: "screener-demographics",
    title: "Screener & Demographics",
    description: "Respondent profile and screener variables.",
    matchers: [/^S\d+/i, /^Q\d+/i, /\bSEC\b/i, /\bAge\b/i, /\bmarital\b/i],
  },
  {
    id: "category-questions",
    title: "Category Questions",
    description: "Category usage and category-level diagnostics.",
    matchers: [/_QC\d*/i, /\bcategory question/i],
  },
  {
    id: "awareness-usage",
    title: "Awareness & Usage",
    description: "Awareness, brand usage and media touchpoints.",
    matchers: [/_BAU\d*/i, /\bawareness\b/i, /\bseen or heard\b/i],
  },
  {
    id: "purchase-behavior",
    title: "Purchase Behavior",
    description: "Purchase path, frequency and buying drivers.",
    matchers: [/_PB\d*/i, /\bpurchase behavior\b/i, /\bbuy\b/i],
  },
  {
    id: "brand-imagery",
    title: "Brand Imagery",
    description: "Brand image and perception statements.",
    matchers: [/_QBI\d*/i, /\bbrand imagery\b/i, /\bbrand that\b/i],
  },
  {
    id: "flavour-flex-section",
    title: "Flavour/Flex Section",
    description: "Flavour consideration and flex activity diagnostics.",
    matchers: [/_FQ\d*/i, /_QFS\d*/i, /_QFSB\d*/i, /_QFW\d*/i, /\bflavou?r\b/i, /\bflex\b/i],
  },
  {
    id: "campaign-check",
    title: "Campaign Check",
    description: "Campaign and advert diagnostics.",
    matchers: [/_A\d*/i, /\bcampaign check\b/i, /\badvert/i, /\bcampaign\b/i],
  },
  {
    id: "video-ad-section",
    title: "Video Ad Section",
    description: "Video ad recall and evaluation.",
    matchers: [/\bvideo ad section\b/i, /\bvideo\b/i],
  },
  {
    id: "other-metrics",
    title: "Other Metrics",
    description: "Unmapped metrics from the questionnaire.",
    matchers: [],
  },
];

async function ensureConnection() {
  if (conn) return conn;
  if (connectionPromise) return connectionPromise;

  connectionPromise = new Promise((resolve, reject) => {
    let candidates = DUCKDB_CANDIDATES.length
      ? DUCKDB_CANDIDATES
      : [{ path: CONFIGURED_DB_PATH, size: 0, mtimeMs: 0 }];

    if (!loggedDuckDbCandidates) {
      const summary = candidates
        .map((candidate) => `${path.basename(candidate.path)} (${candidate.size} bytes)`)
        .join(", ");
      console.log(`[backend] duckdb candidates: ${summary || "(none found)"}`);
      loggedDuckDbCandidates = true;
    }

    let candidateIndex = 0;
    let attemptedConfiguredRecovery = false;

    const tryOpenNextCandidate = () => {
      const candidate = candidates[candidateIndex];
      if (!candidate) {
        connectionPromise = null;
        conn = null;
        db = null;
        return reject(new Error("No usable DuckDB file could be opened from the configured directory."));
      }

      DB_PATH = candidate.path;

      const openError = (message) => {
        const hasNextCandidate = candidateIndex < candidates.length - 1;
        if (hasNextCandidate) {
          console.warn(`[backend] duckdb open failed for ${DB_PATH}: ${message}`);
          candidateIndex += 1;
          conn = null;
          db = null;
          return tryOpenNextCandidate();
        }

        const isConfiguredDb = path.resolve(DB_PATH) === path.resolve(CONFIGURED_DB_PATH);
        if (!attemptedConfiguredRecovery && isConfiguredDb) {
          attemptedConfiguredRecovery = true;
          try {
            const quarantinedPath = quarantineUnreadableDuckDbFile(CONFIGURED_DB_PATH);
            const seededFrom = seedConfiguredDuckDbFromBundledSnapshot(CONFIGURED_DB_PATH);
            const recoverySteps = [];
            if (quarantinedPath) recoverySteps.push(`quarantined to ${quarantinedPath}`);
            if (seededFrom) recoverySteps.push(`seeded from ${seededFrom}`);
            if (!seededFrom) recoverySteps.push("starting with a fresh writable DuckDB");
            console.warn(`[backend] duckdb recovery engaged for ${CONFIGURED_DB_PATH}: ${recoverySteps.join("; ")}`);
            DUCKDB_CANDIDATES = listDuckDbCandidates(CONFIGURED_DB_PATH);
            candidates = DUCKDB_CANDIDATES.length
              ? DUCKDB_CANDIDATES
              : [{ path: CONFIGURED_DB_PATH, size: 0, mtimeMs: 0 }];
            candidateIndex = 0;
            loggedDbPath = false;
            return tryOpenNextCandidate();
          } catch (recoveryErr) {
            console.error(
              `[backend] duckdb recovery failed for ${CONFIGURED_DB_PATH}: ${
                recoveryErr && recoveryErr.message ? recoveryErr.message : String(recoveryErr)
              }`,
            );
          }
        }

        connectionPromise = null;
        conn = null;
        db = null;
        return reject(new Error(message));
      };

      try {
        fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
        const dbExists = fs.existsSync(DB_PATH);
        if (!loggedDbPath) {
          console.log(`[backend] duckdb path: ${DB_PATH}`);
          console.log(`[backend] duckdb exists: ${dbExists}`);
          loggedDbPath = true;
        }

        const openMode = activeDbIsReadOnlyMarketDb() ? duckdb.OPEN_READONLY : undefined;
        db = openMode === undefined
          ? new duckdb.Database(DB_PATH)
          : new duckdb.Database(DB_PATH, openMode);
        conn = db.connect();
        conn.all("SELECT 1 AS ok", (err) => {
          if (err) {
            const hint = dbExists
              ? `Existing DuckDB file at ${DB_PATH} could not be opened. It may be corrupted or incompatible with the deployed duckdb runtime.`
              : `DuckDB file could not be created at ${DB_PATH}. Check the mounted disk path and write permissions.`;
            return openError(`${err.message} (${hint})`);
          }
          resolve(conn);
        });
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        return openError(`${message} (Failed to initialize DuckDB at ${DB_PATH})`);
      }
    };

    try {
      tryOpenNextCandidate();
    } catch (err) {
      const message = err && err.message ? err.message : String(err);
      connectionPromise = null;
      conn = null;
      db = null;
      reject(new Error(`${message} (Failed to initialize DuckDB at ${DB_PATH})`));
    }
  });

  return connectionPromise;
}

async function run(sql) {
  const connection = await ensureConnection();
  return new Promise((resolve, reject) => {
    connection.run(sql, (err) => {
      if (err) return reject(err);
      resolve();
    });
  });
}

async function all(sql) {
  const connection = await ensureConnection();
  return new Promise((resolve, reject) => {
    connection.all(sql, (err, rows) => {
      if (err) return reject(err);
      resolve(rows || []);
    });
  });
}

function readJsonFile(filePath, fallback = null) {
  try {
    if (!fs.existsSync(filePath)) return fallback;
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (_err) {
    return fallback;
  }
}

function writeJsonFile(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2), "utf8");
  fs.renameSync(tmpPath, filePath);
}

const EXPORT_TYPES = new Set(["interim", "full", "rolling", "quarter"]);
const EXPORT_CATEGORY_LABELS = new Map([
  ["noodles", "Noodles"],
  ["toothpaste", "Toothpaste"],
  ["breakfast-cereals", "Breakfast Cereal"],
  ["bleach", "Bleach"],
  ["edible-oil", "Edible Oil"],
  ["snacks", "Snacks"],
  ["condiment-mixes", "Condiment Mixes"],
  ["wet-hair", "Wet Hair"],
  ["dry-hair", "Dry Hair"],
  ["toilet-cleaner", "Toilet Cleaner"],
  ["malt", "Malt"],
]);
const EXPORT_SHEET_NAMES = new Map([
  ["noodles", "Noodles"],
  ["toothpaste", "Toothpaste"],
  ["breakfast-cereals", "BreakfastCereal"],
  ["bleach", "Bleach"],
  ["edible-oil", "Edible Oil"],
  ["snacks", "Snacks"],
  ["condiment-mixes", "Condiment Mix"],
  ["wet-hair", "Wet Hair"],
  ["dry-hair", "Dry Hair"],
  ["toilet-cleaner", "Toilet Cleaner"],
  ["malt", "Malt"],
]);
const EXPORT_ROLLING_QUARTER_CODES = new Map([
  ["noodles", "N"],
  ["toothpaste", "TP"],
  ["breakfast-cereals", "BC"],
  ["bleach", "BL"],
  ["edible-oil", "EO"],
  ["snacks", "SK"],
  ["condiment-mixes", "CM"],
  ["wet-hair", "WH"],
  ["dry-hair", "DH"],
  ["toilet-cleaner", "TC"],
  ["malt", "ML"],
]);
const EXPORT_TEMPLATE_NAVIGATION = [
  { slug: "noodles", label: "Noodles", titleColumn: "N", valueColumn: "O", selection: 1 },
  { slug: "toothpaste", label: "Toothpaste", titleColumn: "S", valueColumn: "T", selection: 2 },
  { slug: "breakfast-cereals", label: "Breakfast Cereal", titleColumn: "X", valueColumn: "Y", selection: 3 },
  { slug: "bleach", label: "Bleach", titleColumn: "AC", valueColumn: "AD", selection: 4 },
  { slug: "edible-oil", label: "Edible Oil", titleColumn: "AG", valueColumn: "AH", selection: 5 },
  { slug: "snacks", label: "Snacks", titleColumn: "AL", valueColumn: "AM", selection: 6 },
  { slug: "condiment-mixes", label: "Condiment Mixes", titleColumn: "AQ", valueColumn: "AR", selection: 7 },
  { slug: "wet-hair", label: "Wet Hair", titleColumn: "AV", valueColumn: "AW", selection: 8 },
  { slug: "dry-hair", label: "Dry Hair", titleColumn: "BA", valueColumn: "BB", selection: 9 },
  { slug: "toilet-cleaner", label: "Toilet Cleaner", titleColumn: "BF", valueColumn: "BG", selection: 10 },
];
const EXPORT_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const EXPORT_BREAK_FIELDS = [
  { field: null, label: "Total" },
  { field: "Region", label: "REGION:" },
  { field: "D3", label: "INCOME:" },
  { field: "Gender", label: "GENDER:" },
  { field: "Age", label: "AGE:" },
  { field: "SEC", label: "SEC:" },
  { field: "Week", label: "WEEK:" },
];
const SPSS_DIMENSION_ALIASES = new Map([
  ["Region", ["SPSS_Region", "Region", "City_1"]],
  ["D3", ["SPSS_D3", "D3", "D3_Q"]],
  ["D5", ["D5", "D5_Q", "Working_Status", "Employment_Status"]],
  ["B1", ["B1", "Marital_Status"]],
  ["Gender", ["SPSS_Gender", "Gender"]],
  ["Age_cal", ["SPSS_Age_cal", "Age_cal", "Age", "AGE_GROUP", "AGE_TRACKER"]],
  ["Age", ["Age", "AGE_GROUP", "AGE_TRACKER", "Age_cal", "SPSS_Age_cal"]],
  ["SEC", ["SPSS_SEC", "SEC"]],
  ["Week", ["SPSS_Week", "Week"]],
]);
const SPSS_CITY_CODE_LABELS = new Map([
  ["1", "Lagos"],
  ["2", "Ibadan"],
  ["3", "Abuja"],
  ["4", "Kano"],
  ["5", "Kaduna"],
  ["6", "PHC"],
  ["7", "Benin"],
  ["8", "Onitsha"],
  ["9", "Enugu"],
  ["10", "Owerri"],
  ["11", "Jos"],
  ["12", "Uyo"],
  ["13", "Ilorin"],
  ["14", "Sokoto"],
  ["15", "Warri"],
]);
let exportTableSpecsCache = null;
let exportFactColumnsCache = null;
let spssExportTablesReady = false;
let exportXlsformMetadataCache = null;
const EXPORT_CUSTOM_TABLE_BORDER = { style: "thin", color: { rgb: "4B5563" } };
const EXPORT_STYLES = {
  title: {
    font: { bold: true, sz: 14, color: { rgb: "1E293B" } },
    alignment: { horizontal: "left", vertical: "center", wrapText: true },
  },
  header: {
    fill: { patternType: "solid", fgColor: { rgb: "E6EEF7" } },
    font: { bold: true, color: { rgb: "1E293B" } },
    alignment: { horizontal: "center", vertical: "center", wrapText: true },
    border: { top: EXPORT_CUSTOM_TABLE_BORDER, bottom: EXPORT_CUSTOM_TABLE_BORDER, left: EXPORT_CUSTOM_TABLE_BORDER, right: EXPORT_CUSTOM_TABLE_BORDER },
  },
  subHeader: {
    fill: { patternType: "solid", fgColor: { rgb: "F4F7FB" } },
    font: { bold: true, color: { rgb: "1E293B" } },
    alignment: { horizontal: "center", vertical: "center", wrapText: true },
    border: { top: EXPORT_CUSTOM_TABLE_BORDER, bottom: EXPORT_CUSTOM_TABLE_BORDER, left: EXPORT_CUSTOM_TABLE_BORDER, right: EXPORT_CUSTOM_TABLE_BORDER },
  },
  label: {
    font: { bold: true, color: { rgb: "1E293B" } },
    alignment: { horizontal: "left", vertical: "center", wrapText: true },
    border: { top: EXPORT_CUSTOM_TABLE_BORDER, bottom: EXPORT_CUSTOM_TABLE_BORDER, left: EXPORT_CUSTOM_TABLE_BORDER, right: EXPORT_CUSTOM_TABLE_BORDER },
  },
  value: {
    alignment: { horizontal: "center", vertical: "center" },
    border: { top: EXPORT_CUSTOM_TABLE_BORDER, bottom: EXPORT_CUSTOM_TABLE_BORDER, left: EXPORT_CUSTOM_TABLE_BORDER, right: EXPORT_CUSTOM_TABLE_BORDER },
  },
  basePct: {
    font: { bold: true, color: { rgb: "1E293B" } },
    alignment: { horizontal: "center", vertical: "center" },
    border: { top: EXPORT_CUSTOM_TABLE_BORDER, bottom: EXPORT_CUSTOM_TABLE_BORDER, left: EXPORT_CUSTOM_TABLE_BORDER, right: EXPORT_CUSTOM_TABLE_BORDER },
  },
};

function parseExportMonth(value) {
  const raw = compactLabel(value);
  const iso = raw.match(/^(\d{4})-(\d{1,2})(?:-\d{1,2})?$/);
  if (iso) {
    const month = Number(iso[2]);
    if (month >= 1 && month <= 12) return { year: Number(iso[1]), month };
  }
  const monthName = raw.match(/^([A-Za-z]{3,9})[-\s_]+(\d{4})$/);
  if (monthName) {
    const idx = EXPORT_MONTH_NAMES.findIndex((name) => name.toLowerCase() === monthName[1].slice(0, 3).toLowerCase());
    if (idx >= 0) return { year: Number(monthName[2]), month: idx + 1 };
  }
  return null;
}

function exportMonthKey(monthInfo) {
  if (!monthInfo) return "";
  return `${monthInfo.year}-${String(monthInfo.month).padStart(2, "0")}`;
}

function currentExportMonthInfo() {
  const now = new Date();
  return { year: now.getUTCFullYear(), month: now.getUTCMonth() + 1 };
}

function exportMonthLabel(monthInfo) {
  if (!monthInfo) return "";
  return `${EXPORT_MONTH_NAMES[monthInfo.month - 1]}-${monthInfo.year}`;
}

function exportFilenameMonthLabel(monthInfo) {
  if (!monthInfo) return "UNKNOWN";
  return `${EXPORT_MONTH_NAMES[monthInfo.month - 1].toUpperCase()}_${monthInfo.year}`;
}

function exportFilenameMultiMonthLabel(months) {
  const parsed = months.map((month) => parseExportMonth(month)).filter(Boolean);
  if (!parsed.length) return "UNKNOWN";
  const first = parsed[0];
  const last = parsed[parsed.length - 1];
  if (first.year === last.year) {
    return `${EXPORT_MONTH_NAMES[first.month - 1].toUpperCase()}_${EXPORT_MONTH_NAMES[last.month - 1].toUpperCase()}_${last.year}`;
  }
  return `${exportFilenameMonthLabel(first)}_${exportFilenameMonthLabel(last)}`;
}

function quarterInfoFromMonths(months) {
  const parsed = months.map((month) => parseExportMonth(month)).filter(Boolean);
  if (parsed.length !== 3) return null;
  parsed.sort((a, b) => exportMonthKey(a).localeCompare(exportMonthKey(b)));
  const [first, second, third] = parsed;
  if (first.year !== second.year || first.year !== third.year) return null;
  if (second.month !== first.month + 1 || third.month !== second.month + 1) return null;
  if (![1, 4, 7, 10].includes(first.month)) return null;
  return { year: first.year, quarter: Math.floor((first.month - 1) / 3) + 1 };
}

function exportSlugLabel(slug) {
  return EXPORT_CATEGORY_LABELS.get(normalizeExportSlug(slug)) || compactLabel(slug).replace(/-/g, " ");
}

function normalizeExportSlug(slug) {
  const value = String(slug || "").trim().toLowerCase();
  if (value === "breakfast-cereal") return "breakfast-cereals";
  if (value === "condiment-mix") return "condiment-mixes";
  return value;
}

function exportFilenameSlug(scope, slug) {
  return scope === "all"
    ? "ALL_CATEGORIES"
    : exportSlugLabel(slug).replace(/&/g, "AND").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
}

function readExportManifest() {
  const manifest = readJsonFile(EXPORT_MANIFEST_PATH, { records: [] });
  return {
    records: Array.isArray(manifest?.records) ? manifest.records : [],
  };
}

function writeExportManifest(manifest) {
  writeJsonFile(EXPORT_MANIFEST_PATH, {
    records: Array.isArray(manifest?.records) ? manifest.records : [],
  });
}

async function getMarketExportMonths(limit = 3) {
  await ensureInitialized();
  const relation = path.resolve(MARKET_DB_PATH) === path.resolve(DB_PATH)
    ? "respondent_dims"
    : "market_db.main.respondent_dims";
  try {
    const rows = await all(`
      SELECT DISTINCT CAST(month AS VARCHAR) AS month
      FROM ${relation}
      WHERE NULLIF(TRIM(CAST(month AS VARCHAR)), '') IS NOT NULL
    `);
    return rows
      .map((row) => compactLabel(row.month))
      .filter(Boolean)
      .sort((a, b) => exportMonthKey(parseExportMonth(b)).localeCompare(exportMonthKey(parseExportMonth(a))))
      .slice(0, limit);
  } catch (err) {
    console.warn(`[backend] export market month lookup failed: ${err && err.message ? err.message : String(err)}`);
    const rows = await all(`
      SELECT DISTINCT CAST(month AS VARCHAR) AS month
      FROM respondent_dims
      WHERE NULLIF(TRIM(CAST(month AS VARCHAR)), '') IS NOT NULL
    `);
    return rows
      .map((row) => compactLabel(row.month))
      .filter(Boolean)
      .sort((a, b) => exportMonthKey(parseExportMonth(b)).localeCompare(exportMonthKey(parseExportMonth(a))))
      .slice(0, limit);
  }
}

async function getCurrentExportMonth() {
  const state = readJsonFile(SYNC_STATE_PATH, {});
  const result = readJsonFile(SYNC_RESULT_PATH, {});
  const synced = parseExportMonth(state?.latest_month || result?.latestMonth || "");
  return synced || currentExportMonthInfo();
}

async function resolveExportPeriod(type) {
  if (type === "interim") {
    const monthInfo = await getCurrentExportMonth();
    return {
      months: [exportMonthKey(monthInfo)],
      display: exportMonthLabel(monthInfo),
      filenamePeriod: exportFilenameMonthLabel(monthInfo),
      scheduleDay: 15,
      active: true,
    };
  }

  const months = await getMarketExportMonths(type === "full" ? 1 : 3);
  if (type === "full") {
    const latest = parseExportMonth(months[0]);
    return {
      months: latest ? [exportMonthKey(latest)] : [],
      display: latest ? exportMonthLabel(latest) : "",
      filenamePeriod: exportFilenameMonthLabel(latest),
      scheduleDay: 3,
      active: Boolean(latest),
    };
  }
  if (type === "rolling") {
    const rollingMonths = months.slice(0, 2).reverse();
    return {
      months: rollingMonths,
      display: rollingMonths.map((month) => exportMonthLabel(parseExportMonth(month))).join(" to "),
      filenamePeriod: exportFilenameMultiMonthLabel(rollingMonths),
      scheduleDay: 3,
      active: rollingMonths.length === 2,
    };
  }

  const quarterMonths = months.slice(0, 3).reverse();
  const quarter = quarterInfoFromMonths(quarterMonths);
  return {
    months: quarterMonths,
    display: quarter ? `Q${quarter.quarter} ${quarter.year}` : "",
    filenamePeriod: quarter ? `Q${quarter.quarter}_${quarter.year}` : "INACTIVE",
    scheduleDay: 3,
    active: Boolean(quarter),
    quarter,
  };
}

function buildExportFilename(type, period, scope, slug) {
  const typeLabel = String(type || "").toUpperCase();
  return `BHT_${typeLabel}_DATA_TABLES_${period.filenamePeriod}_${exportFilenameSlug(scope, slug)}.xlsx`;
}

function getExportCategories(scope, slug) {
  if (scope === "all") return Array.from(EXPORT_CATEGORY_LABELS.keys());
  const normalizedSlug = normalizeExportSlug(slug);
  return EXPORT_CATEGORY_LABELS.has(normalizedSlug) ? [normalizedSlug] : [];
}

function appendJsonSheet(workbook, sheetName, rows) {
  const safeSheetName = sheetName.slice(0, 31);
  if (workbook.SheetNames.includes(safeSheetName)) {
    const idx = workbook.SheetNames.indexOf(safeSheetName);
    workbook.SheetNames.splice(idx, 1);
    delete workbook.Sheets[safeSheetName];
  }
  const sheet = XLSX.utils.json_to_sheet(rows);
  XLSX.utils.book_append_sheet(workbook, sheet, safeSheetName);
}

function safeDiagnosticJson(value) {
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value);
  } catch (_err) {
    return String(value);
  }
}

function buildExportDiagnosticsRows({ type, scope, slug, period, builtBySlug, registryDiagnostics, autorecodeSnapshot }) {
  const rows = [{
    section: "export",
    type,
    scope,
    slug: scope === "all" ? "" : slug,
    months: Array.isArray(period?.months) ? period.months.join(",") : "",
    displayPeriod: period?.display || "",
    generatedAt: new Date().toISOString(),
  }];

  for (const [categorySlug, builtSheets] of builtBySlug.entries()) {
    rows.push({
      section: "category",
      categorySlug,
      percentSheet: builtSheets.percentSheetName || "",
      countSheet: builtSheets.countSheetName || "",
      tableCount: Array.isArray(builtSheets.navigationRows) ? builtSheets.navigationRows.length : 0,
      registryOverlay: safeDiagnosticJson(builtSheets.registryOverlay || null),
    });
    for (const table of builtSheets.navigationRows || []) {
      rows.push({
        section: "table",
        categorySlug,
        percentSheet: builtSheets.percentSheetName || "",
        countSheet: builtSheets.countSheetName || "",
        questionCode: table.questionCode || "",
        title: table.title || "",
        base: table.base ?? "",
        percentStart: table.percentStart ?? "",
        percentEnd: table.percentEnd ?? "",
        countStart: table.countStart ?? "",
        countEnd: table.countEnd ?? "",
        registryGenerated: Boolean(table.registryGenerated),
        registryAugmented: Boolean(table.registryAugmented),
      });
    }
  }

  for (const diagnostic of registryDiagnostics || []) {
    rows.push({
      section: "registry",
      categorySlug: diagnostic.categorySlug || "",
      category: diagnostic.category || "",
      type: diagnostic.type || type,
      months: Array.isArray(diagnostic.months) ? diagnostic.months.join(",") : "",
      registryPeriod: diagnostic.registryPeriod || "",
      brandUniverseCount: diagnostic.brandUniverseCount ?? "",
      questionUniverseCount: diagnostic.questionUniverseCount ?? "",
      unresolvedMetadataWarnings: Array.isArray(diagnostic.unresolvedMetadata) ? diagnostic.unresolvedMetadata.length : "",
      error: diagnostic.error || "",
    });
  }

  rows.push({
    section: "autorecode",
    ok: autorecodeSnapshot?.ok ?? "",
    period: autorecodeSnapshot?.period || "",
    mappingVersions: Array.isArray(autorecodeSnapshot?.versions) ? autorecodeSnapshot.versions.length : "",
    details: safeDiagnosticJson(autorecodeSnapshot || null),
  });

  return rows;
}

function loadExportTableSpecs() {
  if (exportTableSpecsCache) return exportTableSpecsCache;
  const payload = readJsonFile(SPSS_EXPORT_RULES_PATH, null) || readJsonFile(EXPORT_TABLE_SPECS_PATH, null);
  exportTableSpecsCache = payload && payload.categories ? payload : { categories: {} };
  return exportTableSpecsCache;
}

function cloneExportSpec(value) {
  return JSON.parse(JSON.stringify(value || {}));
}

function metadataRegistryPaths() {
  return {
    exportSpecsPath: EXPORT_TABLE_SPECS_PATH,
    spssRulesPath: SPSS_EXPORT_RULES_PATH,
    xlsformMetadataPath: XLSFORM_EXPORT_METADATA_PATH,
  };
}

function metadataRegistryDbApi() {
  return { run, all };
}

async function ensureMetadataRegistryReady() {
  await ensureInitialized();
  await metadataRegistry.ensureMetadataRegistrySchema(metadataRegistryDbApi());
}

function registryQuestionToExportBlock(question) {
  const variable = compactLabel(question.variable);
  if (!variable) return null;
  const answers = Array.isArray(question.responseOptions)
    ? question.responseOptions
      .map((option) => compactLabel(option.option_label))
      .filter(Boolean)
    : [];
  const title = compactLabel(question.title || question.label || variable);
  const breakVariables = Array.isArray(question.breaks) && question.breaks.length
    ? question.breaks
      .map((value) => {
        const key = String(value || "").toUpperCase();
        const aliases = { TOTAL: "", REGION: "REGION", REGION2: "REGION2", D3: "D3", INCOME: "D3", GENDER: "GENDER", AGE: "AGE2", AGE2: "AGE2", SEC: "SEC2", SEC2: "SEC2", WEEK: "WEEK" };
        return aliases[key] ?? key;
      })
      .filter(Boolean)
    : ["REGION", "REGION2", "D3", "GENDER", "AGE2", "SEC2", "WEEK"];
  return {
    title: title.startsWith(`${variable}.`) ? title : `${variable}. ${title}`,
    questionCode: variable,
    answers,
    tableVar: variable,
    tableVars: [variable, "TOTALS"],
    variables: `${variable} + TOTALS`,
    variableExact: [variable, "TOTALS"],
    registryGenerated: true,
    spss: {
      title,
      rowVariable: variable,
      breakVariables,
      filter: compactLabel(question.filterExpression || ""),
      statistics: {
        columnPercent: !String(question.tableType || "").toLowerCase().includes("row"),
        count: true,
      },
    },
  };
}

function blockLooksBrandDriven(block) {
  const text = `${block?.questionCode || ""} ${block?.title || ""} ${block?.tableVar || ""}`;
  if (!Array.isArray(block?.answers) || !block.answers.length) return false;
  if (/_BAU(?:1A|1B|1C|1D|2|3|4|5|AB|CD)/i.test(text)) return true;
  return /\bbrand|awareness|advert|preferred/i.test(text);
}

async function buildRegistryAugmentedExportSpec(categorySlug, category, months, type) {
  const baseSpec = cloneExportSpec(loadExportTableSpecs().categories?.[categorySlug]);
  if (!baseSpec || !Array.isArray(baseSpec.blocks)) return baseSpec;
  let generated = null;
  try {
    generated = await metadataRegistry.generatedExportSpec(metadataRegistryDbApi(), { category, months, type });
  } catch (err) {
    console.warn(`[metadata] registry export-spec overlay skipped for ${categorySlug}: ${err.message}`);
    return baseSpec;
  }

  const overlayBrands = (generated.brandUniverse || [])
    .filter((brand) => !["export_specs", "spss_rules"].includes(String(brand.source || "")))
    .map((brand) => ({ ...brand, label: compactLabel(brand.label) }))
    .filter((brand) => brand.label);
  const appendedBrandRows = [];
  if (overlayBrands.length) {
    for (const block of baseSpec.blocks) {
      if (!blockLooksBrandDriven(block)) continue;
      const seen = new Set((block.answers || []).map(normalizeExportOptionKey));
      let blockChanged = false;
      for (const brand of overlayBrands) {
        const key = normalizeExportOptionKey(brand.label);
        if (!key || seen.has(key)) continue;
        block.answers.push(brand.label);
        seen.add(key);
        blockChanged = true;
        appendedBrandRows.push({
          block: block.questionCode || block.title || "",
          label: brand.label,
          code: brand.code || "",
          activeMonths: brand.activeMonths || [],
        });
      }
      if (blockChanged) block.registryAugmented = true;
    }
  }

  const existingQuestions = new Set(baseSpec.blocks.map((block) => compactLabel(block.questionCode || block.tableVar).toLowerCase()).filter(Boolean));
  const newQuestionBlocks = (generated.generatedTables || [])
    .filter((question) => !["export_specs", "spss_rules"].includes(String(question.source || "")))
    .filter((question) => !existingQuestions.has(compactLabel(question.variable).toLowerCase()))
    .map(registryQuestionToExportBlock)
    .filter(Boolean);
  if (newQuestionBlocks.length) {
    baseSpec.blocks.push(...newQuestionBlocks);
  }
  baseSpec.registryOverlay = {
    brandRowsAdded: appendedBrandRows.length,
    questionBlocksAdded: newQuestionBlocks.length,
    overlayBrandCount: overlayBrands.length,
    overlayQuestionCount: newQuestionBlocks.length,
    registryPeriod: generated.registryPeriod || null,
    months: generated.months || months || [],
    diagnostics: generated.diagnostics || {},
    appendedBrandRows,
    appendedQuestions: newQuestionBlocks.map((block) => ({
      questionCode: block.questionCode || "",
      title: block.title || "",
      answerCount: Array.isArray(block.answers) ? block.answers.length : 0,
    })),
  };
  return baseSpec;
}

async function getExportFactColumns() {
  if (exportFactColumnsCache) return exportFactColumnsCache;
  exportFactColumnsCache = await getTableColumns("responses_fact");
  return exportFactColumnsCache;
}

function exportCreateTablePrefix() {
  return activeDbIsReadOnlyMarketDb() ? "CREATE OR REPLACE TEMP TABLE" : "CREATE OR REPLACE TABLE";
}

async function insertSpssRuleRows(tableName, columns, rows, batchSize = 400) {
  if (!rows.length) return;
  for (let offset = 0; offset < rows.length; offset += batchSize) {
    const batch = rows.slice(offset, offset + batchSize);
    const valuesSql = batch.map((row) => `(${row.map((value) => value == null ? "NULL" : quote(value)).join(", ")})`).join(",\n");
    await run(`INSERT INTO ${quoteIdentifier(tableName)} (${columns.map(quoteIdentifier).join(", ")}) VALUES ${valuesSql}`);
  }
}

async function ensureSpssExportTables() {
  await ensureRespondentDims();
  if (spssExportTablesReady && await tableExists("spss_case_values")) return;
  const rules = loadExportTableSpecs();
  const createPrefix = exportCreateTablePrefix();

  if (!(await tableExists("spss_case_values"))) {
    const factColumns = new Set((await getTableColumns("responses_fact")).map((value) => String(value).toLowerCase()));
    const answerValue = factColumns.has("answer_value") ? "CAST(r.answer_value AS VARCHAR)" : "CAST(r.answer_label AS VARCHAR)";
    const answerNum = factColumns.has("answer_value_num") ? "CAST(r.answer_value_num AS DOUBLE)" : `TRY_CAST(${answerValue} AS DOUBLE)`;
    const answerLabel = factColumns.has("answer_label") ? "CAST(r.answer_label AS VARCHAR)" : answerValue;
    await run(`
      ${createPrefix} spss_case_values AS
      SELECT
        CAST(r.category AS VARCHAR) AS category,
        CAST(r.respondent_id AS VARCHAR) AS respondent_id,
        CAST(r.month AS VARCHAR) AS month,
        CAST(r.question AS VARCHAR) AS variable,
        ${answerValue} AS value_text,
        ${answerNum} AS value_num,
        ${answerLabel} AS value_label
      FROM responses_fact r
      WHERE NULLIF(TRIM(CAST(r.question AS VARCHAR)), '') IS NOT NULL
    `);

    const spssAliases = new Map(Array.from(SPSS_DIMENSION_ALIASES.entries())
      .map(([target, aliases]) => [target, aliases.filter((alias) => spssVariableKey(alias) !== spssVariableKey(target))]));
    for (const [target, aliases] of spssAliases.entries()) {
      for (const alias of aliases) {
        await run(`
          INSERT INTO spss_case_values
          SELECT v.category, v.respondent_id, v.month, ${quote(target)}, v.value_text, v.value_num, v.value_label
          FROM spss_case_values v
          WHERE lower(v.variable)=lower(${quote(alias)})
            AND NOT EXISTS (
              SELECT 1 FROM spss_case_values x
              WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
                AND lower(x.variable)=lower(${quote(target)})
            )
        `);
      }
    }

    const dims = await getTableColumns("respondent_dims");
    for (const [target, candidates] of SPSS_DIMENSION_ALIASES.entries()) {
      const actual = candidates
        .map((candidate) => dims.find((column) => spssVariableKey(column) === spssVariableKey(candidate)))
        .find(Boolean);
      if (!actual) continue;
      await run(`
        INSERT INTO spss_case_values
        SELECT CAST(d.category AS VARCHAR), CAST(d.respondent_id AS VARCHAR), CAST(d.month AS VARCHAR),
               ${quote(target)}, CAST(d.${quoteIdentifier(actual)} AS VARCHAR),
               TRY_CAST(d.${quoteIdentifier(actual)} AS DOUBLE), CAST(d.${quoteIdentifier(actual)} AS VARCHAR)
        FROM respondent_dims d
        WHERE NULLIF(TRIM(CAST(d.${quoteIdentifier(actual)} AS VARCHAR)), '') IS NOT NULL
          AND lower(TRIM(CAST(d.${quoteIdentifier(actual)} AS VARCHAR))) NOT IN ('nan', 'null', 'undefined')
          AND NOT EXISTS (
            SELECT 1 FROM spss_case_values s
            WHERE s.category=CAST(d.category AS VARCHAR)
              AND s.respondent_id=CAST(d.respondent_id AS VARCHAR)
              AND s.month=CAST(d.month AS VARCHAR)
              AND lower(s.variable)=lower(${quote(target)})
          )
      `);
    }
  }

  if (!(await tableExists("spss_region2_map"))) {
    await run(`${createPrefix} spss_region2_map(source_value VARCHAR, target_code INTEGER, target_label VARCHAR)`);
    const region2 = rules.region2 || {};
    const rows = [];
    const seenRegionSources = new Set();
    for (const [source, target] of Object.entries(region2.map || {})) {
      const targetCode = Number(target);
      const targetLabel = (region2.labels || {})[String(target)] || String(target);
      for (const sourceValue of [source, SPSS_CITY_CODE_LABELS.get(String(source))]) {
        const normalized = compactLabel(sourceValue);
        const key = normalizeExportOptionKey(normalized);
        if (!normalized || seenRegionSources.has(key)) continue;
        seenRegionSources.add(key);
        rows.push([normalized, targetCode, targetLabel]);
      }
    }
    await insertSpssRuleRows("spss_region2_map", ["source_value", "target_code", "target_label"], rows);
    if (rows.length) {
      await run(`
        INSERT INTO spss_case_values
        SELECT v.category, v.respondent_id, v.month, 'REGION2',
               CAST(m.target_code AS VARCHAR), CAST(m.target_code AS DOUBLE), m.target_label
        FROM spss_case_values v
        JOIN spss_region2_map m
          ON lower(TRIM(COALESCE(CAST(TRY_CAST(v.value_num AS BIGINT) AS VARCHAR), TRIM(v.value_text))))=lower(TRIM(m.source_value))
        WHERE lower(v.variable)='region'
          AND NOT EXISTS (
            SELECT 1 FROM spss_case_values x
            WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
              AND lower(x.variable)='region2'
          )
      `);
    }
  }

  if (!(await tableExists("spss_bau4_rules"))) {
    await run(`${createPrefix} spss_bau4_rules(target VARCHAR, source1 VARCHAR, source2 VARCHAR, value_label VARCHAR)`);
    const labels = rules.variableLabels || {};
    const rows = (rules.bau4 || []).filter((rule) => rule?.target && Array.isArray(rule.sources) && rule.sources.length >= 2)
      .map((rule) => [rule.target, rule.sources[0], rule.sources[1], labels[rule.target] || rule.target]);
    await insertSpssRuleRows("spss_bau4_rules", ["target", "source1", "source2", "value_label"], rows);
    if (rows.length) {
      await run(`
        INSERT INTO spss_case_values
        SELECT DISTINCT v.category, v.respondent_id, v.month, r.target, '1', 1.0, r.value_label
        FROM spss_bau4_rules r
        JOIN spss_case_values v
          ON lower(v.variable) IN (lower(r.source1), lower(r.source2))
         AND COALESCE(v.value_num, TRY_CAST(v.value_text AS DOUBLE))=1
        WHERE NOT EXISTS (
          SELECT 1 FROM spss_case_values x
          WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
            AND lower(x.variable)=lower(r.target)
        )
      `);
    }
  }

  if (!(await tableExists("spss_autorecode_maps"))) {
    await run(`${createPrefix} spss_autorecode_maps(source_variable VARCHAR, target_variable VARCHAR, source_value VARCHAR, source_label VARCHAR, target_code INTEGER)`);
    for (const autorecode of rules.autorecode || []) {
      const source = compactLabel(autorecode.source);
      const target = compactLabel(autorecode.target);
      if (!source || !target) continue;
      await run(`
        INSERT INTO spss_autorecode_maps
        WITH distinct_values AS (
          SELECT CASE WHEN value_num IS NOT NULL THEN printf('%.15g', value_num) ELSE CAST(value_text AS VARCHAR) END AS source_value,
                 MIN(value_num) AS numeric_value,
                 COALESCE(MAX(NULLIF(TRIM(value_label), '')), MAX(CAST(value_text AS VARCHAR))) AS source_label
          FROM spss_case_values
          WHERE lower(variable)=lower(${quote(source)}) AND NULLIF(TRIM(value_text), '') IS NOT NULL
          GROUP BY CASE WHEN value_num IS NOT NULL THEN printf('%.15g', value_num) ELSE CAST(value_text AS VARCHAR) END
        )
        SELECT ${quote(source)}, ${quote(target)}, source_value, source_label,
               ROW_NUMBER() OVER (ORDER BY CASE WHEN numeric_value IS NULL THEN 1 ELSE 0 END, numeric_value, lower(source_value), source_value)
        FROM distinct_values
      `);
      await run(`
        INSERT INTO spss_case_values
        SELECT v.category, v.respondent_id, v.month, m.target_variable,
               CAST(m.target_code AS VARCHAR), CAST(m.target_code AS DOUBLE), m.source_label
        FROM spss_case_values v
        JOIN spss_autorecode_maps m
          ON lower(v.variable)=lower(m.source_variable)
         AND (CASE WHEN v.value_num IS NOT NULL THEN printf('%.15g', v.value_num) ELSE v.value_text END)=m.source_value
        WHERE lower(m.source_variable)=lower(${quote(source)}) AND lower(m.target_variable)=lower(${quote(target)})
          AND NOT EXISTS (
            SELECT 1 FROM spss_case_values x
            WHERE x.category=v.category AND x.respondent_id=v.respondent_id AND x.month=v.month
              AND lower(x.variable)=lower(m.target_variable)
          )
      `);
    }
  }

  if (!(await tableExists("spss_variable_dictionary"))) {
    await run(`
      ${createPrefix} spss_variable_dictionary AS
      SELECT category, variable, value_text, value_num,
             COALESCE(MAX(NULLIF(TRIM(value_label), '')), value_text) AS value_label
      FROM spss_case_values
      WHERE NULLIF(TRIM(variable), '') IS NOT NULL AND NULLIF(TRIM(value_text), '') IS NOT NULL
      GROUP BY category, variable, value_text, value_num
    `);
  }
  spssExportTablesReady = true;
}

function normalizeExportCellValue(value) {
  return compactLabel(value).toLowerCase();
}

function escapeExportRegex(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function exportVariablePrefixRegex(prefix) {
  const rawPrefix = compactLabel(prefix);
  if (!rawPrefix) return "";
  const hasTrailingSeparator = /[._]$/.test(rawPrefix);
  const noTrailing = rawPrefix.replace(/[._]+$/, "");
  const escaped = Array.from(noTrailing)
    .map((char) => (/[._]/.test(char) ? "[._]" : escapeExportRegex(char)))
    .join("");
  return `(?i)^${escaped}${hasTrailingSeparator ? "[._]" : "[._]?"}[0-9A-Za-z]*`;
}

function exportDimensionVariable(block) {
  const candidates = [
    ...(Array.isArray(block?.variableExact) ? block.variableExact : []),
    compactLabel(block?.tableVar),
    compactLabel(block?.questionCode),
  ].map((value) => compactLabel(value).toLowerCase());
  if (candidates.some((value) => ["d5", "employment", "working status", "employment status"].includes(value))) {
    return respondentDimColumns.includes("D5") ? "D5" : null;
  }
  if (candidates.some((value) => ["b1", "marital", "marital status"].includes(value))) {
    return respondentDimColumns.includes("B1") ? "B1" : null;
  }
  if (candidates.some((value) => ["d3", "income", "annual income"].includes(value))) {
    return respondentDimColumns.includes("D3") ? "D3" : null;
  }
  return null;
}

function normalizeExportOptionKey(value) {
  return normalizeExportCellValue(value)
    .replace(/[’']/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function stripExportQuestionCodePrefix(value) {
  return compactLabel(value).replace(/^[A-Za-z0-9_$]+(?:_[A-Za-z0-9]+)*\.\s*/, "");
}

function isPositiveExportAnswerText(value) {
  const tokens = new Set(normalizeExportCellValue(value).split(/[^a-z0-9]+/i).filter(Boolean));
  return ["1", "yes", "selected", "mentioned", "true"].some((token) => tokens.has(token));
}

function isLikelyPositiveFlagOnly(value) {
  const normalized = normalizeExportOptionKey(value);
  return !normalized || ["1", "yes", "selected", "mentioned", "true"].includes(normalized);
}

function exportAnswerByNumericCode(code, referenceAnswers = []) {
  const numericCode = Number.parseInt(compactLabel(code), 10);
  if (!Number.isFinite(numericCode)) return "";
  if (numericCode === 98) {
    return referenceAnswers.find((answer) => /^others?$/i.test(normalizeExportOptionKey(answer))) || "";
  }
  if (numericCode === 99) {
    return referenceAnswers.find((answer) => /^none(?: of these)?$/i.test(normalizeExportOptionKey(answer))) || "";
  }
  return referenceAnswers[numericCode - 1] || "";
}

function exportAnswerFromQuestionSuffix(question, block) {
  const referenceAnswers = Array.isArray(block?.answers) ? block.answers.map(compactLabel).filter(Boolean) : [];
  if (!referenceAnswers.length) return "";
  const questionText = compactLabel(question);
  const prefixes = Array.isArray(block?.variablePrefixes) ? block.variablePrefixes : [];
  for (const prefix of prefixes) {
    const prefixPattern = exportVariablePrefixRegex(prefix);
    if (!prefixPattern) continue;
    const match = questionText.match(new RegExp(prefixPattern.replace(/^\(\?i\)/, ""), "i"));
    if (!match) continue;
    const suffix = questionText.slice(match[0].length).match(/^(\d+)/)?.[1];
    const answer = exportAnswerByNumericCode(suffix, referenceAnswers);
    if (answer) return answer;
  }
  const fallbackSuffix = questionText.match(/[._](\d+)\s*$/)?.[1];
  return exportAnswerByNumericCode(fallbackSuffix, referenceAnswers);
}

function exportDerivedTotalSpec(block) {
  const prefixes = Array.isArray(block?.variablePrefixes) ? block.variablePrefixes.map(compactLabel) : [];
  const prefix = prefixes.find((value) => /_BAU(?:AB|CD)[123]?\.$/i.test(value));
  const match = prefix?.match(/^([A-Za-z0-9]+)_BAU(AB|CD)([123])\.$/i);
  if (!match) return null;
  const categoryPrefix = match[1];
  const family = match[2].toUpperCase();
  const level = match[3];
  if (family === "AB" && level === "1") return { prefix: categoryPrefix, single: "BAU1A", multi: ["BAU1B"] };
  if (family === "AB" && level === "2") return { prefix: categoryPrefix, single: "BAU1A", multi: ["BAU1B", "BAU2"] };
  if (family === "CD" && level === "1") return { prefix: categoryPrefix, single: "BAU1C", multi: ["BAU1D"] };
  if (family === "CD" && level === "3") return { prefix: categoryPrefix, single: "BAU1C", multi: ["BAU1D", "BAU3"] };
  return null;
}

function exportQuestionPredicateSql(block, factColumns = []) {
  const code = block.questionCode;
  const title = block.title;
  const normalizedCode = compactLabel(code);
  const titlePrefix = compactLabel(title).split(".")[0] || normalizedCode;
  const exactCandidates = [
    ...(Array.isArray(block.variableExact) ? block.variableExact : []),
    normalizedCode,
    titlePrefix,
  ];
  const prefixCandidates = Array.isArray(block.variablePrefixes) ? block.variablePrefixes : [];
  if (!prefixCandidates.length && /^\$/.test(compactLabel(block.tableVar)) && /_QF[HWSB]*2$/i.test(normalizedCode)) {
    prefixCandidates.push(`${normalizedCode}.`);
  }
  const candidates = Array.from(new Set(exactCandidates.map(compactLabel).filter(Boolean)));
  const prefixes = Array.from(new Set(prefixCandidates.map(compactLabel).filter(Boolean)));
  if (!candidates.length) return "1=0";
  const exactSql = candidates.map((value) => quote(value.toUpperCase())).join(", ");
  const columnSet = new Set(factColumns.map((col) => String(col).toLowerCase()));
  const predicates = [];
  if (columnSet.has("question")) predicates.push(`UPPER(CAST(r.question AS VARCHAR)) IN (${exactSql})`);
  if (columnSet.has("question_raw")) predicates.push(`UPPER(CAST(COALESCE(r.question_raw, '') AS VARCHAR)) IN (${exactSql})`);
  for (const prefix of prefixes) {
    const prefixPattern = exportVariablePrefixRegex(prefix);
    if (!prefixPattern) continue;
    if (columnSet.has("question")) predicates.push(`regexp_matches(CAST(r.question AS VARCHAR), ${quote(prefixPattern)})`);
    if (columnSet.has("question_raw")) predicates.push(`regexp_matches(CAST(COALESCE(r.question_raw, '') AS VARCHAR), ${quote(prefixPattern)})`);
    const bau4SurveyCtoPrefix = prefix.match(/^bau4_([A-Za-z0-9]+)_(\d+)_$/i);
    if (bau4SurveyCtoPrefix) {
      const legacyPattern = `(?i)^${escapeExportRegex(bau4SurveyCtoPrefix[1])}[._]BAU4[._]${escapeExportRegex(bau4SurveyCtoPrefix[2])}(?:[._]1)?[._]?[0-9A-Za-z]*`;
      if (columnSet.has("question")) predicates.push(`regexp_matches(CAST(r.question AS VARCHAR), ${quote(legacyPattern)})`);
      if (columnSet.has("question_raw")) predicates.push(`regexp_matches(CAST(COALESCE(r.question_raw, '') AS VARCHAR), ${quote(legacyPattern)})`);
    }
  }
  if (columnSet.has("question_label")) {
    predicates.push(candidates.map((value) => `UPPER(CAST(r.question_label AS VARCHAR)) LIKE ${quote(`${value.toUpperCase()}.%`)}`).join(" OR "));
  }
  return predicates.length ? `(${predicates.join(" OR ")})` : "1=0";
}

function exportMonthFilterSql(months, alias = "r") {
  const monthList = Array.from(new Set((months || []).map((month) => compactLabel(month)).filter(Boolean)));
  if (!monthList.length) return "";
  return `AND CAST(${alias}.month AS VARCHAR) IN (${monthList.map(quote).join(", ")})`;
}

async function getExportBreakColumns(category, months) {
  const monthFilter = exportMonthFilterSql(months, "d");
  const columns = [{ group: "Total", label: "Total", field: null, value: null }];
  for (const breakField of EXPORT_BREAK_FIELDS.filter((item) => item.field)) {
    if (!respondentDimColumns.includes(breakField.field)) continue;
    const rows = await all(`
      SELECT DISTINCT CAST(${quoteIdentifier(breakField.field)} AS VARCHAR) AS value
      FROM respondent_dims d
      WHERE CAST(d.category AS VARCHAR) = ${quote(category)}
      ${monthFilter}
        AND NULLIF(TRIM(CAST(${quoteIdentifier(breakField.field)} AS VARCHAR)), '') IS NOT NULL
      ORDER BY value
      LIMIT 40
    `);
    rows
      .map((row) => compactLabel(row.value))
      .filter(Boolean)
      .forEach((value) => columns.push({ group: breakField.label, label: value, field: breakField.field, value }));
  }
  return columns;
}

async function getExportDimensionBlockCounts({ category, months, block, breakColumns, dimensionVariable }) {
  const monthFilter = exportMonthFilterSql(months, "d");
  const rows = await all(`
    SELECT
      CAST(d.respondent_id AS VARCHAR) AS respondent_id,
      CAST(d.month AS VARCHAR) AS month,
      ${respondentDimColumns.includes("Region") ? "CAST(d.Region AS VARCHAR)" : "NULL"} AS Region,
      ${respondentDimColumns.includes("D3") ? "CAST(d.D3 AS VARCHAR)" : "NULL"} AS D3,
      ${respondentDimColumns.includes("Gender") ? "CAST(d.Gender AS VARCHAR)" : "NULL"} AS Gender,
      ${respondentDimColumns.includes("Age") ? "CAST(d.Age AS VARCHAR)" : "NULL"} AS Age,
      ${respondentDimColumns.includes("SEC") ? "CAST(d.SEC AS VARCHAR)" : "NULL"} AS SEC,
      ${respondentDimColumns.includes("Week") ? "CAST(d.Week AS VARCHAR)" : "NULL"} AS Week,
      CAST(d.${quoteIdentifier(dimensionVariable)} AS VARCHAR) AS answer
    FROM respondent_dims d
    WHERE CAST(d.category AS VARCHAR) = ${quote(category)}
      ${monthFilter}
      AND NULLIF(TRIM(CAST(d.${quoteIdentifier(dimensionVariable)} AS VARCHAR)), '') IS NOT NULL
  `);

  const referenceAnswers = Array.isArray(block.answers) ? block.answers.map(compactLabel).filter(Boolean) : [];
  const referenceAnswerByKey = new Map(referenceAnswers.map((answer) => [normalizeExportOptionKey(answer), answer]));
  const observedAnswerOrder = [];
  const observedAnswerLabelByKey = new Map();
  const countsByAnswerKey = new Map();
  const countSeenByAnswerKey = new Map();
  const ensureObservedAnswer = (label) => {
    const cleanedLabel = stripExportQuestionCodePrefix(label);
    const key = normalizeExportOptionKey(cleanedLabel);
    if (!key) return null;
    const displayLabel = referenceAnswerByKey.get(key) || cleanedLabel;
    if (!observedAnswerLabelByKey.has(key)) {
      observedAnswerLabelByKey.set(key, displayLabel);
      observedAnswerOrder.push(key);
      countsByAnswerKey.set(key, new Array(breakColumns.length).fill(0));
      countSeenByAnswerKey.set(key, breakColumns.map(() => new Set()));
    }
    return key;
  };

  const bases = new Array(breakColumns.length).fill(0);
  const baseSeen = breakColumns.map(() => new Set());
  for (const row of rows) {
    const respondentKey = `${row.respondent_id}__${row.month}`;
    const observedAnswerKey = ensureObservedAnswer(row.answer);
    breakColumns.forEach((column, idx) => {
      if (column.field && normalizeExportCellValue(row[column.field]) !== normalizeExportCellValue(column.value)) return;
      if (!baseSeen[idx].has(respondentKey)) {
        baseSeen[idx].add(respondentKey);
        bases[idx] += 1;
      }
      if (!observedAnswerKey) return;
      const answerSeen = countSeenByAnswerKey.get(observedAnswerKey)[idx];
      if (!answerSeen.has(respondentKey)) {
        answerSeen.add(respondentKey);
        countsByAnswerKey.get(observedAnswerKey)[idx] += 1;
      }
    });
  }

  const observedAnswerKeys = Array.from(new Set([
    ...referenceAnswers.map((answer) => normalizeExportOptionKey(answer)).filter((key) => observedAnswerLabelByKey.has(key)),
    ...observedAnswerOrder.filter((key) => !referenceAnswerByKey.has(key)),
  ]));
  const answers = observedAnswerKeys.map((key) => observedAnswerLabelByKey.get(key));
  const countsByAnswer = new Map(answers.map((answer, idx) => [answer, countsByAnswerKey.get(observedAnswerKeys[idx]) || new Array(breakColumns.length).fill(0)]));
  return { bases, countsByAnswer, answers };
}

async function getExportDerivedTotalBlockCounts({ category, months, block, breakColumns, derivedSpec }) {
  const factColumns = await getExportFactColumns();
  const factColumnSet = new Set(factColumns.map((col) => String(col).toLowerCase()));
  const monthFilterDims = exportMonthFilterSql(months, "d");
  const monthFilterResponses = exportMonthFilterSql(months, "r");
  const questionExpr = factColumnSet.has("question") ? "CAST(r.question AS VARCHAR)" : "''";
  const questionRawExpr = factColumnSet.has("question_raw") ? "CAST(COALESCE(r.question_raw, r.question) AS VARCHAR)" : questionExpr;
  const answerExpr = factColumnSet.has("answer_label") && factColumnSet.has("answer_value")
    ? "CAST(COALESCE(NULLIF(TRIM(CAST(r.answer_label AS VARCHAR)), ''), r.answer_value) AS VARCHAR)"
    : factColumnSet.has("answer_label")
      ? "CAST(r.answer_label AS VARCHAR)"
      : factColumnSet.has("answer_value")
        ? "CAST(r.answer_value AS VARCHAR)"
        : "''";
  const answerLabelExpr = factColumnSet.has("answer_label") ? "CAST(r.answer_label AS VARCHAR)" : "''";
  const answerValueExpr = factColumnSet.has("answer_value") ? "CAST(r.answer_value AS VARCHAR)" : "''";
  const answerValueNumExpr = factColumnSet.has("answer_value_num") ? "CAST(r.answer_value_num AS DOUBLE)" : "NULL";
  const baseRows = await all(`
    SELECT
      CAST(d.respondent_id AS VARCHAR) AS respondent_id,
      CAST(d.month AS VARCHAR) AS month,
      ${respondentDimColumns.includes("Region") ? "CAST(d.Region AS VARCHAR)" : "NULL"} AS Region,
      ${respondentDimColumns.includes("D3") ? "CAST(d.D3 AS VARCHAR)" : "NULL"} AS D3,
      ${respondentDimColumns.includes("Gender") ? "CAST(d.Gender AS VARCHAR)" : "NULL"} AS Gender,
      ${respondentDimColumns.includes("Age") ? "CAST(d.Age AS VARCHAR)" : "NULL"} AS Age,
      ${respondentDimColumns.includes("SEC") ? "CAST(d.SEC AS VARCHAR)" : "NULL"} AS SEC,
      ${respondentDimColumns.includes("Week") ? "CAST(d.Week AS VARCHAR)" : "NULL"} AS Week
    FROM respondent_dims d
    WHERE CAST(d.category AS VARCHAR) = ${quote(category)}
      ${monthFilterDims}
  `);
  const prefix = escapeExportRegex(derivedSpec.prefix).replace(/_/g, "[._]");
  const singlePattern = `(?i)^${prefix}[._]${escapeExportRegex(derivedSpec.single)}$`;
  const multiPattern = `(?i)^${prefix}[._](?:${derivedSpec.multi.map(escapeExportRegex).join("|")})[._][0-9]+$`;
  const rows = await all(`
    SELECT
      CAST(r.respondent_id AS VARCHAR) AS respondent_id,
      CAST(r.month AS VARCHAR) AS month,
      ${questionExpr} AS question,
      ${questionRawExpr} AS question_raw,
      ${respondentDimColumns.includes("Region") ? "CAST(d.Region AS VARCHAR)" : "NULL"} AS Region,
      ${respondentDimColumns.includes("D3") ? "CAST(d.D3 AS VARCHAR)" : "NULL"} AS D3,
      ${respondentDimColumns.includes("Gender") ? "CAST(d.Gender AS VARCHAR)" : "NULL"} AS Gender,
      ${respondentDimColumns.includes("Age") ? "CAST(d.Age AS VARCHAR)" : "NULL"} AS Age,
      ${respondentDimColumns.includes("SEC") ? "CAST(d.SEC AS VARCHAR)" : "NULL"} AS SEC,
      ${respondentDimColumns.includes("Week") ? "CAST(d.Week AS VARCHAR)" : "NULL"} AS Week,
      ${answerExpr} AS answer,
      ${answerLabelExpr} AS answer_label,
      ${answerValueExpr} AS answer_value,
      ${answerValueNumExpr} AS answer_value_num
    FROM responses_fact r
    LEFT JOIN respondent_dims d
      ON CAST(d.category AS VARCHAR) = CAST(r.category AS VARCHAR)
     AND CAST(d.respondent_id AS VARCHAR) = CAST(r.respondent_id AS VARCHAR)
     AND CAST(d.month AS VARCHAR) = CAST(r.month AS VARCHAR)
    WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
      ${monthFilterResponses}
      AND (
        regexp_matches(${questionExpr}, ${quote(singlePattern)})
        OR regexp_matches(${questionRawExpr}, ${quote(singlePattern)})
        OR regexp_matches(${questionExpr}, ${quote(multiPattern)})
        OR regexp_matches(${questionRawExpr}, ${quote(multiPattern)})
      )
  `);

  const referenceAnswers = Array.isArray(block.answers) ? block.answers.map(compactLabel).filter(Boolean) : [];
  const referenceAnswerByKey = new Map(referenceAnswers.map((answer) => [normalizeExportOptionKey(answer), answer]));
  const observedAnswerOrder = [];
  const observedAnswerLabelByKey = new Map();
  const countsByAnswerKey = new Map();
  const countSeenByAnswerKey = new Map();
  const ensureObservedAnswer = (label) => {
    const cleanedLabel = stripExportQuestionCodePrefix(label);
    const key = normalizeExportOptionKey(cleanedLabel);
    if (!key) return null;
    const displayLabel = referenceAnswerByKey.get(key) || cleanedLabel;
    if (!observedAnswerLabelByKey.has(key)) {
      observedAnswerLabelByKey.set(key, displayLabel);
      observedAnswerOrder.push(key);
      countsByAnswerKey.set(key, new Array(breakColumns.length).fill(0));
      countSeenByAnswerKey.set(key, breakColumns.map(() => new Set()));
    }
    return key;
  };

  const bases = new Array(breakColumns.length).fill(0);
  const baseSeen = breakColumns.map(() => new Set());
  for (const row of baseRows) {
    const respondentKey = `${row.respondent_id}__${row.month}`;
    breakColumns.forEach((column, idx) => {
      if (column.field && normalizeExportCellValue(row[column.field]) !== normalizeExportCellValue(column.value)) return;
      if (!baseSeen[idx].has(respondentKey)) {
        baseSeen[idx].add(respondentKey);
        bases[idx] += 1;
      }
    });
  }

  const singleMatcher = new RegExp(singlePattern.replace(/^\(\?i\)/, ""), "i");
  for (const row of rows) {
    const questionText = compactLabel(row.question_raw || row.question);
    const isSingle = singleMatcher.test(questionText);
    if (!isSingle && !isPositiveAnswerSelection(row.answer_label, row.answer_value, row.answer_value_num)) continue;
    const answerLabel = isSingle
      ? (exportAnswerByNumericCode(row.answer, referenceAnswers) || row.answer)
      : exportAnswerByNumericCode(questionText.match(/[._](\d+)\s*$/)?.[1], referenceAnswers);
    const observedAnswerKey = ensureObservedAnswer(answerLabel);
    if (!observedAnswerKey) continue;
    const respondentKey = `${row.respondent_id}__${row.month}`;
    breakColumns.forEach((column, idx) => {
      if (column.field && normalizeExportCellValue(row[column.field]) !== normalizeExportCellValue(column.value)) return;
      const answerSeen = countSeenByAnswerKey.get(observedAnswerKey)[idx];
      if (!answerSeen.has(respondentKey)) {
        answerSeen.add(respondentKey);
        countsByAnswerKey.get(observedAnswerKey)[idx] += 1;
      }
    });
  }

  const observedAnswerKeys = Array.from(new Set([
    ...referenceAnswers.map((answer) => normalizeExportOptionKey(answer)).filter((key) => observedAnswerLabelByKey.has(key)),
    ...observedAnswerOrder.filter((key) => !referenceAnswerByKey.has(key)),
  ]));
  const answers = observedAnswerKeys.map((key) => observedAnswerLabelByKey.get(key));
  const countsByAnswer = new Map(answers.map((answer, idx) => [answer, countsByAnswerKey.get(observedAnswerKeys[idx]) || new Array(breakColumns.length).fill(0)]));
  return { bases, countsByAnswer, answers };
}

async function getExportBlockCounts({ category, months, block, breakColumns }) {
  const dimensionVariable = exportDimensionVariable(block);
  if (dimensionVariable) {
    return getExportDimensionBlockCounts({ category, months, block, breakColumns, dimensionVariable });
  }
  const derivedSpec = exportDerivedTotalSpec(block);
  if (derivedSpec) {
    return getExportDerivedTotalBlockCounts({ category, months, block, breakColumns, derivedSpec });
  }
  const factColumns = await getExportFactColumns();
  const factColumnSet = new Set(factColumns.map((col) => String(col).toLowerCase()));
  const monthFilter = exportMonthFilterSql(months, "r");
  const questionPredicate = exportQuestionPredicateSql(block, factColumns);
  const answerExpr = factColumnSet.has("answer_label") && factColumnSet.has("answer_value")
    ? "CAST(COALESCE(NULLIF(TRIM(CAST(r.answer_label AS VARCHAR)), ''), r.answer_value) AS VARCHAR)"
    : factColumnSet.has("answer_label")
      ? "CAST(r.answer_label AS VARCHAR)"
      : factColumnSet.has("answer_value")
        ? "CAST(r.answer_value AS VARCHAR)"
        : "''";
  const questionExpr = factColumnSet.has("question") ? "CAST(r.question AS VARCHAR)" : "''";
  const questionRawExpr = factColumnSet.has("question_raw") ? "CAST(COALESCE(r.question_raw, r.question) AS VARCHAR)" : questionExpr;
  const questionLabelExpr = factColumnSet.has("question_label") ? "CAST(r.question_label AS VARCHAR)" : "''";
  const answerValueExpr = factColumnSet.has("answer_value") ? "CAST(r.answer_value AS VARCHAR)" : "''";
  const answerValueNumExpr = factColumnSet.has("answer_value_num") ? "CAST(r.answer_value_num AS DOUBLE)" : "NULL";
  const rows = await all(`
    SELECT
      CAST(r.respondent_id AS VARCHAR) AS respondent_id,
      CAST(r.month AS VARCHAR) AS month,
      ${questionExpr} AS question,
      ${questionRawExpr} AS question_raw,
      ${questionLabelExpr} AS question_label,
      ${respondentDimColumns.includes("Region") ? "CAST(d.Region AS VARCHAR)" : "NULL"} AS Region,
      ${respondentDimColumns.includes("D3") ? "CAST(d.D3 AS VARCHAR)" : "NULL"} AS D3,
      ${respondentDimColumns.includes("Gender") ? "CAST(d.Gender AS VARCHAR)" : "NULL"} AS Gender,
      ${respondentDimColumns.includes("Age") ? "CAST(d.Age AS VARCHAR)" : "NULL"} AS Age,
      ${respondentDimColumns.includes("SEC") ? "CAST(d.SEC AS VARCHAR)" : "NULL"} AS SEC,
      ${respondentDimColumns.includes("Week") ? "CAST(d.Week AS VARCHAR)" : "NULL"} AS Week,
      ${answerExpr} AS answer,
      ${answerValueExpr} AS answer_value,
      ${answerValueNumExpr} AS answer_value_num
    FROM responses_fact r
    LEFT JOIN respondent_dims d
      ON CAST(d.category AS VARCHAR) = CAST(r.category AS VARCHAR)
     AND CAST(d.respondent_id AS VARCHAR) = CAST(r.respondent_id AS VARCHAR)
     AND CAST(d.month AS VARCHAR) = CAST(r.month AS VARCHAR)
    WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
      ${monthFilter}
      AND ${questionPredicate}
  `);

  const referenceAnswers = Array.isArray(block.answers) ? block.answers.map(compactLabel).filter(Boolean) : [];
  const referenceAnswerByKey = new Map(referenceAnswers.map((answer) => [normalizeExportOptionKey(answer), answer]));
  const observedAnswerOrder = [];
  const observedAnswerLabelByKey = new Map();
  const countsByAnswerKey = new Map();
  const countSeenByAnswerKey = new Map();
  const ensureObservedAnswer = (label) => {
    const cleanedLabel = stripExportQuestionCodePrefix(label);
    const key = normalizeExportOptionKey(cleanedLabel);
    if (!key) return null;
    const displayLabel = referenceAnswerByKey.get(key) || cleanedLabel;
    if (!observedAnswerLabelByKey.has(key)) {
      observedAnswerLabelByKey.set(key, displayLabel);
      observedAnswerOrder.push(key);
      countsByAnswerKey.set(key, new Array(breakColumns.length).fill(0));
      countSeenByAnswerKey.set(key, breakColumns.map(() => new Set()));
    }
    return key;
  };
  const resolveObservedAnswerKey = (row, isMultiResponseBlock) => {
    const answerLabel = compactLabel(row.answer);
    const questionLabel = stripExportQuestionCodePrefix(row.question_label);
    const answerKey = normalizeExportOptionKey(answerLabel);
    const questionKey = normalizeExportOptionKey(questionLabel);
    if (!isMultiResponseBlock) {
      return ensureObservedAnswer(exportAnswerByNumericCode(answerLabel, referenceAnswers) || answerLabel);
    }
    if (referenceAnswerByKey.has(questionKey)) return ensureObservedAnswer(referenceAnswerByKey.get(questionKey));
    if (referenceAnswerByKey.has(answerKey)) return ensureObservedAnswer(referenceAnswerByKey.get(answerKey));
    const suffixAnswer = exportAnswerFromQuestionSuffix(row.question_raw || row.question, block);
    if (suffixAnswer) return ensureObservedAnswer(suffixAnswer);
    for (const [referenceKey, referenceLabel] of referenceAnswerByKey.entries()) {
      if (questionKey && (questionKey === referenceKey || questionKey.includes(referenceKey))) {
        return ensureObservedAnswer(referenceLabel);
      }
    }
    if (questionLabel && normalizeExportOptionKey(questionLabel) !== normalizeExportOptionKey(block.title)) {
      return ensureObservedAnswer(questionLabel);
    }
    if (!isLikelyPositiveFlagOnly(answerLabel)) return ensureObservedAnswer(answerLabel);
    return null;
  };
  const bases = new Array(breakColumns.length).fill(0);
  const baseSeen = breakColumns.map(() => new Set());

  for (const row of rows) {
    const respondentKey = `${row.respondent_id}__${row.month}`;
    const isMultiResponseBlock = Array.isArray(block.variablePrefixes) && block.variablePrefixes.length > 0;
    const answerValueNum = Number(row.answer_value_num);
    const isPositiveResponse =
      !isMultiResponseBlock ||
      answerValueNum === 1 ||
      isPositiveExportAnswerText(`${row.answer} ${row.answer_value}`);
    const observedAnswerKey = isPositiveResponse ? resolveObservedAnswerKey(row, isMultiResponseBlock) : null;
    breakColumns.forEach((column, idx) => {
      if (column.field && normalizeExportCellValue(row[column.field]) !== normalizeExportCellValue(column.value)) return;
      if (!baseSeen[idx].has(respondentKey)) {
        baseSeen[idx].add(respondentKey);
        bases[idx] += 1;
      }
      if (!observedAnswerKey) return;
      const answerSeen = countSeenByAnswerKey.get(observedAnswerKey)[idx];
      if (!answerSeen.has(respondentKey)) {
        answerSeen.add(respondentKey);
        countsByAnswerKey.get(observedAnswerKey)[idx] += 1;
      }
    });
  }

  const observedAnswerKeys = Array.from(new Set([
    ...referenceAnswers.map((answer) => normalizeExportOptionKey(answer)).filter((key) => observedAnswerLabelByKey.has(key)),
    ...observedAnswerOrder.filter((key) => !referenceAnswerByKey.has(key)),
  ]));
  const answers = observedAnswerKeys.map((key) => observedAnswerLabelByKey.get(key));
  const countsByAnswer = new Map(answers.map((answer, idx) => [answer, countsByAnswerKey.get(observedAnswerKeys[idx]) || new Array(breakColumns.length).fill(0)]));

  return { bases, countsByAnswer, answers };
}

function setExportCell(sheet, row, col, value, style = null, format = null) {
  const address = XLSX.utils.encode_cell({ r: row - 1, c: col - 1 });
  const isNumber = typeof value === "number" && Number.isFinite(value);
  sheet[address] = isNumber ? { t: "n", v: value } : { t: "s", v: value == null ? "" : String(value) };
  if (style) sheet[address].s = style;
  if (format && isNumber) sheet[address].z = format;
}

function mergeExportCells(sheet, startRow, startCol, endRow, endCol) {
  if (endRow < startRow || endCol < startCol) return;
  sheet["!merges"] = sheet["!merges"] || [];
  sheet["!merges"].push({
    s: { r: startRow - 1, c: startCol - 1 },
    e: { r: endRow - 1, c: endCol - 1 },
  });
}

function setExportRowHeight(sheet, row, height) {
  sheet["!rows"] = sheet["!rows"] || [];
  sheet["!rows"][row - 1] = { hpt: height };
}

function spssVariableKey(value) {
  return compactLabel(value).toLowerCase();
}

function spssCaseKey(respondentId, month) {
  return `${compactLabel(respondentId)}__${compactLabel(month)}`;
}

function spssMeaningfulLabel(value) {
  const label = compactLabel(value);
  if (!label) return "";
  const normalized = normalizeExportOptionKey(label);
  if (["yes", "no", "selected", "mentioned", "true", "false", "1", "0"].includes(normalized)) return "";
  return label;
}

function spssEntryNumeric(entry) {
  if (entry?.value_num !== null && entry?.value_num !== undefined && compactLabel(entry.value_num) !== "") {
    const direct = Number(entry.value_num);
    if (Number.isFinite(direct)) return direct;
  }
  const text = compactLabel(entry?.value_text);
  const parsed = text === "" ? NaN : Number(text);
  if (Number.isFinite(parsed)) return parsed;
  const label = normalizeExportOptionKey(entry?.value_label || entry?.value_text);
  if (label === "male") return 1;
  if (label === "female") return 2;
  return null;
}

function spssEntryHasValue(entry) {
  const text = compactLabel(entry?.value_text);
  const label = compactLabel(entry?.value_label);
  const numeric = spssEntryNumeric(entry);
  if (numeric != null) return true;
  const normalizedText = normalizeExportOptionKey(text);
  const normalizedLabel = normalizeExportOptionKey(label);
  if (!normalizedText && !normalizedLabel) return false;
  return !["nan", "null", "undefined"].includes(normalizedText || normalizedLabel);
}

function spssEntryIdentity(entry) {
  const numeric = spssEntryNumeric(entry);
  if (numeric != null) return `n:${numeric}`;
  return `s:${normalizeExportOptionKey(entry?.value_text || entry?.value_label)}`;
}

function spssEntryIsSelected(entry, selectorValue = "1") {
  const expected = Number(selectorValue);
  const numeric = spssEntryNumeric(entry);
  if (Number.isFinite(expected) && numeric != null) return numeric === expected;
  return normalizeExportOptionKey(entry?.value_text || entry?.value_label) === normalizeExportOptionKey(selectorValue);
}

function spssCaseEntries(caseRow, variable) {
  return (caseRow?.values?.get(spssVariableKey(variable)) || []).filter(spssEntryHasValue);
}

function spssCaseFirstEntry(caseRow, variable) {
  return spssCaseEntries(caseRow, variable)[0] || null;
}

function spssStripOuterParentheses(expression) {
  let value = compactLabel(expression);
  while (value.startsWith("(") && value.endsWith(")")) {
    let depth = 0;
    let balanced = true;
    for (let index = 0; index < value.length; index += 1) {
      if (value[index] === "(") depth += 1;
      if (value[index] === ")") depth -= 1;
      if (depth === 0 && index < value.length - 1) {
        balanced = false;
        break;
      }
    }
    if (!balanced) break;
    value = compactLabel(value.slice(1, -1));
  }
  return value;
}

function spssSplitTopLevel(expression, operator) {
  const source = String(expression || "");
  const upper = source.toUpperCase();
  const token = ` ${operator.toUpperCase()} `;
  let depth = 0;
  for (let index = 0; index <= source.length - token.length; index += 1) {
    const char = source[index];
    if (char === "(") depth += 1;
    else if (char === ")") depth -= 1;
    if (depth === 0 && upper.slice(index, index + token.length) === token) {
      return [source.slice(0, index), source.slice(index + token.length)];
    }
  }
  return null;
}

function spssFilterTruth(caseRow, expression) {
  const source = spssStripOuterParentheses(expression);
  if (!source) return true;
  if (/^NOT\b/i.test(source)) {
    const value = spssFilterTruth(caseRow, source.replace(/^NOT\b/i, "").trim());
    return value == null ? null : !value;
  }
  const orParts = spssSplitTopLevel(source, "OR");
  if (orParts) {
    const left = spssFilterTruth(caseRow, orParts[0]);
    const right = spssFilterTruth(caseRow, orParts[1]);
    if (left === true || right === true) return true;
    if (left === false && right === false) return false;
    return null;
  }
  const andParts = spssSplitTopLevel(source, "AND");
  if (andParts) {
    const left = spssFilterTruth(caseRow, andParts[0]);
    const right = spssFilterTruth(caseRow, andParts[1]);
    if (left === false || right === false) return false;
    if (left === true && right === true) return true;
    return null;
  }
  const atomic = source.match(/^([A-Za-z_$][A-Za-z0-9_.$]*)\s*(>=|<=|<>|~=|=|>|<)\s*(-?\d+(?:\.\d+)?)$/i);
  if (!atomic) throw new Error(`Unsupported SPSS filter expression: ${source}`);
  const entries = spssCaseEntries(caseRow, atomic[1]);
  if (!entries.length) return null;
  const expected = Number(atomic[3]);
  let sawNumeric = false;
  for (const entry of entries) {
    const actual = spssEntryNumeric(entry);
    if (actual == null) continue;
    sawNumeric = true;
    if (atomic[2] === "=" && actual === expected) return true;
    if (["<>", "~="].includes(atomic[2]) && actual !== expected) return true;
    if (atomic[2] === ">=" && actual >= expected) return true;
    if (atomic[2] === "<=" && actual <= expected) return true;
    if (atomic[2] === ">" && actual > expected) return true;
    if (atomic[2] === "<" && actual < expected) return true;
  }
  return sawNumeric ? false : null;
}

function spssFilterMatches(caseRow, expression) {
  return spssFilterTruth(caseRow, expression) === true;
}

function spssRangeMatcher(variableSpec) {
  const match = compactLabel(variableSpec).match(/^(.+?)(\d+)\s+TO\s+(.+?)(\d+)$/i);
  if (!match) {
    const exact = spssVariableKey(variableSpec.replace(/[,.]+$/, ""));
    return (variable) => spssVariableKey(variable) === exact;
  }
  const startPrefix = spssVariableKey(match[1]);
  const endPrefix = spssVariableKey(match[3]);
  const start = Number(match[2]);
  const end = Number(match[4]);
  if (startPrefix !== endPrefix) return () => false;
  return (variable) => {
    const normalized = spssVariableKey(variable);
    if (!normalized.startsWith(startPrefix)) return false;
    const suffix = normalized.slice(startPrefix.length);
    if (!/^\d+$/.test(suffix)) return false;
    const numeric = Number(suffix);
    return numeric >= Math.min(start, end) && numeric <= Math.max(start, end);
  };
}

function spssSetVariableMatchers(rowSet) {
  const spec = compactLabel(rowSet?.variables);
  if (!spec) return [];
  const pieces = spec.split(/\s*,\s*|\s+\/\s+/).map(compactLabel).filter(Boolean);
  return pieces.map(spssRangeMatcher);
}

function spssMemberAnswer(memberVariable, entry, answers, variableLabels = {}) {
  const referenceAnswers = Array.isArray(answers) ? answers : [];
  const variableLabel = spssMeaningfulLabel(variableLabels[memberVariable]);
  if (variableLabel) {
    const key = normalizeExportOptionKey(variableLabel);
    const exact = referenceAnswers.find((answer) => normalizeExportOptionKey(answer) === key);
    if (exact) return exact;
  }
  const entryLabel = spssMeaningfulLabel(entry?.value_label);
  if (entryLabel) {
    const key = normalizeExportOptionKey(entryLabel);
    const exact = referenceAnswers.find((answer) => normalizeExportOptionKey(answer) === key);
    if (exact) return exact;
  }
  const suffix = compactLabel(memberVariable).match(/[._](\d+)$/)?.[1];
  const bySuffix = exportAnswerByNumericCode(suffix, referenceAnswers);
  if (bySuffix) return bySuffix;
  const byValue = exportAnswerByNumericCode(entry?.value_text, referenceAnswers);
  if (byValue) return byValue;
  return entryLabel || compactLabel(entry?.value_text);
}

function spssSingleAnswer(entry, answers) {
  const referenceAnswers = Array.isArray(answers) ? answers : [];
  const label = spssMeaningfulLabel(entry?.value_label);
  if (label) {
    const exact = referenceAnswers.find((answer) => normalizeExportOptionKey(answer) === normalizeExportOptionKey(label));
    if (exact) return exact;
  }
  const text = compactLabel(entry?.value_text);
  const exactText = referenceAnswers.find((answer) => normalizeExportOptionKey(answer) === normalizeExportOptionKey(text));
  if (exactText) return exactText;
  return exportAnswerByNumericCode(text, referenceAnswers) || label || text;
}

function spssDerivedSelection(caseRow, block) {
  const derived = exportDerivedTotalSpec(block);
  if (!derived) return null;
  const answers = Array.isArray(block.answers) ? block.answers : [];
  const selected = new Set();
  let hasSource = false;
  const singleVariable = `${derived.prefix}_${derived.single}`;
  for (const entry of spssCaseEntries(caseRow, singleVariable)) {
    hasSource = true;
    // Step 2 compares the original single-response code with a literal
    // sequence 1..N. Codes 98/99 therefore do not populate the temporary
    // first-mention array unless they are also present in the explicit
    // multi-response source ranges.
    const code = spssEntryNumeric(entry);
    if (Number.isInteger(code) && code >= 1 && code <= answers.length) {
      selected.add(answers[code - 1]);
    }
  }
  for (const family of derived.multi) {
    const prefix = spssVariableKey(`${derived.prefix}_${family}_`);
    for (const [variable, entries] of caseRow.values.entries()) {
      if (!variable.startsWith(prefix)) continue;
      hasSource = true;
      entries.forEach((entry) => {
        if (!spssEntryIsSelected(entry, "1")) return;
        const answer = spssMemberAnswer(variable, entry, answers, loadExportTableSpecs().variableLabels || {});
        if (answer) selected.add(answer);
      });
    }
  }
  return { base: selected.size > 0, selected };
}

function spssRowSelection(caseRow, block, rowSetOverride = null) {
  const derived = !rowSetOverride ? spssDerivedSelection(caseRow, block) : null;
  if (derived) return derived;
  const answers = Array.isArray(block.answers) ? block.answers : [];
  const syntax = block.spss || {};
  const rowSet = rowSetOverride || (Array.isArray(syntax.rowSets) && syntax.rowSets.length === 1 ? syntax.rowSets[0] : null);
  if (rowSet) {
    const matchers = spssSetVariableMatchers(rowSet);
    const selected = new Set();
    let base = false;
    for (const [variable, entries] of caseRow.values.entries()) {
      if (!matchers.some((matcher) => matcher(variable))) continue;
      base = true;
      for (const entry of entries) {
        if (rowSet.kind === "MDGROUP" && !spssEntryIsSelected(entry, rowSet.selectorValue || "1")) continue;
        if (rowSet.kind === "MCGROUP" && syntax.metric === "row_percent" && !spssEntryIsSelected(entry, "1")) continue;
        const answer = rowSet.kind === "MCGROUP" && syntax.metric !== "row_percent"
          ? spssSingleAnswer(entry, answers)
          : spssMemberAnswer(variable, entry, answers, loadExportTableSpecs().variableLabels || {});
        if (answer) selected.add(answer);
      }
    }
    return { base, selected };
  }
  const variable = syntax.rowVariable || block.tableVar || block.questionCode;
  const entries = spssCaseEntries(caseRow, variable);
  const selected = new Set(entries.map((entry) => spssSingleAnswer(entry, answers)).filter(Boolean));
  return { base: entries.length > 0, selected };
}

async function loadSpssExportRepository(category, months) {
  await ensureSpssExportTables();
  const monthFilterDims = exportMonthFilterSql(months, "d");
  const monthFilterValues = exportMonthFilterSql(months, "v");
  const dimRows = await all(`
    SELECT CAST(d.respondent_id AS VARCHAR) AS respondent_id, CAST(d.month AS VARCHAR) AS month
    FROM respondent_dims d
    WHERE CAST(d.category AS VARCHAR)=${quote(category)} ${monthFilterDims}
  `);
  const cases = new Map();
  for (const row of dimRows) {
    const key = spssCaseKey(row.respondent_id, row.month);
    cases.set(key, { key, respondent_id: compactLabel(row.respondent_id), month: compactLabel(row.month), values: new Map() });
  }
  const valueRows = await all(`
    SELECT CAST(v.respondent_id AS VARCHAR) AS respondent_id, CAST(v.month AS VARCHAR) AS month,
           CAST(v.variable AS VARCHAR) AS variable, CAST(v.value_text AS VARCHAR) AS value_text,
           CAST(v.value_num AS DOUBLE) AS value_num, CAST(v.value_label AS VARCHAR) AS value_label
    FROM spss_case_values v
    WHERE CAST(v.category AS VARCHAR)=${quote(category)} ${monthFilterValues}
  `);
  for (const row of valueRows) {
    const key = spssCaseKey(row.respondent_id, row.month);
    if (!cases.has(key)) cases.set(key, { key, respondent_id: compactLabel(row.respondent_id), month: compactLabel(row.month), values: new Map() });
    const caseRow = cases.get(key);
    const variable = spssVariableKey(row.variable);
    if (!caseRow.values.has(variable)) caseRow.values.set(variable, []);
    caseRow.values.get(variable).push({
      variable: compactLabel(row.variable),
      value_text: compactLabel(row.value_text),
      value_num: row.value_num == null ? null : Number(row.value_num),
      value_label: compactLabel(row.value_label),
    });
  }
  const dictionaryRows = await all(`
    SELECT CAST(variable AS VARCHAR) AS variable, CAST(value_text AS VARCHAR) AS value_text,
           CAST(value_num AS DOUBLE) AS value_num, CAST(value_label AS VARCHAR) AS value_label
    FROM spss_variable_dictionary
    WHERE CAST(category AS VARCHAR)=${quote(category)}
  `);
  const dictionary = new Map();
  for (const row of dictionaryRows) {
    const variable = spssVariableKey(row.variable);
    if (!dictionary.has(variable)) dictionary.set(variable, []);
    dictionary.get(variable).push({
      variable: compactLabel(row.variable),
      value_text: compactLabel(row.value_text),
      value_num: row.value_num == null ? null : Number(row.value_num),
      value_label: compactLabel(row.value_label),
    });
  }
  return { category, months, cases: Array.from(cases.values()), dictionary };
}

function loadExportXlsformMetadata() {
  if (!exportXlsformMetadataCache) {
    exportXlsformMetadataCache = readJsonFile(XLSFORM_EXPORT_METADATA_PATH, { questions: {}, lists: {} });
  }
  return exportXlsformMetadataCache;
}

function spssMetadataQuestion(variable) {
  const metadata = loadExportXlsformMetadata();
  const aliases = {
    region: ["Region", "City_1"],
    age2: ["Age_cal", "Age"],
    sec2: ["SEC"],
  };
  const candidates = new Set([compactLabel(variable), ...(aliases[spssVariableKey(variable)] || [])].map(spssVariableKey));
  for (const question of Object.values(metadata.questions || {})) {
    const names = [question?.name, ...(question?.aliases || [])].map(spssVariableKey);
    if (names.some((name) => candidates.has(name))) return question;
  }
  return null;
}

function spssMetadataDictionaryEntries(variable) {
  const metadata = loadExportXlsformMetadata();
  const question = spssMetadataQuestion(variable);
  if (!question?.list_name) return [];
  const list = metadata.lists?.[question.list_name] || {};
  return Object.entries(list)
    .filter(([value, label]) => compactLabel(value).toLowerCase() !== "nan" && compactLabel(label).toLowerCase() !== "nan")
    .map(([value, label]) => ({
      variable: compactLabel(variable),
      value_text: compactLabel(value).replace(/\.0$/, ""),
      value_num: Number.isFinite(Number(value)) ? Number(value) : null,
      value_label: compactLabel(label),
    }));
}

function spssVariableLabel(variable) {
  const rules = loadExportTableSpecs();
  const target = spssVariableKey(variable);
  const configured = Object.entries(rules.variableLabels || {}).find(([name]) => spssVariableKey(name) === target)?.[1];
  if (compactLabel(configured)) return compactLabel(configured);
  const question = spssMetadataQuestion(variable);
  const label = compactLabel(question?.label);
  return label && label.toLowerCase() !== "nan" ? label : "";
}

function spssIsAutorecodeTarget(variable) {
  const target = spssVariableKey(variable);
  return (loadExportTableSpecs().autorecode || []).some((item) => spssVariableKey(item?.target) === target);
}

function spssBreakGroupLabel(variable) {
  const labels = {
    region: "REGION:",
    region2: "RECLASSIFIED REGION:",
    d3: "INCOME:",
    gender: "GENDER:",
    age2: "AGE:",
    sec2: "SEC:",
    week: "WEEK:",
  };
  const fixed = labels[spssVariableKey(variable)];
  if (fixed) return fixed;
  const label = spssVariableLabel(variable) || compactLabel(variable);
  return /[:?]$/.test(label) ? label : `${label}:`;
}

function spssSortedDictionaryEntries(entries) {
  const deduped = new Map();
  for (const entry of entries || []) {
    const identity = spssEntryIdentity(entry);
    if (!identity || identity === "s:") continue;
    if (!deduped.has(identity)) deduped.set(identity, entry);
  }
  return Array.from(deduped.values()).sort((a, b) => {
    const aNum = spssEntryNumeric(a);
    const bNum = spssEntryNumeric(b);
    if (aNum != null && bNum != null) return aNum - bNum;
    if (aNum != null) return -1;
    if (bNum != null) return 1;
    return compactLabel(a.value_text).localeCompare(compactLabel(b.value_text), undefined, { numeric: true, sensitivity: "base" });
  });
}

function spssEntryMatchKeys(entry) {
  const keys = new Set();
  const numeric = spssEntryNumeric(entry);
  if (numeric != null) {
    keys.add(`n:${numeric}`);
    keys.add(normalizeExportOptionKey(String(numeric)));
  }
  for (const value of [entry?.value_text, entry?.value_label]) {
    const key = normalizeExportOptionKey(value);
    if (key) keys.add(key);
  }
  return keys;
}

function spssReconcileDictionaryEntries(observedEntries, metadataEntries) {
  const observed = spssSortedDictionaryEntries(observedEntries);
  const metadata = spssSortedDictionaryEntries(metadataEntries);
  if (!metadata.length || !observed.length) return metadata.length ? metadata : observed;

  const observedByKey = new Map();
  for (const entry of observed) {
    for (const key of spssEntryMatchKeys(entry)) {
      if (!observedByKey.has(key)) observedByKey.set(key, entry);
    }
  }

  const used = new Set();
  const reconciled = [];
  for (const metaEntry of metadata) {
    let match = null;
    for (const key of spssEntryMatchKeys(metaEntry)) {
      if (observedByKey.has(key)) {
        match = observedByKey.get(key);
        break;
      }
    }
    if (match) {
      used.add(spssEntryIdentity(match));
      reconciled.push({ ...match, value_label: spssMeaningfulLabel(metaEntry.value_label) || match.value_label });
    } else {
      reconciled.push(metaEntry);
    }
  }
  for (const entry of observed) {
    const identity = spssEntryIdentity(entry);
    if (!used.has(identity)) reconciled.push(entry);
  }
  return reconciled;
}

function getSpssBreakColumns(repository, block) {
  const columns = [{ group: "Total", label: "Total", field: null, identity: null }];
  const variables = Array.isArray(block?.spss?.breakVariables) ? block.spss.breakVariables : [];
  const rules = loadExportTableSpecs();
  for (const variable of variables) {
    let entries = repository.dictionary.get(spssVariableKey(variable)) || [];
    const metadataEntries = spssMetadataDictionaryEntries(variable);
    if (metadataEntries.length && !spssIsAutorecodeTarget(variable) && spssVariableKey(variable) !== "region2") {
      entries = spssReconcileDictionaryEntries(entries, metadataEntries);
    }
    if (spssVariableKey(variable) === "region2") {
      entries = Object.entries(rules.region2?.labels || {}).map(([code, label]) => ({ variable, value_text: code, value_num: Number(code), value_label: label }));
    }
    for (const entry of spssSortedDictionaryEntries(entries)) {
      columns.push({
        group: spssBreakGroupLabel(variable),
        label: spssMeaningfulLabel(entry.value_label) || compactLabel(entry.value_text),
        field: variable,
        identity: spssEntryIdentity(entry),
        matchKeys: Array.from(spssEntryMatchKeys(entry)),
      });
    }
  }
  return columns;
}

function spssCaseMatchesBreak(caseRow, column) {
  if (!column.field) return true;
  const matchKeys = new Set(Array.isArray(column.matchKeys) ? column.matchKeys : [column.identity].filter(Boolean));
  return spssCaseEntries(caseRow, column.field).some((entry) =>
    Array.from(spssEntryMatchKeys(entry)).some((key) => matchKeys.has(key)),
  );
}

function getSpssStandardBlockCounts(repository, block, breakColumns) {
  const answers = Array.isArray(block.answers) ? block.answers.slice() : [];
  const countsByAnswer = new Map(answers.map((answer) => [answer, new Array(breakColumns.length).fill(0)]));
  const bases = new Array(breakColumns.length).fill(0);
  const answerByKey = new Map(answers.map((answer) => [normalizeExportOptionKey(answer), answer]));
  for (const caseRow of repository.cases) {
    if (!spssFilterMatches(caseRow, block?.spss?.filter)) continue;
    const selection = spssRowSelection(caseRow, block);
    if (!selection.base) continue;
    breakColumns.forEach((column, index) => {
      if (!spssCaseMatchesBreak(caseRow, column)) return;
      bases[index] += 1;
      for (const selected of selection.selected) {
        const canonical = answerByKey.get(normalizeExportOptionKey(selected));
        if (canonical) countsByAnswer.get(canonical)[index] += 1;
      }
    });
  }
  return { bases, countsByAnswer, answers };
}

function getSpssQbiBlockCounts(repository, block) {
  const answers = Array.isArray(block.answers) ? block.answers.slice() : [];
  const rowSets = Array.isArray(block?.spss?.rowSets) ? block.spss.rowSets : [];
  const sourcePrefixes = Array.isArray(block.variablePrefixes) ? block.variablePrefixes : [];
  const variableLabels = loadExportTableSpecs().variableLabels || {};
  const rows = [];
  for (let rowIndex = 0; rowIndex < rowSets.length; rowIndex += 1) {
    const rowSet = rowSets[rowIndex];
    const counts = new Array(answers.length).fill(0);
    const sourcePrefix = spssVariableKey(sourcePrefixes[rowIndex] || "");
    const rowSetMatchers = spssSetVariableMatchers(rowSet);
    for (const caseRow of repository.cases) {
      if (!spssFilterMatches(caseRow, block?.spss?.filter)) continue;
      const selected = new Set();
      for (const [variable, entries] of caseRow.values.entries()) {
        const matchesCurrentName = sourcePrefix && variable.startsWith(sourcePrefix);
        const matchesSpssName = rowSetMatchers.some((matcher) => matcher(variable));
        if (!matchesCurrentName && !matchesSpssName) continue;
        for (const entry of entries) {
          if (!spssEntryIsSelected(entry, "1")) continue;
          const answer = spssMemberAnswer(variable, entry, answers, variableLabels);
          if (answer) selected.add(answer);
        }
      }
      selected.forEach((selectedAnswer) => {
        const index = answers.findIndex((answer) => normalizeExportOptionKey(answer) === normalizeExportOptionKey(selectedAnswer));
        if (index >= 0) counts[index] += 1;
      });
    }
    rows.push({ label: rowSet.label || rowSet.name, counts, total: counts.reduce((sum, value) => sum + value, 0) });
  }
  return { answers, rows };
}

function exportSheetRange(sheet, maxRow, maxCol) {
  sheet["!ref"] = XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: Math.max(0, maxRow - 1), c: Math.max(0, maxCol - 1) } });
}

function writeSpssStandardBlock(sheet, startRow, block, breakColumns, result, metric) {
  setExportCell(sheet, startRow, 1, block.title, EXPORT_STYLES.title);
  setExportRowHeight(sheet, startRow, 24);
  const groups = [];
  breakColumns.forEach((column, index) => {
    const col = index + 4;
    const previous = groups[groups.length - 1];
    if (previous && previous.label === column.group && previous.end === col - 1) previous.end = col;
    else groups.push({ label: index === 0 ? "" : column.group, start: col, end: col });
  });
  groups.forEach((group) => {
    if (!group.label) return;
    setExportCell(sheet, startRow + 1, group.start, group.label, EXPORT_STYLES.header);
    if (group.end > group.start) mergeExportCells(sheet, startRow + 1, group.start, startRow + 1, group.end);
  });
  breakColumns.forEach((column, index) => setExportCell(sheet, startRow + 2, index + 4, column.label, EXPORT_STYLES.subHeader));
  setExportCell(sheet, startRow + 3, 1, block.title, EXPORT_STYLES.label);
  setExportCell(sheet, startRow + 3, 2, "Total", EXPORT_STYLES.label);
  setExportCell(sheet, startRow + 3, 3, metric === "percent" ? "Base Total" : "", EXPORT_STYLES.label);
  result.bases.forEach((base, index) => setExportCell(sheet, startRow + 3, index + 4, base, EXPORT_STYLES.basePct, "0"));
  let answerRow = startRow + 4;
  if (metric === "percent") {
    setExportCell(sheet, answerRow, 1, "", EXPORT_STYLES.label);
    setExportCell(sheet, answerRow, 2, "", EXPORT_STYLES.label);
    setExportCell(sheet, answerRow, 3, "Base%", EXPORT_STYLES.label);
    result.bases.forEach((base, index) => setExportCell(sheet, answerRow, index + 4, base > 0 ? 100 : 0, EXPORT_STYLES.basePct, "0"));
    answerRow += 1;
  }
  for (const answer of result.answers) {
    setExportCell(sheet, answerRow, 1, "", EXPORT_STYLES.label);
    setExportCell(sheet, answerRow, 2, answer, EXPORT_STYLES.label);
    setExportCell(sheet, answerRow, 3, metric === "percent" ? "%" : "N", EXPORT_STYLES.label);
    const counts = result.countsByAnswer.get(answer) || [];
    counts.forEach((count, index) => {
      const value = metric === "percent" ? (result.bases[index] > 0 ? count * 100 / result.bases[index] : 0) : count;
      setExportCell(sheet, answerRow, index + 4, value, EXPORT_STYLES.value, "0");
    });
    answerRow += 1;
  }
  return { endRow: answerRow - 1, maxCol: breakColumns.length + 3 };
}

function writeSpssQbiBlock(sheet, startRow, block, result, metric) {
  setExportCell(sheet, startRow, 1, block.title, EXPORT_STYLES.title);
  setExportCell(sheet, startRow + 1, 1, "Statement", EXPORT_STYLES.header);
  setExportCell(sheet, startRow + 1, 2, "Total", EXPORT_STYLES.header);
  result.answers.forEach((answer, index) => setExportCell(sheet, startRow + 1, index + 3, answer, EXPORT_STYLES.subHeader));
  let row = startRow + 2;
  for (const item of result.rows) {
    setExportCell(sheet, row, 1, item.label, EXPORT_STYLES.label);
    setExportCell(sheet, row, 2, metric === "percent" ? (item.total > 0 ? 100 : 0) : item.total, EXPORT_STYLES.basePct, "0");
    item.counts.forEach((count, index) => {
      const value = metric === "percent" ? (item.total > 0 ? count * 100 / item.total : 0) : count;
      setExportCell(sheet, row, index + 3, value, EXPORT_STYLES.value, "0");
    });
    row += 1;
  }
  return { endRow: row - 1, maxCol: result.answers.length + 2 };
}

async function buildExportCategorySheet({ categorySlug, category, months, percentSheetName, countSheetName, type }) {
  const spec = await buildRegistryAugmentedExportSpec(categorySlug, category, months, type);
  if (!spec || !Array.isArray(spec.blocks) || !spec.blocks.length) {
    return {
      percentSheet: XLSX.utils.aoa_to_sheet([["No SPSS export table spec available for this category."]]),
      countSheet: XLSX.utils.aoa_to_sheet([["No SPSS export table spec available for this category."]]),
    };
  }
  const repository = await loadSpssExportRepository(category, months);
  const percentSheet = {};
  const countSheet = {};
  const navigationRows = [];
  let maxPercentRow = 1;
  let maxCountRow = 1;
  let maxPercentCol = 1;
  let maxCountCol = 1;

  for (const block of spec.blocks) {
    const isQbi = block?.spss?.metric === "row_percent" && Array.isArray(block?.spss?.rowSets) && block.spss.rowSets.length > 1;
    const percentStart = Math.max(1, Number(block?.sourceRows?.percentStart || maxPercentRow + 2));
    const countStart = Math.max(1, Number(block?.sourceRows?.countStart || maxCountRow + 2));
    if (isQbi) {
      const result = getSpssQbiBlockCounts(repository, block);
      const percentBounds = writeSpssQbiBlock(percentSheet, percentStart, block, result, "percent");
      const countBounds = writeSpssQbiBlock(countSheet, countStart, block, result, "count");
      navigationRows.push({
        title: block.title,
        questionCode: block.questionCode || "",
        base: result.rows.reduce((maximum, row) => Math.max(maximum, Number(row.total || 0)), 0),
        percentStart,
        percentEnd: percentBounds.endRow,
        countStart,
        countEnd: countBounds.endRow,
        registryGenerated: Boolean(block.registryGenerated),
        registryAugmented: Boolean(block.registryAugmented),
      });
      maxPercentRow = Math.max(maxPercentRow, percentBounds.endRow);
      maxCountRow = Math.max(maxCountRow, countBounds.endRow);
      maxPercentCol = Math.max(maxPercentCol, percentBounds.maxCol);
      maxCountCol = Math.max(maxCountCol, countBounds.maxCol);
    } else {
      const breakColumns = getSpssBreakColumns(repository, block);
      const result = getSpssStandardBlockCounts(repository, block, breakColumns);
      const percentBounds = writeSpssStandardBlock(percentSheet, percentStart, block, breakColumns, result, "percent");
      const countBounds = writeSpssStandardBlock(countSheet, countStart, block, breakColumns, result, "count");
      navigationRows.push({
        title: block.title,
        questionCode: block.questionCode || "",
        base: Number(result.bases?.[0] || 0),
        percentStart,
        percentEnd: percentBounds.endRow,
        countStart,
        countEnd: countBounds.endRow,
        registryGenerated: Boolean(block.registryGenerated),
        registryAugmented: Boolean(block.registryAugmented),
      });
      maxPercentRow = Math.max(maxPercentRow, percentBounds.endRow);
      maxCountRow = Math.max(maxCountRow, countBounds.endRow);
      maxPercentCol = Math.max(maxPercentCol, percentBounds.maxCol);
      maxCountCol = Math.max(maxCountCol, countBounds.maxCol);
    }
  }

  exportSheetRange(percentSheet, maxPercentRow, maxPercentCol);
  exportSheetRange(countSheet, maxCountRow, maxCountCol);
  const maxColumns = Math.max(maxPercentCol, maxCountCol);
  const widths = [{ wch: 42 }, { wch: 26 }, { wch: 12 }, ...Array.from({ length: Math.max(0, maxColumns - 3) }, () => ({ wch: 14 }))];
  percentSheet["!cols"] = widths;
  countSheet["!cols"] = widths;
  return { percentSheet, countSheet, percentSheetName, countSheetName, navigationRows, registryOverlay: spec.registryOverlay || null };
}

function normalizeExportNavigationTitle(value) {
  return compactLabel(value)
    .toLowerCase()
    .replace(/roll[_\s-]*months?/g, "months")
    .replace(/[^a-z0-9]+/g, "");
}

function exportTemplateNavigationPatches({ workbook, builtBySlug, categoriesToExport, type, period, scope, slug }) {
  const patches = { Home: [], BackEnd: [] };
  const backendSheet = workbook.Sheets?.BackEnd;
  const homeSheet = workbook.Sheets?.Home;
  const generated = new Set(categoriesToExport);

  for (const config of EXPORT_TEMPLATE_NAVIGATION) {
    const rows = builtBySlug.get(config.slug)?.navigationRows || [];
    const byTitle = new Map(rows.map((row) => [normalizeExportNavigationTitle(row.title), row]));
    const byQuestion = new Map();
    rows.forEach((row) => {
      const key = normalizeExportNavigationTitle(row.questionCode);
      if (key && !byQuestion.has(key)) byQuestion.set(key, row);
    });
    for (let rowNumber = 2; rowNumber <= 81; rowNumber += 1) {
      const titleCell = backendSheet?.[`${config.titleColumn}${rowNumber}`];
      if (!titleCell || titleCell.v === undefined || titleCell.v === null || titleCell.v === "") continue;
      const title = compactLabel(titleCell.v);
      const normalized = normalizeExportNavigationTitle(title);
      let navigation = byTitle.get(normalized);
      if (!navigation) {
        const questionCode = normalizeExportNavigationTitle(title.split(".")[0]);
        const candidate = byQuestion.get(questionCode);
        if (candidate && rows.filter((item) => normalizeExportNavigationTitle(item.questionCode) === questionCode).length === 1) navigation = candidate;
      }
      patches.BackEnd.push({
        address: `${config.valueColumn}${rowNumber}`,
        value: generated.has(config.slug) && navigation ? Number(navigation.base || 0) : null,
      });
    }
  }

  if (scope === "current") {
    const current = EXPORT_TEMPLATE_NAVIGATION.find((item) => item.slug === slug);
    if (current) patches.BackEnd.push({ address: "E5", value: current.selection });
  }

  const typeLabel = type.charAt(0).toUpperCase() + type.slice(1);
  patches.Home.push({
    address: "C8",
    value: `BHT TRACKER - ${String(period.display || "").toUpperCase()} ${typeLabel.toUpperCase()} ANALYSIS`,
  });

  if (homeSheet) {
    const sheetNameReplacements = new Map();
    for (const config of EXPORT_TEMPLATE_NAVIGATION) {
      const code = EXPORT_ROLLING_QUARTER_CODES.get(config.slug);
      const actualPercent = type === "rolling" || type === "quarter" ? `${code}_%` : EXPORT_SHEET_NAMES.get(config.slug);
      const actualCount = type === "rolling" || type === "quarter" ? `${code}_Count` : `${EXPORT_SHEET_NAMES.get(config.slug)}_count`;
      sheetNameReplacements.set(`${code}_%`, actualPercent);
      sheetNameReplacements.set(`${code}_Count`, actualCount);
    }
    for (let rowNumber = 10; rowNumber <= 69; rowNumber += 1) {
      const cell = homeSheet[`C${rowNumber}`];
      if (!cell?.f) continue;
      let formula = cell.f;
      for (const [sourceName, targetName] of sheetNameReplacements.entries()) {
        formula = formula.split(`"${sourceName}"`).join(`"${targetName}"`);
      }
      patches.Home.push({ address: `C${rowNumber}`, formula });
    }
  }

  return patches;
}

async function generateDataTableExport({ type, scope, slug, requestedBy = "manual" }) {
  if (!XLSX) throw new Error("XLSX dependency is unavailable.");
  if (!EXPORT_TYPES.has(type)) throw new Error("Unsupported export type.");
  if (!["current", "all"].includes(scope)) throw new Error("Unsupported export scope.");
  if (!fs.existsSync(EXPORT_TEMPLATE_PATH)) throw new Error("Export template workbook is missing.");

  const categoriesToExport = getExportCategories(scope, slug);
  if (!categoriesToExport.length) throw new Error("A valid category is required.");
  const period = await resolveExportPeriod(type);
  if (!period.active) {
    throw new Error(type === "quarter"
      ? "Quarter export is inactive because the latest market DB months do not form Jan-Mar, Apr-Jun, Jul-Sep, or Oct-Dec."
      : `No month scope is available for ${type} export.`);
  }

  fs.mkdirSync(EXPORTS_ROOT, { recursive: true });
  const filename = buildExportFilename(type, period, scope, slug);
  const filePath = path.join(EXPORTS_ROOT, filename);
  const workbook = XLSX.readFile(EXPORT_TEMPLATE_PATH, { cellStyles: true, bookVBA: true, cellFormula: true });
  workbook.Workbook = workbook.Workbook || {};
  workbook.Workbook.CalcPr = { ...(workbook.Workbook.CalcPr || {}), fullCalcOnLoad: true, forceFullCalc: true };
  const builtBySlug = new Map();
  const registryDiagnostics = [];
  let autorecodeSnapshot = null;

  for (const categorySlug of categoriesToExport) {
    const baseSheet = type === "rolling" || type === "quarter"
      ? `${EXPORT_ROLLING_QUARTER_CODES.get(categorySlug)}_%`
      : EXPORT_SHEET_NAMES.get(categorySlug);
    const countSheet = type === "rolling" || type === "quarter"
      ? `${EXPORT_ROLLING_QUARTER_CODES.get(categorySlug)}_Count`
      : `${EXPORT_SHEET_NAMES.get(categorySlug)}_count`;
    const builtSheets = await buildExportCategorySheet({
      categorySlug,
      category: normalizeCategory(categorySlug),
      months: period.months,
      type,
      percentSheetName: baseSheet,
      countSheetName: countSheet,
    });
    builtBySlug.set(categorySlug, builtSheets);
    try {
      registryDiagnostics.push(await metadataRegistry.exportRegistryDiagnostics(metadataRegistryDbApi(), {
        categorySlug,
        category: normalizeCategory(categorySlug),
        months: period.months,
        type,
      }));
    } catch (err) {
      registryDiagnostics.push({
        categorySlug,
        category: normalizeCategory(categorySlug),
        type,
        months: period.months,
        error: err.message,
      });
    }
    for (const sheetName of [baseSheet, countSheet]) {
      if (workbook.SheetNames.includes(sheetName)) {
        const idx = workbook.SheetNames.indexOf(sheetName);
        workbook.SheetNames.splice(idx, 1);
        delete workbook.Sheets[sheetName];
      }
    }
    XLSX.utils.book_append_sheet(workbook, builtSheets.percentSheet, baseSheet);
    XLSX.utils.book_append_sheet(workbook, builtSheets.countSheet, countSheet);
  }

  try {
    autorecodeSnapshot = await metadataRegistry.snapshotAutorecodeMappings(metadataRegistryDbApi(), {
      period: period.months[period.months.length - 1] || "1900-01",
      createdBy: requestedBy,
    });
  } catch (err) {
    autorecodeSnapshot = { ok: false, error: err.message };
  }

  appendJsonSheet(workbook, "Export_Diagnostics", buildExportDiagnosticsRows({
    type,
    scope,
    slug,
    period,
    builtBySlug,
    registryDiagnostics,
    autorecodeSnapshot,
  }));

  const templateCellPatches = exportTemplateNavigationPatches({
    workbook,
    builtBySlug,
    categoriesToExport,
    type,
    period,
    scope,
    slug,
  });
  writeTemplatePreservingWorkbook({
    workbook,
    destinationPath: filePath,
    templatePath: EXPORT_TEMPLATE_PATH,
    XLSX,
    templateCellPatches,
  });
  const record = {
    id: crypto.randomUUID(),
    type,
    scope,
    slug: scope === "all" ? null : slug,
    months: period.months,
    period: period.display,
    filename,
    path: filePath,
    size: fs.statSync(filePath).size,
    generatedAt: new Date().toISOString(),
    requestedBy,
    registryDiagnostics,
    autorecodeSnapshot,
  };
  const manifest = readExportManifest();
  manifest.records = [record, ...manifest.records.filter((item) => item.id !== record.id)].slice(0, 200);
  writeExportManifest(manifest);
  return record;
}

async function runExportSchedulerCheck() {
  const now = new Date();
  const day = now.getUTCDate();
  const dueTypes = [];
  if (day === 15) dueTypes.push("interim");
  if (day === 3) dueTypes.push("full", "rolling", "quarter");
  if (!dueTypes.length) return;

  const manifest = readExportManifest();
  for (const type of dueTypes) {
    try {
      const period = await resolveExportPeriod(type);
      if (!period.active) continue;
      const alreadyGenerated = manifest.records.some((record) =>
        record.type === type &&
        record.scope === "all" &&
        Array.isArray(record.months) &&
        record.months.join("|") === period.months.join("|"),
      );
      if (!alreadyGenerated) {
        await generateDataTableExport({ type, scope: "all", slug: "noodles", requestedBy: "scheduler" });
      }
    } catch (err) {
      console.warn(`[backend] export scheduler skipped ${type}: ${err && err.message ? err.message : String(err)}`);
    }
  }
}

function startExportScheduler() {
  if (exportTimer) return;
  const enabled = !/^(0|false|no|off)$/i.test(String(process.env.ENABLE_EXPORT_SCHEDULER || "true"));
  if (!enabled) return;
  runExportSchedulerCheck().catch((err) => {
    console.warn(`[backend] export scheduler check failed: ${err && err.message ? err.message : String(err)}`);
  });
  exportTimer = setInterval(() => {
    runExportSchedulerCheck().catch((err) => {
      console.warn(`[backend] export scheduler check failed: ${err && err.message ? err.message : String(err)}`);
    });
  }, 60 * 60 * 1000);
}

async function getCachedOrInFlight(cache, inFlight, cacheKey, ttlMs, factory) {
  const now = Date.now();
  const cached = cache.get(cacheKey);
  if (cached && now - cached.ts <= ttlMs) return cached.payload;
  if (inFlight.has(cacheKey)) return inFlight.get(cacheKey);

  const pending = Promise.resolve()
    .then(factory)
    .then((payload) => {
      cache.set(cacheKey, { ts: Date.now(), payload });
      return payload;
    })
    .finally(() => {
      inFlight.delete(cacheKey);
    });
  inFlight.set(cacheKey, pending);
  return pending;
}

function clearRuntimeCaches() {
  initialized = false;
  initPromise = null;
  schemaCache = null;
  combinedMarketViewsReady = false;
  combinedMarketMonthCutoff = "";
  questionCacheByCategory = new Map();
  headerAuditMetadataCache = null;
  awarenessQueryCache.clear();
  awarenessQueryInFlight.clear();
  overviewQueryCache.clear();
  overviewQueryInFlight.clear();
  filtersQueryCache.clear();
  pageDataQueryCache.clear();
  pageDataQueryInFlight.clear();
  pageDataMonthlyQueryCache.clear();
  pageDataMonthlyQueryInFlight.clear();
  exportFactColumnsCache = null;
  spssExportTablesReady = false;
  respondentDimColumns = ["category", "respondent_id", "month"];
}

function closeDuckDbConnection() {
  const currentConn = conn;
  const currentDb = db;
  conn = null;
  db = null;
  connectionPromise = null;
  return new Promise((resolve) => {
    const closeDb = () => {
      if (!currentDb || typeof currentDb.close !== "function") return resolve();
      try {
        currentDb.close(() => resolve());
      } catch (_err) {
        resolve();
      }
    };

    if (!currentConn || typeof currentConn.close !== "function") {
      closeDb();
      return;
    }

    try {
      currentConn.close(() => closeDb());
    } catch (_err) {
      closeDb();
    }
  });
}

async function validateDuckDbFile(dbPath) {
  return new Promise((resolve, reject) => {
    let validationDb = null;
    let validationConn = null;
    try {
      validationDb = new duckdb.Database(dbPath, duckdb.OPEN_READONLY);
      validationConn = validationDb.connect();
      validationConn.all(`
        SELECT
          (SELECT COUNT(*) FROM respondent_dims) AS respondent_dims,
          (SELECT COUNT(*) FROM responses_fact) AS responses_fact,
          (SELECT MAX(CAST(month AS VARCHAR)) FROM respondent_dims) AS latest_month
      `, (err, rows) => {
        const finish = (finishErr, payload) => {
          const closeDb = () => {
            if (!validationDb || typeof validationDb.close !== "function") {
              if (finishErr) reject(finishErr);
              else resolve(payload);
              return;
            }
            validationDb.close(() => {
              if (finishErr) reject(finishErr);
              else resolve(payload);
            });
          };

          if (validationConn && typeof validationConn.close === "function") {
            validationConn.close(() => closeDb());
          } else {
            closeDb();
          }
        };

        if (err) return finish(err);
        const row = rows?.[0] || {};
        const counts = {
          respondent_dims: Number(row.respondent_dims || 0),
          responses_fact: Number(row.responses_fact || 0),
          latest_month: row.latest_month || null,
        };
        if (!counts.respondent_dims || !counts.responses_fact) {
          return finish(new Error(`Generated DuckDB failed validation: ${JSON.stringify(counts)}`));
        }
        return finish(null, counts);
      });
    } catch (err) {
      try { if (validationConn?.close) validationConn.close(() => {}); } catch (_closeErr) {}
      try { if (validationDb?.close) validationDb.close(() => {}); } catch (_closeErr) {}
      reject(err);
    }
  });
}

function pruneDuckDbBackups(backupDir) {
  if (DUCKDB_BACKUP_RETENTION <= 0 || !fs.existsSync(backupDir)) return;
  const backups = fs.readdirSync(backupDir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && /\.duckdb$/i.test(entry.name))
    .map((entry) => {
      const fullPath = path.join(backupDir, entry.name);
      const stats = fs.statSync(fullPath);
      return { path: fullPath, mtimeMs: Number(stats.mtimeMs || 0) };
    })
    .sort((a, b) => b.mtimeMs - a.mtimeMs);
  backups.slice(DUCKDB_BACKUP_RETENTION).forEach((backup) => {
    try { fs.unlinkSync(backup.path); } catch (_err) {}
  });
}

function removeDuckDbSidecars(dbPath) {
  for (const suffix of [".wal", "-wal"]) {
    const walPath = `${dbPath}${suffix}`;
    if (fs.existsSync(walPath)) {
      try { fs.unlinkSync(walPath); } catch (_err) {}
    }
  }
}

function quarantineUnreadableDuckDbFile(dbPath) {
  if (!dbPath || !fs.existsSync(dbPath)) return null;

  const backupDir = path.join(DATA_ROOT, "backups");
  fs.mkdirSync(backupDir, { recursive: true });

  const timestamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  const fileStem = path.basename(dbPath, path.extname(dbPath));
  const quarantinedPath = path.join(backupDir, `${fileStem}-unreadable-${timestamp}.duckdb`);

  fs.renameSync(dbPath, quarantinedPath);
  removeDuckDbSidecars(dbPath);
  pruneDuckDbBackups(backupDir);
  return quarantinedPath;
}

function seedConfiguredDuckDbFromBundledSnapshot(targetPath) {
  const resolvedTargetPath = path.resolve(String(targetPath || ""));
  if (!resolvedTargetPath || fs.existsSync(resolvedTargetPath)) return null;
  const targetBasename = path.basename(resolvedTargetPath).toLowerCase();
  if (targetBasename.includes("current") && !/^(1|true|yes|on)$/i.test(String(process.env.ALLOW_BUNDLED_CURRENT_SEED || "false"))) {
    return null;
  }
  if (!fs.existsSync(BUNDLED_DB_PATH)) return null;

  fs.mkdirSync(path.dirname(resolvedTargetPath), { recursive: true });
  fs.copyFileSync(BUNDLED_DB_PATH, resolvedTargetPath);
  removeDuckDbSidecars(resolvedTargetPath);
  return BUNDLED_DB_PATH;
}

async function promoteDuckDbFile(tempDbPath) {
  const resolvedTempPath = path.resolve(String(tempDbPath || ""));
  if (!resolvedTempPath || !fs.existsSync(resolvedTempPath)) {
    throw new Error(`Sync produced no DuckDB file at ${resolvedTempPath || "(missing path)"}`);
  }

  const validation = await validateDuckDbFile(resolvedTempPath);
  syncDatabaseMaintenance = true;
  try {
    await closeDuckDbConnection();

    const activePath = CONFIGURED_DB_PATH;
    const backupDir = path.join(DATA_ROOT, "backups");
    fs.mkdirSync(path.dirname(activePath), { recursive: true });
    fs.mkdirSync(backupDir, { recursive: true });

    const timestamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
    const backupPath = path.join(backupDir, `current-${timestamp}.duckdb`);
    let movedExisting = false;

    try {
      if (fs.existsSync(activePath)) {
        fs.renameSync(activePath, backupPath);
        movedExisting = true;
      }
      fs.renameSync(resolvedTempPath, activePath);
    } catch (err) {
      if (movedExisting && !fs.existsSync(activePath) && fs.existsSync(backupPath)) {
        try { fs.renameSync(backupPath, activePath); } catch (_restoreErr) {}
      }
      throw err;
    }

    removeDuckDbSidecars(resolvedTempPath);

    pruneDuckDbBackups(backupDir);
    DUCKDB_CANDIDATES = listDuckDbCandidates(CONFIGURED_DB_PATH);
    DB_PATH = CONFIGURED_DB_PATH;
    loggedDbPath = false;
    loggedDuckDbCandidates = false;
    clearRuntimeCaches();
    await init();
    return {
      activeDbPath: activePath,
      backupPath: movedExisting ? backupPath : null,
      validation,
    };
  } finally {
    syncDatabaseMaintenance = false;
  }
}

async function tableExists(tableName) {
  const name = String(tableName || "").trim();
  if (!name) return false;
  try {
    await all(`DESCRIBE SELECT * FROM ${quoteIdentifier(name)}`);
    return true;
  } catch (_err) {
    return false;
  }
}

async function ensureInitialized() {
  if (initialized) return;
  if (!initPromise) {
    initPromise = init().catch((err) => {
      initPromise = null;
      throw err;
    });
  }
  await initPromise;
}

async function ensureSchemaLoaded() {
  await ensureInitialized();
  schemaCache = await readSchema();
  return schemaCache;
}

async function ensureTrendTables() {
  await ensureInitialized();
  const needsBaseFlags = !(await tableExists("base_flags"));
  const needsBauFacts = !(await tableExists("bau_metric_facts"));
  if (!needsBaseFlags && !needsBauFacts) return;

  if (needsBaseFlags) {
    await run(`
      CREATE OR REPLACE TEMP TABLE base_flags AS
      SELECT
        CAST(category AS VARCHAR) AS category,
        CAST(respondent_id AS VARCHAR) AS respondent_id,
        CAST(month AS VARCHAR) AS month,
        MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1A$') THEN 1 ELSE 0 END) AS has_brand_base,
        MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1C$') THEN 1 ELSE 0 END) AS has_ad_base
      FROM responses_fact
      GROUP BY 1, 2, 3
    `);
  }

  if (needsBauFacts) {
    const bauFactsSql = buildBauMetricFactsSql() || buildDefaultBauMetricFactsSql();
    await run(
      bauFactsSql.replace(
        /CREATE\s+OR\s+REPLACE\s+TABLE\s+bau_metric_facts\s+AS/i,
        "CREATE OR REPLACE TEMP TABLE bau_metric_facts AS",
      ),
    );
  }

  schemaCache = await readSchema();
}

async function ensureRespondentDims() {
  await ensureInitialized();
  if (!(await tableExists("respondent_dims"))) {
    throw new Error("respondent_dims table not found");
  }
  return ensureSchemaLoaded();
}

function quote(value) {
  return `'${String(value).replace(/'/g, "''")}'`;
}

function escapeRegexLiteral(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function quoteIdentifier(value) {
  return `"${String(value || "").replace(/"/g, "\"\"")}"`;
}

function sqlNormalizedDisplayLabel(expr, fallback = "(No response)") {
  const trimmed = `TRIM(CAST(${expr} AS VARCHAR))`;
  return `
    CASE
      WHEN NULLIF(${trimmed}, '') IS NULL THEN ${quote(fallback)}
      WHEN regexp_matches(${trimmed}, '^\\{0\\}$') THEN 'OTHERS'
      ELSE ${trimmed}
    END
  `;
}

function sqlOptionalDisplayLabel(expr) {
  const trimmed = `TRIM(CAST(${expr} AS VARCHAR))`;
  return `
    CASE
      WHEN NULLIF(${trimmed}, '') IS NULL THEN NULL
      WHEN regexp_matches(${trimmed}, '^\\{0\\}$') THEN 'OTHERS'
      ELSE ${trimmed}
    END
  `;
}

function sqlPreferredDisplayLabel(labelExpr, valueExpr, fallback = "(No response)") {
  const normalizedLabel = sqlOptionalDisplayLabel(labelExpr);
  const normalizedValue = sqlOptionalDisplayLabel(valueExpr);
  return `
    COALESCE(
      (${normalizedLabel}),
      (${normalizedValue}),
      ${quote(fallback)}
    )
  `;
}

function sqlBauCheckboxBrand(questionLabelExpr, answerLabelExpr, answerValueExpr) {
  const trimmedAnswerLabel = `NULLIF(TRIM(CAST(${answerLabelExpr} AS VARCHAR)), '')`;
  const cleanedAnswerLabel = `
    CASE
      WHEN lower(COALESCE(${trimmedAnswerLabel}, '')) IN ('yes', '1', '1.0', 'true') THEN NULL
      ELSE ${trimmedAnswerLabel}
    END
  `;
  const labelFromQuestion = `NULLIF(TRIM(regexp_extract(CAST(${questionLabelExpr} AS VARCHAR), '\\\\(([^()]*)\\\\)\\\\s*$', 1)), '')`;
  const labelFromColon = `NULLIF(TRIM(regexp_extract(CAST(${questionLabelExpr} AS VARCHAR), ':\\\\s*([^:?]+)\\\\??\\\\s*$', 1)), '')`;
  const trimmedAnswerValue = `NULLIF(TRIM(CAST(${answerValueExpr} AS VARCHAR)), '')`;
  const cleanedAnswerValue = `
    CASE
      WHEN lower(COALESCE(${trimmedAnswerValue}, '')) IN ('yes', '1', '1.0', 'true') THEN NULL
      ELSE ${trimmedAnswerValue}
    END
  `;
  return `
    NULLIF(
      TRIM(
        COALESCE(
          ${cleanedAnswerLabel},
          ${labelFromQuestion},
          ${labelFromColon},
          ${cleanedAnswerValue}
        )
      ),
      ''
    )
  `;
}

function sqlPositiveAnswerSelectionPredicate(answerLabelExpr, answerValueExpr, answerValueNumExpr) {
  return `
    (
      COALESCE(CAST(${answerValueNumExpr} AS DOUBLE), 0) = 1
      OR CAST(${answerValueExpr} AS VARCHAR) IN ('1', '1.0')
      OR lower(COALESCE(CAST(${answerLabelExpr} AS VARCHAR), '')) = 'yes'
    )
  `;
}

function sqlMeaningfulNonBinaryAnswer(answerLabelExpr, answerValueExpr) {
  const answerText = `NULLIF(TRIM(COALESCE(NULLIF(CAST(${answerLabelExpr} AS VARCHAR), ''), NULLIF(CAST(${answerValueExpr} AS VARCHAR), ''))), '')`;
  return `
    CASE
      WHEN ${answerText} IS NULL THEN NULL
      WHEN lower(${answerText}) IN ('yes', 'no', '1', '1.0', '0', '0.0', 'true', 'false', 'selected', 'not selected') THEN NULL
      ELSE ${answerText}
    END
  `;
}

function sqlMediaSourceBrand(questionLabelExpr) {
  return `
    NULLIF(
      TRIM(
        COALESCE(
          NULLIF(TRIM(regexp_extract(CAST(${questionLabelExpr} AS VARCHAR), '^[(]([^)]*)[)]', 1)), ''),
          NULLIF(TRIM(regexp_extract(CAST(${questionLabelExpr} AS VARCHAR), '(?i)about\\\\s+(.+?)\\\\s*[.:]', 1)), ''),
          NULLIF(TRIM(regexp_extract(CAST(${questionLabelExpr} AS VARCHAR), '(?i)brand\\\\s+(.+?)\\\\s*[.:]', 1)), '')
        )
      ),
      ''
    )
  `;
}

function sqlCanonicalMediaSourceBrand(category, questionExpr, questionLabelExpr) {
  const brandEntries = getCanonicalBrandCodeEntries(category);
  if (!brandEntries.length) return sqlMediaSourceBrand(questionLabelExpr);
  const brandIndex = `NULLIF(regexp_extract(CAST(${questionExpr} AS VARCHAR), '(?i)^I_([0-9]+)_A_', 1), '')`;
  const cases = brandEntries
    .map((entry) => `WHEN ${quote(String(entry.code))} THEN ${quote(entry.brand)}`)
    .join("\n          ");
  return `
    COALESCE(
      CASE ${brandIndex}
        ${cases}
        ELSE NULL
      END,
      ${sqlMediaSourceBrand(questionLabelExpr)}
    )
  `;
}

function sqlMediaSourceOption(questionLabelExpr, answerLabelExpr, answerValueExpr) {
  const cleanedQuestionLabel = `NULLIF(TRIM(regexp_replace(CAST(${questionLabelExpr} AS VARCHAR), '<[^>]+>', '', 'g')), '')`;
  return `
    NULLIF(
      TRIM(
        COALESCE(
          ${sqlMeaningfulNonBinaryAnswer(answerLabelExpr, answerValueExpr)},
          NULLIF(TRIM(regexp_extract(CAST(${questionLabelExpr} AS VARCHAR), '\\\\(([^()]*)\\\\)\\\\s*$', 1)), ''),
          NULLIF(TRIM(regexp_extract(CAST(${questionLabelExpr} AS VARCHAR), ':\\\\s*([^?]+)', 1)), ''),
          ${cleanedQuestionLabel}
        )
      ),
      ''
    )
  `;
}

function sqlValidDashboardBrandPredicate(brandExpr) {
  return `
    ${brandExpr} IS NOT NULL
    AND ${brandExpr} <> ''
    AND ${brandExpr} <> '{0}'
    AND lower(${brandExpr}) NOT IN ('none', 'none of these', 'other', 'others', 'yes', 'no')
    AND NOT regexp_matches(${brandExpr}, '(?i)^https?://')
  `;
}

function ident(value) {
  return `"${String(value).replace(/"/g, '""')}"`;
}

async function configureDuckDbRuntime() {
  try { fs.mkdirSync(DUCKDB_TEMP_DIRECTORY, { recursive: true }); } catch (_err) {}
  try { await run(`PRAGMA threads=${DUCKDB_THREADS}`); } catch (_err) {}
  try { await run(`PRAGMA memory_limit=${quote(DUCKDB_MEMORY_LIMIT)}`); } catch (_err) {}
  try { await run(`PRAGMA temp_directory=${quote(DUCKDB_TEMP_DIRECTORY)}`); } catch (_err) {}
  try { await run(`PRAGMA max_temp_directory_size=${quote(DUCKDB_MAX_TEMP_DIRECTORY_SIZE)}`); } catch (_err) {}
  try { await run("SET preserve_insertion_order=false"); } catch (_err) {}
  try { await run("PRAGMA enable_object_cache=true"); } catch (_err) {}
}

function normalizeDatamapQuestionKey(value) {
  return compactLabel(value).toLowerCase();
}

function parseDatamapRows(rows) {
  const pairsByNewKey = new Map();
  rows.slice(1).forEach((row) => {
    const oldQuestion = compactLabel(row?.[0]);
    const newQuestion = compactLabel(row?.[1]);
    const newQuestionKey = normalizeDatamapQuestionKey(newQuestion);
    if (!oldQuestion || !newQuestion || !newQuestionKey || pairsByNewKey.has(newQuestionKey)) return;
    pairsByNewKey.set(newQuestionKey, { newQuestionKey, oldQuestion, newQuestion });
  });
  return Array.from(pairsByNewKey.values());
}

function loadDatamapRowsWithPython(datamapPath) {
  const script = [
    "import json, sys",
    "from openpyxl import load_workbook",
    "path = sys.argv[1]",
    "wb = load_workbook(path, read_only=True, data_only=True)",
    "ws = wb[wb.sheetnames[0]]",
    "rows = []",
    "for row in ws.iter_rows(values_only=True):",
    "    rows.append(['' if value is None else str(value) for value in row[:2]])",
    "print(json.dumps(rows))",
  ].join("\n");
  const result = spawnSync(PYTHON_BIN, ["-c", script, datamapPath], {
    encoding: "utf8",
    maxBuffer: 20 * 1024 * 1024,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || `python exited with ${result.status}`).trim());
  }
  return JSON.parse(result.stdout || "[]");
}

function loadDatamapQuestionPairs(datamapPath = BHT_DATAMAP_PATH) {
  if (!datamapPath || !fs.existsSync(datamapPath)) return [];

  try {
    if (XLSX) {
      const workbook = XLSX.readFile(datamapPath);
      const sheetName = workbook.SheetNames[0];
      const sheet = sheetName ? workbook.Sheets[sheetName] : null;
      if (!sheet) return [];
      return parseDatamapRows(XLSX.utils.sheet_to_json(sheet, { header: 1, raw: false, defval: "" }));
    }

    return parseDatamapRows(loadDatamapRowsWithPython(datamapPath));
  } catch (err) {
    console.warn(`[backend] DataMap load failed for ${datamapPath}: ${err && err.message ? err.message : String(err)}`);
    return [];
  }
}

async function setupDatamapQuestionMap() {
  await run(`
    CREATE OR REPLACE TEMP TABLE datamap_question_map (
      new_question_key VARCHAR,
      old_question VARCHAR,
      new_question VARCHAR
    )
  `);

  const pairs = loadDatamapQuestionPairs();
  datamapRawQuestionCodesByOldKey = new Map();
  pairs.forEach((pair) => {
    const oldKey = normalizeDatamapQuestionKey(pair.oldQuestion);
    if (!oldKey) return;
    if (!datamapRawQuestionCodesByOldKey.has(oldKey)) {
      datamapRawQuestionCodesByOldKey.set(oldKey, new Set());
    }
    datamapRawQuestionCodesByOldKey.get(oldKey).add(pair.newQuestion);
  });
  if (!pairs.length) {
    console.log(`[backend] DataMap question map: no mappings loaded from ${BHT_DATAMAP_PATH}`);
    return 0;
  }

  const chunkSize = 500;
  for (let index = 0; index < pairs.length; index += chunkSize) {
    const chunk = pairs.slice(index, index + chunkSize);
    // eslint-disable-next-line no-await-in-loop
    await run(`
      INSERT INTO datamap_question_map (new_question_key, old_question, new_question)
      VALUES ${chunk
        .map((pair) => `(${quote(pair.newQuestionKey)}, ${quote(pair.oldQuestion)}, ${quote(pair.newQuestion)})`)
        .join(", ")}
    `);
  }

  console.log(`[backend] DataMap question map: loaded ${pairs.length} mappings from ${BHT_DATAMAP_PATH}`);
  return pairs.length;
}

function expandRawQuestionCodes(questionCodes) {
  const expanded = new Set();
  (Array.isArray(questionCodes) ? questionCodes : []).forEach((value) => {
    const questionCode = compactLabel(value);
    if (!questionCode) return;
    expanded.add(questionCode);
    const mappedCodes = datamapRawQuestionCodesByOldKey.get(normalizeDatamapQuestionKey(questionCode));
    if (mappedCodes) mappedCodes.forEach((mappedCode) => expanded.add(mappedCode));
  });
  return Array.from(expanded);
}

async function buildResponsesFactQuestionFilterSql(alias, questionCodes) {
  const columns = await describeRelationColumns("responses_fact");
  const expandedCodes = expandRawQuestionCodes(questionCodes);
  const questionColumn = columns.includes("question_raw") ? "question_raw" : "question";
  return `CAST(${alias}.${ident(questionColumn)} AS VARCHAR) IN (${expandedCodes.map((value) => quote(value)).join(", ")})`;
}

/**
 * Keeps market_insights.duckdb and current.duckdb side by side.
 *
 * The historical market DB is attached read-only, then temp views named
 * respondent_dims/responses_fact expose market months plus current months.
 * This avoids copying the large market DB into current.duckdb on Render.
 */
async function mergeMarketInsightsDb() {
  await setupCombinedMarketViews();
  return;

  if (!fs.existsSync(MARKET_DB_PATH)) {
    console.log(`[backend] market_insights merge: file not found at ${MARKET_DB_PATH}, skipping`);
    return;
  }
  if (path.resolve(MARKET_DB_PATH) === path.resolve(DB_PATH)) {
    console.log("[backend] market_insights merge: MARKET_DB_PATH === DB_PATH, skipping");
    return;
  }

  const alias = "mkt_db";
  let attached = false;
  try {
    console.log(`[backend] market_insights merge: attaching ${MARKET_DB_PATH}`);
    await run(`ATTACH ${quote(MARKET_DB_PATH)} AS ${alias} (READ_ONLY)`);
    attached = true;

    // ── 1. Check the secondary DB has the expected tables ──────────────────
    const mktTables = await all(`
      SELECT table_name
      FROM information_schema.tables
      WHERE table_catalog = '${alias}' AND table_schema = 'main'
    `);
    const mktTableNames = new Set(mktTables.map((r) => String(r.table_name || "")));
    const hasDims = mktTableNames.has("respondent_dims");
    const hasFact = mktTableNames.has("responses_fact");
    let marketMonthCutoff = compactLabel(MARKET_INSIGHTS_CURRENT_MONTH);
    if (!marketMonthCutoff && hasDims) {
      const cutoffRows = await all(`
        SELECT MAX(CAST(month AS VARCHAR)) AS latest_month
        FROM ${alias}.main.respondent_dims
      `);
      marketMonthCutoff = compactLabel(cutoffRows?.[0]?.latest_month);
    }
    if (!marketMonthCutoff && hasFact) {
      const cutoffRows = await all(`
        SELECT MAX(CAST(month AS VARCHAR)) AS latest_month
        FROM ${alias}.main.responses_fact
      `);
      marketMonthCutoff = compactLabel(cutoffRows?.[0]?.latest_month);
    }

    if (!hasDims && !hasFact) {
      console.warn("[backend] market_insights merge: secondary DB has neither respondent_dims nor responses_fact — skipping");
      return;
    }

    // ── 2. Merge respondent_dims ───────────────────────────────────────────
    if (!marketMonthCutoff) {
      console.warn("[backend] market_insights merge: could not determine market month cutoff; skipping");
      return;
    }
    console.log(`[backend] market_insights merge: importing months <= ${marketMonthCutoff}`);

    const existingMarketHistory = await all(`
      SELECT
        EXISTS (
          SELECT 1 FROM respondent_dims
          WHERE CAST(month AS VARCHAR) <= ${quote(marketMonthCutoff)}
          LIMIT 1
        ) AS has_dims,
        EXISTS (
          SELECT 1 FROM responses_fact
          WHERE CAST(month AS VARCHAR) <= ${quote(marketMonthCutoff)}
          LIMIT 1
        ) AS has_fact
    `);
    if (Boolean(existingMarketHistory?.[0]?.has_dims) && Boolean(existingMarketHistory?.[0]?.has_fact)) {
      console.log(`[backend] market_insights merge: primary already has market history through ${marketMonthCutoff}, skipping`);
      return;
    }

    if (hasDims) {
      // Detect dim columns available in primary so we only select what exists
      const primaryDimCols = await getTableColumns("respondent_dims");
      const mktDimColRows = await all(`
        SELECT column_name FROM information_schema.columns
        WHERE table_catalog = '${alias}' AND table_schema = 'main' AND table_name = 'respondent_dims'
      `);
      const mktDimCols = new Set(mktDimColRows.map((r) => String(r.column_name || "")));

      // Build SELECT list: only columns that exist in BOTH tables
      const dimSelect = primaryDimCols
        .filter((c) => mktDimCols.has(c))
        .map((c) => `CAST(${ident(c)} AS VARCHAR) AS ${ident(c)}`)
        .join(", ");

      if (!dimSelect) {
        console.warn("[backend] market_insights merge: no overlapping columns in respondent_dims — skipping dims");
      } else {
        const dimResult = await all(`
          INSERT INTO respondent_dims (${primaryDimCols.filter((c) => mktDimCols.has(c)).map((c) => ident(c)).join(", ")})
          SELECT ${dimSelect}
          FROM ${alias}.main.respondent_dims src
          WHERE CAST(src.month AS VARCHAR) <= ${quote(marketMonthCutoff)}
            AND NOT EXISTS (
              SELECT 1 FROM respondent_dims tgt
              WHERE CAST(tgt.category     AS VARCHAR) = CAST(src.category     AS VARCHAR)
                AND CAST(tgt.respondent_id AS VARCHAR) = CAST(src.respondent_id AS VARCHAR)
                AND CAST(tgt.month        AS VARCHAR) = CAST(src.month        AS VARCHAR)
            )
          RETURNING 1
        `);
        console.log(`[backend] market_insights merge: inserted ${dimResult.length} rows into respondent_dims`);
      }
    }

    // ── 3. Merge responses_fact ────────────────────────────────────────────
    if (hasFact) {
      const primaryFactCols = await getTableColumns("responses_fact");
      const mktFactColRows = await all(`
        SELECT column_name FROM information_schema.columns
        WHERE table_catalog = '${alias}' AND table_schema = 'main' AND table_name = 'responses_fact'
      `);
      const mktFactCols = new Set(mktFactColRows.map((r) => String(r.column_name || "")));

      const factColsToInsert = primaryFactCols.filter((c) => mktFactCols.has(c));
      const factSelect = factColsToInsert
        .map((c) => {
          if (c === "answer_value_num") return `CAST(src.${ident(c)} AS DOUBLE) AS ${ident(c)}`;
          return `CAST(src.${ident(c)} AS VARCHAR) AS ${ident(c)}`;
        })
        .join(", ");

      if (!factSelect) {
        console.warn("[backend] market_insights merge: no overlapping columns in responses_fact — skipping fact");
      } else {
        const factResult = await all(`
          INSERT INTO responses_fact (${factColsToInsert.map((c) => ident(c)).join(", ")})
          SELECT ${factSelect}
          FROM ${alias}.main.responses_fact src
          WHERE CAST(src.month AS VARCHAR) <= ${quote(marketMonthCutoff)}
            AND NOT EXISTS (
              SELECT 1 FROM responses_fact tgt
              WHERE CAST(tgt.category      AS VARCHAR) = CAST(src.category      AS VARCHAR)
                AND CAST(tgt.respondent_id AS VARCHAR) = CAST(src.respondent_id AS VARCHAR)
                AND CAST(tgt.month         AS VARCHAR) = CAST(src.month         AS VARCHAR)
                AND CAST(tgt.question      AS VARCHAR) = CAST(src.question      AS VARCHAR)
            )
          RETURNING 1
        `);
        console.log(`[backend] market_insights merge: inserted ${factResult.length} rows into responses_fact`);
      }
    }

    console.log("[backend] market_insights merge: complete");
  } catch (err) {
    console.error("[backend] market_insights merge error:", err && err.message ? err.message : String(err));
  } finally {
    if (attached) {
      try { await run(`DETACH ${alias}`); } catch (_err) {}
    }
  }
}


async function syncInspectTablesIntoPrimary() {
  if (!fs.existsSync(INSPECT_DB_PATH)) return;
  if (path.resolve(DB_PATH) === path.resolve(INSPECT_DB_PATH)) return;

  let attached = false;
  try {
    await run(`ATTACH ${quote(INSPECT_DB_PATH)} AS inspect_db`);
    attached = true;
    const inspectTables = await all(`
      SELECT table_name
      FROM information_schema.tables
      WHERE table_catalog = 'inspect_db'
        AND table_schema = 'main'
      ORDER BY table_name
    `);

    for (const row of inspectTables) {
      const tableName = String(row?.table_name || "").trim();
      if (!tableName) continue;
      // eslint-disable-next-line no-await-in-loop
      if (await tableExists(tableName)) continue;
      // eslint-disable-next-line no-await-in-loop
      await run(`
        CREATE TABLE ${quoteIdentifier(tableName)} AS
        SELECT *
        FROM inspect_db.main.${quoteIdentifier(tableName)}
      `);
      console.log(`[backend] synced inspect table into primary DB: ${tableName}`);
    }
  } finally {
    if (attached) {
      try { await run("DETACH inspect_db"); } catch (_err) {}
    }
  }
}

async function getTableColumns(tableName) {
  const rows = await all(`DESCRIBE SELECT * FROM ${tableName}`);
  return rows.map((row) => row.column_name);
}

async function hasAllTables(tableNames) {
  for (const tableName of tableNames) {
    // eslint-disable-next-line no-await-in-loop
    if (!(await tableExists(tableName))) return false;
  }
  return true;
}

async function getNativeTableCounts() {
  const hasNativeTables = await hasAllTables(REQUIRED_NATIVE_TABLES);
  if (!hasNativeTables) {
    return { respondentDims: 0, responsesFact: 0, latestMonth: null };
  }
  const rows = await all(`
    SELECT
      (SELECT COUNT(*) FROM respondent_dims) AS respondent_dims,
      (SELECT COUNT(*) FROM responses_fact) AS responses_fact,
      (SELECT MAX(CAST(month AS VARCHAR)) FROM respondent_dims) AS latest_month
  `);
  const row = rows[0] || {};
  return {
    respondentDims: Number(row.respondent_dims || 0),
    responsesFact: Number(row.responses_fact || 0),
    latestMonth: row.latest_month || null,
  };
}

async function describeRelationColumns(relationSql) {
  try {
    const rows = await all(`DESCRIBE SELECT * FROM ${relationSql}`);
    return rows.map((row) => String(row.column_name || "")).filter(Boolean);
  } catch (_err) {
    return [];
  }
}

function buildProjectedSelect(relationSql, outputColumns, sourceColumns, numericColumns = new Set(), whereSql = "", options = {}) {
  const sourceSet = new Set(sourceColumns);
  const applyQuestionDatamap = Boolean(options.applyQuestionDatamap && sourceSet.has("question"));
  const selectSql = outputColumns
    .map((column) => {
      if (column === "question_raw" && sourceSet.has("question")) {
        return `CAST(src.${ident("question")} AS VARCHAR) AS ${ident(column)}`;
      }
      if (!sourceSet.has(column)) {
        return numericColumns.has(column)
          ? `CAST(NULL AS DOUBLE) AS ${ident(column)}`
          : `CAST(NULL AS VARCHAR) AS ${ident(column)}`;
      }
      if (applyQuestionDatamap && column === "question") {
        return `COALESCE(dm.old_question, CAST(src.${ident(column)} AS VARCHAR)) AS ${ident(column)}`;
      }
      if (numericColumns.has(column)) return `CAST(src.${ident(column)} AS DOUBLE) AS ${ident(column)}`;
      return `CAST(src.${ident(column)} AS VARCHAR) AS ${ident(column)}`;
    })
    .join(", ");
  const datamapJoin = applyQuestionDatamap
    ? `LEFT JOIN datamap_question_map dm ON lower(trim(CAST(src.${ident("question")} AS VARCHAR))) = dm.new_question_key`
    : "";
  return `SELECT ${selectSql} FROM ${relationSql} src ${datamapJoin} ${whereSql || ""}`;
}

async function auditHistoricalDatamapProjection(months = HISTORICAL_DATAMAP_AUDIT_MONTHS) {
  const monthList = Array.from(new Set(months.map((value) => compactLabel(value)).filter(Boolean)));
  if (!monthList.length) return null;
  const monthSql = monthList.map((month) => quote(month)).join(", ");
  const rows = await all(`
    SELECT
      CAST(r.month AS VARCHAR) AS month,
      COUNT(*)::BIGINT AS response_rows,
      COUNT(DISTINCT CAST(r.question AS VARCHAR))::BIGINT AS old_format_questions,
      SUM(
        CASE
          WHEN r.question_raw IS NOT NULL
           AND lower(trim(CAST(r.question_raw AS VARCHAR))) <> lower(trim(CAST(r.question AS VARCHAR)))
          THEN 1 ELSE 0
        END
      )::BIGINT AS mapped_rows,
      SUM(
        CASE
          WHEN dm.new_question_key IS NOT NULL
           AND lower(trim(CAST(dm.old_question AS VARCHAR))) <> lower(trim(CAST(r.question AS VARCHAR)))
          THEN 1 ELSE 0
        END
      )::BIGINT AS leaked_new_format_rows
    FROM responses_fact r
    LEFT JOIN datamap_question_map dm
      ON lower(trim(CAST(r.question AS VARCHAR))) = dm.new_question_key
    WHERE CAST(r.month AS VARCHAR) IN (${monthSql})
    GROUP BY 1
    ORDER BY 1
  `);

  const missingMonths = monthList.filter(
    (month) => !rows.some((row) => compactLabel(row.month) === month && Number(row.response_rows || 0) > 0),
  );
  const leakedRows = rows.reduce((sum, row) => sum + Number(row.leaked_new_format_rows || 0), 0);
  if (missingMonths.length) {
    throw new Error(`Historical DataMap audit found no response rows for: ${missingMonths.join(", ")}`);
  }
  if (leakedRows > 0) {
    throw new Error(
      `Historical DataMap audit found ${leakedRows} new-format response rows after projection for ${monthList.join(", ")}.`,
    );
  }

  console.log(
    `[backend] historical DataMap audit: ${rows
      .map((row) =>
        `${row.month}=${Number(row.response_rows || 0)} rows, `
        + `${Number(row.old_format_questions || 0)} old-format questions, `
        + `${Number(row.mapped_rows || 0)} rows remapped`,
      )
      .join("; ")}`,
  );
  console.log("[backend] historical header metadata: old-format market audit active for April/May 2026");
  return rows;
}

async function setupCombinedMarketViews() {
  combinedMarketViewsReady = false;
  if (!fs.existsSync(MARKET_DB_PATH)) {
    console.log(`[backend] market_insights attach: file not found at ${MARKET_DB_PATH}, using current DB only`);
    return false;
  }
  if (path.resolve(MARKET_DB_PATH) === path.resolve(DB_PATH)) {
    console.log("[backend] market_insights attach: MARKET_DB_PATH === DB_PATH, using single DB");
    return false;
  }

  const alias = "market_db";
  console.log(`[backend] market_insights attach: ${MARKET_DB_PATH}`);
  try {
    await run(`ATTACH ${quote(MARKET_DB_PATH)} AS ${alias} (READ_ONLY)`);
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    if (!/already exists/i.test(message)) throw err;
  }

  const marketDimColumns = await describeRelationColumns(`${alias}.main.respondent_dims`);
  const marketFactColumns = await describeRelationColumns(`${alias}.main.responses_fact`);
  if (!marketDimColumns.length || !marketFactColumns.length) {
    console.warn("[backend] market_insights attach: market DB is missing respondent_dims or responses_fact");
    return false;
  }

  const cutoffRows = await all(`
    SELECT MAX(CAST(month AS VARCHAR)) AS latest_month
    FROM ${alias}.main.respondent_dims
  `);
  const marketMonthCutoff = compactLabel(MARKET_INSIGHTS_CURRENT_MONTH || cutoffRows?.[0]?.latest_month);
  if (!marketMonthCutoff) {
    console.warn("[backend] market_insights attach: could not determine market month cutoff");
    return false;
  }
  combinedMarketMonthCutoff = marketMonthCutoff;

  // Materialize the small current DB tables before creating same-named combined
  // views. DuckDB lazily binds views, so a snapshot view over main.responses_fact
  // can resolve back to the combined responses_fact view and recurse.
  await run("CREATE OR REPLACE TEMP TABLE current_respondent_dims AS SELECT * FROM main.respondent_dims");
  await run("CREATE OR REPLACE TEMP TABLE current_responses_fact_raw AS SELECT * FROM main.responses_fact");
  await setupDatamapQuestionMap();

  const currentDimColumns = await describeRelationColumns("current_respondent_dims");
  const currentFactColumns = await describeRelationColumns("current_responses_fact_raw");
  const numericFactColumns = new Set(["answer_value_num"]);
  await run(`
    CREATE OR REPLACE TEMP VIEW current_responses_fact AS
    ${buildProjectedSelect("current_responses_fact_raw", currentFactColumns, currentFactColumns, numericFactColumns, "", { applyQuestionDatamap: true })}
  `);
  const dimColumns = Array.from(new Set([
    "category",
    "respondent_id",
    "month",
    ...RESPONDENT_DIM_FIELDS,
    ...marketDimColumns,
    ...currentDimColumns,
  ]));
  const factColumns = Array.from(new Set([
    "category",
    "respondent_id",
    "month",
    "question_raw",
    "question",
    "question_label",
    "answer_label",
    "answer_value",
    "answer_value_num",
    ...marketFactColumns,
    ...currentFactColumns,
  ]));
  const marketWhere = `WHERE CAST(src.month AS VARCHAR) <= ${quote(marketMonthCutoff)}`;
  const currentWhere = `WHERE CAST(src.month AS VARCHAR) > ${quote(marketMonthCutoff)}`;

  await run(`
    CREATE OR REPLACE TEMP VIEW respondent_dims AS
    ${buildProjectedSelect(`${alias}.main.respondent_dims`, dimColumns, marketDimColumns, new Set(), marketWhere)}
    UNION ALL
    ${buildProjectedSelect("current_respondent_dims", dimColumns, currentDimColumns, new Set(), currentWhere)}
  `);
  await run(`
    CREATE OR REPLACE TEMP TABLE dashboard_respondent_dims AS
    SELECT
      CAST(category AS VARCHAR) AS category,
      CAST(respondent_id AS VARCHAR) AS respondent_id,
      CAST(month AS VARCHAR) AS month,
      ${RESPONDENT_DIM_FIELDS.map((field) => `CAST(${ident(field)} AS VARCHAR) AS ${ident(field)}`).join(",\n      ")}
    FROM respondent_dims
  `);
  try {
    await run("CREATE INDEX idx_dashboard_dims_category_month ON dashboard_respondent_dims(category, month)");
  } catch (_err) {}
  try {
    await run(
      "CREATE INDEX idx_dashboard_dims_respondent "
      + "ON dashboard_respondent_dims(category, respondent_id, month)",
    );
  } catch (_err) {}
  await run(`
    CREATE OR REPLACE TEMP TABLE dashboard_filter_options AS
    ${[
      ["month", "month"],
      ...RESPONDENT_DIM_FIELDS.map((field) => [field, field]),
    ]
      .map(
        ([filterKey, column]) => `
          SELECT DISTINCT
            CAST(category AS VARCHAR) AS category,
            ${quote(filterKey)}::VARCHAR AS ${ident("filter_key")},
            CAST(${ident(column)} AS VARCHAR) AS ${ident("value")}
          FROM dashboard_respondent_dims
          WHERE ${ident(column)} IS NOT NULL
            AND TRIM(CAST(${ident(column)} AS VARCHAR)) <> ''
        `,
      )
      .join("\nUNION ALL\n")}
  `);
  try {
    await run("CREATE INDEX idx_dashboard_filter_options ON dashboard_filter_options(category, filter_key)");
  } catch (_err) {}

  await run(`
    CREATE OR REPLACE TEMP VIEW responses_fact AS
    ${buildProjectedSelect(`${alias}.main.responses_fact`, factColumns, marketFactColumns, numericFactColumns, marketWhere, { applyQuestionDatamap: true })}
    UNION ALL
    ${buildProjectedSelect("current_responses_fact_raw", factColumns, currentFactColumns, numericFactColumns, currentWhere, { applyQuestionDatamap: true })}
  `);

  await run(`
    CREATE OR REPLACE TEMP VIEW question_catalog AS
    SELECT DISTINCT CAST(category AS VARCHAR) AS category, CAST(question AS VARCHAR) AS question, CAST(question_label AS VARCHAR) AS question_label
    FROM responses_fact
    WHERE question IS NOT NULL
  `);
  await auditHistoricalDatamapProjection();

  await run(`
    CREATE OR REPLACE TEMP TABLE current_base_flags AS
    SELECT
      CAST(category AS VARCHAR) AS category,
      CAST(respondent_id AS VARCHAR) AS respondent_id,
      CAST(month AS VARCHAR) AS month,
      MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1A$') THEN 1 ELSE 0 END) AS has_brand_base,
      MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1C$') THEN 1 ELSE 0 END) AS has_ad_base
    FROM current_responses_fact
    WHERE CAST(month AS VARCHAR) > ${quote(marketMonthCutoff)}
    GROUP BY 1, 2, 3
  `);
  if ((await describeRelationColumns(`${alias}.main.base_flags`)).length) {
    await run(`
      CREATE OR REPLACE TEMP VIEW base_flags AS
      SELECT category, respondent_id, month, has_brand_base, has_ad_base
      FROM ${alias}.main.base_flags
      WHERE CAST(month AS VARCHAR) <= ${quote(marketMonthCutoff)}
      UNION ALL
      SELECT category, respondent_id, month, has_brand_base, has_ad_base
      FROM current_base_flags
    `);
  } else {
    await run(`
      CREATE OR REPLACE TEMP VIEW base_flags AS
      SELECT
        CAST(category AS VARCHAR) AS category,
        CAST(respondent_id AS VARCHAR) AS respondent_id,
        CAST(month AS VARCHAR) AS month,
        MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1A$') THEN 1 ELSE 0 END) AS has_brand_base,
        MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1C$') THEN 1 ELSE 0 END) AS has_ad_base
      FROM responses_fact
      GROUP BY 1, 2, 3
    `);
  }

  const currentBauSql = buildBauMetricFactsSql(
    DB_PATH,
    "current_responses_fact",
    `WHERE CAST(month AS VARCHAR) > ${quote(marketMonthCutoff)}`,
  ).replace(
    /CREATE\s+OR\s+REPLACE\s+TABLE\s+bau_metric_facts\s+AS/i,
    "CREATE OR REPLACE TEMP TABLE current_bau_metric_facts AS",
  );
  await run(currentBauSql);

  if ((await describeRelationColumns(`${alias}.main.bau_metric_facts`)).length) {
    await run(`
      CREATE OR REPLACE TEMP VIEW bau_metric_facts AS
      SELECT category, respondent_id, month, metric, brand, option
      FROM ${alias}.main.bau_metric_facts
      WHERE CAST(month AS VARCHAR) <= ${quote(marketMonthCutoff)}
      UNION ALL
      SELECT category, respondent_id, month, metric, brand, option
      FROM current_bau_metric_facts
    `);
  } else {
    await run(
      buildBauMetricFactsSql().replace(
        /CREATE\s+OR\s+REPLACE\s+TABLE\s+bau_metric_facts\s+AS/i,
        "CREATE OR REPLACE TEMP VIEW bau_metric_facts AS",
      ),
    );
  }

  combinedMarketViewsReady = true;
  console.log(
    `[backend] market_insights attach: serving market <= ${marketMonthCutoff} plus current DB months after that; `
    + "dashboard filter options precomputed",
  );
  return true;
}

async function inspectDuckDbFile(dbPath) {
  return new Promise((resolve, reject) => {
    let inspectDb = null;
    let inspectConn = null;
    try {
      inspectDb = new duckdb.Database(dbPath);
      inspectConn = inspectDb.connect();
      inspectConn.all(`
        SELECT
          (SELECT COUNT(*) FROM respondent_dims) AS respondent_dims,
          (SELECT COUNT(*) FROM responses_fact) AS responses_fact,
          (SELECT MAX(CAST(month AS VARCHAR)) FROM respondent_dims) AS latest_month
      `, (err, rows) => {
        const finish = (finishErr, payload) => {
          const closeDb = () => {
            if (!inspectDb || typeof inspectDb.close !== "function") {
              if (finishErr) reject(finishErr);
              else resolve(payload);
              return;
            }
            inspectDb.close(() => {
              if (finishErr) reject(finishErr);
              else resolve(payload);
            });
          };

          if (inspectConn && typeof inspectConn.close === "function") {
            inspectConn.close(() => closeDb());
          } else {
            closeDb();
          }
        };

        if (err) return finish(err);
        const row = rows?.[0] || {};
        return finish(null, {
          respondentDims: Number(row.respondent_dims || 0),
          responsesFact: Number(row.responses_fact || 0),
          latestMonth: row.latest_month || null,
        });
      });
    } catch (err) {
      try { if (inspectConn?.close) inspectConn.close(() => {}); } catch (_closeErr) {}
      try { if (inspectDb?.close) inspectDb.close(() => {}); } catch (_closeErr) {}
      reject(err);
    }
  });
}

async function switchToMarketDbIfPrimaryEmpty() {
  if (/^(1|true|yes|on)$/i.test(String(process.env.DISABLE_MARKET_DB_FALLBACK || "false"))) return false;
  if (fs.existsSync(CONFIGURED_DB_PATH) && path.resolve(DB_PATH) === path.resolve(CONFIGURED_DB_PATH)) {
    console.log("[backend] primary DuckDB is empty; keeping current DB active and using market DB through combined views");
    return false;
  }
  if (!fs.existsSync(MARKET_DB_PATH)) return false;
  if (path.resolve(MARKET_DB_PATH) === path.resolve(DB_PATH)) return false;

  const primaryCounts = await getNativeTableCounts();
  if (primaryCounts.respondentDims > 0 && primaryCounts.responsesFact > 0) return false;

  let marketCounts = null;
  try {
    marketCounts = await inspectDuckDbFile(MARKET_DB_PATH);
  } catch (err) {
    console.warn(`[backend] market DB fallback unavailable for ${MARKET_DB_PATH}: ${err && err.message ? err.message : String(err)}`);
    return false;
  }

  if (!marketCounts.respondentDims || !marketCounts.responsesFact) {
    console.warn(`[backend] market DB fallback skipped because ${MARKET_DB_PATH} has no native rows: ${JSON.stringify(marketCounts)}`);
    return false;
  }

  await closeDuckDbConnection();
  DB_PATH = MARKET_DB_PATH;
  DUCKDB_CANDIDATES = [{
    path: MARKET_DB_PATH,
    size: Number(fs.statSync(MARKET_DB_PATH).size || 0),
    mtimeMs: Number(fs.statSync(MARKET_DB_PATH).mtimeMs || 0),
  }];
  loggedDbPath = false;
  loggedDuckDbCandidates = false;
  schemaCache = null;
  questionCacheByCategory = new Map();
  console.warn(
    `[backend] primary DuckDB is empty; using market DB fallback ${MARKET_DB_PATH} `
    + `(${marketCounts.respondentDims} respondent_dims, ${marketCounts.responsesFact} responses_fact)`,
  );
  return true;
}

function buildRespondentDimProjectionSql(alias, columns) {
  return RESPONDENT_DIM_FIELDS
    .map((field) => {
      const sourceColumn = resolveRespondentDimColumn(columns, field);
      return sourceColumn
        ? `CAST(${alias}.${ident(sourceColumn)} AS VARCHAR) AS ${ident(field)}`
        : `CAST(NULL AS VARCHAR) AS ${ident(field)}`;
    })
    .join(",\n      ");
}

async function ensureResponsesFactCompatibilityColumns() {
  if (!(await tableExists("responses_fact"))) return;

  const factColumns = await getTableColumns("responses_fact");
  let addedAnswerValue = false;

  if (!factColumns.includes("answer_value")) {
    await run("ALTER TABLE responses_fact ADD COLUMN answer_value VARCHAR");
    await run(`
      UPDATE responses_fact
      SET answer_value = COALESCE(answer_value, answer_label)
      WHERE answer_value IS NULL
    `);
    console.log("[backend] added responses_fact.answer_value compatibility column");
    addedAnswerValue = true;
  }

  const refreshedColumns = addedAnswerValue ? await getTableColumns("responses_fact") : factColumns;
  if (!refreshedColumns.includes("answer_value_num")) {
    await run("ALTER TABLE responses_fact ADD COLUMN answer_value_num DOUBLE");
    await run(`
      UPDATE responses_fact
      SET answer_value_num = TRY_CAST(
        NULLIF(TRIM(CAST(COALESCE(answer_value, answer_label) AS VARCHAR)), '')
        AS DOUBLE
      )
      WHERE answer_value_num IS NULL
    `);
    console.log("[backend] added responses_fact.answer_value_num compatibility column");
  }
}

async function ensureNativeBackedResponseViews() {
  if (!(await hasAllTables(REQUIRED_NATIVE_TABLES))) return false;

  await ensureResponsesFactCompatibilityColumns();

  const factColumns = await getTableColumns("responses_fact");
  const dimColumns = await getTableColumns("respondent_dims");

  const factCategoryCol = detectColumn(factColumns, ["category"]);
  const factRespondentIdCol = detectColumn(factColumns, ["respondent_id", "SbjNum", "sbjnum"]);
  const factMonthCol = detectColumn(factColumns, ["month", "file_month"]);
  const factQuestionCol = detectColumn(factColumns, ["question"]);
  const factQuestionLabelCol = detectColumn(factColumns, ["question_label"]);
  const factAnswerLabelCol = detectColumn(factColumns, ["answer_label"]);
  const factAnswerValueCol = detectColumn(factColumns, ["answer_value", "answer_label"]);
  const factAnswerValueNumCol = detectColumn(factColumns, ["answer_value_num"]);
  const dimCategoryCol = detectColumn(dimColumns, ["category"]);
  const dimRespondentIdCol = detectColumn(dimColumns, ["respondent_id", "SbjNum", "sbjnum"]);
  const dimMonthCol = detectColumn(dimColumns, ["month", "file_month"]);

  if (!factCategoryCol || !factRespondentIdCol || !factMonthCol || !factQuestionCol || !factAnswerLabelCol) {
    throw new Error("responses_fact is missing required columns for native DuckDB startup.");
  }
  if (!dimCategoryCol || !dimRespondentIdCol || !dimMonthCol) {
    throw new Error("respondent_dims is missing required columns for native DuckDB startup.");
  }

  if (activeDbIsReadOnlyMarketDb()) {
    return true;
  }

  const dimProjectionSql = buildRespondentDimProjectionSql("d", dimColumns);

  await run(`
    CREATE OR REPLACE VIEW responses_long AS
    SELECT
      CAST(f.${ident(factCategoryCol)} AS VARCHAR) AS category,
      CAST(f.${ident(factRespondentIdCol)} AS VARCHAR) AS respondent_id,
      CAST(f.${ident(factRespondentIdCol)} AS VARCHAR) AS SbjNum,
      CAST(f.${ident(factMonthCol)} AS VARCHAR) AS month,
      CAST(f.${ident(factQuestionCol)} AS VARCHAR) AS question,
      ${factQuestionLabelCol
        ? `CAST(f.${ident(factQuestionLabelCol)} AS VARCHAR) AS question_label,`
        : "CAST(NULL AS VARCHAR) AS question_label,"}
      CAST(f.${ident(factAnswerLabelCol)} AS VARCHAR) AS answer_label,
      CAST(f.${ident(factAnswerValueCol)} AS VARCHAR) AS answer_value,
      ${factAnswerValueNumCol
        ? `CAST(f.${ident(factAnswerValueNumCol)} AS DOUBLE) AS answer_value_num,`
        : "CAST(NULL AS DOUBLE) AS answer_value_num,"}
      ${dimProjectionSql}
    FROM responses_fact f
    LEFT JOIN respondent_dims d
      ON CAST(d.${ident(dimCategoryCol)} AS VARCHAR) = CAST(f.${ident(factCategoryCol)} AS VARCHAR)
     AND CAST(d.${ident(dimRespondentIdCol)} AS VARCHAR) = CAST(f.${ident(factRespondentIdCol)} AS VARCHAR)
     AND CAST(d.${ident(dimMonthCol)} AS VARCHAR) = CAST(f.${ident(factMonthCol)} AS VARCHAR)
  `);

  await run(`
    CREATE OR REPLACE VIEW responses_base AS
    SELECT
      CAST(d.${ident(dimCategoryCol)} AS VARCHAR) AS category,
      CAST(d.${ident(dimRespondentIdCol)} AS VARCHAR) AS respondent_id,
      CAST(d.${ident(dimRespondentIdCol)} AS VARCHAR) AS SbjNum,
      CAST(d.${ident(dimMonthCol)} AS VARCHAR) AS month,
      ${dimProjectionSql}
    FROM respondent_dims d
  `);

  return true;
}

async function setupSingleDbDatamapResponseViews() {
  if (!(await tableExists("responses_fact"))) return false;

  const factColumns = await getTableColumns("responses_fact");
  if (!factColumns.includes("question")) return false;

  await setupDatamapQuestionMap();
  const sourceColumns = await describeRelationColumns("main.responses_fact");
  const outputColumns = Array.from(new Set(["question_raw", ...sourceColumns]));
  const numericColumns = new Set(sourceColumns.includes("answer_value_num") ? ["answer_value_num"] : []);

  await run(`
    CREATE OR REPLACE TEMP VIEW responses_fact AS
    ${buildProjectedSelect("main.responses_fact", outputColumns, sourceColumns, numericColumns, "", { applyQuestionDatamap: true })}
  `);

  await run(`
    CREATE OR REPLACE TEMP VIEW question_catalog AS
    SELECT DISTINCT
      CAST(category AS VARCHAR) AS category,
      CAST(question AS VARCHAR) AS question,
      CAST(question_label AS VARCHAR) AS question_label
    FROM responses_fact
    WHERE question IS NOT NULL
  `);

  console.log("[backend] DataMap question map: active DB responses_fact overlay enabled");
  return true;
}

async function applyDatamapToWritableResponsesFact() {
  if (activeDbIsReadOnlyMarketDb()) return false;
  if (!(await tableExists("responses_fact"))) return false;

  const factColumns = await getTableColumns("responses_fact");
  if (!factColumns.includes("question")) return false;

  const mappingCount = await setupDatamapQuestionMap();
  if (!mappingCount) return false;

  await run(`
    UPDATE responses_fact AS f
    SET question = dm.old_question
    FROM datamap_question_map dm
    WHERE lower(trim(CAST(f.question AS VARCHAR))) = dm.new_question_key
      AND CAST(f.question AS VARCHAR) <> dm.old_question
  `);
  console.log("[backend] DataMap question map: applied to writable responses_fact");
  return true;
}

function detectColumn(columns, candidates) {
  const map = new Map(columns.map((col) => [col.toLowerCase(), col]));
  for (const candidate of candidates) {
    const hit = map.get(candidate.toLowerCase());
    if (hit) return hit;
  }
  return null;
}

function resolveColumn(columns, rawKey, candidateMap = null) {
  if (!Array.isArray(columns) || columns.length === 0) return null;
  const map = new Map(columns.map((col) => [col.toLowerCase(), col]));
  const key = String(rawKey || "").trim().toLowerCase();
  if (!key) return null;

  const candidates = [rawKey];
  if (candidateMap?.get(key)?.length) {
    candidates.push(...candidateMap.get(key));
  }
  if (key === "month") candidates.push("file_month");
  if (key === "file_month") candidates.push("month");

  for (const candidate of candidates) {
    const hit = map.get(String(candidate || "").toLowerCase());
    if (hit) return hit;
  }
  return null;
}

function resolveRespondentDimColumn(columns, rawKey) {
  return resolveColumn(columns, rawKey, RESPONDENT_DIM_FIELD_CANDIDATES);
}

function normalizeFilterValue(field, value) {
  const key = compactLabel(field).toLowerCase();
  const text = compactLabel(value);
  if (!text) return "";

  if (key === "age") {
    const normalized = text.replace(/\u2013/g, "-").replace(/\s+/g, " ").trim().toLowerCase();
    const ageMatch = normalized.match(/^(\d+)\s*-\s*(\d+)/);
    if (ageMatch) return `${ageMatch[1]} - ${ageMatch[2]} years`;
    return normalized;
  }

  if (key === "week") {
    const weekMatch = text.match(/^(?:wk|week)\s*(\d+)$/i);
    if (weekMatch) return `Week ${weekMatch[1]}`;
  }

  return text;
}

const WEEK_DISPLAY_ORDER = ["Week 1", "Week 2", "Week 3", "Week 4", "Week 5"];
const CUSTOM_TABLE_CLASSIFIED_AD_CHANNELS_CODE = "__classified_ad_channels__";
const CUSTOM_TABLE_CLASSIFIED_AD_CHANNELS_LABEL = "Classified Ad Channels";
const CUSTOM_TABLE_CLASSIFIED_AD_CHANNEL_LABELS = [
  "Traditional channels(TV, radio, billboard)",
  "Digital Channels(Instagram, YouTube, Facebook)",
];
const CUSTOM_TABLE_OVERALL_SECTION_ID = "overall";
const CUSTOM_TABLE_OVERALL_QUESTION_CODE = "__overall__";
const CUSTOM_TABLE_OVERALL_LABEL = "Overall";

function normalizeRespondentDimDisplayValue(field, value, fallback = "(No response)") {
  const key = compactLabel(field).toLowerCase();
  if (key === "week") {
    const normalized = normalizeFilterValue(field, value);
    return normalized || fallback;
  }
  return normalizeDisplayLabel(value, fallback);
}

function sortWeekDisplayValues(values) {
  const order = new Map(WEEK_DISPLAY_ORDER.map((value, index) => [value, index]));
  return values.sort((left, right) => {
    const leftIndex = order.has(left) ? order.get(left) : Number.MAX_SAFE_INTEGER;
    const rightIndex = order.has(right) ? order.get(right) : Number.MAX_SAFE_INTEGER;
    if (leftIndex !== rightIndex) return leftIndex - rightIndex;
    return String(left).localeCompare(String(right));
  });
}

function isCustomTableClassifiedAdChannelsSpec(spec, questionCodes) {
  const label = normalizeCompactText(spec?.label).toLowerCase();
  return (Array.isArray(questionCodes) ? questionCodes : []).some((code) =>
    normalizeCompactText(code).toLowerCase() === CUSTOM_TABLE_CLASSIFIED_AD_CHANNELS_CODE,
  ) || label === CUSTOM_TABLE_CLASSIFIED_AD_CHANNELS_LABEL.toLowerCase();
}

function isOverallCustomTableSpec(spec) {
  const sectionId = normalizeCompactText(spec?.sectionId).toLowerCase();
  const codes = Array.isArray(spec?.questionCodes)
    ? spec.questionCodes.map((value) => normalizeCompactText(value).toLowerCase())
    : [];

  return sectionId === CUSTOM_TABLE_OVERALL_SECTION_ID
    || codes.includes(CUSTOM_TABLE_OVERALL_QUESTION_CODE);
}

function classifyCustomTableAdChannel(value) {
  const text = normalizeCompactText(value).toLowerCase();
  if (!text) return "";
  if (/\b(?:instagram|youtube|facebook)\b/u.test(text)) return "Digital Channels(Instagram, YouTube, Facebook)";
  if (/\b(?:tv|radio|billboard|hoarding|hoardings)\b/u.test(text)) return "Traditional channels(TV, radio, billboard)";
  return "";
}

function isClassifiedAdChannelDisplayLabel(value) {
  const text = normalizeCompactText(value).toLowerCase();
  return CUSTOM_TABLE_CLASSIFIED_AD_CHANNEL_LABELS.some((label) => label.toLowerCase() === text);
}

function sqlNormalizedFilterExpr(field, expr) {
  const key = compactLabel(field).toLowerCase();
  const valueExpr = `TRIM(CAST(${expr} AS VARCHAR))`;
  if (key === "age") {
    return `
      CASE
        WHEN regexp_matches(${valueExpr}, '(?i)^18\\s*[-–]\\s*25') THEN '18 - 25 years'
        WHEN regexp_matches(${valueExpr}, '(?i)^26\\s*[-–]\\s*35') THEN '26 - 35 years'
        WHEN regexp_matches(${valueExpr}, '(?i)^36\\s*[-–]\\s*45') THEN '36 - 45 years'
        WHEN regexp_matches(${valueExpr}, '(?i)^46\\s*[-–]\\s*55') THEN '46 - 55 years'
        WHEN regexp_matches(${valueExpr}, '(?i)^56\\s*[-–]\\s*65') THEN '56 - 65 years'
        ELSE lower(regexp_replace(replace(${valueExpr}, '–', '-'), '\\s+', ' ', 'g'))
      END
    `;
  }

  if (key === "week") {
    return `
      CASE
        WHEN regexp_matches(${valueExpr}, '(?i)^(wk|week)\\s*1$') THEN 'Week 1'
        WHEN regexp_matches(${valueExpr}, '(?i)^(wk|week)\\s*2$') THEN 'Week 2'
        WHEN regexp_matches(${valueExpr}, '(?i)^(wk|week)\\s*3$') THEN 'Week 3'
        WHEN regexp_matches(${valueExpr}, '(?i)^(wk|week)\\s*4$') THEN 'Week 4'
        WHEN regexp_matches(${valueExpr}, '(?i)^(wk|week)\\s*5$') THEN 'Week 5'
        ELSE ${valueExpr}
      END
    `;
  }

  return `CAST(${expr} AS VARCHAR)`;
}

function buildFilterValuesList(field, values) {
  return values.map((value) => quote(normalizeFilterValue(field, value))).join(", ");
}

function buildFilterSql(filters, columns, resolver = resolveColumn) {
  if (!filters || typeof filters !== "object") return "";
  const clauses = [];

  for (const [key, values] of Object.entries(filters)) {
    const col = resolver(columns, key);
    if (!col) continue;
    if (!Array.isArray(values) || values.length === 0) continue;
    const list = buildFilterValuesList(key, values);
    clauses.push(`${sqlNormalizedFilterExpr(key, ident(col))} IN (${list})`);
  }

  if (!clauses.length) return "";
  return ` AND ${clauses.join(" AND ")}`;
}

function buildAliasedFilterSql(filters, columns, alias, resolver = resolveColumn) {
  if (!filters || typeof filters !== "object") return "";
  const clauses = [];

  for (const [key, values] of Object.entries(filters)) {
    const col = resolver(columns, key);
    if (!col) continue;
    if (!Array.isArray(values) || values.length === 0) continue;
    const list = buildFilterValuesList(key, values);
    clauses.push(`${sqlNormalizedFilterExpr(key, `${alias}.${ident(col)}`)} IN (${list})`);
  }

  if (!clauses.length) return "";
  return ` AND ${clauses.join(" AND ")}`;
}

const CUSTOM_TABLE_REGION_BY_CENTRE = new Map([
  ["lagos", "Lagos 1"],
  ["ibadan", "South West"],
  ["ilorin", "South West"],
  ["benin", "South Central"],
  ["phc", "East1"],
  ["p.h.c", "East1"],
  ["port harcourt", "East1"],
  ["onitsha", "East2"],
  ["enugu", "East3"],
  ["uyo", "East4"],
  ["warri", "East5"],
  ["owerri", "East6"],
  ["abuja", "Central"],
  ["kano", "North 1"],
  ["jos", "North 1"],
  ["kaduna", "North 2"],
  ["sokoto", "North 2"],
]);
const CUSTOM_TABLE_REGION_ORDER = [
  "Lagos 1",
  "South West",
  "South Central",
  "East1",
  "East2",
  "East3",
  "East4",
  "East5",
  "East6",
  "Central",
  "North 1",
  "North 2",
];

function resolveCustomTableCentreColumn(columns) {
  return resolveColumn(columns, "City_1") || resolveColumn(columns, "Region");
}

function buildCustomTableRegionSqlExpr(columns, alias = "") {
  const prefix = alias ? `${alias}.` : "";
  const centreColumn = resolveCustomTableCentreColumn(columns);
  const regionColumn = resolveColumn(columns, "Region");
  const sourceColumn = centreColumn || regionColumn;
  if (!sourceColumn) return null;

  const sourceExpr = `${prefix}${ident(sourceColumn)}`;
  const normalizedSourceExpr = `lower(TRIM(CAST(${sourceExpr} AS VARCHAR)))`;
  const regionExpr = regionColumn ? `NULLIF(TRIM(CAST(${prefix}${ident(regionColumn)} AS VARCHAR)), '')` : null;
  const centreExpr = centreColumn ? `NULLIF(TRIM(CAST(${prefix}${ident(centreColumn)} AS VARCHAR)), '')` : null;
  const fallbackExpr = regionExpr && centreExpr
    ? `COALESCE(${regionExpr}, ${centreExpr})`
    : regionExpr || centreExpr || `TRIM(CAST(${sourceExpr} AS VARCHAR))`;
  const cases = Array.from(CUSTOM_TABLE_REGION_BY_CENTRE.entries())
    .map(([centre, region]) => `WHEN ${normalizedSourceExpr} = ${quote(centre)} THEN ${quote(region)}`)
    .join("\n        ");

  return `
      CASE
        ${cases}
        ELSE ${fallbackExpr}
      END
    `;
}

function buildCustomTableDimSqlExpr(field, columns, alias = "") {
  const key = compactLabel(field).toLowerCase();
  if (key === "region") return buildCustomTableRegionSqlExpr(columns, alias);
  const col = resolveRespondentDimColumn(columns, field);
  return col ? `${alias ? `${alias}.` : ""}${ident(col)}` : null;
}

function buildCustomTableAliasedFilterSql(filters, columns, alias) {
  if (!filters || typeof filters !== "object") return "";
  const clauses = [];

  for (const [key, values] of Object.entries(filters)) {
    const expr = buildCustomTableDimSqlExpr(key, columns, alias);
    if (!expr) continue;
    if (!Array.isArray(values) || values.length === 0) continue;
    const list = buildFilterValuesList(key, values);
    clauses.push(`${sqlNormalizedFilterExpr(key, expr)} IN (${list})`);
  }

  if (!clauses.length) return "";
  return ` AND ${clauses.join(" AND ")}`;
}

function normalizeCategory(slugOrCategory) {
  if (!slugOrCategory) return null;
  const mapped = CATEGORY_SLUG_MAP[String(slugOrCategory).toLowerCase()];
  return mapped || slugOrCategory;
}

function classifyQuestion(question, questionLabel) {
  const text = `${question || ""} ${questionLabel || ""}`;
  for (const page of PAGE_DEFINITIONS) {
    if (!page.matchers.length) continue;
    if (page.matchers.some((regex) => regex.test(text))) {
      return page.id;
    }
  }
  return "other-metrics";
}

function extractQuestionFamilyCode(questionCode) {
  const code = compactLabel(questionCode);
  const parts = code.split("_");
  const family = parts.find((part) => /^(?:BAU\d+[A-Z]?|PB\d+[A-Z]?|QC\d+[A-Z]?|A\d+)$/i.test(part));
  return family ? family.toUpperCase() : "";
}

function groupKeyForQuestionCode(questionCode, pageId) {
  const compactCode = compactLabel(questionCode);
  const familyCode = extractQuestionFamilyCode(compactCode);
  if (familyCode && pageId !== "brand-imagery") {
    return familyCode;
  }
  return compactCode.replace(/_\d+$/, "");
}

function compactLabel(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

const HEADER_AUDIT_PAGE_ID_BY_TITLE = new Map([
  ["screener & demographics", "screener-demographics"],
  ["category questions", "category-questions"],
  ["awareness & usage - raw questions", "awareness-usage"],
  ["purchase behavior", "purchase-behavior"],
  ["brand imagery", "brand-imagery"],
  ["flavour", "flavour-flex-section"],
  ["flex", "flavour-flex-section"],
  ["flavour/flex section", "flavour-flex-section"],
  ["campaign check", "campaign-check"],
  ["video ad section", "video-ad-section"],
  ["other metrics", "other-metrics"],
]);
const HEADER_AUDIT_METRIC_KEY_BY_PAGE_AND_HEADER = new Map([
  ["awareness - brand awareness::brand tom", "brand_tom"],
  ["awareness - brand awareness::brand spont", "brand_spont"],
  ["awareness - brand awareness::aided", "aided"],
  ["awareness - ad awareness::ad tom", "ad_tom"],
  ["awareness - ad awareness::ad spont", "ad_spont"],
  ["awareness - ad awareness::aided ad", "aided_ad"],
  ["awareness - media source::media source", "media_source"],
  ["awareness - usage::ever consumed", "ever_consumed"],
  ["awareness - usage::last 3 months", "last_3_months"],
  ["awareness - usage::last 1 month", "last_1_month"],
  ["awareness - usage::last 7 days", "last_7_days"],
  ["awareness - usage::most often used", "most_often_used"],
  ["awareness - usage::prefrence", "prefrence"],
  ["awareness - usage::preference", "prefrence"],
]);

function loadHeaderAuditMetadataCache() {
  if (headerAuditMetadataCache) return headerAuditMetadataCache;
  const marketMetadata = readJsonFile(MARKET_HEADER_AUDIT_METADATA_PATH, null);
  const currentMetadata = readJsonFile(CURRENT_HEADER_AUDIT_METADATA_PATH, null);
  headerAuditMetadataCache = {
    current: currentMetadata || marketMetadata,
    market: marketMetadata,
  };
  return headerAuditMetadataCache;
}

function getHeaderAuditMetadataForDbPath(dbPath = DB_PATH) {
  const cache = loadHeaderAuditMetadataCache();
  return cache.market || null;
}

function getHeaderAuditMetadataBySource(source, dbPath = DB_PATH) {
  const cache = loadHeaderAuditMetadataCache();
  const normalizedSource = compactLabel(source).toLowerCase();
  if (normalizedSource === "current") return cache.current || cache.market || null;
  if (normalizedSource === "market") return cache.market || cache.current || null;
  return getHeaderAuditMetadataForDbPath(dbPath);
}

function isMonthAtOrAfter(value, cutoff = CURRENT_HEADER_AUDIT_MONTH_CUTOFF) {
  const month = compactLabel(value);
  const threshold = compactLabel(cutoff);
  return /^\d{4}-\d{2}$/.test(month) && /^\d{4}-\d{2}$/.test(threshold) && month >= threshold;
}

function resolveHeaderAuditSourceForMonths(months) {
  const monthList = Array.isArray(months) ? months.map((value) => compactLabel(value)).filter(Boolean) : [];
  if (!monthList.length) return null;
  const marketCutoff = compactLabel(combinedMarketMonthCutoff || MARKET_INSIGHTS_CURRENT_MONTH);
  if (!marketCutoff) return "current";
  return monthList.every((month) => month <= marketCutoff) ? "market" : "current";
}

function resolveHeaderAuditSourcesForMonths(months) {
  const monthList = Array.isArray(months) ? months.map((value) => compactLabel(value)).filter(Boolean) : [];
  if (!monthList.length) return [];
  const marketCutoff = compactLabel(combinedMarketMonthCutoff || MARKET_INSIGHTS_CURRENT_MONTH);
  if (!marketCutoff) return ["current", "market"];
  const sources = [];
  if (monthList.some((month) => month <= marketCutoff)) sources.push("market");
  if (monthList.some((month) => month > marketCutoff)) {
    sources.push("current");
    if (!sources.includes("market")) sources.push("market");
  }
  return sources;
}

async function getLatestMonthForCategory(category) {
  const normalizedCategory = compactLabel(category);
  if (!normalizedCategory) return "";
  const rows = await all(`
    SELECT MAX(CAST(month AS VARCHAR)) AS latest_month
    FROM respondent_dims
    WHERE CAST(category AS VARCHAR) = ${quote(normalizedCategory)}
  `);
  return compactLabel(rows?.[0]?.latest_month);
}

async function resolveHeaderAuditSourceForRequest(category, months = []) {
  const fromMonths = resolveHeaderAuditSourceForMonths(months);
  if (fromMonths) return fromMonths;
  return "market";
}

function getHeaderAuditCategoryRows(category, source = null, dbPath = DB_PATH) {
  const metadata = getHeaderAuditMetadataBySource(source, dbPath);
  if (!metadata?.categories || !category) return [];
  return Array.isArray(metadata.categories?.[category]?.rows)
    ? metadata.categories[category].rows
    : [];
}

function getHeaderAuditPageId(row) {
  const pageTitle = compactLabel(row?.page).toLowerCase();
  return HEADER_AUDIT_PAGE_ID_BY_TITLE.get(pageTitle) || null;
}

function resolveHeaderAuditQuestionPageId(row) {
  const questionCodes = Array.isArray(row?.sourceVariableNames)
    ? row.sourceVariableNames.map((value) => compactLabel(value)).filter(Boolean)
    : [];
  if (questionCodes.some((code) => /(?:^|_)QFS1(?:_|$)/i.test(code))) {
    return "flavour-flex-section";
  }
  return getHeaderAuditPageId(row);
}

function buildHeaderAuditQuestionId(row, index) {
  const pageId = resolveHeaderAuditQuestionPageId(row) || "question";
  const label = compactLabel(row?.headerLabel || row?.sourceVariableNames?.[0] || `question_${index + 1}`)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return `${pageId}__${label || index + 1}`;
}

function isCurrentHeaderAuditSource(source) {
  return compactLabel(source).toLowerCase() === "current";
}

function isCurrentBau4Code(questionCode) {
  return /^(?:[A-Z]+_BAU4\.\d+(?:\.1)?(?:_\d+|_OTH)?|bau4_[A-Z]+_\d+_\d+)$/i.test(compactLabel(questionCode));
}

function isCurrentBau4SurveyCtoCode(questionCode) {
  return /^bau4_[A-Z]+_\d+_\d+$/i.test(compactLabel(questionCode));
}

function isCurrentBau4OptionCode(questionCode) {
  return /^[A-Z]+_BAU4\.\d+(?:\.1)?_\d+$/i.test(compactLabel(questionCode));
}

function toCurrentBau4SurveyCtoCode(questionCode) {
  const code = compactLabel(questionCode);
  if (isCurrentBau4SurveyCtoCode(code)) return code;
  const match = code.match(/^([A-Z]+)_BAU4\.(\d+)(?:\.1)?_(\d+)$/i);
  if (!match) return code;
  return `bau4_${match[1]}_${match[2]}_${match[3]}`;
}

function normalizeCurrentBau4QuestionEntries(source, questionCodes, sourceQuestionLabels = []) {
  const codes = Array.isArray(questionCodes)
    ? questionCodes.map((value) => compactLabel(value)).filter(Boolean)
    : [];
  const labels = Array.isArray(sourceQuestionLabels)
    ? sourceQuestionLabels.map((value) => compactLabel(value))
    : [];
  if (!isCurrentHeaderAuditSource(source) || !codes.some(isCurrentBau4Code)) {
    return { questionCodes: codes, sourceQuestionLabels: labels };
  }

  const questionCodesOut = [];
  const sourceQuestionLabelsOut = [];
  const seen = new Set();
  codes.forEach((code, index) => {
    if (!isCurrentBau4SurveyCtoCode(code) && !isCurrentBau4OptionCode(code)) return;
    const normalizedCode = toCurrentBau4SurveyCtoCode(code);
    if (!normalizedCode || seen.has(normalizedCode)) return;
    seen.add(normalizedCode);
    questionCodesOut.push(normalizedCode);
    sourceQuestionLabelsOut.push(labels[index] || normalizedCode);
  });
  return { questionCodes: questionCodesOut, sourceQuestionLabels: sourceQuestionLabelsOut };
}

function normalizeCurrentBau4QuestionCodes(source, questionCodes) {
  return normalizeCurrentBau4QuestionEntries(source, questionCodes).questionCodes;
}

function isCurrentBau4ParentQuestionGroup(source, questionCodes) {
  const codes = Array.isArray(questionCodes)
    ? questionCodes.map((value) => compactLabel(value)).filter(Boolean)
    : [];
  return (
    isCurrentHeaderAuditSource(source)
    && codes.some(isCurrentBau4Code)
    && !codes.some((code) => /^[A-Z]+_BAU4\.\d+\.1(?:_\d+|_OTH)?$/i.test(code))
    && !codes.some(isCurrentBau4SurveyCtoCode)
  );
}

function buildHeaderAuditQuestions(category, source = null, dbPath = DB_PATH) {
  const questions = getHeaderAuditCategoryRows(category, source, dbPath)
    .map((row, index) => {
      const pageId = resolveHeaderAuditQuestionPageId(row);
      const rawQuestionCodes = Array.isArray(row?.sourceVariableNames)
        ? row.sourceVariableNames.map((value) => compactLabel(value)).filter(Boolean)
        : [];
      if (isCurrentBau4ParentQuestionGroup(source, rawQuestionCodes)) return null;
      const normalizedEntries = normalizeCurrentBau4QuestionEntries(
        source,
        rawQuestionCodes,
        Array.isArray(row?.sourceQuestionLabels) ? row.sourceQuestionLabels : [],
      );
      const questionCodes = normalizedEntries.questionCodes;
      if (!pageId || !questionCodes.length) return null;
      if (compactLabel(row?.headerType).toLowerCase() === "overview_dimension") return null;
      if (compactLabel(row?.headerType).toLowerCase() === "derived_metric") return null;

      return {
        id: buildHeaderAuditQuestionId(row, index),
        question: questionCodes[0],
        questionLabel: compactLabel(row?.headerLabel) || questionCodes[0],
        sourceQuestionLabels: normalizedEntries.sourceQuestionLabels,
        pageId,
        questionCodes,
        pageLabel: compactLabel(row?.page),
        subpageLabel: compactLabel(row?.subpage),
        headerType: compactLabel(row?.headerType),
        metadataSource: "header_audit",
        headerAuditSource: source || null,
        order: index,
      };
    })
    .filter(Boolean);
  return questions.concat(buildSupplementalHeaderAuditQuestions(category, source, questions.length));
}

function buildSupplementalHeaderAuditQuestions(category, source = null, offset = 0) {
  const normalizedCategory = compactLabel(category);
  const normalizedSource = compactLabel(source).toLowerCase();
  if (normalizedCategory !== "Noodles" || normalizedSource !== "current") return [];
  return [
    {
      id: "campaign-check__n_qfh1",
      question: "N_QFH1",
      questionLabel: "Have you seen this ad before?",
      sourceQuestionLabels: ["Have you seen this ad before?"],
      pageId: "campaign-check",
      questionCodes: ["N_QFH1"],
      pageLabel: "Campaign Check",
      subpageLabel: "Ad Recall",
      headerType: "single_select",
      metadataSource: "header_audit_supplement",
      headerAuditSource: normalizedSource,
      order: offset + 1,
    },
  ];
}

function getHeaderAuditMetricKey(row) {
  const key = `${compactLabel(row?.page).toLowerCase()}::${compactLabel(row?.headerLabel).toLowerCase()}`;
  return HEADER_AUDIT_METRIC_KEY_BY_PAGE_AND_HEADER.get(key) || null;
}

function getHeaderAuditMetricRows(dbPath = DB_PATH, source = null) {
  const metadata = getHeaderAuditMetadataBySource(source, dbPath);
  if (!metadata?.categories) return [];
  return Object.entries(metadata.categories).flatMap(([category, entry]) =>
    (Array.isArray(entry?.rows) ? entry.rows : [])
      .map((row) => {
        const normalizedEntries = normalizeCurrentBau4QuestionEntries(
          source,
          Array.isArray(row?.sourceVariableNames) ? row.sourceVariableNames : [],
          Array.isArray(row?.sourceQuestionLabels) ? row.sourceQuestionLabels : [],
        );
        return {
          category,
          row: { ...row, sourceQuestionLabels: normalizedEntries.sourceQuestionLabels },
          source,
          metricKey: getHeaderAuditMetricKey(row),
          questionCodes: normalizedEntries.questionCodes,
        };
      })
      .filter((item) =>
        item.metricKey
        && compactLabel(item?.row?.headerType).toLowerCase() === "derived_metric"
        && item.questionCodes.length > 0,
      ));
}

function getAllHeaderAuditMetricRows(dbPath = DB_PATH) {
  const deduped = new Map();
  ["market", "current"].forEach((source) => {
    getHeaderAuditMetricRows(dbPath, source).forEach((metricRow) => {
      const key = [
        compactLabel(metricRow.category),
        compactLabel(metricRow.metricKey),
        ...(Array.isArray(metricRow.questionCodes) ? metricRow.questionCodes.map((value) => compactLabel(value)) : []),
      ].join("::");
      if (!deduped.has(key)) deduped.set(key, metricRow);
    });
  });
  return Array.from(deduped.values());
}

function normalizeDisplayLabel(value, fallback = "(No response)") {
  const text = compactLabel(value);
  if (!text) return fallback;
  if (/^\{0\}$/i.test(text)) return "OTHERS";
  return text;
}

function isAttachmentUrlLabel(value) {
  return /^https?:\/\/\S+/i.test(compactLabel(value));
}

function isPlaceholderDashLabel(value) {
  const text = compactLabel(value);
  if (!text) return true;
  return /^(?:-|[\u2012-\u2015])$/.test(text);
}

function normalizeOptionalDisplayLabel(value) {
  const text = compactLabel(value);
  if (!text) return "";
  const normalized = text.replace(/\s*,\s*$/u, "");
  return /^\{0\}$/i.test(normalized) ? "OTHERS" : normalized;
}

function preferredDisplayLabel(answerLabel, answerValue, fallback = "(No response)") {
  const label = compactLabel(answerLabel);
  if (label) return normalizeDisplayLabel(label, fallback);
  const value = compactLabel(answerValue);
  if (value) return normalizeDisplayLabel(value, fallback);
  return fallback;
}

function getPinnedDisplayLabelRank(value) {
  const text = compactLabel(value);
  if (!text) return 0;
  const trailingParen = text.match(/\(([^()]+)\)\s*$/);
  if (trailingParen?.[1]) {
    const nestedRank = getPinnedDisplayLabelRank(trailingParen[1]);
    if (nestedRank > 0) return nestedRank;
  }
  if (/^others?$/i.test(text)) return 1;
  if (/^none(?:\s+of\s+these)?$/i.test(text)) return 2;
  return 0;
}

function comparePinnedDisplayLabelsLast(leftValue, rightValue) {
  const leftRank = getPinnedDisplayLabelRank(leftValue);
  const rightRank = getPinnedDisplayLabelRank(rightValue);
  if (leftRank !== rightRank) {
    if (leftRank === 0) return -1;
    if (rightRank === 0) return 1;
    return leftRank - rightRank;
  }
  return 0;
}

function movePinnedDisplayLabelsToEnd(values) {
  const regular = [];
  const others = [];
  const none = [];

  (Array.isArray(values) ? values : []).forEach((value) => {
    const rank = getPinnedDisplayLabelRank(value);
    if (rank === 1) {
      others.push(value);
    } else if (rank === 2) {
      none.push(value);
    } else {
      regular.push(value);
    }
  });

  return [...regular, ...others, ...none];
}

function sortAnswerRowsWithPinnedDisplayLabels(rows, labelKey = "answer", countKey = "count") {
  return [...(Array.isArray(rows) ? rows : [])].sort((left, right) => {
    const specialOrder = comparePinnedDisplayLabelsLast(left?.[labelKey], right?.[labelKey]);
    if (specialOrder !== 0) return specialOrder;

    const countDiff = Number(right?.[countKey] || 0) - Number(left?.[countKey] || 0);
    if (countDiff !== 0) return countDiff;

    return compactLabel(left?.[labelKey]).localeCompare(compactLabel(right?.[labelKey]), undefined, {
      numeric: true,
      sensitivity: "base",
    });
  });
}

function takeAnswerRowsWithPinnedDisplayLabels(rows, limit, labelKey = "answer") {
  const items = Array.isArray(rows) ? rows : [];
  const hardLimit = Math.max(1, Number(limit) || items.length || 1);
  const regular = [];
  const special = [];

  items.forEach((item) => {
    if (getPinnedDisplayLabelRank(item?.[labelKey]) > 0) {
      special.push(item);
    } else {
      regular.push(item);
    }
  });

  const regularSlots = Math.max(0, hardLimit - special.length);
  return [...regular.slice(0, regularSlots), ...special].slice(0, hardLimit);
}

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function titleCase(value) {
  return compactLabel(value)
    .split(" ")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function buildExtraStopWords(...values) {
  const extra = new Set();
  values.forEach((value) => {
    compactLabel(value)
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .filter((part) => part.length >= 2)
      .forEach((part) => extra.add(part));
  });
  return extra;
}

function tokenizeVerbatimText(value, extraStopWords = new Set()) {
  const cleaned = String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9'\s-]+/g, " ")
    .replace(/[_-]+/g, " ");
  return cleaned
    .split(/\s+/)
    .map((token) => token.replace(/^'+|'+$/g, "").trim())
    .filter(
      (token) =>
        token.length >= 3
        && !/^\d+$/.test(token)
        && !VERBATIM_STOP_WORDS.has(token)
        && !extraStopWords.has(token),
    );
}

function stemVerbatimToken(token) {
  const text = compactLabel(token).toLowerCase();
  if (text.length <= 4) return text;
  if (text.endsWith("ies") && text.length > 5) return `${text.slice(0, -3)}y`;
  if (text.endsWith("ing") && text.length > 6) return text.slice(0, -3);
  if (text.endsWith("ed") && text.length > 5) return text.slice(0, -2);
  if (text.endsWith("ly") && text.length > 5) return text.slice(0, -2);
  if (text.endsWith("es") && text.length > 5) return text.slice(0, -2);
  if (text.endsWith("s") && text.length > 4 && !text.endsWith("ss")) return text.slice(0, -1);
  return text;
}

const POSITIVE_SENTIMENT_MATCHES = new Set([
  ...POSITIVE_SENTIMENT_TOKENS,
  ...Array.from(POSITIVE_SENTIMENT_TOKENS).map((token) => stemVerbatimToken(token)),
]);
const NEGATIVE_SENTIMENT_MATCHES = new Set([
  ...NEGATIVE_SENTIMENT_TOKENS,
  ...Array.from(NEGATIVE_SENTIMENT_TOKENS).map((token) => stemVerbatimToken(token)),
]);

function buildWordCloudTerms(rows, extraStopWords = new Set()) {
  const counts = new Map();
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    tokenizeVerbatimText(row.text, extraStopWords).forEach((token) => {
      counts.set(token, Number(counts.get(token) || 0) + 1);
    });
  });

  return Array.from(counts.entries())
    .map(([text, value]) => ({ text, value: Number(value || 0) }))
    .sort((left, right) => {
      const diff = Number(right.value || 0) - Number(left.value || 0);
      if (diff !== 0) return diff;
      return left.text.localeCompare(right.text, undefined, { sensitivity: "base" });
    })
    .slice(0, 70);
}

function collectPhraseCountsFromTokens(tokens) {
  const counts = new Map();
  const push = (phrase) => {
    if (!phrase) return;
    counts.set(phrase, Number(counts.get(phrase) || 0) + 1);
  };

  tokens.forEach((token) => push(token));
  for (let i = 0; i < tokens.length - 1; i += 1) {
    push(`${tokens[i]} ${tokens[i + 1]}`);
  }
  for (let i = 0; i < tokens.length - 2; i += 1) {
    push(`${tokens[i]} ${tokens[i + 1]} ${tokens[i + 2]}`);
  }

  return counts;
}

function incrementMapValue(map, key, amount = 1) {
  if (!key) return;
  map.set(key, Number(map.get(key) || 0) + Number(amount || 0));
}

function collectSemanticFeatureCounts(tokens, stemmedTokens) {
  const counts = new Map();
  stemmedTokens.forEach((token) => incrementMapValue(counts, token, 1));
  for (let i = 0; i < stemmedTokens.length - 1; i += 1) {
    incrementMapValue(counts, `${stemmedTokens[i]} ${stemmedTokens[i + 1]}`, 1.8);
  }
  for (let i = 0; i < stemmedTokens.length - 2; i += 1) {
    incrementMapValue(counts, `${stemmedTokens[i]} ${stemmedTokens[i + 1]} ${stemmedTokens[i + 2]}`, 2.2);
  }

  const rawPhraseCounts = collectPhraseCountsFromTokens(tokens);
  rawPhraseCounts.forEach((value, phrase) => {
    if (phrase.includes(" ")) incrementMapValue(counts, phrase, Number(value || 0) * 0.7);
  });
  return counts;
}

function buildNormalizedSemanticVectors(docs) {
  const docFreq = new Map();

  docs.forEach((doc) => {
    const seen = new Set(doc.featureCounts.keys());
    seen.forEach((feature) => {
      docFreq.set(feature, Number(docFreq.get(feature) || 0) + 1);
    });
  });

  const totalDocs = docs.length || 1;
  docs.forEach((doc) => {
    const vector = new Map();
    let norm = 0;
    doc.featureCounts.forEach((count, feature) => {
      const df = Number(docFreq.get(feature) || 1);
      const idf = Math.log((1 + totalDocs) / (1 + df)) + 1;
      const weight = Number(count || 0) * idf;
      vector.set(feature, weight);
      norm += weight * weight;
    });
    doc.vector = vector;
    doc.norm = Math.sqrt(norm) || 1;
  });
}

function cosineSimilarity(leftDoc, rightDoc) {
  if (!leftDoc?.vector || !rightDoc?.vector) return 0;
  const [smaller, larger] =
    leftDoc.vector.size <= rightDoc.vector.size ? [leftDoc.vector, rightDoc.vector] : [rightDoc.vector, leftDoc.vector];
  let dot = 0;
  smaller.forEach((weight, feature) => {
    const otherWeight = larger.get(feature);
    if (otherWeight) dot += Number(weight || 0) * Number(otherWeight || 0);
  });
  const denom = Number(leftDoc.norm || 0) * Number(rightDoc.norm || 0);
  if (!denom) return 0;
  return dot / denom;
}

function buildSemanticTopicClusters(docs, minTopicSize) {
  const clusters = [];
  const similarityThreshold = docs.length >= 120 ? 0.28 : docs.length >= 50 ? 0.24 : 0.2;

  docs.forEach((doc) => {
    let bestCluster = null;
    let bestScore = 0;

    clusters.forEach((cluster) => {
      const similarity = cosineSimilarity(doc, cluster.prototype);
      if (similarity > bestScore) {
        bestScore = similarity;
        bestCluster = cluster;
      }
    });

    if (!bestCluster || bestScore < similarityThreshold) {
      clusters.push({
        docs: [doc],
        prototype: doc,
      });
      return;
    }

    bestCluster.docs.push(doc);
    if (doc.tokens.length > bestCluster.prototype.tokens.length) {
      bestCluster.prototype = doc;
    }
  });

  return clusters.filter((cluster) => cluster.docs.length >= minTopicSize);
}

function topKeywordsFromDocs(docs, extraStopWords = new Set()) {
  const counts = new Map();
  docs.forEach((doc) => {
    doc.tokens.forEach((token) => {
      if (extraStopWords.has(token)) return;
      counts.set(token, Number(counts.get(token) || 0) + 1);
    });
  });

  return Array.from(counts.entries())
    .sort((left, right) => {
      const diff = Number(right[1] || 0) - Number(left[1] || 0);
      if (diff !== 0) return diff;
      return String(left[0]).localeCompare(String(right[0]), undefined, { sensitivity: "base" });
    })
    .slice(0, 5)
    .map(([token]) => String(token));
}

const TOPIC_LABEL_GENERIC_TOKENS = new Set([
  "advert",
  "advertisement",
  "ad",
  "brand",
  "brands",
  "food",
  "good",
  "great",
  "like",
  "love",
  "nice",
  "product",
  "products",
  "really",
  "taste",
  "tasty",
  "very",
]);

function isUsefulTopicPhrase(phrase, extraStopWords = new Set()) {
  const tokens = compactLabel(phrase)
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);

  if (tokens.length < 2 || tokens.length > 4) return false;

  const meaningfulTokens = tokens.filter(
    (token) => !extraStopWords.has(token) && !VERBATIM_STOP_WORDS.has(token) && !TOPIC_LABEL_GENERIC_TOKENS.has(token),
  );

  if (!meaningfulTokens.length) return false;
  return meaningfulTokens.some((token) => token.length >= 4);
}

function bestTopicPhraseFromDocs(docs, extraStopWords = new Set(), minDocCount = 2) {
  const phraseMap = new Map();

  docs.forEach((doc) => {
    const seen = new Set();
    doc.tokens.forEach((token) => {
      if (extraStopWords.has(token)) return;
      const entry = phraseMap.get(token) || { phrase: token, docFreq: 0, totalFreq: 0, length: 1 };
      if (!seen.has(token)) {
        entry.docFreq += 1;
        seen.add(token);
      }
      entry.totalFreq += 1;
      phraseMap.set(token, entry);
    });

    for (let size = 2; size <= 4; size += 1) {
      for (let i = 0; i <= doc.tokens.length - size; i += 1) {
        const phraseTokens = doc.tokens.slice(i, i + size);
        if (phraseTokens.some((token) => extraStopWords.has(token))) continue;
        const phrase = phraseTokens.join(" ");
        const entry = phraseMap.get(phrase) || { phrase, docFreq: 0, totalFreq: 0, length: size };
        if (!seen.has(phrase)) {
          entry.docFreq += 1;
          seen.add(phrase);
        }
        entry.totalFreq += 1;
        phraseMap.set(phrase, entry);
      }
    }
  });

  const ranked = Array.from(phraseMap.values())
    .filter((entry) => entry.docFreq >= minDocCount)
    .map((entry) => ({
      ...entry,
      score: entry.docFreq * 5 + entry.totalFreq + entry.length * 6,
    }))
    .sort((left, right) => {
      const diff = Number(right.score || 0) - Number(left.score || 0);
      if (diff !== 0) return diff;
      return String(left.phrase).localeCompare(String(right.phrase), undefined, { sensitivity: "base" });
    });

  const preferred = ranked.find((entry) => isUsefulTopicPhrase(entry.phrase, extraStopWords));
  return preferred?.phrase || ranked[0]?.phrase || "";
}

function buildReadableTopicLabel(docs, fallbackPhrase, extraStopWords = new Set(), minDocCount = 2) {
  const preferredPhrase = bestTopicPhraseFromDocs(docs, extraStopWords, minDocCount);
  if (preferredPhrase && isUsefulTopicPhrase(preferredPhrase, extraStopWords)) {
    return titleCase(preferredPhrase);
  }

  if (fallbackPhrase && isUsefulTopicPhrase(fallbackPhrase, extraStopWords)) {
    return titleCase(fallbackPhrase);
  }

  const keywords = topKeywordsFromDocs(docs, extraStopWords).slice(0, 3);
  if (keywords.length >= 2) {
    return titleCase(`${keywords[0]} and ${keywords[1]}`);
  }
  if (keywords.length === 1) {
    return titleCase(`${keywords[0]} related feedback`);
  }
  return "General Feedback";
}

function deriveTopicSentiment(docs, contextTokens = []) {
  let positive = 0;
  let negative = 0;

  (Array.isArray(docs) ? docs : []).forEach((doc) => {
    const tokens = new Set([
      ...(Array.isArray(doc?.tokens) ? doc.tokens : []),
      ...(Array.isArray(doc?.stemmedTokens) ? doc.stemmedTokens : []),
    ]);
    tokens.forEach((token) => {
      if (POSITIVE_SENTIMENT_MATCHES.has(token)) positive += 1;
      if (NEGATIVE_SENTIMENT_MATCHES.has(token)) negative += 1;
    });
  });

  (Array.isArray(contextTokens) ? contextTokens : []).forEach((token) => {
    const normalized = stemVerbatimToken(token);
    if (POSITIVE_SENTIMENT_MATCHES.has(token) || POSITIVE_SENTIMENT_MATCHES.has(normalized)) positive += 1;
    if (NEGATIVE_SENTIMENT_MATCHES.has(token) || NEGATIVE_SENTIMENT_MATCHES.has(normalized)) negative += 1;
  });

  const totalHits = positive + negative;
  if (!totalHits) {
    return { label: "Neutral", score: 0 };
  }

  const score = Number(((positive - negative) / totalHits).toFixed(2));
  if (score >= 0.12) {
    return { label: "Positive", score };
  }
  if (score <= -0.12) {
    return { label: "Negative", score };
  }
  return { label: "Neutral", score };
}

function buildDeterministicTopics(rows, extraStopWords = new Set()) {
  const docs = (Array.isArray(rows) ? rows : [])
    .map((row, index) => {
      const tokens = tokenizeVerbatimText(row.text, extraStopWords);
      if (!tokens.length) return null;
      const stemmedTokens = tokens.map((token) => stemVerbatimToken(token));
      const phraseCounts = collectPhraseCountsFromTokens(tokens);
      return {
        id: index,
        text: compactLabel(row.text),
        tokens,
        stemmedTokens,
        phraseCounts,
        featureCounts: collectSemanticFeatureCounts(tokens, stemmedTokens),
        uniquePhrases: new Set(phraseCounts.keys()),
      };
    })
    .filter(Boolean);

  if (!docs.length) return [];

  const totalDocs = docs.length;
  const minTopicSize = totalDocs >= 100 ? 5 : totalDocs >= 40 ? 4 : totalDocs >= 16 ? 3 : 2;
  buildNormalizedSemanticVectors(docs);
  const clusters = buildSemanticTopicClusters(docs, minTopicSize)
    .sort((left, right) => right.docs.length - left.docs.length)
    .slice(0, 6);

  if (!clusters.length) {
    const label = "General Feedback";
    const keywords = topKeywordsFromDocs(docs, extraStopWords);
    const sentiment = deriveTopicSentiment(
      docs,
      tokenizeVerbatimText(`${label} ${keywords.join(" ")}`),
    );
    return [
      {
        id: "general-feedback",
        label,
        responseCount: totalDocs,
        share: 100,
        sentiment: sentiment.label,
        sentimentScore: sentiment.score,
        keywords,
        samples: Array.from(new Set(docs.map((doc) => doc.text))).slice(0, 3),
      },
    ];
  }

  const clusteredDocIds = new Set();
  const topics = clusters.map((cluster, index) => {
    cluster.docs.forEach((doc) => clusteredDocIds.add(doc.id));
    const fallbackPhrase = bestTopicPhraseFromDocs(cluster.docs, extraStopWords, minTopicSize);
    const phraseTokens = fallbackPhrase.split(" ").filter(Boolean);
    const keywordStopWords = new Set([...extraStopWords, ...phraseTokens]);
    const keywords = topKeywordsFromDocs(cluster.docs, keywordStopWords);
    const samples = Array.from(new Set(cluster.docs.map((doc) => doc.text))).slice(0, 3);
    const label = buildReadableTopicLabel(cluster.docs, fallbackPhrase, extraStopWords, minTopicSize);
    const sentiment = deriveTopicSentiment(
      cluster.docs,
      tokenizeVerbatimText(`${label} ${keywords.join(" ")}`),
    );

    return {
      id: `${label.toLowerCase().replace(/[^a-z0-9]+/g, "-") || "topic"}-${index + 1}`,
      label,
      responseCount: cluster.docs.length,
      share: Number(((cluster.docs.length / totalDocs) * 100).toFixed(1)),
      sentiment: sentiment.label,
      sentimentScore: sentiment.score,
      keywords,
      samples,
    };
  });

  const remainingDocs = docs.filter((doc) => !clusteredDocIds.has(doc.id));
  if (remainingDocs.length >= minTopicSize) {
    const label = "Other Feedback";
    const keywords = topKeywordsFromDocs(remainingDocs, extraStopWords);
    const sentiment = deriveTopicSentiment(
      remainingDocs,
      tokenizeVerbatimText(`${label} ${keywords.join(" ")}`),
    );
    topics.push({
      id: "other-feedback",
      label,
      responseCount: remainingDocs.length,
      share: Number(((remainingDocs.length / totalDocs) * 100).toFixed(1)),
      sentiment: sentiment.label,
      sentimentScore: sentiment.score,
      keywords,
      samples: Array.from(new Set(remainingDocs.map((doc) => doc.text))).slice(0, 3),
    });
  }

  return topics;
}

function deriveMainBrandQuestionCode(questionCode) {
  const code = compactLabel(questionCode);
  return /BAU6D$/i.test(code) ? code.replace(/BAU6D$/i, "BAU6C") : "";
}

function normalizeVerbatimQuestionLabel(questionCode, questionLabel) {
  const code = compactLabel(questionCode);
  if (/BAU6D$/i.test(code)) {
    return "Primary reason for choosing this brand over other brands.";
  }
  return compactLabel(questionLabel) || code;
}

async function fetchVerbatimResponses(category, questionCode, filters, months, groupByBrand = false) {
  const normalizedQuestionCode = compactLabel(questionCode);
  const monthFilterSql =
    Array.isArray(months) && months.length > 0
      ? ` AND CAST(d.month AS VARCHAR) IN (${months.map((month) => quote(month)).join(", ")})`
      : "";
  const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");
  const mainBrandQuestionCode = groupByBrand ? deriveMainBrandQuestionCode(normalizedQuestionCode) : "";
  const brandJoinSql = mainBrandQuestionCode
    ? `
      LEFT JOIN (
        SELECT
          CAST(r.respondent_id AS VARCHAR) AS respondent_id,
          CAST(r.month AS VARCHAR) AS month,
          MAX(${sqlPreferredDisplayLabel("r.answer_label", "r.answer_value", "Unspecified Brand")}) AS brand
        FROM responses_fact r
        JOIN respondent_dims d
          ON CAST(d.category AS VARCHAR) = CAST(r.category AS VARCHAR)
         AND CAST(d.respondent_id AS VARCHAR) = CAST(r.respondent_id AS VARCHAR)
         AND CAST(d.month AS VARCHAR) = CAST(r.month AS VARCHAR)
        WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
          AND lower(CAST(r.question AS VARCHAR)) = lower(${quote(mainBrandQuestionCode)})
          ${monthFilterSql}
          ${filterSql}
        GROUP BY 1, 2
      ) main_brand
        ON main_brand.respondent_id = CAST(r.respondent_id AS VARCHAR)
       AND main_brand.month = CAST(r.month AS VARCHAR)
    `
    : "";

  const rows = await all(`
    SELECT
      CAST(r.respondent_id AS VARCHAR) AS respondent_id,
      CAST(r.month AS VARCHAR) AS month,
      CAST(r.question AS VARCHAR) AS question,
      CAST(r.question_label AS VARCHAR) AS question_label,
      ${sqlPreferredDisplayLabel("r.answer_label", "r.answer_value", "")} AS response_text
      ${mainBrandQuestionCode ? ", COALESCE(main_brand.brand, 'Unspecified Brand') AS brand" : ", 'All Responses' AS brand"}
    FROM responses_fact r
    JOIN respondent_dims d
      ON CAST(d.category AS VARCHAR) = CAST(r.category AS VARCHAR)
     AND CAST(d.respondent_id AS VARCHAR) = CAST(r.respondent_id AS VARCHAR)
     AND CAST(d.month AS VARCHAR) = CAST(r.month AS VARCHAR)
    ${brandJoinSql}
    WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
      AND lower(CAST(r.question AS VARCHAR)) = lower(${quote(normalizedQuestionCode)})
      ${monthFilterSql}
      ${filterSql}
  `);

  return rows
    .map((row) => ({
      respondentId: compactLabel(row.respondent_id),
      month: compactLabel(row.month),
      question: compactLabel(row.question),
      questionLabel: normalizeVerbatimQuestionLabel(row.question, row.question_label),
      text: compactLabel(row.response_text),
      brand: normalizeDisplayLabel(row.brand, "All Responses"),
    }))
    .filter((row) => row.text && row.text !== "(No response)");
}

function buildVerbatimInsightGroups(rows, groupByBrand) {
  const grouped = new Map();
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const key = groupByBrand ? normalizeDisplayLabel(row.brand, "Unspecified Brand") : "All Responses";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(row);
  });

  return Array.from(grouped.entries())
    .map(([label, items]) => {
      const extraStopWords = buildExtraStopWords(label);
      return {
        id: label.toLowerCase().replace(/[^a-z0-9]+/g, "-") || "all-responses",
        label,
        responseCount: items.length,
        wordCloud: buildWordCloudTerms(items, extraStopWords),
        topics: buildDeterministicTopics(items, extraStopWords),
      };
    })
    .sort((left, right) => {
      const diff = Number(right.responseCount || 0) - Number(left.responseCount || 0);
      if (diff !== 0) return diff;
      return left.label.localeCompare(right.label, undefined, { sensitivity: "base" });
    });
}

function stripTrailingOption(label) {
  return compactLabel(label).replace(/\s*[:;,\-]?\s*\([^)]*\)\s*$/, "").trim();
}

function isAudioAuditQuestionCode(value) {
  return /^audio_audit_/iu.test(compactLabel(value));
}

function buildSectionQuestionList(questions, pageId) {
  const directRows = (Array.isArray(questions) ? questions : [])
    .filter((q) => q.pageId === pageId && q.metadataSource === "header_audit");
  if (directRows.length > 0) {
    return directRows
      .map((q, index) => {
        const rawQuestionCodes = Array.isArray(q.questionCodes) && q.questionCodes.length > 0
          ? q.questionCodes
          : [q.question];
        const rawSourceQuestionLabels = Array.isArray(q.sourceQuestionLabels) ? q.sourceQuestionLabels : [];
        const questionPairs = rawQuestionCodes
          .map((item, pairIndex) => ({
            code: compactLabel(item),
            label: compactLabel(rawSourceQuestionLabels[pairIndex] || ""),
          }))
          .filter((item) => item.code && !isAudioAuditQuestionCode(item.code));

        const questionCodes = questionPairs.map((item) => item.code);
        const displayLabel = compactLabel(q.questionLabel || q.question) || compactLabel(q.question);

        return {
          id: compactLabel(q.id) || `${pageId}_${index + 1}`,
          label: cleanCustomTableSideQuestionDisplayLabel(displayLabel, questionCodes),
          grouped: questionPairs.length > 1,
          questionCodes,
          sourceQuestionLabels: questionPairs.map((item) => item.label),
        };
      })
      .filter((q) => q.questionCodes.length > 0);
  }

  const grouped = new Map();

  questions
    .filter((q) => q.pageId === pageId)
    .forEach((q, index) => {
      const questionCode = compactLabel(q.question);
      if (!questionCode || isAudioAuditQuestionCode(questionCode)) return;
      const key = groupKeyForQuestionCode(questionCode, pageId);
      if (!grouped.has(key)) {
        grouped.set(key, {
          key,
          order: index,
          items: [],
        });
      }
      grouped.get(key).items.push({
        question: questionCode,
        questionLabel: compactLabel(q.questionLabel || q.question),
      });
    });

  return Array.from(grouped.values())
    .sort((a, b) => a.order - b.order)
    .map((entry) => {
      const base = entry.items[0];
      const isGrouped = entry.items.length > 1;
      const label = isGrouped
        ? stripTrailingOption(base.questionLabel) || base.questionLabel || base.question
        : base.questionLabel || base.question;

      return {
        id: isGrouped ? `${entry.key}__group` : base.question,
        label: cleanCustomTableSideQuestionDisplayLabel(compactLabel(label) || base.question, entry.items.map((item) => item.question)),
        grouped: isGrouped,
        questionCodes: entry.items.map((item) => item.question),
      };
    });
}

function normalizeCompactText(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

function isKnownCategoryPrefixToken(value) {
  return /^(?:N|TP|EO|BL|TC|SK|BC|CM|WH|DH|ML)$/i.test(normalizeCompactText(value));
}

function extractQuestionSurveyCode(questionCode, questionLabel) {
  const code = normalizeCompactText(questionCode);
  const label = normalizeCompactText(questionLabel);
  const codeParts = code.split("_");
  const codeMatch = codeParts.find((part) => /^(?:BAU\d+[A-Z]?|PB\d+[A-Z]?|QC\d+[A-Z]?|QFS\d+[A-Z]?|FQ\d+[A-Z]?|A\d+|QBI)$/i.test(part));
  if (codeMatch) return codeMatch.toUpperCase();

  const labelMatch = label.match(/\b[A-Z]+_(BAU\d+[A-Z]?|PB\d+[A-Z]?|QC\d+[A-Z]?|QFS\d+[A-Z]?|FQ\d+[A-Z]?|A\d+)\b/i);
  if (labelMatch?.[1]) return labelMatch[1].toUpperCase();
  if (/\bQBI\b/i.test(label)) return "QBI";
  return "";
}

function getTopLevelParenRanges(value) {
  const text = String(value || "");
  const ranges = [];
  let depth = 0;
  let start = -1;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (char === "(") {
      if (depth === 0) start = i;
      depth += 1;
      continue;
    }
    if (char === ")" && depth > 0) {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        ranges.push({ start, end: i });
        start = -1;
      }
    }
  }

  return ranges;
}

function extractLeadingBrandLabel(questionCode, questionLabel) {
  const label = normalizeCompactText(questionLabel);
  const parenBrand = label.match(/^\(([^)]+)\)/)?.[1]?.trim();
  if (parenBrand && !/^\{0\}$/i.test(parenBrand)) return parenBrand;

  const code = normalizeCompactText(questionCode);
  const legacyGrid = code.match(/^I_\d+_A_([A-Za-z0-9][A-Za-z0-9 '&.\-]{0,80})_(?:BAU\d+[A-Z]?|QBI|FQ\d+)/i);
  if (legacyGrid?.[1] && !isKnownCategoryPrefixToken(legacyGrid[1])) return normalizeCompactText(legacyGrid[1]);

  const coded = code.match(/^([A-Za-z0-9][A-Za-z0-9 '&.\-]{0,80})_(?:BAU\d+[A-Z]?|PB\d+[A-Z]?|QC\d+[A-Z]?|A\d+|QBI|FQ\d+)/i);
  if (coded?.[1] && !isKnownCategoryPrefixToken(coded[1])) return normalizeCompactText(coded[1]);
  return "";
}

function extractTrailingOptionLabel(questionLabel) {
  const label = normalizeCompactText(questionLabel);
  const ranges = getTopLevelParenRanges(label);
  if (!ranges.length) return "";
  const firstText = ranges[0] ? label.slice(ranges[0].start + 1, ranges[0].end).trim() : "";
  for (let i = ranges.length - 1; i >= 0; i -= 1) {
    const range = ranges[i];
    const candidate = label.slice(range.start + 1, range.end).trim();
    if (!candidate) continue;
    if (/^\{[01]\}$/i.test(candidate)) continue;
    if (/^none( of these)?$/i.test(candidate)) continue;
    if (firstText && candidate.toLowerCase() === firstText.toLowerCase() && i === 0 && range.start === 0) continue;
    return candidate;
  }
  return "";
}

function extractBrandImageryAttributeLabel(questionCode, questionLabel) {
  const text = normalizeCompactText(questionLabel || questionCode);
  if (!text) return "";

  const allParens = Array.from(text.matchAll(/\(([^)]+)\)/g));
  if (allParens.length > 0) {
    const first = normalizeCompactText(allParens[0]?.[1] || "");
    if (first && !/^\{0\}$/i.test(first)) return first;

    const last = normalizeCompactText(allParens[allParens.length - 1]?.[1] || "");
    if (last && !/^\{0\}$/i.test(last)) return last;
  }

  let cleaned = text;
  cleaned = cleaned.replace(/^\s*[A-Za-z0-9]+_QBI[0-9A-Za-z_]*\.?\s*/u, "");
  cleaned = cleaned.replace(/^\s*QBI[0-9A-Za-z_]*\.?\s*/iu, "");
  cleaned = cleaned.replace(/^\s*\{0\}\s*/u, "");
  const dotIndex = cleaned.indexOf(". ");
  if (dotIndex >= 0 && dotIndex < cleaned.length - 2) {
    cleaned = cleaned.slice(dotIndex + 2);
  }

  cleaned = cleaned.replace(/\s*:\s*[^:?]+\??\s*$/u, "");

  return normalizeCompactText(cleaned);
}

function extractBrandImageryQuestionGroupKey(questionCode) {
  return normalizeCompactText(questionCode).replace(/_\d+$/u, "");
}

function brandImagerySelectionSpansMultipleAttributes(questionCodes) {
  const attributeKeys = new Set(
    (Array.isArray(questionCodes) ? questionCodes : [])
      .map((questionCode) => extractBrandImageryQuestionGroupKey(questionCode))
      .filter(Boolean),
  );
  return attributeKeys.size > 1;
}

function isGroupedParentQuestionCode(questionCode, questionCodes) {
  const code = normalizeCompactText(questionCode);
  if (!code || !Array.isArray(questionCodes) || questionCodes.length <= 1) return false;
  return questionCodes
    .map((value) => normalizeCompactText(value))
    .some((candidate) => candidate && candidate !== code && candidate.startsWith(`${code}_`));
}

function isBau4ParentQuestionCodeWhenChildExists(questionCode, questionCodes) {
  const code = normalizeCompactText(questionCode);
  if (!code || !Array.isArray(questionCodes) || questionCodes.length <= 1) return false;

  const match = code.match(/^(.+_BAU4\.\d+)_(\d+)$/iu);
  if (!match) return false;

  const childCode = normalizeCompactText(`${match[1]}.1_${match[2]}`);
  return questionCodes
    .map((value) => normalizeCompactText(value))
    .some((candidate) => candidate === childCode);
}

function shouldSkipGroupedQuestionRow(questionCode, questionCodes) {
  return (
    isGroupedParentQuestionCode(questionCode, questionCodes)
    || isBau4ParentQuestionCodeWhenChildExists(questionCode, questionCodes)
  );
}

function shouldSkipCustomTableGroupedRow(questionCode, questionCodes, row) {
  if (isBau4ParentQuestionCodeWhenChildExists(questionCode, questionCodes)) return true;
  if (!isGroupedParentQuestionCode(questionCode, questionCodes)) return false;
  if (!Boolean(row?.has_child_rows_for_month)) return false;
  const rawText = normalizeCompactText(row?.answer_label || row?.answer_value);
  return isGroupedParentNumericAnswerText(rawText);
}

function getQuestionCodeSuffix(questionCode) {
  const match = normalizeCompactText(questionCode).match(/_(\d+)$/u);
  return match?.[1] || "";
}

function normalizeGroupedAnswerCodeToken(value) {
  const token = normalizeCompactText(value);
  if (!token) return "";
  const numeric = Number(token);
  if (Number.isFinite(numeric) && Number.isInteger(numeric)) return String(numeric);
  return token;
}

function parseGroupedParentAnswerCodes(value) {
  const text = normalizeCompactText(value);
  if (!text) return [];
  return text
    .split(/[\s,;|]+/u)
    .map((token) => normalizeGroupedAnswerCodeToken(token))
    .filter((token) => /^\d+$/u.test(token));
}

function isGroupedParentNumericAnswerText(value) {
  const text = normalizeCompactText(value);
  if (!text) return false;
  const tokens = text.split(/[\s,;|]+/u).filter(Boolean);
  return tokens.length > 0 && tokens.every((token) => /^\d+(?:\.0+)?$/u.test(token));
}

function getCustomTableEquivalentQuestionCodes(questionCode) {
  const code = normalizeCompactText(questionCode);
  if (!code) return [];
  const aliases = [];

  if (/^[A-Z]+_BAU/iu.test(code)) {
    aliases.push(`A_${code}`);
  }

  if (/^A_[A-Z]+_BAU/iu.test(code)) {
    aliases.push(code.replace(/^A_/iu, ""));
  }

  return aliases;
}

function getLegacyBau4QuestionCodeFromSurveyCtoCode(questionCode) {
  const match = normalizeCompactText(questionCode).match(/^([A-Za-z]+)_BAU4\.(\d+)(?:\.1)?_(\d+)$/u);
  if (!match) return "";
  return `bau4_${match[1]}_${match[2]}_${match[3]}`;
}

function getBrandImageryParentQuestionCode(questionCode) {
  const match = normalizeCompactText(questionCode).match(/^(.+_QBI\.\d+)_\d+$/iu);
  return match?.[1] || "";
}

function expandEquivalentCustomTableQuestionCodes(questionCodes) {
  const codes = Array.isArray(questionCodes)
    ? questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  const expanded = [];
  const seen = new Set();

  const addCandidate = (candidate) => {
      const normalized = normalizeCompactText(candidate);
      const key = normalized.toLowerCase();
      if (!normalized || seen.has(key)) return false;
      seen.add(key);
      expanded.push(normalized);
      return true;
  };

  codes.forEach((code) => addCandidate(code));
  codes.forEach((code) => {
    getCustomTableEquivalentQuestionCodes(code).forEach((candidate) => addCandidate(candidate));
  });

  return expanded;
}

function getSpecSourceQuestionLabels(spec) {
  return Array.isArray(spec?.sourceQuestionLabels)
    ? spec.sourceQuestionLabels.map((value) => normalizeCompactText(value))
    : [];
}

function buildSpecSourceQuestionLabelMap(spec, questionCodes) {
  const labels = getSpecSourceQuestionLabels(spec);
  const map = new Map();
  if (labels.length !== (Array.isArray(questionCodes) ? questionCodes.length : 0)) return map;
  (Array.isArray(questionCodes) ? questionCodes : []).forEach((questionCode, index) => {
    const code = normalizeCompactText(questionCode);
    const label = normalizeCompactText(labels[index] || "");
    if (code && label) map.set(code, label);
  });
  return map;
}

async function buildCustomTableQuestionLabelMap(category, questionCodes, spec, rows = []) {
  const map = buildSpecSourceQuestionLabelMap(spec, questionCodes);

  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const code = normalizeCompactText(row?.question);
    const label = normalizeCompactText(row?.question_label);
    if (code && label && !map.has(code)) map.set(code, label);
  });

  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const code = normalizeCompactText(row?.question);
    const label = normalizeCompactText(row?.question_label);
    const legacyBau4Code = getLegacyBau4QuestionCodeFromSurveyCtoCode(code);
    if (!legacyBau4Code || !label) return;
    if (!deriveBau4CustomTableValueLabel(label)) return;
    map.set(legacyBau4Code, label);
  });

  const brandImageryParentLabels = new Map();
  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const code = normalizeCompactText(row?.question);
    const label = normalizeCustomTableValueLabel(row?.question_label);
    if (!/^.+_QBI\.\d+$/iu.test(code)) return;
    if (!isMeaningfulCustomTableValue(label)) return;
    brandImageryParentLabels.set(code.toLowerCase(), label);
  });

  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const code = normalizeCompactText(row?.question);
    const parentCode = getBrandImageryParentQuestionCode(code);
    if (!parentCode) return;
    const attribute = brandImageryParentLabels.get(parentCode.toLowerCase());
    const brand = normalizeCustomTableValueLabel(row?.question_label);
    if (!attribute || !isMeaningfulCustomTableValue(brand)) return;
    map.set(code, formatBrandImageryValueLabel(attribute, brand));
  });

  const missingCodes = (Array.isArray(questionCodes) ? questionCodes : [])
    .map((value) => normalizeCompactText(value))
    .filter((code) => code && !map.has(code));
  if (!missingCodes.length) return map;

  const labelRows = await all(`
    SELECT
      CAST(question AS VARCHAR) AS question,
      MIN(CAST(question_label AS VARCHAR)) AS question_label
    FROM responses_fact
    WHERE CAST(category AS VARCHAR) = ${quote(category)}
      AND lower(CAST(question AS VARCHAR)) IN (${missingCodes.map((value) => `lower(${quote(value)})`).join(", ")})
      AND question_label IS NOT NULL
      AND CAST(question_label AS VARCHAR) <> ''
    GROUP BY CAST(question AS VARCHAR)
  `);

  labelRows.forEach((row) => {
    const code = normalizeCompactText(row?.question);
    const label = normalizeCompactText(row?.question_label);
    if (code && label && !map.has(code)) map.set(code, label);
  });

  return map;
}

function deriveGroupedParentMultiValues(spec, questionCodes, row, sourceLabelByQuestionCode = new Map()) {
  const questionCode = normalizeCompactText(row?.question);
  if (!isGroupedParentQuestionCode(questionCode, questionCodes)) return [];

  const rawText = normalizeCompactText(row?.answer_label || row?.answer_value);
  if (!isGroupedParentNumericAnswerText(rawText)) return [];

  const selectedCodes = new Set(parseGroupedParentAnswerCodes(rawText));
  const sourceLabels = getSpecSourceQuestionLabels(spec);
  const values = [];
  const seen = new Set();

  (Array.isArray(questionCodes) ? questionCodes : []).forEach((candidate, index) => {
    if (/^A_[A-Z]+_BAU/iu.test(normalizeCompactText(candidate))) return;
    const suffix = getQuestionCodeSuffix(candidate);
    if (!suffix || !selectedCodes.has(suffix)) return;
    const candidateCode = normalizeCompactText(candidate);
    const sourceLabel = sourceLabels[index] || sourceLabelByQuestionCode.get(candidateCode) || "";
    const candidateSurveyCode = extractQuestionSurveyCode(candidate, sourceLabel) || extractQuestionSurveyCode(questionCode, row?.question_label);
    const derivedLabel = deriveCustomTableQuestionOptionLabel(spec?.sectionId, candidateSurveyCode, candidate, sourceLabel, "");
    let label = derivedLabel;
    if (normalizeCompactText(spec?.sectionId) === "brand-imagery" && !label) {
      const attribute = extractBrandImageryAttributeLabel(questionCode, row?.question_label);
      const brand = normalizeCustomTableValueLabel(sourceLabel);
      label = attribute && isMeaningfulCustomTableValue(brand)
        ? formatBrandImageryValueLabel(attribute, brand)
        : "";
    } else if (!label) {
      label = normalizeCustomTableValueLabel(sourceLabel) || candidateCode;
    }
    if (!isMeaningfulCustomTableValue(label) || isBinaryAnswerToken(label)) return;
    if (seen.has(label)) return;
    seen.add(label);
    values.push(label);
  });

  return values;
}

function buildCustomTableGroupedOptionLabels(spec, questionCodes) {
  if (!Array.isArray(questionCodes) || questionCodes.length <= 1) return [];
  const sourceLabels = getSpecSourceQuestionLabels(spec);
  const labels = [];
  const seen = new Set();

  questionCodes.forEach((questionCode, index) => {
    const code = normalizeCompactText(questionCode);
    if (/^A_[A-Z]+_BAU/iu.test(code)) return;
    if (!code || isGroupedParentQuestionCode(code, questionCodes) || shouldSkipGroupedQuestionRow(code, questionCodes)) return;
    const sourceLabel = sourceLabels[index] || "";
    const candidateSurveyCode = extractQuestionSurveyCode(code, sourceLabel);
    const label = normalizeCustomTableValueLabel(
      deriveCustomTableQuestionOptionLabel(spec?.sectionId, candidateSurveyCode, code, sourceLabel, ""),
    );
    if (!isMeaningfulCustomTableValue(label) || isBinaryAnswerToken(label) || seen.has(label)) return;
    seen.add(label);
    labels.push(label);
  });

  return labels;
}

function looksLikeNumericGridToken(value) {
  return /^\d+(?:_\d+)+$/u.test(normalizeCompactText(value));
}

function extractBrandImageryBrandLabel(questionCode, questionLabel, answerText) {
  if (isMeaningfulResponseValue(answerText) && !isBinaryAnswerToken(answerText) && !looksLikeNumericGridToken(answerText)) {
    return answerText;
  }

  const trailingLabel = extractTrailingOptionLabel(questionLabel);
  if (trailingLabel && !looksLikeNumericGridToken(trailingLabel)) return trailingLabel;

  const questionOption = extractQuestionLabelOption(questionLabel);
  if (questionOption && !looksLikeNumericGridToken(questionOption)) return questionOption;

  return extractLeadingBrandLabel(questionCode, questionLabel);
}

function formatBrandImageryValueLabel(attributeLabel, brandLabel) {
  const attribute = normalizeCompactText(attributeLabel);
  const brand = normalizeDisplayLabel(brandLabel, "");

  if (attribute && brand && attribute.toLowerCase() !== brand.toLowerCase()) {
    return `${attribute} (${brand})`;
  }

  return attribute || brand;
}

function parseFormattedAttributeBrandLabel(value) {
  const match = normalizeCompactText(value).match(/^(.*)\s+\(([^()]+)\)\s*$/u);
  const attribute = normalizeCompactText(match?.[1] || "");
  const brand = normalizeCompactText(match?.[2] || "");
  if (!attribute || !brand) return null;
  return { attribute, brand };
}

function deriveBrandImageryCustomTableValueLabel(questionCode, questionLabel, answerText) {
  const formatted = parseFormattedAttributeBrandLabel(questionLabel);
  if (formatted) return formatBrandImageryValueLabel(formatted.attribute, formatted.brand);
  if (isGroupedParentNumericAnswerText(answerText)) return "";

  const attributeLabel = extractBrandImageryAttributeLabel(questionCode, questionLabel);
  const brandLabel = extractBrandImageryBrandLabel(questionCode, questionLabel, answerText);
  const label = formatBrandImageryValueLabel(attributeLabel, brandLabel);
  return parseFormattedAttributeBrandLabel(label) ? label : "";
}

function formatMediaSourceCustomTableValueLabel(optionLabel, brandLabel) {
  const option = normalizeCompactText(optionLabel).replace(/\s*,\s*$/u, "");
  const brand = normalizeDisplayLabel(brandLabel, "");

  if (option && brand && option.toLowerCase() !== brand.toLowerCase()) {
    return `${option} (${brand})`;
  }

  return option || brand;
}

function isMeaningfulResponseValue(value) {
  const text = normalizeDisplayLabel(value, "");
  if (!text) return false;
  if (text === "(No response)") return false;
  if (isPlaceholderDashLabel(text)) return false;
  if (isAttachmentUrlLabel(text)) return false;
  return true;
}

function isCustomTableVariableCodeLabel(value) {
  return /^(?:[A-Z]{1,4}_)?(?:BAU|PB|QC|QFS|QFSB|QFW|FQ|QBI|A)\d+[A-Z]?(?:[._]\d+)*(?:_[A-Z0-9]+)?$/i.test(normalizeCompactText(value));
}

function isMeaningfulCustomTableValue(value) {
  return isMeaningfulResponseValue(value) && !isCustomTableVariableCodeLabel(value);
}

function getCustomTableAnswerText(answerLabel, answerValue) {
  const candidates = [answerLabel, answerValue]
    .map((value) => normalizeDisplayLabel(value, ""))
    .filter((value) =>
      isMeaningfulResponseValue(value)
      && !isBinaryAnswerToken(value)
      && !isKnownCategoryPrefixToken(value)
      && !isCustomTableVariableCodeLabel(value),
    );
  return candidates[0] || "";
}

function isBinaryAnswerToken(value) {
  const text = normalizeCompactText(value).toLowerCase();
  if (!text) return false;
  return [
    "1",
    "1.0",
    "0",
    "0.0",
    "yes",
    "no",
    "true",
    "false",
    "selected",
    "not selected",
    "agree",
    "agreed",
    "disagree",
    "disagreed",
    "strongly agree",
    "strongly disagree",
    "neutral",
  ].includes(text);
}

function isPositiveAnswerSelection(answerLabel, answerValue, answerValueNum) {
  const label = normalizeCompactText(answerLabel).toLowerCase();
  const value = normalizeCompactText(answerValue).toLowerCase();
  const numeric = Number(answerValueNum);
  if (Number.isFinite(numeric) && numeric === 1) return true;
  return ["1", "1.0", "yes", "true", "selected", "agree", "agreed", "strongly agree"].includes(label)
    || ["1", "1.0", "yes", "true", "selected", "agree", "agreed", "strongly agree"].includes(value);
}

function normalizeCustomTableValueLabel(value) {
  const text = normalizeDisplayLabel(value, "");
  if (!text) return "";

  const unwrapped = text.match(/^\(([^()]+)\)$/u)?.[1]?.trim();
  const comparable = normalizeCompactText(unwrapped || text);

  if (/^none(?:\s+of\s+these)?$/i.test(comparable)) return "None";
  if (/^others?$/i.test(comparable)) return "Other";
  return text;
}

function isBrandMultiSelectSurveyCode(surveyCode) {
  return /^(?:BAU1B|BAU1D|FQ1)$/i.test(normalizeCompactText(surveyCode));
}

function isAwarenessAdTomPromptStemLabel(text) {
  const t = normalizeCompactText(text);
  if (t.length >= 50 && /\bTV,\s*Radio,\s*Digital\/LED\s+Billboard/i.test(t)) return true;
  if (t.length >= 50 && /\bStatic\s+Billboard\b/i.test(t) && /\bBRT\b/i.test(t)) return true;
  return false;
}

function isAwarenessMediaChannelListLabel(text) {
  const t = normalizeCompactText(text);
  if (!t) return false;
  if (isAwarenessAdTomPromptStemLabel(t)) return true;
  if (/^on TV,\s*Radio,/i.test(t)) return true;
  if (/^TV,\s*Radio,\s*Digital\/LED/i.test(t)) return true;
  return false;
}

function cleanAwarenessAdPromptDisplayLabel(label) {
  let text = normalizeCompactText(label);
  if (!text) return "";
  text = text.replace(/\s*\((?:on\s+)?TV,\s*Radio,[\s\S]*?BRT\s+etc\.\)\s*/giu, " ");
  text = text.replace(/\s*(?:on\s+)?TV,\s*Radio,\s*Digital\/LED\s+Billboard\s*\(Billboard\s+that\s+shows\s+videos?\),\s*Static\s+Billboard\s*\(Billboard\s+that\s+shows\s+pictures?\),\s*Internet,\s*BRT\s+etc\.?/giu, " ");
  text = text.replace(/\s+/g, " ").trim();
  return text;
}

function isNonBrandTomAnswerValue(value) {
  const text = normalizeCompactText(value).toLowerCase();
  if (!text) return true;
  if (isAwarenessMediaChannelListLabel(text)) return true;
  if (/\bf(?:l|k)avou?r\b/u.test(text)) return true;
  if (/\bsuper\s*p(?:a|ar)?(?:ck|k)\b/u.test(text)) return true;
  if (/\bpack(?:et|s)?\b/u.test(text)) return true;
  if (/\btable\b/u.test(text)) return true;
  return false;
}

const BAU4_HIDDEN_MEDIA_LABELS = new Set([
  "that shows videos",
  "billboard that shows pictures",
  "video or picture",
  "other",
  "others",
  "specify",
  "other specify",
  "others specify",
  "other, specify",
  "others, specify",
  "—",
  "-",
  "–",
]);

function shouldDropCustomTableRowLabel(label) {
  const t = normalizeCompactText(label);
  if (!t) return true;
  if (isPlaceholderDashLabel(t)) return true;
  if (isAttachmentUrlLabel(t)) return true;
  if (isAwarenessMediaChannelListLabel(t)) return true;
  if (BAU4_HIDDEN_MEDIA_LABELS.has(t.toLowerCase())) return true;
  return false;
}

function cleanBau4MediaOptionLabel(option) {
  const o = normalizeCompactText(option);
  if (!o) return "";
  if (BAU4_HIDDEN_MEDIA_LABELS.has(o.toLowerCase())) return "";
  if (/^that shows videos$/i.test(o)) return "";
  if (/^billboard that shows pictures$/i.test(o)) return "";
  if (/^video or picture$/i.test(o)) return "";
  return o;
}

function deriveBau4CustomTableValueLabel(questionLabel) {
  const option = cleanBau4MediaOptionLabel(extractQuestionLabelOption(questionLabel));
  const brand = deriveMediaSourceBrandFromLabel(questionLabel);
  if (!option || !brand) return "";

  const formatted = normalizeCustomTableValueLabel(formatMediaSourceCustomTableValueLabel(option, brand));
  if (shouldDropCustomTableRowLabel(formatted)) return "";
  return formatted;
}

function cleanCustomTableSideQuestionDisplayLabel(label, questionCodes) {
  let t = normalizeCompactText(label);
  if (!t) return t;
  const codeBlob = (Array.isArray(questionCodes) ? questionCodes : []).join(" ");
  if (/(^|_)BAU1[CD](?:_|$)/i.test(codeBlob)) {
    t = cleanAwarenessAdPromptDisplayLabel(t);
  }
  if (/(^|_)BAU2(?:_|$)/i.test(codeBlob)) {
    t = t.replace(/\s*\?\s*:\s*[A-Za-z0-9][A-Za-z0-9 '&.\-]{0,40}\?/gi, "?");
    t = t.replace(/\s*:\s*Golden Penny\s*\?/gi, "");
    t = t.replace(/\s+/g, " ").trim();
  }
  if (/(^|_)QBI(?:_|$)/i.test(codeBlob) || /brand\s*imagery/i.test(t)) {
    t = t.replace(/:\s*Golden Penny\s*\?/gi, "?");
    t = t.replace(/\?\s*\?/g, "?");
    t = t.replace(/\s+/g, " ").trim();
  }
  return t;
}

function specUsesOnlyBauTomCodes(questionCodes) {
  const codes = Array.isArray(questionCodes) ? questionCodes : [];
  if (!codes.length) return false;
  return codes.every((c) => {
    const x = normalizeCompactText(c);
    return /(^|_)BAU1A(?:_|$)/i.test(x) || /(^|_)BAU1C(?:_|$)/i.test(x);
  });
}

function deriveCustomTableQuestionOptionLabel(sectionId, surveyCode, questionCode, questionLabel, answerText = "") {
  const normalizedSectionId = normalizeCompactText(sectionId);
  const normalizedSurveyCode = normalizeCompactText(surveyCode || extractQuestionSurveyCode(questionCode, questionLabel));
  const normalizedAnswerText = normalizeCustomTableValueLabel(answerText);

  if (normalizedSectionId === "brand-imagery" || normalizedSurveyCode === "QBI") {
    return deriveBrandImageryCustomTableValueLabel(questionCode, questionLabel, normalizedAnswerText);
  }

  if (/^BAU4/i.test(normalizedSurveyCode)) {
    return deriveBau4CustomTableValueLabel(questionLabel);
  }

  const leadingBrand = extractLeadingBrandLabel(questionCode, questionLabel);
  const trailingOption = extractTrailingOptionLabel(questionLabel);
  const questionOption = extractQuestionLabelOption(questionLabel);
  const directOptionLabel = normalizeCustomTableValueLabel(questionLabel);
  const canUseDirectOptionLabel =
    directOptionLabel.length > 0
    && directOptionLabel.length <= 80
    && !/[?.]/u.test(directOptionLabel)
    && isMeaningfulCustomTableValue(directOptionLabel)
    && !isBinaryAnswerToken(directOptionLabel);

  if (isBrandMultiSelectSurveyCode(normalizedSurveyCode)) {
    return normalizeCustomTableValueLabel(
      leadingBrand
        || questionOption
        || trailingOption
        || (canUseDirectOptionLabel ? directOptionLabel : "")
        || (isMeaningfulCustomTableValue(normalizedAnswerText) ? normalizedAnswerText : ""),
    );
  }

  return normalizeCustomTableValueLabel(
    questionOption
      || trailingOption
      || leadingBrand
      || (canUseDirectOptionLabel ? directOptionLabel : "")
      || (isMeaningfulCustomTableValue(normalizedAnswerText) ? normalizedAnswerText : ""),
  );
}

function deriveCustomTableValue(sectionId, questionCodes, row) {
  const questionCode = normalizeCompactText(row.question);
  const questionLabel = normalizeCompactText(row.question_label);
  const answerLabel = normalizeDisplayLabel(row.answer_label, "");
  const answerValue = normalizeDisplayLabel(row.answer_value, "");
  const answerText = getCustomTableAnswerText(answerLabel, answerValue);
  const surveyCode = extractQuestionSurveyCode(questionCode, questionLabel);
  const grouped = Array.isArray(questionCodes) && questionCodes.length > 1;
  const positive = isPositiveAnswerSelection(answerLabel, answerValue, row.answer_value_num);
  if (grouped && shouldSkipCustomTableGroupedRow(questionCode, questionCodes, row)) return "";

  if (sectionId === "brand-imagery" || surveyCode === "QBI") {
    if (grouped && !positive) return "";
    return deriveBrandImageryCustomTableValueLabel(questionCode, questionLabel, answerText);
  }

  if (/^BAU4/i.test(surveyCode)) {
    if (grouped && !positive && !isMeaningfulResponseValue(answerText)) return "";
    return deriveBau4CustomTableValueLabel(questionLabel);
  }

  if (/(^|_)BAU1A(?:_|$)/i.test(questionCode) || surveyCode === "BAU1A") {
    if (grouped && !positive && !isMeaningfulCustomTableValue(answerText)) return "";
    if (isMeaningfulCustomTableValue(answerText) && !isNonBrandTomAnswerValue(answerText)) {
      return normalizeCustomTableValueLabel(answerText);
    }
    const brand = extractLeadingBrandLabel(questionCode, questionLabel);
    if (brand) return normalizeCustomTableValueLabel(brand);
    const directLabel = normalizeCustomTableValueLabel(questionLabel);
    if (isMeaningfulCustomTableValue(directLabel) && !isAwarenessMediaChannelListLabel(directLabel)) {
      return directLabel;
    }
    return "";
  }

  if (/(^|_)BAU1C(?:_|$)/i.test(questionCode) || surveyCode === "BAU1C") {
    if (grouped && !positive && !isMeaningfulCustomTableValue(answerText)) return "";
    if (isMeaningfulCustomTableValue(answerText) && !isNonBrandTomAnswerValue(answerText)) {
      return normalizeCustomTableValueLabel(answerText);
    }
    const brand = extractLeadingBrandLabel(questionCode, questionLabel);
    if (brand) return normalizeCustomTableValueLabel(brand);
    const directLabel = normalizeCustomTableValueLabel(questionLabel);
    if (isMeaningfulCustomTableValue(directLabel) && !isAwarenessMediaChannelListLabel(directLabel)) {
      return directLabel;
    }
    return "";
  }

  if (/(^|_)BAU1D(?:_|$)/i.test(questionCode) || surveyCode === "BAU1D") {
    if (grouped && !positive && !isMeaningfulCustomTableValue(answerText)) return "";
    if (isGroupedParentNumericAnswerText(answerText)) return "";
    if (isMeaningfulCustomTableValue(answerText) && !isAwarenessMediaChannelListLabel(answerText)) {
      return normalizeCustomTableValueLabel(answerText);
    }
    const brand = extractLeadingBrandLabel(questionCode, questionLabel);
    if (brand) return normalizeCustomTableValueLabel(brand);
    const directLabel = normalizeCustomTableValueLabel(questionLabel);
    if (isMeaningfulCustomTableValue(directLabel) && !isAwarenessMediaChannelListLabel(directLabel)) {
      return directLabel;
    }
    return "";
  }

  if (grouped) {
    if (positive) {
      return deriveCustomTableQuestionOptionLabel(sectionId, surveyCode, questionCode, questionLabel, answerText);
    }

    // Grouped questions are not always checkbox/multi-select grids.
    // Some single-select questions are grouped by survey code in the UI, and
    // their selected option is stored directly as answer_label/answer_value.
    // Do not drop those rows just because the answer is not a positive checkbox token.
    if (isMeaningfulCustomTableValue(answerText)) return normalizeCustomTableValueLabel(answerText);
    return "";
  }

  if (isMeaningfulCustomTableValue(answerText)) return normalizeCustomTableValueLabel(answerText);
  const trailing = extractTrailingOptionLabel(questionLabel);
  if (isAwarenessMediaChannelListLabel(trailing)) return "";
  return normalizeCustomTableValueLabel(trailing);
}

function buildQuestionSpecCacheKey(spec) {
  const codes = Array.isArray(spec?.questionCodes) ? spec.questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean).sort() : [];
  return `${normalizeCompactText(spec?.sectionId)}::${codes.join("|")}::${normalizeCompactText(spec?.label)}`;
}

async function expandLegacyBau4CodesForCustomTable(category, questionCodes, months) {
  const codes = Array.isArray(questionCodes)
    ? questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  if (!codes.some((c) => /bau4_|_BAU4|BAU4[._]/i.test(c))) return codes;

  const questionExpr = await getResponsesFactQuestionSqlExpr();
  const brands = new Set();
  const surveyCtoCodeClauses = [];
  codes.forEach((compact) => {
    let m = compact.match(/^bau4_([A-Za-z0-9]+)_\d+_\d+$/i);
    if (m) brands.add(m[1].toUpperCase());
    m = compact.match(/^bau4_([A-Za-z0-9]+)_(\d+)(?:_(\d+))?$/i);
    if (m) {
      const prefix = m[1];
      const brandIndex = m[2];
      const optionIndex = m[3];
      const pattern = optionIndex
        ? `(?i)^${prefix}_BAU4\\.${brandIndex}(?:\\.1)?_${optionIndex}$`
        : `(?i)^${prefix}_BAU4\\.${brandIndex}(?:\\.1)?(?:_[0-9]+)?$`;
      surveyCtoCodeClauses.push(`regexp_matches(${questionExpr}, ${quote(pattern)})`);
    }
    m = compact.match(/^([A-Za-z0-9]+)_BAU4/i);
    if (m?.[1] && !isKnownCategoryPrefixToken(m[1])) brands.add(m[1].toUpperCase());
    m = compact.match(/^I_\d+_A_([A-Za-z0-9]+)_BAU4/i);
    if (m) brands.add(m[1].toUpperCase());
  });

  if (brands.size === 0 && surveyCtoCodeClauses.length === 0) return codes;

  const monthValues = Array.isArray(months)
    ? months.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  const monthSql = monthValues.length
    ? `AND CAST(month AS VARCHAR) IN (${monthValues.map((month) => quote(month)).join(", ")})`
    : "";

  const brandClauses = Array.from(brands)
    .map((b) => `regexp_matches(${questionExpr}, ${quote(`(?i)^I_[0-9]+_A_${b}_BAU4_[0-9]+$`)})`)
    .concat(surveyCtoCodeClauses)
    .join(" OR ");

  const rows = await all(`
    SELECT DISTINCT ${questionExpr} AS question
    FROM responses_fact
    WHERE CAST(category AS VARCHAR) = ${quote(category)}
      ${monthSql}
      AND (${brandClauses})
  `);

  let extra = rows
    .map((row) => normalizeCompactText(row.question))
    .filter(Boolean);
  if (!extra.length && monthSql) {
    const fallbackRows = await all(`
      SELECT DISTINCT ${questionExpr} AS question
      FROM responses_fact
      WHERE CAST(category AS VARCHAR) = ${quote(category)}
        AND (${brandClauses})
    `);
    extra = fallbackRows
      .map((row) => normalizeCompactText(row.question))
      .filter(Boolean);
  }
  if (!extra.length) return codes;
  return expandEquivalentCustomTableQuestionCodes([...codes, ...extra]);
}

async function expandLegacySurveyQuestionCodesForCustomTable(category, questionCodes, months) {
  const codes = Array.isArray(questionCodes)
    ? questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  if (!codes.length) return codes;

  const needs = {
    fq1: codes.some((c) => /FQ1/i.test(c)),
    qfs1: codes.some((c) => /QFS1/i.test(c)),
    qbi: codes.some((c) => /(^|_)QBI$/i.test(c) || /(^|_)QBI\.\d+$/i.test(c)),
    bau1a: codes.some((c) => /(^|_)BAU1A(?:_|$)/i.test(c)),
    bau1c: codes.some((c) => /(^|_)BAU1C(?:_|$)/i.test(c)),
    bau1d: codes.some((c) => /(^|_)BAU1D(?:_|$)/i.test(c)),
  };
  if (!needs.fq1 && !needs.qfs1 && !needs.qbi && !needs.bau1a && !needs.bau1c && !needs.bau1d) return codes;

  const monthValues = Array.isArray(months)
    ? months.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  const monthSql = monthValues.length
    ? `AND CAST(month AS VARCHAR) IN (${monthValues.map((month) => quote(month)).join(", ")})`
    : "";

  const ors = [];
  const questionExpr = await getResponsesFactQuestionSqlExpr();
  if (needs.fq1) ors.push(`regexp_matches(${questionExpr}, '(?i)FQ1')`);
  if (needs.qfs1) ors.push(`regexp_matches(${questionExpr}, '(?i)QFS1')`);
  if (needs.qbi) {
    const qbiParentClauses = codes
      .map((code) => {
        if (/(^|_)QBI$/i.test(code)) return `regexp_matches(${questionExpr}, '(?i)QBI')`;
        const escaped = code.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        if (/(^|_)QBI\.\d+$/i.test(code)) {
          return `regexp_matches(${questionExpr}, ${quote(`(?i)^${escaped}(?:_[0-9]+)?$`)})`;
        }
        return "";
      })
      .filter(Boolean);
    ors.push(...qbiParentClauses);
  }
  if (needs.bau1a) ors.push(`regexp_matches(${questionExpr}, '(?i)(^|_)BAU1A(?:_|$)')`);
  if (needs.bau1c) ors.push(`regexp_matches(${questionExpr}, '(?i)(^|_)BAU1C(?:_|$)')`);
  if (needs.bau1d) ors.push(`regexp_matches(${questionExpr}, '(?i)(^|_)BAU1D(?:_|$)')`);

  const rows = await all(`
    SELECT DISTINCT ${questionExpr} AS question
    FROM responses_fact
    WHERE CAST(category AS VARCHAR) = ${quote(category)}
      ${monthSql}
      AND (${ors.join(" OR ")})
  `);
  let extra = rows
    .map((row) => normalizeCompactText(row.question))
    .filter(Boolean);
  if (!extra.length && monthSql) {
    const fallbackRows = await all(`
      SELECT DISTINCT ${questionExpr} AS question
      FROM responses_fact
      WHERE CAST(category AS VARCHAR) = ${quote(category)}
        AND (${ors.join(" OR ")})
    `);
    extra = fallbackRows
      .map((row) => normalizeCompactText(row.question))
      .filter(Boolean);
  }
  if (!extra.length) return codes;
  return expandEquivalentCustomTableQuestionCodes([...codes, ...extra]);
}

async function expandCustomTableQuestionCodesFromFacts(category, questionCodes, months) {
  const codes = Array.isArray(questionCodes)
    ? questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  const expandableParents = codes.filter((code) =>
    code
    && !/_\d+$/u.test(code)
    && !/^bau4_[A-Z]+_\d+_\d+$/iu.test(code),
  );
  if (!expandableParents.length) return expandEquivalentCustomTableQuestionCodes(codes);

  const monthValues = Array.isArray(months)
    ? months.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  const monthSql = monthValues.length
    ? `AND CAST(month AS VARCHAR) IN (${monthValues.map((value) => quote(value)).join(", ")})`
    : "";
  const questionExpr = await getResponsesFactQuestionSqlExpr();
  const patternParents = expandEquivalentCustomTableQuestionCodes(expandableParents);
  const patternSql = patternParents
    .flatMap((code) => [
      `lower(${questionExpr}) LIKE lower(${quote(`${code}_%`)})`,
      `lower(${questionExpr}) LIKE lower(${quote(`${code}.%`)})`,
      `regexp_matches(${questionExpr}, ${quote(`(?i)^${escapeRegexLiteral(code)}_[0-9]+$`)})`,
      `regexp_matches(${questionExpr}, ${quote(`(?i)^${escapeRegexLiteral(code)}\\.[0-9]+(?:_[0-9]+)?$`)})`,
    ])
    .join(" OR ");
  if (!patternSql) return expandEquivalentCustomTableQuestionCodes(codes);

  let childRows = await all(`
    SELECT DISTINCT ${questionExpr} AS question
    FROM responses_fact
    WHERE CAST(category AS VARCHAR) = ${quote(category)}
      ${monthSql}
      AND (${patternSql})
    ORDER BY question
  `);
  if ((!Array.isArray(childRows) || !childRows.length) && monthSql) {
    childRows = await all(`
      SELECT DISTINCT ${questionExpr} AS question
      FROM responses_fact
      WHERE CAST(category AS VARCHAR) = ${quote(category)}
        AND (${patternSql})
      ORDER BY question
    `);
  }
  const childCodes = childRows
    .map((row) => normalizeCompactText(row.question))
    .filter((question) => !/_OTH$/iu.test(question) && !/^audio_audit_/iu.test(question))
    .filter(Boolean);
  if (!childCodes.length) return expandEquivalentCustomTableQuestionCodes(codes);

  return expandEquivalentCustomTableQuestionCodes([...codes, ...childCodes]);
}

async function customTableQuestionHasFactRows(category, questionCodes, months) {
  const expandedCodes = await expandCustomTableQuestionCodesFromFacts(category, questionCodes, months);
  if (!expandedCodes.length) return false;

  const monthValues = Array.isArray(months)
    ? months.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  const monthSql = monthValues.length
    ? `AND CAST(month AS VARCHAR) IN (${monthValues.map((value) => quote(value)).join(", ")})`
    : "";
  const questionExpr = await getResponsesFactQuestionSqlExpr();
  const rows = await all(`
    SELECT COUNT(*) AS total
    FROM responses_fact
    WHERE CAST(category AS VARCHAR) = ${quote(category)}
      ${monthSql}
      AND lower(${questionExpr}) IN (${expandedCodes.map((value) => `lower(${quote(value)})`).join(", ")})
    LIMIT 1
  `);
  return Number(rows?.[0]?.total || 0) > 0;
}

async function getCustomTableRequestCached(requestCache, cacheName, key, factory) {
  if (!requestCache) return factory();
  if (!requestCache[cacheName]) requestCache[cacheName] = new Map();
  const cache = requestCache[cacheName];
  if (cache.has(key)) return cache.get(key);
  const pending = Promise.resolve().then(factory);
  cache.set(key, pending);
  try {
    const value = await pending;
    cache.set(key, value);
    return value;
  } catch (err) {
    cache.delete(key);
    throw err;
  }
}

async function getResponsesFactQuestionSqlExpr(alias = "") {
  const columns = await describeRelationColumns("responses_fact");
  const prefix = alias ? `${alias}.` : "";
  return columns.includes("question_raw")
    ? `COALESCE(CAST(${prefix}${ident("question_raw")} AS VARCHAR), CAST(${prefix}${ident("question")} AS VARCHAR))`
    : `CAST(${prefix}${ident("question")} AS VARCHAR)`;
}

function buildCustomTableExpansionCacheKey(category, questionCodes, months) {
  const codes = Array.isArray(questionCodes) ? questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean) : [];
  const monthValues = Array.isArray(months) ? months.map((value) => normalizeCompactText(value)).filter(Boolean) : [];
  return JSON.stringify({
    category: normalizeCompactText(category),
    questionCodes: codes,
    months: monthValues,
  });
}

async function expandQuestionCodesForCustomTableData(category, questionCodes, months, requestCache = null) {
  return getCustomTableRequestCached(
    requestCache,
    "expandedQuestionCodes",
    buildCustomTableExpansionCacheKey(category, questionCodes, months),
    async () => {
      let expandedCodes = await expandCustomTableQuestionCodesFromFacts(category, questionCodes, months);
      expandedCodes = await expandLegacyBau4CodesForCustomTable(category, expandedCodes, months);
      expandedCodes = await expandLegacySurveyQuestionCodesForCustomTable(category, expandedCodes, months);
      return expandedCodes;
    },
  );
}

async function fetchCustomTableQuestionData(category, spec, filters, months, requestCache = null) {
  if (isOverallCustomTableSpec(spec)) {
    const monthValues = Array.isArray(months)
      ? months.map((value) => normalizeCompactText(value)).filter(Boolean)
      : [];
    const monthSql = monthValues.length
      ? `AND CAST(d.month AS VARCHAR) IN (${monthValues.map((month) => quote(month)).join(", ")})`
      : "";
    const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");

    const rows = await all(`
      SELECT
        CAST(d.respondent_id AS VARCHAR) AS respondent_id,
        CAST(d.month AS VARCHAR) AS month
      FROM respondent_dims d
      WHERE CAST(d.category AS VARCHAR) = ${quote(category)}
        ${monthSql}
      ${filterSql}
    `);

    const valueMap = new Map();
    rows.forEach((row) => {
      const key = `${normalizeCompactText(row.respondent_id)}__${normalizeCompactText(row.month)}`;
      if (!key || key === "__") return;
      valueMap.set(key, new Set([CUSTOM_TABLE_OVERALL_LABEL]));
    });

    return {
      valueMap,
      valueOrder: [CUSTOM_TABLE_OVERALL_LABEL],
      isMultiValued: false,
    };
  }

  let questionCodes = Array.isArray(spec?.questionCodes)
    ? spec.questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];

  if (String(spec?.sectionId || "").startsWith("month-grouping")) {
    const monthValues = Array.isArray(spec?.monthValues)
      ? spec.monthValues
          .map((item) => ({
            label: normalizeCompactText(item?.label),
            months: Array.isArray(item?.months)
              ? item.months.map((value) => normalizeCompactText(value)).filter(Boolean)
              : [],
          }))
          .filter((item) => item.label && item.months.length > 0)
      : [];
    const groupMonths = Array.isArray(spec?.months)
      ? spec.months.map((value) => normalizeCompactText(value)).filter(Boolean)
      : [];
    const label = normalizeCompactText(spec?.label) || groupMonths.join(", ");
    const valueSpecs = monthValues.length > 0
      ? monthValues
      : groupMonths.length > 0 && label
        ? [{ label, months: groupMonths }]
        : [];
    const allGroupMonths = Array.from(new Set(valueSpecs.flatMap((item) => item.months)));
    if (!allGroupMonths.length) {
      return {
        valueMap: new Map(),
        valueOrder: [],
        isMultiValued: false,
      };
    }

    const scopedMonths = Array.isArray(months) && months.length > 0
      ? allGroupMonths.filter((month) => months.includes(month))
      : allGroupMonths;
    if (!scopedMonths.length) {
      return {
        valueMap: new Map(),
        valueOrder: [],
        isMultiValued: false,
      };
    }

    const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");
    const rows = await all(`
      SELECT
        CAST(d.respondent_id AS VARCHAR) AS respondent_id,
        CAST(d.month AS VARCHAR) AS month
      FROM respondent_dims d
      WHERE CAST(d.category AS VARCHAR) = ${quote(category)}
        AND CAST(d.month AS VARCHAR) IN (${scopedMonths.map((month) => quote(month)).join(", ")})
      ${filterSql}
    `);

    const valueMap = new Map();
    const labelsByMonth = new Map();
    valueSpecs.forEach((item) => {
      item.months.forEach((month) => {
        if (!scopedMonths.includes(month)) return;
        if (!labelsByMonth.has(month)) labelsByMonth.set(month, []);
        labelsByMonth.get(month).push(item.label);
      });
    });
    rows.forEach((row) => {
      const key = `${normalizeCompactText(row.respondent_id)}__${normalizeCompactText(row.month)}`;
      if (!key || key === "__") return;
      const monthLabels = labelsByMonth.get(normalizeCompactText(row.month)) || [];
      if (!monthLabels.length) return;
      if (!valueMap.has(key)) valueMap.set(key, new Set());
      monthLabels.forEach((monthLabel) => valueMap.get(key).add(monthLabel));
    });

    return {
      valueMap,
      valueOrder: Array.from(new Set(valueSpecs.map((item) => item.label).filter(Boolean))),
      isMultiValued: valueSpecs.some((item) => item.months.length > 1),
    };
  }

  if (isCustomTableClassifiedAdChannelsSpec(spec, questionCodes)) {
    const classifiedQuestionExpr = await getResponsesFactQuestionSqlExpr("r");
    const monthFilterSql =
      Array.isArray(months) && months.length > 0
        ? ` AND CAST(r.month AS VARCHAR) IN (${months.map((month) => quote(month)).join(", ")})`
        : "";
    const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");
    const rows = await all(`
      SELECT
        CAST(r.respondent_id AS VARCHAR) AS respondent_id,
        CAST(r.month AS VARCHAR) AS month,
        ${classifiedQuestionExpr} AS question,
        CAST(r.question_label AS VARCHAR) AS question_label,
        CAST(r.answer_label AS VARCHAR) AS answer_label,
        CAST(r.answer_value AS VARCHAR) AS answer_value,
        CAST(r.answer_value_num AS DOUBLE) AS answer_value_num
      FROM responses_fact r
      JOIN respondent_dims d
        ON CAST(d.category AS VARCHAR) = CAST(r.category AS VARCHAR)
       AND CAST(d.respondent_id AS VARCHAR) = CAST(r.respondent_id AS VARCHAR)
       AND CAST(d.month AS VARCHAR) = CAST(r.month AS VARCHAR)
      WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
        AND regexp_matches(${classifiedQuestionExpr}, '(?i)(BAU4[._]|_BAU4|BAU4_)')
        ${monthFilterSql}
        ${filterSql}
    `);

    const valueMap = new Map();
    rows.forEach((row) => {
      const answerLabel = normalizeDisplayLabel(row.answer_label, "");
      const answerValue = normalizeDisplayLabel(row.answer_value, "");
      const answerText = getCustomTableAnswerText(answerLabel, answerValue);
      const selected = isPositiveAnswerSelection(answerLabel, answerValue, row.answer_value_num)
        || (isMeaningfulResponseValue(answerText) && !isBinaryAnswerToken(answerText));
      if (!selected) return;

      const channel = deriveCustomTableValue("awareness-usage", [row.question], row);
      const classifiedChannel = classifyCustomTableAdChannel(channel || answerText || row.question_label);
      if (!classifiedChannel) return;

      const key = `${normalizeCompactText(row.respondent_id)}__${normalizeCompactText(row.month)}`;
      if (!key || key === "__") return;
      if (!valueMap.has(key)) valueMap.set(key, new Set());
      valueMap.get(key).add(classifiedChannel);
    });

    return {
      valueMap,
      valueOrder: CUSTOM_TABLE_CLASSIFIED_AD_CHANNEL_LABELS,
      isMultiValued: true,
    };
  }

  if (spec?.sectionId === "screener-demographics") {
    const field = normalizeCompactText(questionCodes[0] || "");
    const valueExpr = buildCustomTableDimSqlExpr(field, respondentDimColumns, "d");
    if (!field || !valueExpr) {
      return {
        valueMap: new Map(),
        valueOrder: [],
        isMultiValued: false,
      };
    }

    const monthFilterSql =
      Array.isArray(months) && months.length > 0
        ? ` AND CAST(d.month AS VARCHAR) IN (${months.map((month) => quote(month)).join(", ")})`
        : "";
    const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");
    const rows = await all(`
      SELECT
        CAST(d.respondent_id AS VARCHAR) AS respondent_id,
        CAST(d.month AS VARCHAR) AS month,
        CAST(${valueExpr} AS VARCHAR) AS value
      FROM respondent_dims d
      WHERE CAST(d.category AS VARCHAR) = ${quote(category)}
      ${monthFilterSql}
      ${filterSql}
    `);

    const valueMap = new Map();
    const valueOrder = [];
    const seenValues = new Set();

    rows.forEach((row) => {
      const value = normalizeRespondentDimDisplayValue(field, row.value, "");
      if (!isMeaningfulResponseValue(value)) return;
      const key = `${normalizeCompactText(row.respondent_id)}__${normalizeCompactText(row.month)}`;
      if (!valueMap.has(key)) valueMap.set(key, new Set());
      valueMap.get(key).add(value);
      if (!seenValues.has(value)) {
        seenValues.add(value);
        valueOrder.push(value);
      }
    });

    if (compactLabel(field).toLowerCase() === "week") {
      sortWeekDisplayValues(valueOrder);
    }

    if (compactLabel(field).toLowerCase() === "region") {
      const regionOrder = new Map(CUSTOM_TABLE_REGION_ORDER.map((value, index) => [value, index]));
      valueOrder.sort((left, right) => {
        const leftIndex = regionOrder.has(left) ? regionOrder.get(left) : Number.MAX_SAFE_INTEGER;
        const rightIndex = regionOrder.has(right) ? regionOrder.get(right) : Number.MAX_SAFE_INTEGER;
        if (leftIndex !== rightIndex) return leftIndex - rightIndex;
        return String(left).localeCompare(String(right));
      });
    }

    return {
      valueMap,
      valueOrder,
      isMultiValued: false,
    };
  }

  questionCodes = await expandQuestionCodesForCustomTableData(category, questionCodes, months, requestCache);

  if (!questionCodes.length) {
    return {
      valueMap: new Map(),
      valueOrder: [],
      isMultiValued: false,
    };
  }

  const monthFilterSql =
    Array.isArray(months) && months.length > 0
      ? ` AND CAST(r.month AS VARCHAR) IN (${months.map((month) => quote(month)).join(", ")})`
      : "";
  const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");
  const questionInListSql = questionCodes.map((value) => `lower(${quote(value)})`).join(", ");
  const factQuestionExpr = await getResponsesFactQuestionSqlExpr("r");

  const rawRows = await all(`
    SELECT
      CAST(r.respondent_id AS VARCHAR) AS respondent_id,
      CAST(r.month AS VARCHAR) AS month,
      ${factQuestionExpr} AS question,
      CAST(r.question_label AS VARCHAR) AS question_label,
      CAST(r.answer_label AS VARCHAR) AS answer_label,
      CAST(r.answer_value AS VARCHAR) AS answer_value,
      CAST(r.answer_value_num AS DOUBLE) AS answer_value_num
    FROM responses_fact r
    JOIN respondent_dims d
      ON CAST(d.category AS VARCHAR) = CAST(r.category AS VARCHAR)
     AND CAST(d.respondent_id AS VARCHAR) = CAST(r.respondent_id AS VARCHAR)
     AND CAST(d.month AS VARCHAR) = CAST(r.month AS VARCHAR)
    WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
      AND lower(${factQuestionExpr}) IN (${questionInListSql})
      ${monthFilterSql}
      ${filterSql}
  `);

  const parentChildKeySet = new Set();
  if (questionCodes.length > 1 && Array.isArray(rawRows) && rawRows.length > 0) {
    const questionSetByMonth = new Map();
    rawRows.forEach((row) => {
      const question = normalizeCompactText(row.question);
      const month = normalizeCompactText(row.month);
      if (!question || !month) return;
      if (!questionSetByMonth.has(month)) questionSetByMonth.set(month, new Set());
      questionSetByMonth.get(month).add(question.toLowerCase());
    });

    rawRows.forEach((row) => {
      const question = normalizeCompactText(row.question);
      const month = normalizeCompactText(row.month);
      const monthQuestions = questionSetByMonth.get(month);
      if (!question || !monthQuestions || !monthQuestions.size) return;
      const prefix = `${question.toLowerCase()}_`;
      for (const candidate of monthQuestions) {
        if (candidate.startsWith(prefix)) {
          parentChildKeySet.add(`${question}__${month}`);
          break;
        }
      }
    });
  }

  const rows = (Array.isArray(rawRows) ? rawRows : []).map((row) => ({
    ...row,
    has_child_rows_for_month: parentChildKeySet.has(
      `${normalizeCompactText(row.question)}__${normalizeCompactText(row.month)}`,
    ),
  }));

  if (normalizeCompactText(spec?.sectionId) === "brand-imagery") {
    const labelByQuestion = new Map();
    rows.forEach((row) => {
      const question = normalizeCompactText(row.question);
      const label = normalizeCustomTableValueLabel(row.question_label);
      if (question && label && !labelByQuestion.has(question.toLowerCase())) {
        labelByQuestion.set(question.toLowerCase(), label);
      }
    });

    const missingBrandChildCodes = new Set();
    rows.forEach((row) => {
      const question = normalizeCompactText(row.question);
      if (!/^.+_QBI\.\d+$/iu.test(question)) return;
      parseGroupedParentAnswerCodes(row.answer_label || row.answer_value).forEach((token) => {
        const childCode = `${question}_${token}`;
        if (!labelByQuestion.has(childCode.toLowerCase())) missingBrandChildCodes.add(childCode);
      });
    });

    if (missingBrandChildCodes.size > 0) {
      const brandImageryQuestionExpr = await getResponsesFactQuestionSqlExpr();
      const brandImageryMonthFilterSql =
        Array.isArray(months) && months.length > 0
          ? ` AND CAST(month AS VARCHAR) IN (${months.map((month) => quote(month)).join(", ")})`
          : "";
      const missingRows = await all(`
        SELECT
          ${brandImageryQuestionExpr} AS question,
          MIN(CAST(question_label AS VARCHAR)) AS question_label
        FROM responses_fact
        WHERE CAST(category AS VARCHAR) = ${quote(category)}
          ${brandImageryMonthFilterSql}
          AND lower(${brandImageryQuestionExpr}) IN (${Array.from(missingBrandChildCodes).map((value) => `lower(${quote(value)})`).join(", ")})
          AND question_label IS NOT NULL
          AND CAST(question_label AS VARCHAR) <> ''
        GROUP BY ${brandImageryQuestionExpr}
      `);
      missingRows.forEach((row) => {
        const question = normalizeCompactText(row.question);
        const label = normalizeCustomTableValueLabel(row.question_label);
        if (question && label) labelByQuestion.set(question.toLowerCase(), label);
      });
    }

    const valueMap = new Map();
    const valueOrder = [];
    const seenValues = new Set();
    let maxValuesPerRespondent = 0;

    const addValue = (row, value) => {
      const label = normalizeCustomTableValueLabel(value);
      if (!parseFormattedAttributeBrandLabel(label)) return;
      const key = `${normalizeCompactText(row.respondent_id)}__${normalizeCompactText(row.month)}`;
      if (!key || key === "__") return;
      if (!valueMap.has(key)) valueMap.set(key, new Set());
      valueMap.get(key).add(label);
      if (!seenValues.has(label)) {
        seenValues.add(label);
        valueOrder.push(label);
      }
      if (valueMap.get(key).size > maxValuesPerRespondent) maxValuesPerRespondent = valueMap.get(key).size;
    };

    rows.forEach((row) => {
      const question = normalizeCompactText(row.question);
      const parentMatch = question.match(/^(.+_QBI\.\d+)$/iu);
      const childMatch = question.match(/^(.+_QBI\.\d+)_(\d+)$/iu);

      if (parentMatch) {
        const attribute = normalizeCustomTableValueLabel(row.question_label);
        if (!isMeaningfulCustomTableValue(attribute)) return;
        parseGroupedParentAnswerCodes(row.answer_label || row.answer_value).forEach((token) => {
          const brand = labelByQuestion.get(`${question}_${token}`.toLowerCase());
          if (!brand || !isMeaningfulCustomTableValue(brand)) return;
          addValue(row, formatBrandImageryValueLabel(attribute, brand));
        });
        return;
      }

      if (childMatch && isPositiveAnswerSelection(row.answer_label, row.answer_value, row.answer_value_num)) {
        const attribute = labelByQuestion.get(childMatch[1].toLowerCase());
        const brand = normalizeCustomTableValueLabel(row.question_label);
        if (!attribute || !isMeaningfulCustomTableValue(brand)) return;
        addValue(row, formatBrandImageryValueLabel(attribute, brand));
      }
    });

    return {
      valueMap,
      valueOrder,
      isMultiValued: maxValuesPerRespondent > 1,
    };
  }

  const valueMap = new Map();
  const seededValueOrder = buildCustomTableGroupedOptionLabels(spec, questionCodes);
  const valueOrder = [...seededValueOrder];
  const seenValues = new Set(seededValueOrder);
  const valueSortOrder = new Map();
  let maxValuesPerRespondent = 0;
  const questionCodeOrder = new Map(questionCodes.map((value, index) => [normalizeCompactText(value), index]));
  const sourceLabelByQuestionCode = await buildCustomTableQuestionLabelMap(category, questionCodes, spec, rows);
  const shouldSortBrandImageryValues =
    normalizeCompactText(spec?.sectionId) === "brand-imagery" && brandImagerySelectionSpansMultipleAttributes(questionCodes);
  const groupedQuestionHasChildRows =
    questionCodes.length > 1
    && rows.some((row) => {
      const rowQuestion = normalizeCompactText(row.question);
      return rowQuestion && questionCodes.includes(rowQuestion) && !shouldSkipGroupedQuestionRow(rowQuestion, questionCodes);
    });

  rows.forEach((row) => {
    const sourceQuestionLabel = sourceLabelByQuestionCode.get(normalizeCompactText(row.question));
    const rowForDerivation = sourceQuestionLabel
      ? { ...row, question_label: sourceQuestionLabel }
      : row;
    const isGroupedParentRow = shouldSkipCustomTableGroupedRow(normalizeCompactText(row.question), questionCodes, rowForDerivation);
    if (groupedQuestionHasChildRows && isGroupedParentRow) return;
    const derivedValues = deriveGroupedParentMultiValues(spec, questionCodes, rowForDerivation, sourceLabelByQuestionCode);
    const isNumericGroupedParentAnswer =
      isGroupedParentQuestionCode(normalizeCompactText(row.question), questionCodes)
      && isGroupedParentNumericAnswerText(rowForDerivation.answer_label || rowForDerivation.answer_value);
    const fallbackValue = isGroupedParentRow || isNumericGroupedParentAnswer
      ? ""
      : deriveCustomTableValue(spec.sectionId, questionCodes, rowForDerivation);
    const values = derivedValues.length > 0 ? derivedValues : [fallbackValue];
    const meaningfulValues = values
      .map((value) => normalizeCustomTableValueLabel(value))
      .filter((value) => isMeaningfulCustomTableValue(value))
      .filter((value) => !isGroupedParentNumericAnswerText(value))
      .filter((value) => !shouldDropCustomTableRowLabel(value));
    if (!meaningfulValues.length) return;
    const key = `${normalizeCompactText(row.respondent_id)}__${normalizeCompactText(row.month)}`;
    if (!valueMap.has(key)) valueMap.set(key, new Set());
    const valueSet = valueMap.get(key);
    meaningfulValues.forEach((value) => {
      valueSet.add(value);
      const sortIndex = Number(questionCodeOrder.get(normalizeCompactText(row.question)));
      if (!valueSortOrder.has(value) || (Number.isFinite(sortIndex) && sortIndex < Number(valueSortOrder.get(value)))) {
        valueSortOrder.set(value, Number.isFinite(sortIndex) ? sortIndex : Number.MAX_SAFE_INTEGER);
      }
      if (!seenValues.has(value)) {
        seenValues.add(value);
        valueOrder.push(value);
      }
    });
    if (valueSet.size > maxValuesPerRespondent) maxValuesPerRespondent = valueSet.size;
  });

  if (shouldSortBrandImageryValues) {
    valueOrder.sort((left, right) => {
      const leftIndex = Number(valueSortOrder.get(left) ?? Number.MAX_SAFE_INTEGER);
      const rightIndex = Number(valueSortOrder.get(right) ?? Number.MAX_SAFE_INTEGER);
      if (leftIndex !== rightIndex) return leftIndex - rightIndex;
      return String(left).localeCompare(String(right), undefined, { sensitivity: "base" });
    });
  }

  if (normalizeCompactText(spec?.sectionId) === "awareness-usage" && specUsesOnlyBauTomCodes(questionCodes)) {
    valueOrder.sort((left, right) => {
      const leftIndex = Number(valueSortOrder.get(left) ?? Number.MAX_SAFE_INTEGER);
      const rightIndex = Number(valueSortOrder.get(right) ?? Number.MAX_SAFE_INTEGER);
      if (leftIndex !== rightIndex) return leftIndex - rightIndex;
      return String(left).localeCompare(String(right), undefined, { sensitivity: "base", numeric: true });
    });
  }

  return {
    valueMap,
    valueOrder,
    isMultiValued: maxValuesPerRespondent > 1,
  };
}

function computeChiSquareSummary(matrix) {
  if (!Array.isArray(matrix) || matrix.length < 2 || matrix[0]?.length < 2) return null;
  const rowSums = matrix.map((row) => row.reduce((sum, value) => sum + Number(value || 0), 0));
  const colCount = matrix[0].length;
  const colSums = Array.from({ length: colCount }, (_, index) =>
    matrix.reduce((sum, row) => sum + Number(row[index] || 0), 0),
  );
  const total = rowSums.reduce((sum, value) => sum + value, 0);
  if (total <= 0) return null;

  let statistic = 0;
  matrix.forEach((row, rowIndex) => {
    row.forEach((value, colIndex) => {
      const expected = (rowSums[rowIndex] * colSums[colIndex]) / total;
      if (expected > 0) {
        statistic += ((Number(value || 0) - expected) ** 2) / expected;
      }
    });
  });

  return {
    statistic: Number(statistic.toFixed(3)),
    degreesOfFreedom: Math.max(1, (matrix.length - 1) * (colCount - 1)),
  };
}

function buildValueRespondentMap(questionData) {
  const counts = new Map();
  (questionData?.valueMap || new Map()).forEach((values, respondentKey) => {
    values.forEach((value) => {
      if (!counts.has(value)) counts.set(value, new Set());
      counts.get(value).add(respondentKey);
    });
  });
  return counts;
}

function getCachedValueRespondentMap(questionData, cache) {
  if (!cache || !questionData) return buildValueRespondentMap(questionData);
  if (!cache.has(questionData)) cache.set(questionData, buildValueRespondentMap(questionData));
  return cache.get(questionData);
}

function countSetOverlap(leftSet, rightSet) {
  if (!leftSet || !rightSet || leftSet.size === 0 || rightSet.size === 0) return 0;

  const smaller = leftSet.size <= rightSet.size ? leftSet : rightSet;
  const larger = leftSet.size <= rightSet.size ? rightSet : leftSet;
  let count = 0;

  smaller.forEach((value) => {
    if (larger.has(value)) count += 1;
  });

  return count;
}

function orderedValueLabels(valueOrder, valueCountMap) {
  const ordered = Array.isArray(valueOrder) ? valueOrder.filter(Boolean) : [];
  valueCountMap.forEach((_respondents, value) => {
    if (!ordered.includes(value)) ordered.push(value);
  });
  return movePinnedDisplayLabelsToEnd(ordered);
}

function formatSignificanceColumnLetter(index) {
  let value = Number(index);
  if (!Number.isInteger(value) || value < 0) return "";
  let letter = "";

  while (value >= 0) {
    letter = String.fromCharCode(65 + (value % 26)) + letter;
    value = Math.floor(value / 26) - 1;
  }

  return letter;
}

function computeColumnSignificanceZScore(countA, baseA, countB, baseB) {
  const aCount = Number(countA || 0);
  const aBase = Number(baseA || 0);
  const bCount = Number(countB || 0);
  const bBase = Number(baseB || 0);
  if (aBase <= 0 || bBase <= 0) return null;

  const pooled = (aCount + bCount) / (aBase + bBase);
  const variance = pooled * (1 - pooled) * ((1 / aBase) + (1 / bBase));
  if (!(variance > 0)) return null;

  return ((aCount / aBase) - (bCount / bBase)) / Math.sqrt(variance);
}

function computeCustomTableSignificanceLetters(counts, columnBases) {
  const columnLetters = Array.isArray(columnBases)
    ? columnBases.map((_value, index) => formatSignificanceColumnLetter(index))
    : [];
  const significanceLetters = Array.isArray(counts)
    ? counts.map((row) => (Array.isArray(columnBases) ? columnBases.map(() => "") : []))
    : [];
  let comparablePairCount = 0;

  counts.forEach((row, rowIndex) => {
    if (!Array.isArray(row)) return;
    const rowLetters = Array.isArray(columnBases) ? columnBases.map(() => new Set()) : [];

    for (let leftIndex = 0; leftIndex < columnBases.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < columnBases.length; rightIndex += 1) {
        const zScore = computeColumnSignificanceZScore(
          Number(row[leftIndex] || 0),
          Number(columnBases[leftIndex] || 0),
          Number(row[rightIndex] || 0),
          Number(columnBases[rightIndex] || 0),
        );
        if (zScore === null) continue;
        comparablePairCount += 1;
        if (Math.abs(zScore) < 1.959963984540054) continue;

        const leftPct = Number(columnBases[leftIndex] || 0) > 0 ? Number(row[leftIndex] || 0) / Number(columnBases[leftIndex] || 1) : 0;
        const rightPct =
          Number(columnBases[rightIndex] || 0) > 0 ? Number(row[rightIndex] || 0) / Number(columnBases[rightIndex] || 1) : 0;
        if (leftPct === rightPct) continue;

        if (leftPct > rightPct) {
          rowLetters[leftIndex].add(columnLetters[rightIndex]);
        } else {
          rowLetters[rightIndex].add(columnLetters[leftIndex]);
        }
      }
    }

    significanceLetters[rowIndex] = rowLetters.map((letterSet) => Array.from(letterSet).sort().join(""));
  });

  return {
    columnLetters,
    significanceLetters,
    comparablePairCount,
  };
}

function buildCustomTableColumnBlock(
  topSpec,
  topData,
  sideData,
  rowLabels,
  analysisOptions,
  displayModeId = "column_pct",
  valueRespondentMapCache = null,
  periodData = null,
) {
  const columnCountByValue = getCachedValueRespondentMap(topData, valueRespondentMapCache);
  const rowCountByValue = getCachedValueRespondentMap(sideData, valueRespondentMapCache);
  const matrixMap = new Map();
  const pairRespondents = new Set();
  const periodLabels = periodData
    ? (Array.isArray(periodData.valueOrder) ? periodData.valueOrder : [])
        .map((value) => normalizeCompactText(value))
        .filter(Boolean)
    : [];
  const periodRespondentMap = periodData
    ? getCachedValueRespondentMap(periodData, valueRespondentMapCache)
    : new Map();
  const usePeriodColumns = periodLabels.length > 1;

  topData.valueMap.forEach((topValues, respondentKey) => {
    const sideValues = sideData.valueMap.get(respondentKey);
    if (!sideValues || sideValues.size === 0 || topValues.size === 0) return;
    const periodValues = usePeriodColumns ? periodData?.valueMap?.get(respondentKey) : null;
    if (usePeriodColumns && (!periodValues || periodValues.size === 0)) return;
    pairRespondents.add(respondentKey);

    sideValues.forEach((rowLabel) => {
      if (!matrixMap.has(rowLabel)) matrixMap.set(rowLabel, new Map());
      const rowMap = matrixMap.get(rowLabel);
      topValues.forEach((columnLabel) => {
        const scopedPeriodLabels = usePeriodColumns ? Array.from(periodValues) : [""];
        scopedPeriodLabels.forEach((periodLabel) => {
          const scopedColumnLabel = usePeriodColumns ? `${columnLabel}|||${periodLabel}` : columnLabel;
          if (!rowMap.has(scopedColumnLabel)) rowMap.set(scopedColumnLabel, new Set());
          rowMap.get(scopedColumnLabel).add(respondentKey);
        });
      });
    });
  });

  const topValueLabels = orderedValueLabels(topData.valueOrder, columnCountByValue);
  const columnLabels = usePeriodColumns
    ? topValueLabels.flatMap(() => periodLabels)
    : topValueLabels;
  const columnKeys = usePeriodColumns
    ? topValueLabels.flatMap((topValue) => periodLabels.map((periodLabel) => `${topValue}|||${periodLabel}`))
    : topValueLabels;
  const columnGroups = usePeriodColumns
    ? topValueLabels.map((topValue) => ({ label: topValue, colSpan: periodLabels.length }))
    : [];
  const counts = rowLabels.map((rowLabel) =>
    columnKeys.map((columnLabel) => Number(matrixMap.get(rowLabel)?.get(columnLabel)?.size || 0)),
  );
  const respondentColumnBases = usePeriodColumns
    ? topValueLabels.flatMap((topValue) =>
        periodLabels.map((periodLabel) => {
          const topRespondents = columnCountByValue.get(topValue) || new Set();
          const periodRespondents = periodRespondentMap.get(periodLabel) || new Set();
          return countSetOverlap(topRespondents, periodRespondents);
        }),
      )
    : columnLabels.map((columnLabel) => Number(columnCountByValue.get(columnLabel)?.size || 0));
  const rowColumnBases = usePeriodColumns
    ? rowLabels.map((rowLabel) => {
        const rowRespondents = rowCountByValue.get(rowLabel) || new Set();
        return topValueLabels.flatMap(() =>
          periodLabels.map((periodLabel) => {
            const periodRespondents = periodRespondentMap.get(periodLabel) || new Set();
            return countSetOverlap(rowRespondents, periodRespondents);
          }),
        );
      })
    : rowLabels.map((rowLabel) =>
        columnLabels.map(() => Number(rowCountByValue.get(rowLabel)?.size || 0)),
      );
  const totalColumnBases = usePeriodColumns
    ? topValueLabels.flatMap(() =>
        periodLabels.map((periodLabel) => Number(periodRespondentMap.get(periodLabel)?.size || 0)),
      )
    : [];
  const columnBases =
    displayModeId === "counts"
      ? columnLabels.map((_, columnIndex) =>
          counts.reduce((sum, row) => sum + Number(row[columnIndex] || 0), 0),
        )
      : respondentColumnBases;
  const notes = [];
  let columnLetterLabels = [];
  let significanceLetters = counts.map((row) => row.map(() => ""));

  let chiSquare = null;
  if (Array.isArray(analysisOptions) && analysisOptions.includes("chi_square")) {
    if (topData.isMultiValued || sideData.isMultiValued) {
      notes.push("Chi-square summary is unavailable for multi-response question combinations.");
    } else {
      chiSquare = computeChiSquareSummary(counts);
      if (!chiSquare) notes.push("Chi-square summary requires at least two populated row and column categories.");
    }
  }

  if (Array.isArray(analysisOptions) && analysisOptions.includes("significance")) {
    if (topData.isMultiValued) {
      notes.push("Significance letters are unavailable when Top Break columns come from a multi-response question.");
    } else if (columnLabels.length < 2) {
      notes.push("Significance letters require at least two populated Top Break columns.");
    } else {
      const significanceColumnBases = displayModeId === "counts" ? respondentColumnBases : columnBases;
      const significanceResult = computeCustomTableSignificanceLetters(counts, significanceColumnBases);
      columnLetterLabels = significanceResult.columnLetters;
      significanceLetters = significanceResult.significanceLetters;
      if (significanceResult.comparablePairCount === 0) {
        notes.push("Significance letters require non-zero bases in at least two Top Break columns.");
      }
    }
  }

  return {
    id: topSpec.id,
    topQuestion: {
      ...topSpec,
      label: cleanCustomTableSideQuestionDisplayLabel(topSpec.label, topSpec.questionCodes),
    },
    columnLabels,
    columnGroups,
    columnLetterLabels,
    columnBases,
    rowColumnBases,
    totalColumnBases,
    counts,
    significanceLetters,
    pairRespondents: pairRespondents.size,
    chiSquare,
    notes,
  };
}

function buildCustomTable(
  sideSpec,
  topQuestions,
  questionDataCache,
  totalRespondents,
  analysisOptions,
  displayModeId = "column_pct",
  valueRespondentMapCache = null,
  periodData = null,
) {
  const sideData = questionDataCache.get(buildQuestionSpecCacheKey(sideSpec));
  if (!sideData) {
    return {
      id: sideSpec.id,
      sideQuestion: sideSpec,
      rowLabels: [],
      rowBases: [],
      totalRespondents,
      topBlocks: [],
    };
  }

  const rowCountByValue = getCachedValueRespondentMap(sideData, valueRespondentMapCache);
  const rowLabels = orderedValueLabels(sideData.valueOrder, rowCountByValue);
  const respondentRowBases = rowLabels.map((rowLabel) => Number(rowCountByValue.get(rowLabel)?.size || 0));

  const topBlocks = topQuestions.map((topSpec) => {
    const topData = questionDataCache.get(buildQuestionSpecCacheKey(topSpec));
    if (!topData) {
      return {
        id: topSpec.id,
        topQuestion: {
          ...topSpec,
          label: cleanCustomTableSideQuestionDisplayLabel(topSpec.label, topSpec.questionCodes),
        },
        columnLabels: [],
        columnGroups: [],
        columnLetterLabels: [],
        columnBases: [],
        counts: rowLabels.map(() => []),
        significanceLetters: rowLabels.map(() => []),
        pairRespondents: 0,
        chiSquare: null,
        notes: ["No data found for this top break question."],
      };
    }
    return buildCustomTableColumnBlock(topSpec, topData, sideData, rowLabels, analysisOptions, displayModeId, valueRespondentMapCache, periodData);
  });

  let rowBases = respondentRowBases;
  if (displayModeId === "counts" && topBlocks.length > 0 && Array.isArray(topBlocks[0].counts) && topBlocks[0].counts.length === rowLabels.length) {
    const mtx = topBlocks[0].counts;
    rowBases = rowLabels.map((_, rowIndex) =>
      mtx[rowIndex].reduce((sum, value) => sum + Number(value || 0), 0),
    );
  }

  return {
    id: sideSpec.id,
    sideQuestion: sideSpec,
    rowLabels,
    rowBases,
    totalRespondents,
    topBlocks,
  };
}

function normalizeCustomTableQuestionSpec(rawSpec, fallbackPrefix, index) {
  const rawQuestionCodes = Array.isArray(rawSpec?.questionCodes) ? rawSpec.questionCodes : [];
  const rawSourceQuestionLabels = Array.isArray(rawSpec?.sourceQuestionLabels) ? rawSpec.sourceQuestionLabels : [];
  const questionPairs = rawQuestionCodes
    .map((value, pairIndex) => ({
      code: normalizeCompactText(value),
      label: normalizeCompactText(rawSourceQuestionLabels[pairIndex] || ""),
    }))
    .filter((item) => item.code && !isAudioAuditQuestionCode(item.code));
  const questionCodes = questionPairs.map((item) => item.code);
  const months = Array.isArray(rawSpec?.months)
    ? rawSpec.months.map((value) => normalizeCompactText(value)).filter(Boolean)
    : [];
  const id = normalizeCompactText(rawSpec?.id) || `${fallbackPrefix}_${index + 1}`;
  const sectionId = normalizeCompactText(rawSpec?.sectionId);
  const sectionTitle = normalizeCompactText(rawSpec?.sectionTitle) || sectionId || fallbackPrefix;
  const isOverallSpec =
    sectionId.toLowerCase() === CUSTOM_TABLE_OVERALL_SECTION_ID
    || questionCodes.map((value) => value.toLowerCase()).includes(CUSTOM_TABLE_OVERALL_QUESTION_CODE);
  const label = isOverallSpec
    ? CUSTOM_TABLE_OVERALL_LABEL
    : normalizeCompactText(rawSpec?.label) || questionCodes[0] || id;
  const sourceQuestionLabels = questionPairs.map((item) => item.label);
  const monthValues = Array.isArray(rawSpec?.monthValues)
    ? rawSpec.monthValues
        .map((item, itemIndex) => {
          const itemMonths = Array.isArray(item?.months)
            ? item.months.map((value) => normalizeCompactText(value)).filter(Boolean)
            : [];
          const itemLabel = normalizeCompactText(item?.label) || itemMonths.join(", ") || `Month ${itemIndex + 1}`;
          const itemId = normalizeCompactText(item?.id) || itemLabel;
          return itemMonths.length > 0 ? { id: itemId, label: itemLabel, months: itemMonths } : null;
        })
        .filter(Boolean)
    : [];

  if (!questionCodes.length && !months.length && !isOverallSpec) return null;

  return {
    id,
    label,
    sectionId,
    sectionTitle,
    questionCodes: isOverallSpec
      ? [CUSTOM_TABLE_OVERALL_QUESTION_CODE]
      : questionCodes.length > 0
        ? questionCodes
        : months,
    months,
    sourceQuestionLabels,
    monthValues,
  };
}

function normalizeCustomTableMonthGroups(rawGroups, monthList) {
  const groups = Array.isArray(rawGroups) ? rawGroups : [];
  const normalizedGroupsRaw = groups
    .map((group, index) => {
      const months = Array.isArray(group?.months)
        ? group.months.map((value) => normalizeCompactText(value)).filter(Boolean)
        : [];
      const label = normalizeCompactText(group?.label) || months.join(", ") || `Month ${index + 1}`;
      const id = normalizeCompactText(group?.id) || label;
      return months.length > 0 ? { id, label, months } : null;
    })
    .filter(Boolean);

  const normalizedGroups = [];
  const groupByLabel = new Map();
  normalizedGroupsRaw.forEach((group) => {
    if (!groupByLabel.has(group.label)) {
      groupByLabel.set(group.label, {
        id: group.id,
        label: group.label,
        months: [],
      });
      normalizedGroups.push(groupByLabel.get(group.label));
    }
    const merged = groupByLabel.get(group.label);
    group.months.forEach((month) => {
      if (!merged.months.includes(month)) merged.months.push(month);
    });
  });

  if (normalizedGroups.length > 0) return normalizedGroups;
  return (Array.isArray(monthList) ? monthList : [])
    .map((month) => normalizeCompactText(month))
    .filter(Boolean)
    .map((month) => ({ id: month, label: month, months: [month] }));
}

async function fetchCustomTablePeriodData(category, monthGroups, filters) {
  const groups = normalizeCustomTableMonthGroups(monthGroups, []);
  const allMonths = Array.from(new Set(groups.flatMap((group) => group.months))).filter(Boolean);
  if (!groups.length || !allMonths.length) {
    return {
      valueMap: new Map(),
      valueOrder: [],
      isMultiValued: false,
    };
  }

    const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");
  const rows = await all(`
    SELECT
      CAST(d.respondent_id AS VARCHAR) AS respondent_id,
      CAST(d.month AS VARCHAR) AS month
    FROM respondent_dims d
    WHERE CAST(d.category AS VARCHAR) = ${quote(category)}
      AND CAST(d.month AS VARCHAR) IN (${allMonths.map((month) => quote(month)).join(", ")})
    ${filterSql}
  `);

  const labelsByMonth = new Map();
  groups.forEach((group) => {
    group.months.forEach((month) => {
      if (!labelsByMonth.has(month)) labelsByMonth.set(month, []);
      labelsByMonth.get(month).push(group.label);
    });
  });

  const valueMap = new Map();
  rows.forEach((row) => {
    const key = `${normalizeCompactText(row.respondent_id)}__${normalizeCompactText(row.month)}`;
    if (!key || key === "__") return;
    const labels = labelsByMonth.get(normalizeCompactText(row.month)) || [];
    if (!labels.length) return;
    if (!valueMap.has(key)) valueMap.set(key, new Set());
    labels.forEach((label) => valueMap.get(key).add(label));
  });

  return {
    valueMap,
    valueOrder: groups
      .map((group) => normalizeCompactText(group.label))
      .filter(Boolean),
    isMultiValued: groups.some((group) => group.months.length > 1),
  };
}

async function readSchema() {
  const describeColumns = async (tableName) => {
    if (!(await tableExists(tableName))) return [];
    const rows = await all(`DESCRIBE SELECT * FROM ${quoteIdentifier(tableName)}`);
    return rows.map((row) => row.column_name);
  };

  const factColumns = await describeColumns("responses_fact");
  const dimColumns = await describeColumns("respondent_dims");
  const baseFlagColumns = await describeColumns("base_flags");
  const bauMetricColumns = await describeColumns("bau_metric_facts");

  const detected = {
    question: detectColumn(factColumns, ["question"]),
    answer: detectColumn(factColumns, ["answer_label"]),
    category: detectColumn(factColumns, ["category"]),
    month: detectColumn(factColumns, ["month", "file_month"]),
    factRespondentId: detectColumn(factColumns, ["respondent_id", "SbjNum", "sbjnum"]),
    dimCategory: detectColumn(dimColumns, ["category"]),
    dimMonth: detectColumn(dimColumns, ["month", "file_month"]),
    dimRespondentId: detectColumn(dimColumns, ["respondent_id", "SbjNum", "sbjnum"]),
    baseFlagsCategory: detectColumn(baseFlagColumns, ["category"]),
    baseFlagsMonth: detectColumn(baseFlagColumns, ["month", "file_month"]),
    baseFlagsRespondentId: detectColumn(baseFlagColumns, ["respondent_id", "SbjNum", "sbjnum"]),
    bauCategory: detectColumn(bauMetricColumns, ["category"]),
    bauMonth: detectColumn(bauMetricColumns, ["month", "file_month"]),
    bauRespondentId: detectColumn(bauMetricColumns, ["respondent_id", "SbjNum", "sbjnum"]),
  };

  return {
    detected,
    longColumns: factColumns,
    baseColumns: dimColumns,
    baseFlagColumns,
    bauMetricColumns,
  };
}

function questionMatchesPattern(questionSpec, patternRegex) {
  if (!patternRegex) return true;
  const text = [
    String(questionSpec?.question || ""),
    String(questionSpec?.questionLabel || ""),
    ...(Array.isArray(questionSpec?.questionCodes) ? questionSpec.questionCodes : []),
  ].join(" ");
  return patternRegex.test(text);
}

function buildSelectedQuestionCodeMap(selectedQuestions) {
  const codeMap = new Map();
  (Array.isArray(selectedQuestions) ? selectedQuestions : []).forEach((questionSpec, index) => {
    const questionCodes = Array.isArray(questionSpec?.questionCodes) && questionSpec.questionCodes.length > 0
      ? questionSpec.questionCodes
      : [questionSpec?.question];
    questionCodes
      .map((value) => normalizeCompactText(value))
      .filter(Boolean)
      .forEach((questionCode) => {
        if (!codeMap.has(questionCode)) {
          codeMap.set(questionCode, {
            ...questionSpec,
            order: Number.isFinite(Number(questionSpec?.order)) ? Number(questionSpec.order) : index,
            questionCodes,
          });
        }
      });
  });
  return codeMap;
}

function buildAggregatedPageQuestions(rows, selectedQuestions, limitAnswers, includeMonth = false) {
  const codeMap = buildSelectedQuestionCodeMap(selectedQuestions);
  const byQuestion = new Map();

  (Array.isArray(rows) ? rows : []).forEach((row) => {
    const questionCode = normalizeCompactText(row?.question);
    const spec = codeMap.get(questionCode);
    if (!spec) return;

    const monthKey = normalizeCompactText(row?.month);
    const aggregateKey = includeMonth ? `${monthKey}__${spec.id}` : String(spec.id || questionCode);
    if (!byQuestion.has(aggregateKey)) {
      byQuestion.set(aggregateKey, {
        order: Number.isFinite(Number(spec.order)) ? Number(spec.order) : 0,
        month: monthKey,
        question: normalizeCompactText(spec.question || questionCode),
        questionLabel: normalizeCompactText(spec.questionLabel || spec.question || questionCode),
        questionCodes: Array.isArray(spec.questionCodes) ? spec.questionCodes.map((value) => normalizeCompactText(value)).filter(Boolean) : [questionCode],
        respondents: new Set(),
        answerCounts: new Map(),
      });
    }

    const item = byQuestion.get(aggregateKey);
    const respondentKey = `${normalizeCompactText(row?.respondent_id)}__${monthKey}`;
    if (!respondentKey || respondentKey === "__") return;
    item.respondents.add(respondentKey);

    const sourceLabelByQuestionCode = buildSpecSourceQuestionLabelMap(spec, item.questionCodes);
    const sourceQuestionLabel = sourceLabelByQuestionCode.get(normalizeCompactText(row.question));
    const rowForDerivation = sourceQuestionLabel
      ? { ...row, question_label: sourceQuestionLabel }
      : row;
    const derivedValues = deriveGroupedParentMultiValues(spec, item.questionCodes, rowForDerivation, sourceLabelByQuestionCode);
    const isGroupedParentRow = shouldSkipCustomTableGroupedRow(normalizeCompactText(row.question), item.questionCodes, rowForDerivation);
    const isNumericGroupedParentAnswer =
      isGroupedParentQuestionCode(normalizeCompactText(row.question), item.questionCodes)
      && /^\d+(?:\s+\d+)*$/u.test(normalizeCompactText(rowForDerivation.answer_label || rowForDerivation.answer_value));
    const fallbackValue = isGroupedParentRow || isNumericGroupedParentAnswer
      ? ""
      : deriveCustomTableValue(spec.pageId, item.questionCodes, rowForDerivation);
    const values = (derivedValues.length > 0 ? derivedValues : [fallbackValue])
      .map((value) => normalizeCustomTableValueLabel(value))
      .filter((value) => isMeaningfulResponseValue(value));
    values.forEach((derivedValue) => {
      if (!item.answerCounts.has(derivedValue)) item.answerCounts.set(derivedValue, new Set());
      item.answerCounts.get(derivedValue).add(respondentKey);
    });
  });

  return Array.from(byQuestion.values())
    .sort((left, right) => {
      if (includeMonth && left.month !== right.month) return String(left.month) > String(right.month) ? -1 : 1;
      if (left.order !== right.order) return left.order - right.order;
      return String(left.questionLabel).localeCompare(String(right.questionLabel), undefined, {
        numeric: true,
        sensitivity: "base",
      });
    })
    .map((item) => {
      const total = item.respondents.size;
      const answers = takeAnswerRowsWithPinnedDisplayLabels(
        sortAnswerRowsWithPinnedDisplayLabels(
          Array.from(item.answerCounts.entries()).map(([answer, respondentSet]) => ({
            answer,
            count: respondentSet.size,
          })),
        ),
        Math.max(1, Number(limitAnswers) || 8),
      ).map((answerRow) => ({
        ...answerRow,
        pct: total > 0 ? Number(((Number(answerRow.count || 0) / total) * 100).toFixed(2)) : 0,
      }));

      return {
        ...(includeMonth ? { month: item.month } : {}),
        question: item.question,
        questionLabel: item.questionLabel,
        questionCodes: item.questionCodes,
        total,
        answers,
      };
    });
}

async function getQuestionsForCategory(category, options = {}) {
  const requestedSources = options?.headerAuditSource
    ? [options.headerAuditSource]
    : resolveHeaderAuditSourcesForMonths(options?.months || []);
  if (requestedSources.length > 1) {
    const key = `${String(category || "").toLowerCase()}::${requestedSources.join("+")}`;
    if (questionCacheByCategory.has(key)) return questionCacheByCategory.get(key);
    const byKey = new Map();
    requestedSources.forEach((source) => {
      buildHeaderAuditQuestions(category, source).forEach((question) => {
        const dedupeKey = `${question.pageId}::${(question.questionCodes || [question.question]).join("|")}`;
        if (!byKey.has(dedupeKey)) byKey.set(dedupeKey, question);
      });
    });
    const combined = Array.from(byKey.values()).sort((left, right) => {
      if (left.pageId !== right.pageId) return String(left.pageId).localeCompare(String(right.pageId));
      return Number(left.order || 0) - Number(right.order || 0);
    });
    if (combined.length > 0) {
      questionCacheByCategory.set(key, combined);
      return combined;
    }
  }

  const source = options?.headerAuditSource || await resolveHeaderAuditSourceForRequest(category, options?.months || []);
  const key = `${String(category || "").toLowerCase()}::${source || "default"}`;
  if (questionCacheByCategory.has(key)) return questionCacheByCategory.get(key);

  const headerAuditQuestions = buildHeaderAuditQuestions(category, source);
  if (headerAuditQuestions.length > 0) {
    questionCacheByCategory.set(key, headerAuditQuestions);
    return headerAuditQuestions;
  }

  const rows = await all(`
    SELECT DISTINCT
      CAST(question AS VARCHAR) AS question,
      CAST(question_label AS VARCHAR) AS question_label
    FROM responses_fact
    WHERE CAST(category AS VARCHAR) = ${quote(category)}
      AND question IS NOT NULL
    ORDER BY question
  `);

  const mapped = rows.map((row) => ({
    question: row.question,
    questionLabel: row.question_label || row.question,
    pageId: classifyQuestion(row.question, row.question_label),
  }));

  questionCacheByCategory.set(key, mapped);
  return mapped;
}

function buildDefaultBauMetricFactsSql(sourceTable = "responses_fact", sourceWhereSql = "") {
  return `
      CREATE OR REPLACE TABLE bau_metric_facts AS
      WITH src AS (
        SELECT
          CAST(category AS VARCHAR) AS category,
          CAST(respondent_id AS VARCHAR) AS respondent_id,
          CAST(month AS VARCHAR) AS month,
          CAST(question AS VARCHAR) AS question,
          CAST(question_label AS VARCHAR) AS question_label,
          CAST(answer_label AS VARCHAR) AS answer_label,
          CAST(answer_value AS VARCHAR) AS answer_value,
          CAST(answer_value_num AS DOUBLE) AS answer_value_num
        FROM ${sourceTable}
        ${sourceWhereSql || ""}
      )
      SELECT * FROM (
        SELECT category, respondent_id, month, 'brand_tom'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU1A$')

        UNION ALL
        SELECT category, respondent_id, month, 'brand_spont'::TEXT AS metric,
               ${sqlBauCheckboxBrand("question_label", "answer_label", "answer_value")} AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU1B_[0-9]+$')
          AND ${sqlPositiveAnswerSelectionPredicate("answer_label", "answer_value", "answer_value_num")}

        UNION ALL
        SELECT category, respondent_id, month, 'ad_tom'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU1C$')

        UNION ALL
        SELECT category, respondent_id, month, 'ad_spont'::TEXT AS metric,
               ${sqlBauCheckboxBrand("question_label", "answer_label", "answer_value")} AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU1D_[0-9]+$')
          AND ${sqlPositiveAnswerSelectionPredicate("answer_label", "answer_value", "answer_value_num")}

        UNION ALL
        SELECT category, respondent_id, month, 'aided'::TEXT AS metric,
               ${sqlBauCheckboxBrand("question_label", "answer_label", "answer_value")} AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU2_[0-9]+$')
          AND ${sqlPositiveAnswerSelectionPredicate("answer_label", "answer_value", "answer_value_num")}

        UNION ALL
        SELECT category, respondent_id, month, 'aided_ad'::TEXT AS metric,
               ${sqlBauCheckboxBrand("question_label", "answer_label", "answer_value")} AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU3_[0-9]+$')
          AND ${sqlPositiveAnswerSelectionPredicate("answer_label", "answer_value", "answer_value_num")}

        UNION ALL
        SELECT category, respondent_id, month, 'ever_consumed'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU5A_[0-9]+$')

        UNION ALL
        SELECT category, respondent_id, month, 'last_3_months'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU5C_[0-9]+$')

        UNION ALL
        SELECT category, respondent_id, month, 'last_1_month'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU6A_[0-9]+$')

        UNION ALL
        SELECT category, respondent_id, month, 'last_7_days'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU6B_[0-9]+$')

        UNION ALL
        SELECT category, respondent_id, month, 'most_often_used'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU6C$')

        UNION ALL
        SELECT category, respondent_id, month, 'prefrence'::TEXT AS metric,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS brand,
               CAST(NULL AS VARCHAR) AS option
        FROM src
        WHERE regexp_matches(question, '(?i)(^|_)BAU8$')

        UNION ALL
        SELECT category, respondent_id, month, 'media_source'::TEXT AS metric,
               NULLIF(TRIM(regexp_extract(question_label, '^\\(([^)]+)\\)', 1)), '') AS brand,
               NULLIF(TRIM(COALESCE(NULLIF(answer_label, ''), NULLIF(answer_value, ''))), '') AS option
        FROM src
        WHERE regexp_matches(question, '(?i)BAU4_[0-9]+$|BAU4_[0-9]+_[0-9]+$')
          AND (COALESCE(answer_value_num, 0) = 1 OR answer_value IN ('1', '1.0') OR lower(COALESCE(answer_label, '')) = 'yes' OR answer_label IS NOT NULL)
      )
      WHERE brand IS NOT NULL AND brand <> '' AND brand <> '{0}' AND lower(brand) NOT IN ('none', 'none of these')
    `;
}

function buildHeaderAuditMetricSelect(metricRow) {
  const metricKey = compactLabel(metricRow?.metricKey);
  const category = compactLabel(metricRow?.category);
  const questionCodes = Array.isArray(metricRow?.questionCodes)
    ? metricRow.questionCodes.map((value) => compactLabel(value)).filter(Boolean)
    : [];
  if (!metricKey || !category || !questionCodes.length) return null;

  const questionInList = questionCodes.map((value) => quote(value)).join(", ");
  const baseWhere = `category = ${quote(category)} AND question IN (${questionInList})`;
  const simpleBrand = sqlPreferredDisplayLabel("answer_label", "answer_value", "");
  const positivePredicate = sqlPositiveAnswerSelectionPredicate("answer_label", "answer_value", "answer_value_num");

  if (["brand_tom", "ad_tom", "most_often_used", "prefrence"].includes(metricKey)) {
    return `
      SELECT category, respondent_id, month, ${quote(metricKey)}::TEXT AS metric,
             ${simpleBrand} AS brand,
             CAST(NULL AS VARCHAR) AS option
      FROM src
      WHERE ${baseWhere}
    `;
  }

  if (["brand_spont", "ad_spont", "aided", "aided_ad", "ever_consumed", "last_3_months", "last_1_month", "last_7_days"].includes(metricKey)) {
    return `
      SELECT category, respondent_id, month, ${quote(metricKey)}::TEXT AS metric,
             ${sqlBauCheckboxBrand("question_label", "answer_label", "answer_value")} AS brand,
             CAST(NULL AS VARCHAR) AS option
      FROM src
      WHERE ${baseWhere}
        AND ${positivePredicate}
    `;
  }

  if (metricKey === "media_source") {
    return `
      SELECT category, respondent_id, month, 'media_source'::TEXT AS metric,
             ${sqlMediaSourceBrand("question_label")} AS brand,
             ${sqlMediaSourceOption("question_label", "answer_label", "answer_value")} AS option
      FROM src
      WHERE ${baseWhere}
        AND ${positivePredicate}
    `;
  }

  return null;
}

function deriveMediaSourceBrandFromLabel(value) {
  const text = compactLabel(value).replace(/<[^>]*>/g, " ");
  const aboutMatch = text.match(/\babout\s+(.+?)\s*[.:?]/i);
  if (aboutMatch?.[1]) return compactLabel(aboutMatch[1]);
  const parenMatch = text.match(/^\(([^)]+)\)/);
  if (parenMatch?.[1]) return compactLabel(parenMatch[1]);
  return "";
}

function extractQuestionLabelOption(value) {
  const trailing = extractTrailingOptionLabel(value);
  if (trailing) return trailing;
  const singleParen = compactLabel(value).match(/\(([^()]*)\)\s*$/u);
  if (singleParen?.[1] && !/^\{[01]\}$/i.test(singleParen[1]) && !/^none( of these)?$/i.test(singleParen[1])) {
    return compactLabel(singleParen[1]);
  }
  const match = compactLabel(value).match(/:\s*([^:?]+)\??\s*$/);
  return match?.[1] ? compactLabel(match[1]) : "";
}

function buildSurveyCtoMediaSourceCode(questionCode, questionLabel) {
  const code = compactLabel(questionCode);
  const match = code.match(/^([A-Za-z]+)_BAU4\.(\d+)(?:\.1)?_(\d+)$/i);
  if (!match) return null;
  const categoryPrefix = match[1];
  const brandIndex = match[2];
  const optionIndex = match[3];
  const brand = deriveMediaSourceBrandFromLabel(questionLabel);
  if (!brand) return null;
  return {
    question: `bau4_${categoryPrefix}_${brandIndex}_${optionIndex}`,
    brand,
    option: extractQuestionLabelOption(questionLabel),
  };
}

function buildMediaSourceCodeEntries(metricRow) {
  const sourceLabels = Array.isArray(metricRow?.row?.sourceQuestionLabels)
    ? metricRow.row.sourceQuestionLabels.map((value) => compactLabel(value))
    : [];
  const entries = [];
  const seen = new Set();
  (Array.isArray(metricRow?.questionCodes) ? metricRow.questionCodes : [])
    .map((value) => compactLabel(value))
    .filter(Boolean)
    .forEach((questionCode, index) => {
      const isOptionCode = /_\d+$/i.test(questionCode);
      const optionOverride = extractQuestionLabelOption(sourceLabels[index] || "");
      if (isOptionCode) {
        const originalKey = `${questionCode}::::${optionOverride}`;
        if (!seen.has(originalKey)) {
          entries.push({ question: questionCode, brand: "", option: optionOverride });
          seen.add(originalKey);
        }
      }
      const alternate = buildSurveyCtoMediaSourceCode(questionCode, sourceLabels[index] || "");
      if (!alternate) return;
      const alternateKey = `${alternate.question}::${alternate.brand}::${alternate.option || ""}`;
      if (seen.has(alternateKey)) return;
      entries.push(alternate);
      seen.add(alternateKey);
    });
  return entries;
}

function metricUsesCheckboxBrand(metricKey) {
  return [
    "brand_spont",
    "ad_spont",
    "aided",
    "aided_ad",
    "ever_consumed",
    "last_3_months",
    "last_1_month",
    "last_7_days",
  ].includes(compactLabel(metricKey));
}

function buildMetricMapEntries(metricRow, canonicalEntries = []) {
  const metricKey = compactLabel(metricRow?.metricKey);
  const sourceLabels = Array.isArray(metricRow?.row?.sourceQuestionLabels)
    ? metricRow.row.sourceQuestionLabels.map((value) => compactLabel(value))
    : [];
  const canonicalByCode = new Map(canonicalEntries.map((entry) => [String(entry.code), entry.brand]));
  return (Array.isArray(metricRow?.questionCodes) ? metricRow.questionCodes : [])
    .map((questionCode, index) => {
      const code = compactLabel(questionCode);
      if (!code) return null;
      const codeMatch = code.match(/[._](\d+)$/u);
      const sourceBrand = normalizeBrandCodeLabel(extractQuestionLabelOption(sourceLabels[index] || ""));
      const brandOverride = metricUsesCheckboxBrand(metricKey)
        ? (
            sourceBrand
            || (codeMatch ? canonicalByCode.get(codeMatch[1]) : "")
          )
        : "";
      if (metricUsesCheckboxBrand(metricKey) && !brandOverride && !/_\d+$/i.test(code)) return null;
      return {
        category: compactLabel(metricRow?.category),
        metric: metricKey,
        question: code,
        brandOverride,
      };
    })
    .filter(Boolean);
}

function normalizeBrandCodeLabel(value) {
  let label = compactLabel(value);
  const wrapped = label.match(/^\(([^()]+)\)$/u);
  if (wrapped?.[1]) label = compactLabel(wrapped[1]);
  if (!label) return "";
  if (/^none( of these)?$/i.test(label)) return "";
  if (/^others?$/i.test(label)) return "";
  return label;
}

function getCanonicalBrandCodeEntries(category) {
  const entries = new Map();
  const categoryRows = getHeaderAuditCategoryRows(category, "current");
  const usageBrandRows = categoryRows.filter((row) =>
    compactLabel(row?.headerType).toLowerCase() === "derived_metric"
    && compactLabel(row?.headerLabel).toLowerCase() === "ever consumed",
  );
  const sourceRows = usageBrandRows.length > 0 ? usageBrandRows : categoryRows;
  sourceRows.forEach((row) => {
    if (compactLabel(row?.headerType).toLowerCase() !== "derived_metric") return;
    const metricKey = getHeaderAuditMetricKey(row);
    if (!metricKey || metricKey === "media_source") return;
    const questionCodes = Array.isArray(row?.sourceVariableNames) ? row.sourceVariableNames : [];
    const sourceLabels = Array.isArray(row?.sourceQuestionLabels) ? row.sourceQuestionLabels : [];
    questionCodes.forEach((questionCode, index) => {
      const match = compactLabel(questionCode).match(/[._](\d+)$/u);
      if (!match || entries.has(match[1])) return;
      const brand = normalizeBrandCodeLabel(extractQuestionLabelOption(sourceLabels[index] || ""));
      if (!brand) return;
      entries.set(match[1], brand);
    });
  });
  return Array.from(entries.entries())
    .map(([code, brand]) => ({ code, brand, order: Number(code) }))
    .sort((left, right) => left.order - right.order);
}

function getCanonicalBrandOrder(category, brand) {
  const normalizedBrand = normalizeExportOptionKey(brand);
  const entry = getCanonicalBrandCodeEntries(category)
    .find((item) => normalizeExportOptionKey(item.brand) === normalizedBrand);
  return entry ? entry.order : Number.MAX_SAFE_INTEGER;
}

function buildBrandCodeEntries(metricRow, allMetricRows) {
  const entries = new Map();
  const addRows = (row) => {
    const questionCodes = Array.isArray(row?.questionCodes) ? row.questionCodes : [];
    const sourceLabels = Array.isArray(row?.row?.sourceQuestionLabels)
      ? row.row.sourceQuestionLabels.map((value) => compactLabel(value))
      : [];
    questionCodes.forEach((questionCode, index) => {
      const match = compactLabel(questionCode).match(/_(\d+)$/u);
      if (!match) return;
      const brand = normalizeBrandCodeLabel(extractQuestionLabelOption(sourceLabels[index] || ""));
      if (!brand || entries.has(match[1])) return;
      entries.set(match[1], brand);
    });
  };

  addRows(metricRow);
  (Array.isArray(allMetricRows) ? allMetricRows : [])
    .filter((row) => compactLabel(row?.category) === compactLabel(metricRow?.category))
    .forEach(addRows);

  return Array.from(entries.entries()).map(([code, brand]) => ({ code, brand }));
}

function findParentMultiCodeQuestion(metricRow, inferLegacyParent = false) {
  const questionCodes = Array.isArray(metricRow?.questionCodes)
    ? metricRow.questionCodes.map((value) => compactLabel(value)).filter(Boolean)
    : [];
  const explicitParent = questionCodes.find((questionCode) =>
    questionCodes.some((candidate) => candidate !== questionCode && candidate.startsWith(`${questionCode}_`)),
  );
  if (explicitParent) return explicitParent;
  if (!inferLegacyParent) return "";
  const legacyChild = questionCodes
    .map((questionCode) => questionCode.match(/^A_(.+)_\d+$/i))
    .find(Boolean);
  return legacyChild?.[1] || "";
}

function buildParentMultiCodeMapEntries(metricRow, allMetricRows, canonicalEntries = []) {
  const metricKey = compactLabel(metricRow?.metricKey);
  if (!metricUsesCheckboxBrand(metricKey)) return [];
  const parentQuestion = findParentMultiCodeQuestion(metricRow, canonicalEntries.length > 0);
  if (!parentQuestion) return [];
  const childEntries = buildMetricMapEntries(metricRow, canonicalEntries).filter((entry) =>
    compactLabel(entry?.question) !== parentQuestion && compactLabel(entry?.brandOverride),
  );
  const childBrandEntries = childEntries
    .map((entry) => {
      const code = compactLabel(entry.question).match(/[._](\d+)$/u)?.[1] || "";
      return code && entry.brandOverride ? { code, brand: entry.brandOverride } : null;
    })
    .filter(Boolean);
  const brandEntries = childBrandEntries.length > 0
    ? Array.from(new Map(childBrandEntries.map((entry) => [entry.code, entry])).values())
    : canonicalEntries.length > 0
      ? canonicalEntries
      : buildBrandCodeEntries(metricRow, allMetricRows);
  return brandEntries.map((entry) => ({
    category: compactLabel(metricRow?.category),
    metric: metricKey,
    question: parentQuestion,
    answerCode: entry.code,
    brand: entry.brand,
  }));
}

function buildHeaderAuditBauMetricFactsSql(dbPath = DB_PATH, sourceTable = "responses_fact", sourceWhereSql = "") {
  const metricRows = getHeaderAuditMetricRows(dbPath, "market");
  const mappedMetricRows = metricRows.filter((metricRow) => metricRow.metricKey !== "media_source");
  const mediaSourceSelects = metricRows
    .filter((metricRow) => metricRow.metricKey === "media_source" && metricRow.source !== "market")
    .map((metricRow) => {
      const entries = buildMediaSourceCodeEntries(metricRow);
      if (!entries.length) return null;
      const valueRows = entries.map((entry) => `(${quote(entry.question)}, ${quote(entry.brand)}, ${quote(entry.option || "")})`);
      return `
        SELECT s.category, s.respondent_id, s.month, 'media_source'::TEXT AS metric,
               COALESCE(NULLIF(mm.brand_override, ''), ${sqlMediaSourceBrand("s.question_label")}) AS brand,
               COALESCE(NULLIF(mm.option_override, ''), ${sqlMediaSourceOption("s.question_label", "s.answer_label", "s.answer_value")}) AS option
        FROM src s
        JOIN (
          SELECT *
          FROM (
            VALUES
              ${valueRows.join(",\n              ")}
          ) AS t(question, brand_override, option_override)
        ) mm ON mm.question = s.question
        WHERE s.category = ${quote(metricRow.category)}
          AND ${sqlPositiveAnswerSelectionPredicate("s.answer_label", "s.answer_value", "s.answer_value_num")}
      `;
    })
    .filter(Boolean);
  const valueRows = mappedMetricRows.flatMap((metricRow) =>
    buildMetricMapEntries(metricRow).map((entry) =>
      `(${quote(entry.category)}, ${quote(entry.metric)}, ${quote(entry.question)}, ${quote(entry.brandOverride)})`),
  );
  const parentCodeRows = mappedMetricRows.flatMap((metricRow) =>
    buildParentMultiCodeMapEntries(metricRow, metricRows).map((entry) =>
      `(${quote(entry.category)}, ${quote(entry.metric)}, ${quote(entry.question)}, ${quote(entry.answerCode)}, ${quote(entry.brand)})`),
  );
  if (!valueRows.length && !mediaSourceSelects.length && !parentCodeRows.length) return null;

  const unionParts = [];
  if (valueRows.length) {
    unionParts.push(`
        SELECT * FROM (
          SELECT
            category,
            respondent_id,
            month,
            metric,
            CASE
              WHEN metric IN ('brand_tom', 'ad_tom', 'most_often_used', 'prefrence')
                THEN ${sqlPreferredDisplayLabel("answer_label", "answer_value", "")}
              WHEN metric IN ('brand_spont', 'ad_spont', 'aided', 'aided_ad', 'ever_consumed', 'last_3_months', 'last_1_month', 'last_7_days')
                THEN COALESCE(${sqlBauCheckboxBrand("question_label", "answer_label", "answer_value")}, NULLIF(brand_override, ''))
              ELSE NULL
            END AS brand,
            CAST(NULL AS VARCHAR) AS option
          FROM joined
          WHERE (
            metric NOT IN ('brand_spont', 'ad_spont', 'aided', 'aided_ad', 'ever_consumed', 'last_3_months', 'last_1_month', 'last_7_days')
            OR ${sqlPositiveAnswerSelectionPredicate("answer_label", "answer_value", "answer_value_num")}
          )
        )
    `);
  }
  if (parentCodeRows.length) {
    unionParts.push(`
        SELECT
          s.category,
          s.respondent_id,
          s.month,
          parent_code_map.metric,
          parent_code_map.brand,
          CAST(NULL AS VARCHAR) AS option
        FROM src s
        JOIN parent_code_map
          ON parent_code_map.category = s.category
         AND parent_code_map.question = s.question
        CROSS JOIN UNNEST(
          string_split(
            regexp_replace(
              TRIM(COALESCE(NULLIF(CAST(s.answer_label AS VARCHAR), ''), NULLIF(CAST(s.answer_value AS VARCHAR), ''), '')),
              '\\s+',
              ' ',
              'g'
            ),
            ' '
          )
        ) AS token(answer_code)
        WHERE TRIM(CAST(token.answer_code AS VARCHAR)) = parent_code_map.answer_code
          ${valueRows.length ? `AND NOT EXISTS (
            SELECT 1
            FROM joined child
            WHERE child.category = s.category
              AND child.respondent_id = s.respondent_id
              AND child.month = s.month
              AND child.metric = parent_code_map.metric
              AND child.question <> s.question
              AND ${sqlPositiveAnswerSelectionPredicate("child.answer_label", "child.answer_value", "child.answer_value_num")}
              AND COALESCE(${sqlBauCheckboxBrand("child.question_label", "child.answer_label", "child.answer_value")}, NULLIF(child.brand_override, '')) = parent_code_map.brand
          )` : ""}
    `);
  }
  unionParts.push(`
        SELECT * FROM (
          SELECT
            s.category,
            s.respondent_id,
            s.month,
            'media_source'::TEXT AS metric,
            ${sqlMediaSourceBrand("s.question_label")} AS brand,
            ${sqlMediaSourceOption("s.question_label", "s.answer_label", "s.answer_value")} AS option
          FROM src s
          WHERE s.month < ${quote(CURRENT_HEADER_AUDIT_MONTH_CUTOFF)}
            AND regexp_matches(s.question, '(?i)^I_[0-9]+_A_[A-Z]+_BAU4_[0-9]+$')
            AND ${sqlPositiveAnswerSelectionPredicate("s.answer_label", "s.answer_value", "s.answer_value_num")}
        )
  `);
  unionParts.push(...mediaSourceSelects);

  return `
      CREATE OR REPLACE TABLE bau_metric_facts AS
      WITH src AS (
        SELECT
          CAST(category AS VARCHAR) AS category,
          CAST(respondent_id AS VARCHAR) AS respondent_id,
          CAST(month AS VARCHAR) AS month,
          CAST(question AS VARCHAR) AS question,
          CAST(question_label AS VARCHAR) AS question_label,
          CAST(answer_label AS VARCHAR) AS answer_label,
          CAST(answer_value AS VARCHAR) AS answer_value,
          CAST(answer_value_num AS DOUBLE) AS answer_value_num
        FROM ${sourceTable}
        ${sourceWhereSql || ""}
      )
      ${valueRows.length ? `,
      metric_map AS (
        SELECT *
        FROM (
          VALUES
            ${valueRows.join(",\n            ")}
        ) AS t(category, metric, question, brand_override)
      ),
      joined AS (
        SELECT
          src.category,
          src.respondent_id,
          src.month,
          src.question,
          src.question_label,
          src.answer_label,
          src.answer_value,
          src.answer_value_num,
          metric_map.metric,
          metric_map.brand_override
        FROM src
        JOIN metric_map
          ON metric_map.category = src.category
         AND metric_map.question = src.question
      )` : ""}
      ${parentCodeRows.length ? `,
      parent_code_map AS (
        SELECT *
        FROM (
          VALUES
            ${parentCodeRows.join(",\n            ")}
        ) AS t(category, metric, question, answer_code, brand)
      )` : ""}
      SELECT * FROM (
        ${unionParts.join("\nUNION ALL\n")}
      )
      WHERE ${sqlValidDashboardBrandPredicate("brand")}
    `;
}

function buildBauMetricFactsSql(dbPath = DB_PATH, sourceTable = "responses_fact", sourceWhereSql = "") {
  return buildHeaderAuditBauMetricFactsSql(dbPath, sourceTable, sourceWhereSql)
    || buildDefaultBauMetricFactsSql(sourceTable, sourceWhereSql);
}

function buildCanonicalTrendMetricFactsSql(category, mode, marketCutoff, months = []) {
  const metricKeys = mode === "usage"
    ? ["ever_consumed", "last_3_months", "last_1_month", "last_7_days", "most_often_used", "prefrence"]
    : ["brand_tom", "brand_spont", "ad_tom", "ad_spont", "aided", "aided_ad"];
  const metricKeySet = new Set(metricKeys);
  const canonicalEntries = getCanonicalBrandCodeEntries(category);
  if (!canonicalEntries.length) return "";

  const simpleRows = [];
  const childRows = [];
  const parentRows = [];
  ["market", "current"].forEach((source) => {
    const metadataSources = source === "current" ? ["current", "market"] : ["market"];
    const sourceMetricRowsByKey = new Map();
    metadataSources.forEach((metadataSource) => {
      getHeaderAuditMetricRows(DB_PATH, metadataSource).forEach((metricRow) => {
        const key = [
          compactLabel(metricRow.category),
          compactLabel(metricRow.metricKey),
          ...(Array.isArray(metricRow.questionCodes) ? metricRow.questionCodes.map(compactLabel) : []),
        ].join("::");
        if (!sourceMetricRowsByKey.has(key)) sourceMetricRowsByKey.set(key, metricRow);
      });
    });
    const sourceMetricRows = Array.from(sourceMetricRowsByKey.values())
      .filter((metricRow) =>
        compactLabel(metricRow.category) === compactLabel(category)
        && metricKeySet.has(compactLabel(metricRow.metricKey)),
      );
    sourceMetricRows.forEach((metricRow) => {
      const metricKey = compactLabel(metricRow.metricKey);
      if (metricUsesCheckboxBrand(metricKey)) {
        buildMetricMapEntries(metricRow, canonicalEntries).forEach((entry) => {
          if (!entry.brandOverride) return;
          const questionAliases = [entry.question];
          if (!/^A_/i.test(entry.question) && /_\d+$/i.test(entry.question)) {
            questionAliases.push(`A_${entry.question}`);
          }
          questionAliases.forEach((question) => {
            childRows.push({
              source,
              metric: metricKey,
              question,
              brand: entry.brandOverride,
            });
          });
        });
        buildParentMultiCodeMapEntries(metricRow, sourceMetricRows, canonicalEntries).forEach((entry) => {
          parentRows.push({
            source,
            metric: metricKey,
            question: entry.question,
            answerCode: entry.answerCode,
            brand: entry.brand,
          });
        });
        return;
      }
      (Array.isArray(metricRow.questionCodes) ? metricRow.questionCodes : []).forEach((question) => {
        simpleRows.push({ source, metric: metricKey, question: compactLabel(question) });
      });
    });
  });

  const sourcePredicate = (sourceExpr, monthExpr) => {
    if (!marketCutoff) return `${sourceExpr} = 'current'`;
    return `((${sourceExpr} = 'market' AND ${monthExpr} <= ${quote(marketCutoff)})
      OR (${sourceExpr} = 'current' AND ${monthExpr} > ${quote(marketCutoff)}))`;
  };
  const requestedMonthPredicate = Array.isArray(months) && months.length > 0
    ? `AND CAST(r.month AS VARCHAR) IN (${months.map((month) => quote(month)).join(", ")})`
    : "";
  const valuesSql = (rows, fields) => rows.map((row) =>
    `(${fields.map((field) => quote(row[field])).join(", ")})`,
  ).join(",\n          ");
  const codeMapRows = canonicalEntries.map((entry) =>
    `(${quote(String(entry.code))}, ${quote(entry.brand)})`,
  ).join(",\n          ");
  const unionParts = [];

  if (simpleRows.length) {
    unionParts.push(`
      SELECT
        r.category,
        r.respondent_id,
        r.month,
        sm.metric,
        CASE
          WHEN NULLIF(TRIM(CAST(r.answer_label AS VARCHAR)), '') IS NOT NULL
            AND lower(TRIM(CAST(r.answer_label AS VARCHAR))) NOT IN ('nan', 'none')
            AND NOT regexp_matches(TRIM(CAST(r.answer_label AS VARCHAR)), '^[0-9]+(?:[.]0+)?$')
            THEN ${sqlOptionalDisplayLabel("r.answer_label")}
          ELSE bcm.brand
        END AS brand,
        CAST(NULL AS VARCHAR) AS option
      FROM responses_fact r
      JOIN (
        SELECT * FROM (VALUES
          ${valuesSql(simpleRows, ["source", "metric", "question"])}
        ) AS t(source, metric, question)
      ) sm ON UPPER(sm.question) = UPPER(CAST(r.question AS VARCHAR))
      LEFT JOIN (
        SELECT * FROM (VALUES
          ${codeMapRows}
        ) AS t(answer_code, brand)
      ) bcm ON bcm.answer_code = CAST(
        TRY_CAST(COALESCE(r.answer_value_num, TRY_CAST(r.answer_value AS DOUBLE)) AS INTEGER)
        AS VARCHAR
      )
      WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
        ${requestedMonthPredicate}
        AND ${sourcePredicate("sm.source", "CAST(r.month AS VARCHAR)")}
    `);
  }
  if (childRows.length) {
    unionParts.push(`
      SELECT
        r.category,
        r.respondent_id,
        r.month,
        cm.metric,
        COALESCE(
          ${sqlBauCheckboxBrand("r.question_label", "r.answer_label", "r.answer_value")},
          cm.brand
        ) AS brand,
        CAST(NULL AS VARCHAR) AS option
      FROM responses_fact r
      JOIN (
        SELECT * FROM (VALUES
          ${valuesSql(childRows, ["source", "metric", "question", "brand"])}
        ) AS t(source, metric, question, brand)
      ) cm ON UPPER(cm.question) = UPPER(CAST(r.question AS VARCHAR))
      WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
        ${requestedMonthPredicate}
        AND ${sourcePredicate("cm.source", "CAST(r.month AS VARCHAR)")}
        AND ${sqlPositiveAnswerSelectionPredicate("r.answer_label", "r.answer_value", "r.answer_value_num")}
    `);
  }
  if (parentRows.length) {
    unionParts.push(`
      SELECT r.category, r.respondent_id, r.month, pm.metric, pm.brand, CAST(NULL AS VARCHAR) AS option
      FROM responses_fact r
      JOIN (
        SELECT * FROM (VALUES
          ${valuesSql(parentRows, ["source", "metric", "question", "answerCode", "brand"])}
        ) AS t(source, metric, question, answer_code, brand)
      ) pm ON UPPER(pm.question) = UPPER(CAST(r.question AS VARCHAR))
      CROSS JOIN UNNEST(
        string_split(
          regexp_replace(
            TRIM(COALESCE(NULLIF(CAST(r.answer_label AS VARCHAR), ''), NULLIF(CAST(r.answer_value AS VARCHAR), ''), '')),
            '\\s+',
            ' ',
            'g'
          ),
          ' '
        )
      ) AS token(answer_code)
      WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
        ${requestedMonthPredicate}
        AND ${sourcePredicate("pm.source", "CAST(r.month AS VARCHAR)")}
        AND TRIM(CAST(token.answer_code AS VARCHAR)) = pm.answer_code
    `);
  }
  if (mode === "usage") {
    unionParts.push(`
      SELECT
        r.category,
        r.respondent_id,
        r.month,
        CASE
          WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU5A_[0-9]+$') THEN 'ever_consumed'
          WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU5C_[0-9]+$') THEN 'last_3_months'
          WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU6A_[0-9]+$') THEN 'last_1_month'
          WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU6B_[0-9]+$') THEN 'last_7_days'
          ELSE NULL
        END AS metric,
        ${sqlBauCheckboxBrand("r.question_label", "r.answer_label", "r.answer_value")} AS brand,
        CAST(NULL AS VARCHAR) AS option
      FROM responses_fact r
      WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
        ${requestedMonthPredicate}
        AND regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)(BAU5A|BAU5C|BAU6A|BAU6B)_[0-9]+$')
        AND ${sqlPositiveAnswerSelectionPredicate("r.answer_label", "r.answer_value", "r.answer_value_num")}
    `);
  }
  if (!unionParts.length) return "";
  return `
    SELECT DISTINCT category, respondent_id, month, brand, option, metric
    FROM (
      ${unionParts.join("\nUNION ALL\n")}
    ) canonical_metric_facts
    WHERE ${sqlValidDashboardBrandPredicate("brand")}
  `;
}

function buildHeaderAuditMediaSourceSelects(dbPath = DB_PATH) {
  const metricRows = getHeaderAuditMetricRows(dbPath).filter((metricRow) => metricRow.metricKey === "media_source");
  const selects = metricRows
    .map((metricRow) => {
      const questionCodes = Array.isArray(metricRow.questionCodes)
        ? metricRow.questionCodes.map((value) => compactLabel(value)).filter(Boolean)
        : [];
      if (!questionCodes.length) return null;
      return `
        SELECT * FROM (
          SELECT
            CAST(category AS VARCHAR) AS category,
            CAST(respondent_id AS VARCHAR) AS respondent_id,
            CAST(month AS VARCHAR) AS month,
            'media_source'::TEXT AS metric,
            ${sqlMediaSourceBrand("question_label")} AS brand,
            ${sqlMediaSourceOption("question_label", "answer_label", "answer_value")} AS option
          FROM responses_fact
          WHERE CAST(category AS VARCHAR) = ${quote(metricRow.category)}
            AND CAST(question AS VARCHAR) IN (${questionCodes.map((value) => quote(value)).join(", ")})
            AND ${sqlPositiveAnswerSelectionPredicate("answer_label", "answer_value", "answer_value_num")}
        )
        WHERE brand IS NOT NULL AND brand <> '' AND brand <> '{0}' AND lower(brand) NOT IN ('none', 'none of these')
      `;
    })
    .filter(Boolean);
  return selects;
}

function buildHeaderAuditMediaSourceInsertSql(dbPath = DB_PATH) {
  const selects = buildHeaderAuditMediaSourceSelects(dbPath);
  if (!selects.length) return null;
  return selects.join("\nUNION ALL\n");
}

async function init() {
  if (initialized) return;

  await configureDuckDbRuntime();

  const nativeTablesReadyAtStart = await hasAllTables(REQUIRED_NATIVE_TABLES);

  if (!nativeTablesReadyAtStart) {
    let parquetBootstrapOk = false;
    try {
      await run(`
        CREATE OR REPLACE VIEW responses_long AS
        SELECT * FROM read_parquet(${quote(LONG_GLOB)}, hive_partitioning = true);
      `);
      await run(`
        CREATE OR REPLACE VIEW responses_base AS
        SELECT * FROM read_parquet(${quote(BASE_GLOB)}, hive_partitioning = true);
      `);
      const rawLongColumns = (await all("DESCRIBE SELECT * FROM responses_long")).map((row) => row.column_name);
      const dimSelectSql = RESPONDENT_DIM_FIELDS
        .map((field) => {
          const sourceColumn = resolveRespondentDimColumn(rawLongColumns, field);
          return sourceColumn
            ? `CAST(${ident(sourceColumn)} AS VARCHAR) AS ${ident(field)}`
            : `CAST(NULL AS VARCHAR) AS ${ident(field)}`;
        })
        .join(",\n      ");

      if (!(await tableExists("respondent_dims"))) {
        await run(`
          CREATE TABLE respondent_dims AS
          SELECT DISTINCT
            CAST(category AS VARCHAR) AS category,
            CAST(SbjNum AS VARCHAR) AS respondent_id,
            CAST(month AS VARCHAR) AS month,
            ${dimSelectSql}
          FROM responses_long
        `);
      }

      if (!(await tableExists("responses_fact"))) {
        await run(`
          CREATE TABLE responses_fact AS
          SELECT
            CAST(category AS VARCHAR) AS category,
            CAST(SbjNum AS VARCHAR) AS respondent_id,
            CAST(month AS VARCHAR) AS month,
            CAST(question AS VARCHAR) AS question,
            CAST(question_label AS VARCHAR) AS question_label,
            COALESCE(NULLIF(TRIM(CAST(answer_label AS VARCHAR)), ''), '(No response)') AS answer_label,
            CAST(answer_value AS VARCHAR) AS answer_value,
            CAST(answer_value_num AS DOUBLE) AS answer_value_num
          FROM responses_long
          WHERE category IS NOT NULL
            AND question IS NOT NULL
        `);
      }
      parquetBootstrapOk = true;
    } catch (_bootstrapErr) {
      console.log("[backend] parquet unavailable; creating empty tables — server will serve 0 counts until sync completes");
    }

    if (!parquetBootstrapOk) {
      if (!(await tableExists("respondent_dims"))) {
        await run(`
          CREATE TABLE respondent_dims (
            category VARCHAR, respondent_id VARCHAR, month VARCHAR,
            Region VARCHAR, D3 VARCHAR, Gender VARCHAR, Age VARCHAR, SEC VARCHAR, Week VARCHAR
          )
        `);
      }
      if (!(await tableExists("responses_fact"))) {
        await run(`
          CREATE TABLE responses_fact (
            category VARCHAR, respondent_id VARCHAR, month VARCHAR,
            question VARCHAR, question_label VARCHAR, answer_label VARCHAR,
            answer_value VARCHAR, answer_value_num DOUBLE
          )
        `);
      }
    }
  } else {
    console.log("[backend] native DuckDB tables found, skipping parquet source load");
  }

  await ensureNativeBackedResponseViews();

  // Merge market_insights.duckdb data into primary DB before rebuilding
  // derived tables (question_catalog, base_flags, bau_metric_facts).
  // This is idempotent — skips rows already present in the primary DB.
  await mergeMarketInsightsDb();

  if (await switchToMarketDbIfPrimaryEmpty()) {
    await configureDuckDbRuntime();
    await ensureNativeBackedResponseViews();
  }

  if (!combinedMarketViewsReady && activeDbIsReadOnlyMarketDb()) {
    console.warn("[backend] DataMap question map: skipped read-only single-DB overlay; set DUCKDB_PATH to current.duckdb and MARKET_DB_PATH to market_insights.duckdb");
  }

  if (combinedMarketViewsReady) {
    console.log("[backend] combined market/current views active; skipping physical merge and derived table rebuild");
  } else if (activeDbIsReadOnlyMarketDb()) {
    console.log("[backend] active market DB opened read-only; skipping derived table and index rebuild");
  } else {
    await applyDatamapToWritableResponsesFact();

    await run(`
      CREATE OR REPLACE TABLE question_catalog AS
      SELECT DISTINCT
        CAST(category AS VARCHAR) AS category,
        CAST(question AS VARCHAR) AS question,
        CAST(question_label AS VARCHAR) AS question_label
      FROM responses_fact
      WHERE question IS NOT NULL
    `);

    await run(`
      CREATE OR REPLACE TABLE base_flags AS
      SELECT
        CAST(category AS VARCHAR) AS category,
        CAST(respondent_id AS VARCHAR) AS respondent_id,
        CAST(month AS VARCHAR) AS month,
        MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1A$') THEN 1 ELSE 0 END) AS has_brand_base,
        MAX(CASE WHEN regexp_matches(CAST(question AS VARCHAR), '(?i)(^|_)BAU1C$') THEN 1 ELSE 0 END) AS has_ad_base
      FROM responses_fact
      GROUP BY 1, 2, 3
    `);

    await run(buildBauMetricFactsSql());

    try { await run("CREATE INDEX idx_dims_category_month ON respondent_dims(category, month)"); } catch (_e) {}
    try { await run("CREATE INDEX idx_dims_filters ON respondent_dims(category, month, Region, D3, Gender, Age, SEC, Week)"); } catch (_e) {}
    try { await run("CREATE INDEX idx_question_catalog_category ON question_catalog(category, question)"); } catch (_e) {}
    try { await run("CREATE INDEX idx_fact_cat_month_question ON responses_fact(category, month, question)"); } catch (_e) {}
    try { await run("CREATE INDEX idx_fact_resp ON responses_fact(category, respondent_id, month)"); } catch (_e) {}
    try { await run("CREATE INDEX idx_base_flags ON base_flags(category, month, respondent_id)"); } catch (_e) {}
    try { await run("CREATE INDEX idx_bau_facts ON bau_metric_facts(category, month, metric, brand)"); } catch (_e) {}
  }

  schemaCache = await readSchema();
  respondentDimColumns = schemaCache.baseColumns || ["category", "respondent_id", "month"];
  questionCacheByCategory = new Map();
  initialized = true;
}

function hasSurveyCtoConfig() {
  return Boolean(
    process.env.SURVEYCTO_SERVER
      && process.env.SURVEYCTO_FORM_ID
      && process.env.SURVEYCTO_USERNAME
      && process.env.SURVEYCTO_PASSWORD,
  );
}

function secureTokenMatches(actual, expected) {
  const actualBuffer = Buffer.from(String(actual || ""), "utf8");
  const expectedBuffer = Buffer.from(String(expected || ""), "utf8");
  if (!actualBuffer.length || actualBuffer.length !== expectedBuffer.length) return false;
  return crypto.timingSafeEqual(actualBuffer, expectedBuffer);
}

function isAuthorizedSyncTrigger(req) {
  const expectedToken = String(process.env.SYNC_TRIGGER_TOKEN || "").trim();
  if (!expectedToken) return false;
  const authorization = String(req.get("authorization") || "");
  const bearerMatch = authorization.match(/^Bearer\s+(.+)$/i);
  const suppliedToken = bearerMatch?.[1]?.trim() || String(req.get("x-sync-token") || "").trim();
  return secureTokenMatches(suppliedToken, expectedToken);
}

function shouldEnableSurveyCtoSync() {
  const configuredMode = String(process.env.SURVEYCTO_SCHEDULER_MODE || "").trim().toLowerCase();
  const schedulerMode = configuredMode || (process.env.SYNC_TRIGGER_TOKEN ? "external" : "internal");
  if (schedulerMode !== "internal") return false;
  if (/^(1|true|yes|on)$/i.test(String(process.env.DISABLE_SURVEYCTO_SYNC || "false"))) return false;
  if (/^(1|true|yes|on)$/i.test(String(process.env.ENABLE_SURVEYCTO_SYNC || "false"))) return true;
  return false;
}

function getSurveyCtoSchedulerMode() {
  const configuredMode = String(process.env.SURVEYCTO_SCHEDULER_MODE || "").trim().toLowerCase();
  return configuredMode || (process.env.SYNC_TRIGGER_TOKEN ? "external" : "internal");
}

function parseLastJsonLine(output) {
  const lines = String(output || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    const line = lines[index];
    if (!line.startsWith("{") || !line.endsWith("}")) continue;
    try {
      return JSON.parse(line);
    } catch (_err) {}
  }
  return null;
}

async function runMetadataDetectionAfterSync(syncResult) {
  if (!syncResult || syncResult.ok === false) return null;
  if (/^(0|false|no|off)$/i.test(String(process.env.ENABLE_METADATA_DETECTION || "true"))) return null;
  try {
    await ensureMetadataRegistryReady();
    const period = syncResult.latestMonth || (Array.isArray(syncResult.fetchedMonths) ? syncResult.fetchedMonths.slice(-1)[0] : "") || "";
    const detection = await metadataRegistry.detectMetadataChanges(metadataRegistryDbApi(), metadataRegistryPaths(), { period });
    const currentResult = readJsonFile(SYNC_RESULT_PATH, syncResult);
    writeJsonFile(SYNC_RESULT_PATH, {
      ...currentResult,
      metadataDetection: detection,
    });
    syncStatus.lastResult = {
      ...(syncStatus.lastResult || syncResult),
      metadataDetection: detection,
    };
    return detection;
  } catch (err) {
    const warning = err && err.message ? err.message : String(err);
    console.warn(`[metadata] detection after sync failed: ${warning}`);
    const currentResult = readJsonFile(SYNC_RESULT_PATH, syncResult);
    writeJsonFile(SYNC_RESULT_PATH, {
      ...currentResult,
      metadataDetection: { ok: false, error: warning },
    });
    return { ok: false, error: warning };
  }
}

function runSurveyCtoSync(reason = "scheduled", options = {}) {
  if (syncInFlight) return syncInFlight;
  if (!options.allowWhenSchedulerDisabled && !shouldEnableSurveyCtoSync()) {
    syncStatus = {
      ...syncStatus,
      enabled: false,
      running: false,
      lastError: hasSurveyCtoConfig() ? null : "SurveyCTO sync is disabled because credentials/form env vars are incomplete.",
    };
    return Promise.resolve({ skipped: true, reason: "disabled" });
  }

  syncStatus = {
    ...syncStatus,
    enabled: true,
    running: true,
    lastRunStartedAt: new Date().toISOString(),
    lastRunFinishedAt: null,
    lastError: null,
  };

  syncInFlight = (async () => {
    return new Promise((resolve, reject) => {
    const child = spawn(PYTHON_BIN, [SYNC_SCRIPT_PATH], {
      cwd: path.resolve(__dirname, ".."),
      env: {
        ...process.env,
        DATA_ROOT,
        DUCKDB_PATH: CONFIGURED_DB_PATH,
      },
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("error", reject);
    child.on("close", async (code) => {
      try {
        const resultFromOutput = parseLastJsonLine(stdout);
        const resultFromFile = readJsonFile(SYNC_RESULT_PATH, null);
        const result = resultFromOutput || resultFromFile || {};

        if (code !== 0) {
          const message = result.error || stderr.trim() || `SurveyCTO sync failed with exit code ${code}`;
          throw new Error(message);
        }

        let promotion = null;
        if (result.changed && result.promotion) {
          promotion = result.promotion;
        } else if (result.changed && result.tempDbPath) {
          promotion = await promoteDuckDbFile(result.tempDbPath);
          if (result.stateUpdate && typeof result.stateUpdate === "object") {
            const currentState = readJsonFile(SYNC_STATE_PATH, {});
            writeJsonFile(SYNC_STATE_PATH, {
              ...currentState,
              ...result.stateUpdate,
              last_success_at: new Date().toISOString(),
            });
          }
        }

        const finishedAt = new Date().toISOString();
        syncStatus = {
          ...syncStatus,
          enabled: true,
          running: false,
          lastRunFinishedAt: finishedAt,
          lastSuccessAt: finishedAt,
          lastError: null,
          lastResult: {
            ...result,
            reason,
            promotion,
          },
        };
        writeJsonFile(SYNC_RESULT_PATH, syncStatus.lastResult);
        resolve(syncStatus.lastResult);
      } catch (err) {
        const message = err && err.message ? err.message : String(err);
        syncStatus = {
          ...syncStatus,
          enabled: true,
          running: false,
          lastRunFinishedAt: new Date().toISOString(),
          lastError: message,
        };
        console.error("[sync] SurveyCTO sync failed:", message);
        reject(err);
      }
    });
    });
  })().finally(async () => {
    if (!initialized) {
      try {
        await init();
      } catch (err) {
        console.error("[sync] failed to reopen DuckDB after sync:", err && err.message ? err.message : String(err));
      }
    }
    if (syncStatus.lastResult && syncStatus.lastResult.ok !== false) {
      await runMetadataDetectionAfterSync(syncStatus.lastResult);
    }
    syncDatabaseMaintenance = false;
    syncInFlight = null;
  });

  return syncInFlight;
}

function startSurveyCtoScheduler() {
  if (syncTimer) return;
  syncStatus = {
    ...syncStatus,
    enabled: shouldEnableSurveyCtoSync(),
    running: false,
    lastResult: readJsonFile(SYNC_RESULT_PATH, syncStatus.lastResult),
  };

  if (!syncStatus.enabled) {
    console.warn("[sync] SurveyCTO sync disabled in the web process; set ENABLE_SURVEYCTO_SYNC=true to run it here.");
    return;
  }

  syncTimer = setInterval(() => {
    runSurveyCtoSync("scheduled").catch(() => {});
  }, SYNC_INTERVAL_MS);
  if (typeof syncTimer.unref === "function") syncTimer.unref();
  console.log(`[sync] SurveyCTO sync scheduled every ${SYNC_INTERVAL_MS}ms`);
}

async function getCurrentDuckDbSummary() {
  try {
    await ensureInitialized();
    const rows = await all(`
      SELECT
        (SELECT COUNT(*) FROM respondent_dims) AS respondent_dims,
        (SELECT COUNT(*) FROM responses_fact) AS responses_fact,
        (SELECT MAX(CAST(month AS VARCHAR)) FROM respondent_dims) AS latest_month
    `);
    const categories = await all(`
      SELECT
        CAST(category AS VARCHAR) AS category,
        COUNT(DISTINCT CAST(respondent_id AS VARCHAR)) AS respondents,
        MIN(CAST(month AS VARCHAR)) AS first_month,
        MAX(CAST(month AS VARCHAR)) AS latest_month
      FROM dashboard_respondent_dims
      GROUP BY 1
      ORDER BY respondents DESC
      LIMIT 20
    `);
    const row = rows[0] || {};
    return {
      respondentDims: Number(row.respondent_dims || 0),
      responsesFact: Number(row.responses_fact || 0),
      latestMonth: row.latest_month || null,
      categories: categories.map((categoryRow) => ({
        category: categoryRow.category || null,
        respondents: Number(categoryRow.respondents || 0),
        firstMonth: categoryRow.first_month || null,
        latestMonth: categoryRow.latest_month || null,
      })),
      tables: await getDuckDbTableDiagnostics(),
    };
  } catch (err) {
    return {
      error: err && err.message ? err.message : String(err),
    };
  }
}

async function getDuckDbTableDiagnostics() {
  const tableRows = await all(`
    SELECT table_name, table_type
    FROM information_schema.tables
    WHERE table_schema = 'main'
    ORDER BY table_name
    LIMIT 80
  `);
  const columnRows = await all(`
    SELECT table_name, column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = 'main'
    ORDER BY table_name, ordinal_position
  `);
  const columnsByTable = new Map();
  columnRows.forEach((row) => {
    const tableName = String(row.table_name || "");
    if (!columnsByTable.has(tableName)) columnsByTable.set(tableName, []);
    if (columnsByTable.get(tableName).length < 30) {
      columnsByTable.get(tableName).push({
        name: row.column_name,
        type: row.data_type,
      });
    }
  });

  const diagnostics = [];
  for (const row of tableRows) {
    const tableName = String(row.table_name || "");
    let rowCount = null;
    let countError = null;
    try {
      // eslint-disable-next-line no-await-in-loop
      const countRows = await all(`SELECT COUNT(*) AS total FROM ${quoteIdentifier(tableName)}`);
      rowCount = Number(countRows[0]?.total || 0);
    } catch (err) {
      countError = err && err.message ? err.message : String(err);
    }
    diagnostics.push({
      name: tableName,
      type: row.table_type || null,
      rowCount,
      countError,
      columns: columnsByTable.get(tableName) || [],
    });
  }
  return diagnostics;
}

app.use(cors());
app.use(express.json({ limit: "3mb" }));
app.use("/api", (req, res, next) => {
  if (!syncDatabaseMaintenance) return next();
  if (req.path === "/health" || req.path === "/sync/status" || req.path === "/sync/trigger") return next();
  return res.status(503).json({
    ok: false,
    error: "Database refresh in progress. Retry shortly.",
  });
});

app.get("/api", (_req, res) => {
  res.json({
    app: "market-insights-backend",
    status: "ok",
    endpoints: [
      "/api/health",
      "/api/schema",
      "/api/pages/:slug",
      "/api/filters/:slug",
      "/api/overview-demographics",
      "/api/page-data",
      "/api/options/:field",
      "/api/distribution",
      "/api/table",
    ],
  });
});

app.get("/api/health", (_req, res) => {
  if (syncDatabaseMaintenance) {
    return res.json({ ok: true, databaseReady: false, refreshing: true });
  }
  return res.json({ ok: true, databaseReady: true, refreshing: false });
});

app.get("/api/sync/status", async (_req, res) => {
  const state = readJsonFile(SYNC_STATE_PATH, {});
  const lastResult = readJsonFile(SYNC_RESULT_PATH, syncStatus.lastResult);
  const includeDiagnostics = /^(1|true|yes|on)$/i.test(String(_req.query?.diagnostics || "false"));
  const payload = {
    ok: !syncStatus.lastError,
    enabled: shouldEnableSurveyCtoSync() || Boolean(process.env.SYNC_TRIGGER_TOKEN),
    schedulerMode: getSurveyCtoSchedulerMode(),
    schedulerEnabled: shouldEnableSurveyCtoSync(),
    externalTriggerConfigured: Boolean(process.env.SYNC_TRIGGER_TOKEN),
    running: Boolean(syncStatus.running),
    intervalMs: SYNC_INTERVAL_MS,
    runOnStart: RUN_SYNC_ON_START,
    activeDbPath: DB_PATH,
    configuredDbPath: CONFIGURED_DB_PATH,
    marketDbPath: MARKET_DB_PATH,
    bundledDbPath: BUNDLED_DB_PATH,
    marketDbIsActiveDb: path.resolve(MARKET_DB_PATH) === path.resolve(DB_PATH),
    dataRoot: DATA_ROOT,
    lastRunStartedAt: syncStatus.lastRunStartedAt,
    lastRunFinishedAt: syncStatus.lastRunFinishedAt,
    lastSuccessAt:
      syncStatus.lastSuccessAt
      || state.last_success_at
      || (lastResult?.ok ? lastResult.completedAt : null)
      || null,
    lastError: syncStatus.lastError || lastResult?.error || null,
    latestMonth: state.latest_month || lastResult?.latestMonth || null,
    rowCounts: {
      respondentDims: null,
      responsesFact: null,
      rawRows: state.raw_rows ?? lastResult?.rawRows ?? null,
    },
    lastResult: lastResult
      ? {
          ok: Boolean(lastResult.ok),
          changed: Boolean(lastResult.changed),
          completedAt: lastResult.completedAt || null,
          reason: lastResult.reason || null,
          fetchedRows: lastResult.fetchedRows ?? null,
          rawRows: lastResult.rawRows ?? null,
          latestMonth: lastResult.latestMonth || null,
          fetchedMonths: Array.isArray(lastResult.fetchedMonths) ? lastResult.fetchedMonths : [],
          surveyctoFetch: lastResult.surveyctoFetch || null,
          reconciliation: lastResult.reconciliation || null,
          error: lastResult.error || null,
        }
      : null,
  };

  if (includeDiagnostics) {
    payload.lastResult = lastResult;
    const dbSummary = await getCurrentDuckDbSummary();
    payload.latestMonth = dbSummary.latestMonth || payload.latestMonth;
    payload.rowCounts.respondentDims = dbSummary.respondentDims ?? null;
    payload.rowCounts.responsesFact = dbSummary.responsesFact ?? null;
    payload.categories = dbSummary.categories || [];
    payload.tables = dbSummary.tables || [];
    if (dbSummary.error) payload.diagnosticsError = dbSummary.error;
  }

  res.json(payload);
});

app.post("/api/sync/trigger", (req, res) => {
  if (!isAuthorizedSyncTrigger(req)) {
    return res.status(401).json({ ok: false, error: "Unauthorized sync trigger." });
  }
  if (!hasSurveyCtoConfig()) {
    return res.status(503).json({ ok: false, error: "SurveyCTO configuration is incomplete." });
  }
  if (syncInFlight) {
    return res.status(409).json({
      ok: true,
      accepted: false,
      running: true,
      message: "A SurveyCTO sync is already running.",
      startedAt: syncStatus.lastRunStartedAt,
    });
  }

  runSurveyCtoSync("render-cron", { allowWhenSchedulerDisabled: true }).catch(() => {});
  return res.status(202).json({
    ok: true,
    accepted: true,
    running: true,
    message: "SurveyCTO sync accepted.",
  });
});

app.get("/api/exports/status", async (_req, res) => {
  try {
    const [interim, full, rolling, quarter] = await Promise.all([
      resolveExportPeriod("interim"),
      resolveExportPeriod("full"),
      resolveExportPeriod("rolling"),
      resolveExportPeriod("quarter"),
    ]);
    const manifest = readExportManifest();
    res.json({
      ok: true,
      exportRoot: EXPORTS_ROOT,
      types: {
        interim,
        full,
        rolling,
        quarter,
      },
      recent: manifest.records.slice(0, 25).map((record) => ({
        id: record.id,
        type: record.type,
        scope: record.scope,
        slug: record.slug,
        months: record.months,
        period: record.period,
        filename: record.filename,
        size: record.size,
        generatedAt: record.generatedAt,
      })),
    });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.post("/api/exports/generate", async (req, res) => {
  try {
    const type = String(req.body?.type || "").trim().toLowerCase();
    const scope = String(req.body?.scope || "current").trim().toLowerCase();
    const slug = String(req.body?.slug || "").trim().toLowerCase();
    const requestedBy = String(req.body?.requestedBy || "manual").trim() || "manual";
    const record = await generateDataTableExport({ type, scope, slug, requestedBy });
    res.json({
      ok: true,
      exportId: record.id,
      filename: record.filename,
      size: record.size,
      generatedAt: record.generatedAt,
      downloadUrl: `/api/exports/download/${record.id}`,
    });
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.get("/api/exports/download/:exportId", (req, res) => {
  const manifest = readExportManifest();
  const record = manifest.records.find((item) => item.id === req.params.exportId);
  if (!record || !record.path || !fs.existsSync(record.path)) {
    return res.status(404).json({ ok: false, error: "Export file not found." });
  }
  return res.download(record.path, record.filename);
});

app.get("/api/admin/metadata/diagnostics", async (_req, res) => {
  try {
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.metadataDiagnostics(metadataRegistryDbApi()));
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.get("/api/admin/metadata/registry/brands", async (_req, res) => {
  try {
    await ensureMetadataRegistryReady();
    res.json({ ok: true, brands: await metadataRegistry.registryBrands(metadataRegistryDbApi()) });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.get("/api/admin/metadata/registry/questions", async (_req, res) => {
  try {
    await ensureMetadataRegistryReady();
    res.json({ ok: true, questions: await metadataRegistry.registryQuestions(metadataRegistryDbApi()) });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.get("/api/admin/metadata/autorecode", async (_req, res) => {
  try {
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.autorecodeDiagnostics(metadataRegistryDbApi()));
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/autorecode/snapshot", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureSpssExportTables();
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.snapshotAutorecodeMappings(metadataRegistryDbApi(), {
      period: String(req.body?.period || "").trim() || "1900-01",
      createdBy: String(req.body?.createdBy || req.body?.approvedBy || "admin").trim() || "admin",
    }));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.get("/api/admin/metadata/export-spec", async (req, res) => {
  try {
    await ensureMetadataRegistryReady();
    const months = String(req.query?.months || "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    res.json(await metadataRegistry.generatedExportSpec(metadataRegistryDbApi(), {
      category: String(req.query?.category || "").trim(),
      type: String(req.query?.type || "").trim(),
      months,
    }));
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.get("/api/admin/metadata/review", async (req, res) => {
  try {
    await ensureMetadataRegistryReady();
    const status = String(req.query?.status || "pending_review");
    res.json({ ok: true, items: await metadataRegistry.listReviewQueue(metadataRegistryDbApi(), status) });
  } catch (err) {
    res.status(500).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/detect", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    const result = await metadataRegistry.detectMetadataChanges(metadataRegistryDbApi(), metadataRegistryPaths(), {
      period: String(req.body?.period || "").trim(),
    });
    res.json(result);
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/migrate/dry-run", async (_req, res) => {
  try {
    await ensureMetadataRegistryReady();
    res.json({ ok: true, report: await metadataRegistry.seedInitialRegistry(metadataRegistryDbApi(), metadataRegistryPaths(), { dryRun: true }) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/migrate/apply", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json({ ok: true, report: await metadataRegistry.seedInitialRegistry(metadataRegistryDbApi(), metadataRegistryPaths(), { dryRun: false }) });
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/review/:id/approve", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.approveReviewChange(metadataRegistryDbApi(), req.params.id, req.body || {}));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/review/:id/reject", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.rejectReviewChange(metadataRegistryDbApi(), req.params.id, req.body || {}));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/review/:id/merge-brand", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.approveReviewChange(metadataRegistryDbApi(), req.params.id, {
      ...(req.body || {}),
      note: req.body?.note || "Merged through metadata review.",
      replaces_brand_id: req.body?.targetBrandId,
    }));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/review/:id/add-alias", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.addAliasFromReview(metadataRegistryDbApi(), req.params.id, req.body || {}));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/review/:id/link-question", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.linkQuestionFromReview(metadataRegistryDbApi(), req.params.id, req.body || {}));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/review/:id/non-reportable", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.markQuestionNonReportableFromReview(metadataRegistryDbApi(), req.params.id, req.body || {}));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.post("/api/admin/metadata/review/:id/replace-question", async (req, res) => {
  try {
    if (!metadataRegistry.requireAdminToken(req)) {
      return res.status(401).json({ ok: false, error: "Unauthorized metadata review request." });
    }
    await ensureMetadataRegistryReady();
    res.json(await metadataRegistry.approveReviewChange(metadataRegistryDbApi(), req.params.id, {
      ...(req.body || {}),
      replaces_question_id: req.body?.targetQuestionId,
      note: req.body?.note || "Approved as replacement question through metadata review.",
    }));
  } catch (err) {
    res.status(400).json({ ok: false, error: err.message });
  }
});

app.get("/api/schema", async (_req, res) => {
  try {
    await ensureInitialized();
    await ensureSchemaLoaded();
    res.json(schemaCache);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

async function handleOverviewDemographics(req, res, source) {
  try {
    await ensureRespondentDims();
    if (!schemaCache || !schemaCache.baseColumns?.length || !schemaCache.longColumns?.length) {
      schemaCache = await readSchema();
    }

    const payload = source === "query" ? req.query || {} : req.body || {};
    const slug = payload.slug;
    const monthsRaw = payload.months;
    const months = Array.isArray(monthsRaw)
      ? monthsRaw
      : typeof monthsRaw === "string" && monthsRaw.length
        ? monthsRaw.split(",").map((x) => x.trim()).filter(Boolean)
        : [];
    if (!slug) {
      return res.status(400).json({
        error: "slug is required",
        exampleGet: "/api/overview-demographics?slug=noodles&months=2025-10,2025-11",
        examplePost: {
          slug: "noodles",
          months: ["2025-10", "2025-11"],
        },
      });
    }

    const category = normalizeCategory(slug);
    const dimColumns = schemaCache.baseColumns || [];
    const requestedFields = ["Region", "D3", "Gender", "Age", "SEC", "Week"];
    const availableFields = requestedFields.filter((field) => Boolean(resolveRespondentDimColumn(dimColumns, field)));
    const monthField = detectColumn(dimColumns, ["month", "file_month"]);
    const respondentField = detectColumn(dimColumns, ["respondent_id", "SbjNum", "sbjnum"]);
    if (!respondentField) {
      return res.status(500).json({ error: "Respondent ID column not found in respondent_dims" });
    }
    const cacheKey = JSON.stringify({
      category,
      months: [...months].sort(),
      fields: availableFields,
    });

    const payloadOut = await getCachedOrInFlight(overviewQueryCache, overviewQueryInFlight, cacheKey, OVERVIEW_QUERY_CACHE_TTL_MS, async () => {
    const monthFilter =
      monthField && Array.isArray(months) && months.length
        ? ` AND CAST(${ident(monthField)} AS VARCHAR) IN (${months.map((m) => quote(m)).join(", ")})`
        : "";

    const selectedColumns = Array.from(new Set([
      respondentField,
      ...(monthField ? [monthField] : []),
      ...availableFields
        .map((field) => resolveRespondentDimColumn(dimColumns, field))
        .filter(Boolean),
    ]));
    const respondentRows = await all(`
      SELECT ${selectedColumns.map((column) => `CAST(${ident(column)} AS VARCHAR) AS ${ident(column)}`).join(", ")}
      FROM dashboard_respondent_dims
      WHERE CAST(category AS VARCHAR) = ${quote(category)}
      ${monthFilter}
    `);
    const respondentIds = new Set();
    const respondentSetsByField = new Map(
      availableFields.map((field) => [field, new Map()]),
    );
    respondentRows.forEach((row) => {
      const respondentId = String(row[respondentField] || "").trim();
      if (!respondentId) return;
      respondentIds.add(respondentId);
      availableFields.forEach((field) => {
        const sourceField = resolveRespondentDimColumn(dimColumns, field);
        if (!sourceField) return;
        const value = normalizeRespondentDimDisplayValue(field, row[sourceField]);
        const valueMap = respondentSetsByField.get(field);
        if (!valueMap.has(value)) valueMap.set(value, new Set());
        valueMap.get(value).add(respondentId);
      });
    });
    const totalRespondents = respondentIds.size;
    const distributions = Object.fromEntries(
      availableFields.map((field) => {
        const valueMap = respondentSetsByField.get(field) || new Map();
        const mapped = sortAnswerRowsWithPinnedDisplayLabels(
          Array.from(valueMap.entries()).map(([value, ids]) => ({ value, count: ids.size })),
          "value",
          "count",
        ).map((row) => ({
          value: row.value,
          count: Number(row.count || 0),
          pct: totalRespondents > 0 ? Number(((Number(row.count || 0) / totalRespondents) * 100).toFixed(2)) : 0,
        }));
        return [field, mapped];
      }),
    );

    const monthRows = monthField
      ? await all(`
          SELECT DISTINCT CAST(${ident(monthField)} AS VARCHAR) AS value
          FROM dashboard_respondent_dims
          WHERE CAST(category AS VARCHAR) = ${quote(category)}
          ORDER BY value
        `)
      : [];

    return {
      category,
      monthsAvailable: monthRows.map((r) => r.value).filter(Boolean),
      monthsSelected: Array.isArray(months) ? months : [],
      totalRespondents,
      distributions,
    };
    });
    res.json(payloadOut);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
}

app.get("/api/overview-demographics", (req, res) => handleOverviewDemographics(req, res, "query"));
app.post("/api/overview-demographics", (req, res) => handleOverviewDemographics(req, res, "body"));

app.get("/api/pages/:slug", async (req, res) => {
  try {
    await ensureInitialized();
    const category = normalizeCategory(req.params.slug);
    const questions = await getQuestionsForCategory(category);
    const counts = new Map();
    for (const q of questions) {
      counts.set(q.pageId, (counts.get(q.pageId) || 0) + 1);
    }

    const pages = PAGE_DEFINITIONS.map((page) => ({
      id: page.id,
      title: page.title,
      description: page.description,
      questionCount: counts.get(page.id) || 0,
    })).filter((page) => page.questionCount > 0);

    res.json({ category, pages });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get("/api/filters/:slug", async (req, res) => {
  try {
    await ensureRespondentDims();
    await ensureSchemaLoaded();
    const category = normalizeCategory(req.params.slug);
    const monthField = detectColumn(respondentDimColumns, ["month", "file_month"]);
    const filterDefs = [
      { key: "month", column: monthField },
      ...RESPONDENT_DIM_FIELDS.map((field) => ({
        key: field,
        column: resolveRespondentDimColumn(respondentDimColumns, field),
      })),
    ].filter((entry) => Boolean(entry.column));

    const cacheKey = JSON.stringify({ category, filterDefs });
    const now = Date.now();
    const cached = filtersQueryCache.get(cacheKey);
    if (cached && now - cached.ts <= FILTERS_QUERY_CACHE_TTL_MS) {
      return res.json(cached.payload);
    }

    const optionRows = await all(`
      SELECT filter_key, value
      FROM dashboard_filter_options
      WHERE category = ${quote(category)}
      ORDER BY filter_key, value
    `);
    const valuesByKey = new Map(filterDefs.map((def) => [def.key, []]));
    optionRows.forEach((row) => {
      if (valuesByKey.has(row.filter_key)) valuesByKey.get(row.filter_key).push(row.value);
    });
    const filters = Object.fromEntries(
      filterDefs.map((def) => {
        const rawValues = valuesByKey.get(def.key) || [];
        const values = Array.from(
          new Set(
            rawValues
              .map((value) => normalizeRespondentDimDisplayValue(def.key, value, ""))
              .filter(Boolean),
          ),
        )
          .sort((left, right) =>
            String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: "base" }),
          )
          .slice(0, 200);
        return [def.key, values];
      }),
    );

    const payloadOut = { category, filters };
    filtersQueryCache.set(cacheKey, { ts: now, payload: payloadOut });
    res.json(payloadOut);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/api/section-questions", async (req, res) => {
  try {
    await init();
    const { slug, pageId, months = [] } = req.body || {};

    if (!slug || !pageId) {
      return res.status(400).json({ error: "slug and pageId are required" });
    }

    const category = normalizeCategory(slug);
    const monthList = Array.isArray(months)
      ? months.map((m) => String(m).trim()).filter(Boolean)
      : typeof months === "string" && months.length
        ? months.split(",").map((m) => m.trim()).filter(Boolean)
        : [];
    const questions = await getQuestionsForCategory(category);
    const sectionQuestions = buildSectionQuestionList(questions, pageId);

    res.json({
      category,
      pageId,
      questions: sectionQuestions,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/api/page-data", async (req, res) => {
  try {
    await ensureInitialized();
    await ensureTrendTables();
    await ensureSchemaLoaded();

    const {
      slug,
      pageId,
      filters = {},
      limitQuestions = 12,
      limitAnswers = 8,
    } = req.body || {};

    if (!slug || !pageId) {
      return res.status(400).json({ error: "slug and pageId are required" });
    }

    const category = normalizeCategory(slug);
    const cacheKey = JSON.stringify({
      category,
      pageId,
      filters,
      limitQuestions: Math.max(1, Number(limitQuestions) || 12),
      limitAnswers: Math.max(1, Number(limitAnswers) || 8),
    });

    const payloadOut = await getCachedOrInFlight(pageDataQueryCache, pageDataQueryInFlight, cacheKey, PAGE_DATA_QUERY_CACHE_TTL_MS, async () => {
    const questions = await getQuestionsForCategory(category);
    const selectedQuestions = questions
      .filter((q) => q.pageId === pageId)
      .slice(0, Math.max(1, Number(limitQuestions) || 12));

    if (!selectedQuestions.length) {
      return { category, pageId, questions: [] };
    }

    const factMonthCol =
      schemaCache.detected?.month || detectColumn(schemaCache.longColumns || [], ["month", "file_month"]);
    const dimMonthCol =
      schemaCache.detected?.dimMonth || detectColumn(schemaCache.baseColumns || [], ["month", "file_month"]);
    const factRespCol =
      schemaCache.detected?.factRespondentId || detectColumn(schemaCache.longColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    const dimRespCol =
      schemaCache.detected?.dimRespondentId || detectColumn(schemaCache.baseColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    if (!factMonthCol || !dimMonthCol || !factRespCol || !dimRespCol) {
      return res.status(500).json({ error: "Required month/respondent columns not found in fact/dimension tables." });
    }

    const filterSql = buildAliasedFilterSql(filters, respondentDimColumns, "rd", resolveRespondentDimColumn);
    const selectedQuestionCodes = Array.from(
      new Set(
        selectedQuestions.flatMap((q) =>
          (Array.isArray(q.questionCodes) && q.questionCodes.length > 0 ? q.questionCodes : [q.question])
            .map((value) => normalizeCompactText(value))
            .filter(Boolean),
        ),
      ),
    );
    const questionFilterSql = await buildResponsesFactQuestionFilterSql("rf", selectedQuestionCodes);

    const rows = await all(`
      WITH filtered_resp AS MATERIALIZED (
        SELECT DISTINCT
          CAST(rd.category AS VARCHAR) AS category,
          CAST(rd.${ident(dimRespCol)} AS VARCHAR) AS respondent_id,
          CAST(rd.${ident(dimMonthCol)} AS VARCHAR) AS month
        FROM dashboard_respondent_dims rd
        WHERE CAST(rd.category AS VARCHAR) = ${quote(category)}
          ${filterSql}
      )
      SELECT
        CAST(rf.${ident(factRespCol)} AS VARCHAR) AS respondent_id,
        CAST(rf.${ident(factMonthCol)} AS VARCHAR) AS month,
        CAST(rf.question AS VARCHAR) AS question,
        CAST(rf.question_label AS VARCHAR) AS question_label,
        CAST(rf.answer_label AS VARCHAR) AS answer_label,
        CAST(rf.answer_value AS VARCHAR) AS answer_value,
        CAST(rf.answer_value_num AS DOUBLE) AS answer_value_num
      FROM responses_fact rf
      JOIN filtered_resp rd
        ON rd.category = CAST(rf.category AS VARCHAR)
       AND rd.respondent_id = CAST(rf.${ident(factRespCol)} AS VARCHAR)
       AND rd.month = CAST(rf.${ident(factMonthCol)} AS VARCHAR)
      WHERE CAST(rf.category AS VARCHAR) = ${quote(category)}
        AND ${questionFilterSql}
    `);

    const out = buildAggregatedPageQuestions(rows, selectedQuestions, limitAnswers, false);

    return { category, pageId, questions: out };
    });
    res.json(payloadOut);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/api/page-data-monthly", async (req, res) => {
  try {
    await ensureInitialized();
    await ensureTrendTables();
    await ensureSchemaLoaded();

    const {
      slug,
      pageId,
      filters = {},
      months = [],
      questionPattern = "",
      limitQuestions = 2000,
      limitAnswers = 200,
      includeRespondentBase = false,
    } = req.body || {};

    if (!slug || !pageId) {
      return res.status(400).json({ error: "slug and pageId are required" });
    }

    const category = normalizeCategory(slug);
    const monthList = Array.isArray(months)
      ? months.map((m) => String(m).trim()).filter(Boolean)
      : typeof months === "string" && months.length
        ? months.split(",").map((m) => m.trim()).filter(Boolean)
        : [];

    const cacheKey = JSON.stringify({
      category,
      pageId,
      filters,
      months: [...monthList].sort(),
      questionPattern: String(questionPattern || ""),
      limitQuestions: Math.max(1, Number(limitQuestions) || 2000),
      limitAnswers: Math.max(1, Number(limitAnswers) || 200),
      includeRespondentBase: Boolean(includeRespondentBase),
    });

    const payloadOut = await getCachedOrInFlight(
      pageDataMonthlyQueryCache,
      pageDataMonthlyQueryInFlight,
      cacheKey,
      PAGE_DATA_MONTHLY_QUERY_CACHE_TTL_MS,
      async () => {
    const questions = await getQuestionsForCategory(category, { months: monthList });
    const normalizedPattern = String(questionPattern || "").trim();
    const patternRegex = normalizedPattern
      ? (() => {
          try {
            return new RegExp(normalizedPattern, "i");
          } catch (_err) {
            return null;
          }
        })()
      : null;
    const selectedQuestions = questions
      .filter((q) => q.pageId === pageId)
      .filter((q) => questionMatchesPattern(q, patternRegex))
      .slice(0, Math.max(1, Number(limitQuestions) || 2000));

    const factMonthCol =
      schemaCache.detected?.month || detectColumn(schemaCache.longColumns || [], ["month", "file_month"]);
    const dimCategoryCol =
      schemaCache.detected?.dimCategory || detectColumn(schemaCache.baseColumns || [], ["category"]);
    const dimMonthCol =
      schemaCache.detected?.dimMonth || detectColumn(schemaCache.baseColumns || [], ["month", "file_month"]);
    const factRespCol =
      schemaCache.detected?.factRespondentId || detectColumn(schemaCache.longColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    const dimRespCol =
      schemaCache.detected?.dimRespondentId || detectColumn(schemaCache.baseColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    if (!factMonthCol || !dimCategoryCol || !dimMonthCol || !factRespCol || !dimRespCol) {
      return res.status(500).json({ error: "Required month/respondent columns not found in fact/dimension tables." });
    }

    const filterSql = buildAliasedFilterSql(filters, respondentDimColumns, "rd", resolveRespondentDimColumn);
    const monthSql =
      monthList.length > 0
        ? ` AND CAST(rf.${ident(factMonthCol)} AS VARCHAR) IN (${monthList.map((m) => quote(m)).join(", ")})`
        : "";
    const respondentBaseByMonth =
      includeRespondentBase
        ? Object.fromEntries(
            (
              await all(`
                SELECT
                  CAST(rd.${ident(dimMonthCol)} AS VARCHAR) AS month,
                  COUNT(DISTINCT CAST(rd.${ident(dimRespCol)} AS VARCHAR))::INT AS total
                FROM dashboard_respondent_dims rd
                WHERE CAST(rd.${ident(dimCategoryCol)} AS VARCHAR) = ${quote(category)}
                  ${monthList.length > 0 ? `AND CAST(rd.${ident(dimMonthCol)} AS VARCHAR) IN (${monthList.map((m) => quote(m)).join(", ")})` : ""}
                  ${filterSql}
                GROUP BY 1
                ORDER BY 1 DESC
              `)
            ).map((row) => [String(row.month || ""), Number(row.total || 0)]),
          )
        : undefined;

    if (!selectedQuestions.length) {
      return { category, pageId, questions: [] };
    }

    const selectedQuestionCodes = Array.from(
      new Set(
        selectedQuestions.flatMap((q) =>
          (Array.isArray(q.questionCodes) && q.questionCodes.length > 0 ? q.questionCodes : [q.question])
            .map((value) => normalizeCompactText(value))
            .filter(Boolean),
        ),
      ),
    );
    const questionFilterSql = await buildResponsesFactQuestionFilterSql("rf", selectedQuestionCodes);

    const rows = await all(`
      WITH filtered_resp AS MATERIALIZED (
        SELECT DISTINCT
          CAST(rd.${ident(dimCategoryCol)} AS VARCHAR) AS category,
          CAST(rd.${ident(dimRespCol)} AS VARCHAR) AS respondent_id,
          CAST(rd.${ident(dimMonthCol)} AS VARCHAR) AS month
        FROM dashboard_respondent_dims rd
        WHERE CAST(rd.${ident(dimCategoryCol)} AS VARCHAR) = ${quote(category)}
          ${monthList.length > 0 ? `AND CAST(rd.${ident(dimMonthCol)} AS VARCHAR) IN (${monthList.map((m) => quote(m)).join(", ")})` : ""}
          ${filterSql}
      )
      SELECT
        CAST(rf.${ident(factRespCol)} AS VARCHAR) AS respondent_id,
        CAST(rf.${ident(factMonthCol)} AS VARCHAR) AS month,
        CAST(rf.question AS VARCHAR) AS question,
        CAST(rf.question_label AS VARCHAR) AS question_label,
        CAST(rf.answer_label AS VARCHAR) AS answer_label,
        CAST(rf.answer_value AS VARCHAR) AS answer_value,
        CAST(rf.answer_value_num AS DOUBLE) AS answer_value_num
      FROM responses_fact rf
      JOIN filtered_resp rd
        ON rd.category = CAST(rf.category AS VARCHAR)
       AND rd.respondent_id = CAST(rf.${ident(factRespCol)} AS VARCHAR)
       AND rd.month = CAST(rf.${ident(factMonthCol)} AS VARCHAR)
      WHERE CAST(rf.category AS VARCHAR) = ${quote(category)}
        AND ${questionFilterSql}
        ${monthSql}
    `);

    const out = buildAggregatedPageQuestions(rows, selectedQuestions, limitAnswers, true);

    return includeRespondentBase
      ? { category, pageId, questions: out, respondentBaseByMonth }
      : { category, pageId, questions: out };
      },
    );
    res.json(payloadOut);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/api/question-data-monthly", async (req, res) => {
  try {
    await init();
    if (!schemaCache) schemaCache = await readSchema();

    const {
      slug,
      filters = {},
      months = [],
      questionPattern = "",
      limitQuestions = 2000,
      limitAnswers = 200,
      includeRespondentBase = false,
    } = req.body || {};

    if (!slug || !String(questionPattern || "").trim()) {
      return res.status(400).json({ error: "slug and questionPattern are required" });
    }

    const category = normalizeCategory(slug);
    const monthList = Array.isArray(months)
      ? months.map((m) => String(m).trim()).filter(Boolean)
      : typeof months === "string" && months.length
        ? months.split(",").map((m) => m.trim()).filter(Boolean)
        : [];
    const cacheKey = JSON.stringify({
      route: "question-data-monthly",
      category,
      filters,
      months: [...monthList].sort(),
      questionPattern: String(questionPattern || ""),
      limitQuestions: Math.max(1, Number(limitQuestions) || 2000),
      limitAnswers: Math.max(1, Number(limitAnswers) || 200),
      includeRespondentBase: Boolean(includeRespondentBase),
    });

    const payloadOut = await getCachedOrInFlight(
      pageDataMonthlyQueryCache,
      pageDataMonthlyQueryInFlight,
      cacheKey,
      PAGE_DATA_MONTHLY_QUERY_CACHE_TTL_MS,
      async () => {
    const normalizedPattern = String(questionPattern || "").trim();
    let patternRegex = null;
    try {
      patternRegex = new RegExp(normalizedPattern, "i");
    } catch (_err) {
      return res.status(400).json({ error: "questionPattern must be a valid regular expression" });
    }

    const questions = await getQuestionsForCategory(category, { months: monthList });
    const selectedQuestions = questions
      .filter((q) => {
        const text = `${String(q.question || "")} ${String(q.questionLabel || "")}`;
        return patternRegex.test(text);
      })
      .slice(0, Math.max(1, Number(limitQuestions) || 2000));

    const factMonthCol =
      schemaCache.detected?.month || detectColumn(schemaCache.longColumns || [], ["month", "file_month"]);
    const dimCategoryCol =
      schemaCache.detected?.dimCategory || detectColumn(schemaCache.baseColumns || [], ["category"]);
    const dimMonthCol =
      schemaCache.detected?.dimMonth || detectColumn(schemaCache.baseColumns || [], ["month", "file_month"]);
    const factRespCol =
      schemaCache.detected?.factRespondentId || detectColumn(schemaCache.longColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    const dimRespCol =
      schemaCache.detected?.dimRespondentId || detectColumn(schemaCache.baseColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    if (!factMonthCol || !dimCategoryCol || !dimMonthCol || !factRespCol || !dimRespCol) {
      return res.status(500).json({ error: "Required month/respondent columns not found in fact/dimension tables." });
    }

    const filterSql = buildAliasedFilterSql(filters, respondentDimColumns, "rd", resolveRespondentDimColumn);
    const monthSql =
      monthList.length > 0
        ? ` AND CAST(rf.${ident(factMonthCol)} AS VARCHAR) IN (${monthList.map((m) => quote(m)).join(", ")})`
        : "";
    const respondentBaseByMonth =
      includeRespondentBase
        ? Object.fromEntries(
            (
              await all(`
                SELECT
                  CAST(rd.${ident(dimMonthCol)} AS VARCHAR) AS month,
                  COUNT(DISTINCT CAST(rd.${ident(dimRespCol)} AS VARCHAR))::INT AS total
                FROM dashboard_respondent_dims rd
                WHERE CAST(rd.${ident(dimCategoryCol)} AS VARCHAR) = ${quote(category)}
                  ${monthList.length > 0 ? `AND CAST(rd.${ident(dimMonthCol)} AS VARCHAR) IN (${monthList.map((m) => quote(m)).join(", ")})` : ""}
                  ${filterSql}
                GROUP BY 1
                ORDER BY 1 DESC
              `)
            ).map((row) => [String(row.month || ""), Number(row.total || 0)]),
          )
        : undefined;

    if (!selectedQuestions.length) {
      return includeRespondentBase
        ? { category, questionPattern: normalizedPattern, questions: [], respondentBaseByMonth }
        : { category, questionPattern: normalizedPattern, questions: [] };
    }

    const selectedQuestionCodes = Array.from(
      new Set(
        selectedQuestions.flatMap((q) =>
          (Array.isArray(q.questionCodes) && q.questionCodes.length > 0 ? q.questionCodes : [q.question])
            .map((value) => normalizeCompactText(value))
            .filter(Boolean),
        ),
      ),
    );
    const questionFilterSql = await buildResponsesFactQuestionFilterSql("rf", selectedQuestionCodes);
    const rows = await all(`
      WITH filtered_resp AS MATERIALIZED (
        SELECT DISTINCT
          CAST(rd.${ident(dimCategoryCol)} AS VARCHAR) AS category,
          CAST(rd.${ident(dimRespCol)} AS VARCHAR) AS respondent_id,
          CAST(rd.${ident(dimMonthCol)} AS VARCHAR) AS month
        FROM dashboard_respondent_dims rd
        WHERE CAST(rd.${ident(dimCategoryCol)} AS VARCHAR) = ${quote(category)}
          ${monthList.length > 0 ? `AND CAST(rd.${ident(dimMonthCol)} AS VARCHAR) IN (${monthList.map((m) => quote(m)).join(", ")})` : ""}
          ${filterSql}
      )
      SELECT
        CAST(rf.${ident(factMonthCol)} AS VARCHAR) AS month,
        CAST(rf.question AS VARCHAR) AS question,
        CAST(rf.question_label AS VARCHAR) AS question_label,
        CAST(rf.answer_label AS VARCHAR) AS answer,
        COUNT(DISTINCT CAST(rf.${ident(factRespCol)} AS VARCHAR)) AS count
      FROM responses_fact rf
      JOIN filtered_resp rd
        ON rd.category = CAST(rf.category AS VARCHAR)
       AND rd.respondent_id = CAST(rf.${ident(factRespCol)} AS VARCHAR)
       AND rd.month = CAST(rf.${ident(factMonthCol)} AS VARCHAR)
      WHERE CAST(rf.category AS VARCHAR) = ${quote(category)}
        AND ${questionFilterSql}
        ${monthSql}
      GROUP BY 1, 2, 3, 4
    `);

    const byMonthQuestion = new Map();
    selectedQuestions.forEach((question) => {
      const questionCode = String(question.question || "");
      if (!questionCode) return;
      if (monthList.length > 0) {
        monthList.forEach((month) => {
          const key = `${month}__${questionCode}`;
          if (!byMonthQuestion.has(key)) {
            byMonthQuestion.set(key, {
              month,
              question: questionCode,
              questionLabel: question.questionLabel || questionCode,
              total: 0,
              answerCounts: new Map(),
            });
          }
        });
      }
    });

    for (const row of rows) {
      const monthKey = String(row.month || "");
      const key = `${monthKey}__${row.question}`;
      if (!byMonthQuestion.has(key)) {
        byMonthQuestion.set(key, {
          month: monthKey,
          question: row.question,
          questionLabel: row.question_label || row.question,
          total: 0,
          answerCounts: new Map(),
        });
      }
      const item = byMonthQuestion.get(key);
      const count = Number(row.count || 0);
      const answer = normalizeDisplayLabel(row.answer);
      item.total += count;
      item.answerCounts.set(answer, Number(item.answerCounts.get(answer) || 0) + count);
    }

    const out = Array.from(byMonthQuestion.values())
      .map((q) => {
        const limited = takeAnswerRowsWithPinnedDisplayLabels(
          sortAnswerRowsWithPinnedDisplayLabels(
            Array.from(q.answerCounts.entries()).map(([answer, count]) => ({ answer, count })),
          ),
          Math.max(1, Number(limitAnswers) || 200),
        ).map((a) => ({
          ...a,
          pct: q.total > 0 ? Number(((a.count / q.total) * 100).toFixed(2)) : 0,
        }));
        return { ...q, answers: limited };
      })
      .sort((a, b) => {
        if (a.month === b.month) return String(a.question).localeCompare(String(b.question));
        return String(a.month) > String(b.month) ? -1 : 1;
      });

    return includeRespondentBase
      ? { category, questionPattern: normalizedPattern, questions: out, respondentBaseByMonth }
      : { category, questionPattern: normalizedPattern, questions: out };
      },
    );
    res.json(payloadOut);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

async function handleAwarenessSummary(req, res, source) {
  try {
    await ensureInitialized();
    await ensureTrendTables();
    await ensureSchemaLoaded();

    const payload = source === "query" ? req.query || {} : req.body || {};
    const slug = payload.slug;
    const monthsRaw = payload.months;
    const months = Array.isArray(monthsRaw)
      ? monthsRaw
      : typeof monthsRaw === "string" && monthsRaw.length
        ? monthsRaw
            .split(",")
            .map((x) => x.trim())
            .filter(Boolean)
        : [];
    const toList = (value) => {
      if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean);
      if (typeof value === "string" && value.length) return value.split(",").map((item) => item.trim()).filter(Boolean);
      return [];
    };
    const explicitFilters = payload.filters && typeof payload.filters === "object" ? payload.filters : {};
    const explicitModeRaw = String(payload.mode || "").toLowerCase();
    const explicitMode =
      explicitModeRaw === "media_source" || explicitModeRaw === "media-source"
        ? "media_source"
        : explicitModeRaw === "usage"
          ? "usage"
          : explicitModeRaw === "awareness"
            ? "awareness"
            : "";
    const subpageId = String(payload.subpageId || "").toLowerCase();
    const isMediaSourceMode =
      subpageId === "media_source" ||
      subpageId === "media-source" ||
      /(^|__)media-source$/.test(subpageId);
    const isUsageMode =
      subpageId === "usage" ||
      /(^|__)usage$/.test(subpageId);
    const inferredMode = isMediaSourceMode
      ? "media_source"
      : isUsageMode
        ? "usage"
        : "awareness";
    const mode = explicitMode || inferredMode;
    const regionValues = toList(payload.region ?? explicitFilters.region ?? explicitFilters.Region);
    const incomeValues = toList(payload.income ?? explicitFilters.income ?? explicitFilters.D3);
    const genderValues = toList(payload.gender ?? explicitFilters.gender ?? explicitFilters.Gender);
    const ageValues = toList(payload.age ?? explicitFilters.age ?? explicitFilters.Age);
    const secValues = toList(payload.sec ?? explicitFilters.sec ?? explicitFilters.SEC);
    const weekValues = toList(payload.week ?? explicitFilters.week ?? explicitFilters.Week);

    if (!slug) {
      return res.status(400).json({ error: "slug is required" });
    }

    const category = normalizeCategory(slug);
    const dimCategoryCol =
      schemaCache.detected?.dimCategory || detectColumn(schemaCache.baseColumns || [], ["category"]);
    const dimMonthCol =
      schemaCache.detected?.dimMonth || detectColumn(schemaCache.baseColumns || [], ["month", "file_month"]);
    const dimRespCol =
      schemaCache.detected?.dimRespondentId || detectColumn(schemaCache.baseColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    const baseCategoryCol =
      schemaCache.detected?.baseFlagsCategory || detectColumn(schemaCache.baseFlagColumns || [], ["category"]);
    const baseMonthCol =
      schemaCache.detected?.baseFlagsMonth || detectColumn(schemaCache.baseFlagColumns || [], ["month", "file_month"]);
    const baseRespCol =
      schemaCache.detected?.baseFlagsRespondentId || detectColumn(schemaCache.baseFlagColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    const bauCategoryCol =
      schemaCache.detected?.bauCategory || detectColumn(schemaCache.bauMetricColumns || [], ["category"]);
    const bauMonthCol =
      schemaCache.detected?.bauMonth || detectColumn(schemaCache.bauMetricColumns || [], ["month", "file_month"]);
    const bauRespCol =
      schemaCache.detected?.bauRespondentId || detectColumn(schemaCache.bauMetricColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    if (
      !dimCategoryCol
      || !dimMonthCol
      || !dimRespCol
      || !baseCategoryCol
      || !baseMonthCol
      || !baseRespCol
      || !bauCategoryCol
      || !bauMonthCol
      || !bauRespCol
    ) {
      return res.status(500).json({ error: "Required category/month/respondent columns not found for awareness query." });
    }

    const monthFilterDims =
      months.length > 0
        ? ` AND CAST(d.${ident(dimMonthCol)} AS VARCHAR) IN (${months.map((m) => quote(m)).join(", ")})`
        : "";
    const monthFilterBau =
      months.length > 0
        ? ` AND CAST(f.${ident(bauMonthCol)} AS VARCHAR) IN (${months.map((m) => quote(m)).join(", ")})`
        : "";
    const monthFilterResponses =
      months.length > 0
        ? ` AND CAST(r.month AS VARCHAR) IN (${months.map((m) => quote(m)).join(", ")})`
        : "";
    const dimFilterSql = buildFilterSql(
      {
        Region: regionValues,
        D3: incomeValues,
        Gender: genderValues,
        Age: ageValues,
        SEC: secValues,
        Week: weekValues,
      },
      respondentDimColumns,
      resolveRespondentDimColumn,
    );

    const modeMetrics = mode === "usage"
      ? ["ever_consumed", "last_3_months", "last_1_month", "last_7_days", "most_often_used", "prefrence"]
      : mode === "media_source"
        ? ["media_source"]
        : [
            "brand_tom",
            "brand_spont",
            "ad_tom",
            "ad_spont",
            "aided",
            "aided_ad",
            "total_awareness",
            "total_ad_awareness",
          ];
    const modeMetricSql = modeMetrics.map((m) => quote(m)).join(", ");
    const awarenessBrandBaseMetrics = ["brand_tom", "brand_spont", "aided", "total_awareness"];
    const usageBrandBaseMetrics = ["ever_consumed", "last_3_months", "last_1_month", "last_7_days", "most_often_used", "prefrence"];
    const baseBrandMetricSql = [...(mode === "awareness" ? awarenessBrandBaseMetrics : []), ...(mode === "usage" ? usageBrandBaseMetrics : [])]
      .map((m) => quote(m))
      .join(", ");
    const awarenessCurrentMonthCutoff =
      compactLabel(combinedMarketMonthCutoff || MARKET_INSIGHTS_CURRENT_MONTH)
      || CURRENT_HEADER_AUDIT_MONTH_CUTOFF;
    const canonicalTrendMetricFactsSql = mode === "media_source"
      ? ""
      : buildCanonicalTrendMetricFactsSql(category, mode, awarenessCurrentMonthCutoff, months);

    const cacheKey = JSON.stringify({
      category,
      mode,
      months: [...months].sort(),
      regionValues,
      incomeValues,
      genderValues,
      ageValues,
      secValues,
      weekValues,
    });

    const payloadOut = await getCachedOrInFlight(awarenessQueryCache, awarenessQueryInFlight, cacheKey, AWARENESS_QUERY_CACHE_TTL_MS, async () => {
    const rows = await all(`
      WITH filtered_resp AS (
        SELECT DISTINCT
          CAST(d.${ident(dimRespCol)} AS VARCHAR) AS respondent_id,
          CAST(d.${ident(dimMonthCol)} AS VARCHAR) AS month
        FROM dashboard_respondent_dims d
        WHERE CAST(d.${ident(dimCategoryCol)} AS VARCHAR) = ${quote(category)}
        ${monthFilterDims}
        ${dimFilterSql}
      ),
      base_brand AS (
        SELECT fr.month, COUNT(DISTINCT fr.respondent_id)::INT AS base_n
        FROM filtered_resp fr
        GROUP BY fr.month
      ),
      base_ad AS (
        SELECT fr.month, COUNT(DISTINCT fr.respondent_id)::INT AS base_n
        FROM filtered_resp fr
        JOIN base_flags bf
          ON CAST(bf.${ident(baseCategoryCol)} AS VARCHAR) = ${quote(category)}
         AND CAST(bf.${ident(baseRespCol)} AS VARCHAR) = fr.respondent_id
         AND CAST(bf.${ident(baseMonthCol)} AS VARCHAR) = fr.month
        WHERE bf.has_ad_base = 1
        GROUP BY fr.month
      ),
      metric_facts AS (
        ${canonicalTrendMetricFactsSql || `
        SELECT
          CAST(f.${ident(bauCategoryCol)} AS VARCHAR) AS category,
          CAST(f.${ident(bauRespCol)} AS VARCHAR) AS respondent_id,
          CAST(f.${ident(bauMonthCol)} AS VARCHAR) AS month,
          CAST(f.brand AS VARCHAR) AS brand,
          CAST(f.option AS VARCHAR) AS option,
          CAST(f.metric AS VARCHAR) AS metric
        FROM bau_metric_facts f
        WHERE CAST(f.${ident(bauCategoryCol)} AS VARCHAR) = ${quote(category)}
          ${monthFilterBau}
          AND CAST(f.metric AS VARCHAR) IN (${modeMetricSql})
          AND (
            ${quote(mode)} <> 'awareness'
            OR CAST(f.${ident(bauMonthCol)} AS VARCHAR) <= ${quote(awarenessCurrentMonthCutoff)}
          )

        UNION ALL

        SELECT *
        FROM (
          SELECT
            CAST(r.category AS VARCHAR) AS category,
            CAST(r.respondent_id AS VARCHAR) AS respondent_id,
            CAST(r.month AS VARCHAR) AS month,
            CASE
              WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU1A$|(^|_)BAU1C$')
                THEN ${sqlPreferredDisplayLabel("r.answer_label", "r.answer_value", "")}
              ELSE ${sqlBauCheckboxBrand("r.question_label", "r.answer_label", "r.answer_value")}
            END AS brand,
            CAST(NULL AS VARCHAR) AS option,
            CASE
              WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU1A$') THEN 'brand_tom'
              WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU1B_[0-9]+$') THEN 'brand_spont'
              WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU1C$') THEN 'ad_tom'
              WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU1D_[0-9]+$') THEN 'ad_spont'
              WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU2_[0-9]+$') THEN 'aided'
              WHEN regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU3_[0-9]+$') THEN 'aided_ad'
              ELSE NULL
            END AS metric
          FROM responses_fact r
          WHERE ${quote(mode)} = 'awareness'
            AND CAST(r.category AS VARCHAR) = ${quote(category)}
            AND CAST(r.month AS VARCHAR) > ${quote(awarenessCurrentMonthCutoff)}
            ${monthFilterResponses}
            AND (
              regexp_matches(CAST(r.question AS VARCHAR), '(?i)(^|_)BAU1A$|(^|_)BAU1C$')
              OR (
                regexp_matches(
                  CAST(r.question AS VARCHAR),
                  '(?i)(^|_)(BAU1B|BAU1D|BAU2|BAU3)_[0-9]+$'
                )
                AND ${sqlPositiveAnswerSelectionPredicate("r.answer_label", "r.answer_value", "r.answer_value_num")}
              )
            )
        ) current_awareness
        WHERE current_awareness.metric IS NOT NULL
          AND current_awareness.brand IS NOT NULL
          AND current_awareness.brand <> ''
        `}
      ),
      metric_facts_with_totals AS (
        SELECT category, respondent_id, month, brand, option, metric
        FROM metric_facts

        UNION ALL

        SELECT DISTINCT
          category,
          respondent_id,
          month,
          brand,
          CAST(NULL AS VARCHAR) AS option,
          CASE
            WHEN metric IN ('brand_tom', 'brand_spont', 'aided') THEN 'total_awareness'
            WHEN metric IN ('ad_tom', 'ad_spont', 'aided_ad') THEN 'total_ad_awareness'
            ELSE NULL
          END AS metric
        FROM metric_facts
        WHERE ${quote(mode)} = 'awareness'
          AND metric IN ('brand_tom', 'brand_spont', 'aided', 'ad_tom', 'ad_spont', 'aided_ad')
      ),
      dist AS (
        SELECT
          CAST(f.${ident(bauMonthCol)} AS VARCHAR) AS month,
          f.brand,
          f.option,
          f.metric,
          COUNT(DISTINCT CAST(f.${ident(bauRespCol)} AS VARCHAR))::INT AS mention_n
        FROM metric_facts_with_totals f
        JOIN filtered_resp fr
          ON fr.respondent_id = CAST(f.${ident(bauRespCol)} AS VARCHAR)
         AND fr.month = CAST(f.${ident(bauMonthCol)} AS VARCHAR)
        WHERE CAST(f.${ident(bauCategoryCol)} AS VARCHAR) = ${quote(category)}
          AND f.metric IN (${modeMetricSql})
          AND f.brand IS NOT NULL
          AND f.brand <> ''
          AND (${quote(mode)} <> 'media_source' OR (
            f.option IS NOT NULL
            AND f.option <> ''
          ))
        GROUP BY f.month, f.brand, f.option, f.metric
        UNION ALL
        SELECT
          CAST(r.month AS VARCHAR) AS month,
          ${sqlCanonicalMediaSourceBrand(category, "r.question", "r.question_label")} AS brand,
          ${sqlMediaSourceOption("r.question_label", "r.answer_label", "r.answer_value")} AS option,
          'media_source'::TEXT AS metric,
          COUNT(DISTINCT CAST(r.respondent_id AS VARCHAR))::INT AS mention_n
        FROM responses_fact r
        JOIN filtered_resp fr
          ON fr.respondent_id = CAST(r.respondent_id AS VARCHAR)
         AND fr.month = CAST(r.month AS VARCHAR)
        WHERE ${quote(mode)} = 'media_source'
          AND CAST(r.category AS VARCHAR) = ${quote(category)}
          ${monthFilterResponses}
          AND regexp_matches(CAST(r.question AS VARCHAR), '(?i)^I_[0-9]+_A_[A-Z]+_BAU4_[0-9]+$')
          AND ${sqlPositiveAnswerSelectionPredicate("r.answer_label", "r.answer_value", "r.answer_value_num")}
        GROUP BY 1, 2, 3, 4
        UNION ALL
        SELECT
          CAST(r.month AS VARCHAR) AS month,
          NULLIF(TRIM(COALESCE(NULLIF(CAST(r.answer_label AS VARCHAR), ''), NULLIF(CAST(r.answer_value AS VARCHAR), ''))), '') AS brand,
          CAST(NULL AS VARCHAR) AS option,
          'brand_tom'::TEXT AS metric,
          COUNT(DISTINCT CAST(r.respondent_id AS VARCHAR))::INT AS mention_n
        FROM responses_fact r
        JOIN filtered_resp fr
          ON fr.respondent_id = CAST(r.respondent_id AS VARCHAR)
         AND fr.month = CAST(r.month AS VARCHAR)
        WHERE ${quote(mode)} = 'awareness'
          AND ${quote(category)} = 'Malt_Beverage'
          ${monthFilterResponses}
          AND regexp_matches(CAST(r.question AS VARCHAR), '(?i)^ML_BAU1A$')
        GROUP BY 1, 2, 3, 4
        UNION ALL
        SELECT
          CAST(r.month AS VARCHAR) AS month,
          NULLIF(TRIM(COALESCE(NULLIF(CAST(r.answer_label AS VARCHAR), ''), NULLIF(CAST(r.answer_value AS VARCHAR), ''))), '') AS brand,
          CAST(NULL AS VARCHAR) AS option,
          'brand_tom'::TEXT AS metric,
          COUNT(DISTINCT CAST(r.respondent_id AS VARCHAR))::INT AS mention_n
        FROM responses_fact r
        JOIN filtered_resp fr
          ON fr.respondent_id = CAST(r.respondent_id AS VARCHAR)
         AND fr.month = CAST(r.month AS VARCHAR)
        WHERE ${quote(mode)} = 'awareness'
          AND ${quote(category)} = 'Malt_Beverage'
          ${monthFilterResponses}
          AND regexp_matches(CAST(r.question AS VARCHAR), '(?i)^Q_284$')
          AND NOT EXISTS (
            SELECT 1
            FROM responses_fact x
            WHERE CAST(x.category AS VARCHAR) = ${quote(category)}
              AND CAST(x.month AS VARCHAR) = CAST(r.month AS VARCHAR)
              AND regexp_matches(CAST(x.question AS VARCHAR), '(?i)^ML_BAU1A$')
          )
        GROUP BY 1, 2, 3, 4
      ),
      dist_final AS (
        SELECT month, brand, option, metric, mention_n
        FROM dist
      )
      SELECT
        d.month,
        d.brand,
        CASE WHEN ${quote(mode)} = 'media_source' THEN d.option ELSE NULL END AS option,
        d.metric,
        d.mention_n,
        CASE
          WHEN ${quote(mode)} = 'media_source' THEN COALESCE(bb.base_n, 0)::INT
          WHEN d.metric IN (${baseBrandMetricSql || "''"}) THEN COALESCE(bb.base_n, 0)::INT
          ELSE COALESCE(ba.base_n, 0)::INT
        END AS base_n,
        CASE
          WHEN ${quote(mode)} = 'media_source' AND COALESCE(bb.base_n, 0) > 0
            THEN ROUND((d.mention_n::DOUBLE * 100.0) / bb.base_n, 1)
          WHEN d.metric IN (${baseBrandMetricSql || "''"}) AND COALESCE(bb.base_n, 0) > 0
            THEN ROUND((d.mention_n::DOUBLE * 100.0) / bb.base_n, 1)
          WHEN d.metric NOT IN (${baseBrandMetricSql || "''"}) AND COALESCE(ba.base_n, 0) > 0
            THEN ROUND((d.mention_n::DOUBLE * 100.0) / ba.base_n, 1)
          ELSE 0
        END AS pct
      FROM dist_final d
      LEFT JOIN base_brand bb ON bb.month = d.month
      LEFT JOIN base_ad ba ON ba.month = d.month
      ORDER BY d.metric ASC, d.month ASC, d.mention_n DESC, d.brand ASC
    `);

    const hasHistoricalMediaRows = mode === "media_source"
      && rows.some((row) => compactLabel(row?.metric) === "media_source" && compactLabel(row?.month) < CURRENT_HEADER_AUDIT_MONTH_CUTOFF);
    const historicalMediaRows = mode === "media_source" && !hasHistoricalMediaRows
      ? await all(`
        WITH filtered_resp AS (
          SELECT DISTINCT
            CAST(d.${ident(dimRespCol)} AS VARCHAR) AS respondent_id,
            CAST(d.${ident(dimMonthCol)} AS VARCHAR) AS month
          FROM dashboard_respondent_dims d
          WHERE CAST(d.${ident(dimCategoryCol)} AS VARCHAR) = ${quote(category)}
          ${monthFilterDims}
          ${dimFilterSql}
        ),
        base_brand AS (
          SELECT fr.month, COUNT(DISTINCT fr.respondent_id)::INT AS base_n
          FROM filtered_resp fr
          GROUP BY fr.month
        ),
        media_mentions AS (
          SELECT
            CAST(r.month AS VARCHAR) AS month,
            ${sqlCanonicalMediaSourceBrand(category, "r.question", "r.question_label")} AS brand,
            ${sqlMediaSourceOption("r.question_label", "r.answer_label", "r.answer_value")} AS option,
            'media_source'::TEXT AS metric,
            COUNT(DISTINCT CAST(r.respondent_id AS VARCHAR))::INT AS mention_n
          FROM responses_fact r
          JOIN filtered_resp fr
            ON fr.respondent_id = CAST(r.respondent_id AS VARCHAR)
           AND fr.month = CAST(r.month AS VARCHAR)
          WHERE CAST(r.category AS VARCHAR) = ${quote(category)}
            AND CAST(r.month AS VARCHAR) < ${quote(CURRENT_HEADER_AUDIT_MONTH_CUTOFF)}
            AND regexp_matches(CAST(r.question AS VARCHAR), '(?i)^I_[0-9]+_A_[A-Z]+_BAU4_[0-9]+$')
            AND ${sqlPositiveAnswerSelectionPredicate("r.answer_label", "r.answer_value", "r.answer_value_num")}
          GROUP BY 1, 2, 3, 4
        )
        SELECT
          m.month,
          m.brand,
          m.option,
          m.metric,
          m.mention_n,
          COALESCE(b.base_n, 0)::INT AS base_n,
          CASE
            WHEN COALESCE(b.base_n, 0) > 0 THEN ROUND((m.mention_n::DOUBLE * 100.0) / b.base_n, 1)
            ELSE 0
          END AS pct
        FROM media_mentions m
        LEFT JOIN base_brand b ON b.month = m.month
      `)
      : [];

    const normalizedRowsMap = new Map();
    [...rows, ...historicalMediaRows].forEach((row) => {
      const brand = normalizeDisplayLabel(row.brand);
      const option =
        row.option === null || row.option === undefined
          ? null
          : normalizeOptionalDisplayLabel(row.option);
      if (!brand || brand === "(No response)") return;
      if (mode === "media_source" && (!option || option === "(No response)")) return;

      const key = `${String(row.month || "")}__${String(row.metric || "")}__${brand}__${option || ""}`;
      if (!normalizedRowsMap.has(key)) {
        normalizedRowsMap.set(key, {
          month: row.month,
          brand,
          brand_order: getCanonicalBrandOrder(category, brand),
          option,
          metric: row.metric,
          mention_n: 0,
          base_n: 0,
          pct: 0,
        });
      }

      const entry = normalizedRowsMap.get(key);
      entry.mention_n = mode === "media_source"
        ? Math.max(Number(entry.mention_n || 0), Number(row.mention_n || 0))
        : Number(entry.mention_n || 0) + Number(row.mention_n || 0);
      entry.base_n = Math.max(Number(entry.base_n || 0), Number(row.base_n || 0));
      entry.brand_order = Math.min(
        Number(entry.brand_order ?? Number.MAX_SAFE_INTEGER),
        getCanonicalBrandOrder(category, brand),
      );
    });

    const normalizedRows = Array.from(normalizedRowsMap.values())
      .map((row) => ({
        ...row,
        pct: Number(row.base_n || 0) > 0 ? Number(((Number(row.mention_n || 0) * 100) / Number(row.base_n || 0)).toFixed(1)) : 0,
      }))
      .sort((a, b) => {
        if (String(a.metric || "") !== String(b.metric || "")) return String(a.metric || "").localeCompare(String(b.metric || ""));
        if (String(a.month || "") !== String(b.month || "")) return String(a.month || "") > String(b.month || "") ? 1 : -1;
        const brandSpecialOrder = comparePinnedDisplayLabelsLast(a.brand, b.brand);
        if (brandSpecialOrder !== 0) return brandSpecialOrder;
        if (Number(a.brand_order) !== Number(b.brand_order)) return Number(a.brand_order) - Number(b.brand_order);
        const optionSpecialOrder = comparePinnedDisplayLabelsLast(a.option, b.option);
        if (optionSpecialOrder !== 0) return optionSpecialOrder;
        if (Number(a.mention_n || 0) !== Number(b.mention_n || 0)) return Number(b.mention_n || 0) - Number(a.mention_n || 0);
        const brandCompare = String(a.brand || "").localeCompare(String(b.brand || ""), undefined, {
          numeric: true,
          sensitivity: "base",
        });
        if (brandCompare !== 0) return brandCompare;
        return String(a.option || "").localeCompare(String(b.option || ""), undefined, {
          numeric: true,
          sensitivity: "base",
        });
      });

    return {
      category,
      monthsSelected: months,
      mode,
      rows: normalizedRows,
    };
    });

    res.json(payloadOut);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
}

app.get("/api/awareness-summary", (req, res) => handleAwarenessSummary(req, res, "query"));
app.post("/api/awareness-summary", (req, res) => handleAwarenessSummary(req, res, "body"));
app.get("/api/respondents/category/awareness-summary", (req, res) => handleAwarenessSummary(req, res, "query"));
app.post("/api/respondents/category/awareness-summary", (req, res) => handleAwarenessSummary(req, res, "body"));

app.get("/api/options/:field", async (req, res) => {
  try {
    await init();
    if (!schemaCache) schemaCache = await readSchema();
    const factColumns = schemaCache.longColumns || [];
    const dimColumns = schemaCache.baseColumns || [];
    const map = new Map([...factColumns, ...dimColumns].map((col) => [col.toLowerCase(), col]));
    const column = map.get(req.params.field.toLowerCase());

    if (!column) {
      return res.status(400).json({ error: `Unknown field: ${req.params.field}` });
    }

    const sourceTable = dimColumns.includes(column) ? "respondent_dims" : "responses_fact";
    const sourceIdent = ident(column);

    const rows = await all(`
      SELECT DISTINCT ${sqlNormalizedFilterExpr(column, sourceIdent)} AS value
      FROM ${sourceTable}
      WHERE ${sourceIdent} IS NOT NULL
      ORDER BY value
      LIMIT 300
    `);

    res.json({ field: column, values: rows.map((row) => normalizeFilterValue(column, row.value)).filter(Boolean) });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/api/distribution", async (req, res) => {
  try {
    await init();
    if (!schemaCache) schemaCache = await readSchema();

    const { questionCode, filters = {}, limit = 25 } = req.body || {};
    if (!questionCode) return res.status(400).json({ error: "questionCode is required" });

    const { detected, longColumns } = schemaCache;
    if (!detected.question || !detected.answer) {
      return res.status(500).json({ error: "Question/answer columns could not be detected." });
    }

    const filterSql = buildFilterSql(filters, longColumns);
    const hardLimit = Math.max(1, Number(limit) || 25);

    const rows = await all(`
      SELECT
        COALESCE(NULLIF(TRIM(CAST(${detected.answer} AS VARCHAR)), ''), '(No response)') AS answer,
        COUNT(*) AS count
      FROM responses_fact
      WHERE CAST(${detected.question} AS VARCHAR) = ${quote(questionCode)}
      ${filterSql}
      GROUP BY 1
      ORDER BY count DESC
    `);

    const answerCounts = new Map();
    rows.forEach((row) => {
      const answer = normalizeDisplayLabel(row.answer);
      const count = Number(row.count || 0);
      answerCounts.set(answer, Number(answerCounts.get(answer) || 0) + count);
    });
    const data = takeAnswerRowsWithPinnedDisplayLabels(
      sortAnswerRowsWithPinnedDisplayLabels(
        Array.from(answerCounts.entries()).map(([answer, count]) => ({ answer, count: Number(count || 0) })),
      ),
      hardLimit,
    );
    const total = data.reduce((sum, row) => sum + Number(row.count || 0), 0);
    const normalizedData = data.map((row) => ({
      ...row,
      pct: total > 0 ? Number(((row.count / total) * 100).toFixed(2)) : 0,
    }));

    res.json({
      questionCode,
      total,
      data: normalizedData,
      detected: { question: "question", answer: "answer_label" },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/api/verbatim-topics", async (req, res) => {
  try {
    await init();
    const {
      slug,
      questionCode,
      filters = {},
      months = [],
      groupByBrand = false,
    } = req.body || {};

    const category = normalizeCategory(slug);
    const normalizedQuestionCode = compactLabel(questionCode);
    if (!category) return res.status(400).json({ error: "slug is required" });
    if (!normalizedQuestionCode) return res.status(400).json({ error: "questionCode is required" });

    const monthList = Array.isArray(months)
      ? months.map((value) => String(value).trim()).filter(Boolean)
      : typeof months === "string" && months.length
        ? months.split(",").map((value) => value.trim()).filter(Boolean)
        : [];

    const rows = await fetchVerbatimResponses(category, normalizedQuestionCode, filters, monthList, Boolean(groupByBrand));
    const questionLabel = normalizeVerbatimQuestionLabel(normalizedQuestionCode, rows[0]?.questionLabel || normalizedQuestionCode);
    const groups = buildVerbatimInsightGroups(rows, Boolean(groupByBrand));

    res.json({
      category,
      questionCode: normalizedQuestionCode,
      questionLabel,
      groupByBrand: Boolean(groupByBrand),
      totalResponses: rows.length,
      generatedAt: new Date().toISOString(),
      groups,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post("/api/custom-table", async (req, res) => {
  try {
    await ensureInitialized();
    await ensureSchemaLoaded();

    const {
      slug,
      topQuestions = [],
      sideQuestions = [],
      displayMode = "column_pct",
      analysisOptions = [],
      formatOptions = {},
      filters = {},
      months = [],
      monthGroups = [],
    } = req.body || {};

    if (!slug) {
      return res.status(400).json({ error: "slug is required" });
    }

    const category = normalizeCategory(slug);
    const monthList = Array.isArray(months)
      ? months.map((value) => String(value).trim()).filter(Boolean)
      : typeof months === "string" && months.length
        ? months.split(",").map((value) => value.trim()).filter(Boolean)
        : [];
    const normalizedTopQuestions = Array.isArray(topQuestions)
      ? topQuestions
          .map((spec, index) => normalizeCustomTableQuestionSpec(spec, "top_question", index))
          .filter(Boolean)
      : [];
    const normalizedSideQuestions = Array.isArray(sideQuestions)
      ? sideQuestions
          .map((spec, index) => normalizeCustomTableQuestionSpec(spec, "side_question", index))
          .filter(Boolean)
      : [];
    const normalizedAnalysisOptions = Array.isArray(analysisOptions)
      ? analysisOptions.filter((value) => value === "significance" || value === "chi_square")
      : [];

    if (!normalizedTopQuestions.length || !normalizedSideQuestions.length) {
      return res.status(400).json({ error: "topQuestions and sideQuestions are required" });
    }

    const dimCategoryCol =
      schemaCache.detected?.dimCategory || detectColumn(schemaCache.baseColumns || [], ["category"]);
    const dimMonthCol =
      schemaCache.detected?.dimMonth || detectColumn(schemaCache.baseColumns || [], ["month", "file_month"]);
    const dimRespCol =
      schemaCache.detected?.dimRespondentId || detectColumn(schemaCache.baseColumns || [], ["respondent_id", "SbjNum", "sbjnum"]);
    if (!dimCategoryCol || !dimRespCol) {
      return res.status(500).json({ error: "Required respondent dimension columns not found for custom table." });
    }

    const monthFilterSql =
      dimMonthCol && monthList.length > 0
        ? ` AND CAST(d.${ident(dimMonthCol)} AS VARCHAR) IN (${monthList.map((month) => quote(month)).join(", ")})`
        : "";
    const filterSql = buildCustomTableAliasedFilterSql(filters, respondentDimColumns, "d");
    const customTableRespondentKeySql = dimMonthCol
      ? `CAST(d.${ident(dimRespCol)} AS VARCHAR) || '__' || CAST(d.${ident(dimMonthCol)} AS VARCHAR)`
      : `CAST(d.${ident(dimRespCol)} AS VARCHAR)`;
    const totalRows = await all(`
      SELECT COUNT(DISTINCT ${customTableRespondentKeySql}) AS total
      FROM respondent_dims d
      WHERE CAST(d.${ident(dimCategoryCol)} AS VARCHAR) = ${quote(category)}
      ${monthFilterSql}
      ${filterSql}
    `);
    const totalRespondents = Number(totalRows[0]?.total || 0);

    const questionSpecMap = new Map();
    [...normalizedTopQuestions, ...normalizedSideQuestions].forEach((spec) => {
      questionSpecMap.set(buildQuestionSpecCacheKey(spec), spec);
    });

    const customTableRequestCache = {};
    const normalizedMonthGroups = normalizeCustomTableMonthGroups(monthGroups, monthList);
    const periodData = await fetchCustomTablePeriodData(category, normalizedMonthGroups, filters);
    console.log("[custom-table] monthList", monthList);
    console.log("[custom-table] normalizedMonthGroups", normalizedMonthGroups);
    console.log("[custom-table] periodData.valueOrder", periodData.valueOrder);
    const questionDataEntries = await Promise.all(
      Array.from(questionSpecMap.values()).map(async (spec) => {
        const data = await fetchCustomTableQuestionData(category, spec, filters, monthList, customTableRequestCache);
        return [buildQuestionSpecCacheKey(spec), data];
      }),
    );
    const questionDataCache = new Map(questionDataEntries);
    const valueRespondentMapCache = new Map();
    const normalizedDisplayMode = ["counts", "column_pct", "row_pct", "total_pct"].includes(String(displayMode))
      ? String(displayMode)
      : "column_pct";
    const tables = normalizedSideQuestions.map((sideSpec) => {
      const table = buildCustomTable(
        sideSpec,
        normalizedTopQuestions,
        questionDataCache,
        totalRespondents,
        normalizedAnalysisOptions,
        normalizedDisplayMode,
        valueRespondentMapCache,
        periodData,
      );
      return {
        ...table,
        sideQuestion: {
          ...table.sideQuestion,
          label: cleanCustomTableSideQuestionDisplayLabel(table.sideQuestion.label, table.sideQuestion.questionCodes),
        },
      };
    });

    res.json({
      category,
      displayMode,
      analysisOptions: normalizedAnalysisOptions,
      formatOptions,
      totalRespondents,
      generatedAt: new Date().toISOString(),
      tables,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

if (FRONTEND_DIST_PATH && FRONTEND_INDEX_PATH) {
  app.use(express.static(FRONTEND_DIST_PATH));

  app.get("*", (req, res, next) => {
    if (req.path.startsWith("/api/")) return next();
    if (path.extname(req.path)) return next();

    return res.sendFile(FRONTEND_INDEX_PATH, (err) => {
      if (err) next(err);
    });
  });
} else {
  console.warn("[backend] frontend dist not found; root URL will not serve the SPA");
}

async function bootstrapNativeTablesFromParquet() {
  await init();
  return DB_PATH;
}

async function startServer() {
  await init();

  if (process.argv.includes("--bootstrap-only")) {
    return null;
  }

  return new Promise((resolve, reject) => {
    const server = app.listen(port, () => {
      console.log(`[backend] running on http://localhost:${port}`);
      console.log(`[backend] duckdb path: ${DB_PATH}`);
      console.log(`[backend] frontend dist: ${FRONTEND_DIST_PATH || "(not found)"}`);
      console.log(`[backend] long parquet: ${LONG_GLOB}`);
      console.log(`[backend] base parquet: ${BASE_GLOB}`);
      startSurveyCtoScheduler();
      startExportScheduler();
      resolve(server);
    });

    server.on("error", reject);
  });
}

if (require.main === module) {
  startServer().catch((err) => {
    console.error("[backend] failed to initialize:", err.message);
    process.exitCode = 1;
  });
}

module.exports = {
  app,
  init,
  startServer,
  bootstrapNativeTablesFromParquet,
  DB_PATH,
};
