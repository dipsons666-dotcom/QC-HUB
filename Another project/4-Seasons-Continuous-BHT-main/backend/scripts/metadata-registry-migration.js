#!/usr/bin/env node
const duckdb = require("duckdb");
const fs = require("fs");
const path = require("path");
const metadataRegistry = require("../src/metadata-registry");

function argValue(name, fallback = "") {
  const prefix = `--${name}=`;
  const found = process.argv.find((arg) => arg.startsWith(prefix));
  return found ? found.slice(prefix.length) : fallback;
}

function hasFlag(name) {
  return process.argv.includes(`--${name}`);
}

function connectDuckDb(dbPath) {
  return new Promise((resolve, reject) => {
    const db = new duckdb.Database(dbPath);
    db.connect((err, conn) => {
      if (err) return reject(err);
      resolve({ db, conn });
    });
  });
}

function run(conn, sql) {
  return new Promise((resolve, reject) => {
    conn.run(sql, (err) => (err ? reject(err) : resolve()));
  });
}

function all(conn, sql) {
  return new Promise((resolve, reject) => {
    conn.all(sql, (err, rows) => (err ? reject(err) : resolve(rows || [])));
  });
}

function close(db, conn) {
  return new Promise((resolve) => {
    const closeDb = () => {
      try {
        db.close(() => resolve());
      } catch (_err) {
        resolve();
      }
    };
    try {
      conn.close(() => closeDb());
    } catch (_err) {
      closeDb();
    }
  });
}

async function main() {
  const backendRoot = path.resolve(__dirname, "..");
  const dbPath = path.resolve(argValue("db", process.env.DUCKDB_PATH || path.join(process.cwd(), "current.duckdb")));
  const dryRun = hasFlag("dry-run") || !hasFlag("apply");
  const reportPath = argValue("report", path.join(backendRoot, "data", "metadata_registry_migration_report.json"));
  const paths = {
    exportSpecsPath: path.resolve(argValue("export-specs", path.join(backendRoot, "data", "export_table_specs.json"))),
    spssRulesPath: path.resolve(argValue("spss-rules", path.join(backendRoot, "data", "spss_export_rules.json"))),
    xlsformMetadataPath: path.resolve(argValue("xlsform", path.join(backendRoot, "data", "xlsform_metadata.json"))),
  };

  const { db, conn } = await connectDuckDb(dbPath);
  try {
    const report = await metadataRegistry.seedInitialRegistry({ run: (sql) => run(conn, sql), all: (sql) => all(conn, sql) }, paths, { dryRun });
    report.dbPath = dbPath;
    report.paths = paths;
    fs.mkdirSync(path.dirname(reportPath), { recursive: true });
    fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
    console.log(JSON.stringify(report, null, 2));
  } finally {
    await close(db, conn);
  }
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exitCode = 1;
});
