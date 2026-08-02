process.env.ALLOW_PARQUET_BOOTSTRAP = "1";
process.argv.push("--bootstrap-only");

const { startServer, DB_PATH } = require("../src/server.js");

startServer()
  .then(() => {
    console.log(`[backend] bootstrap script finished: ${DB_PATH}`);
    process.exit(0);
  })
  .catch((err) => {
    console.error("[backend] bootstrap failed:", err && err.message ? err.message : err);
    process.exit(1);
  });
