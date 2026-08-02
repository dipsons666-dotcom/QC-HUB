#!/usr/bin/env node

const triggerUrl = String(process.env.SYNC_TRIGGER_URL || "").trim();
const triggerToken = String(process.env.SYNC_TRIGGER_TOKEN || "").trim();

if (!triggerUrl || !triggerToken) {
  console.error("SYNC_TRIGGER_URL and SYNC_TRIGGER_TOKEN are required.");
  process.exit(1);
}

async function main() {
  const response = await fetch(triggerUrl, {
    method: "POST",
    headers: {
      authorization: `Bearer ${triggerToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ source: "render-cron" }),
    signal: AbortSignal.timeout(30_000),
  });

  const body = await response.text();
  if (!response.ok && response.status !== 409) {
    throw new Error(`Sync trigger failed (${response.status}): ${body.slice(0, 1000)}`);
  }

  console.log(body);
}

main().catch((error) => {
  console.error(error.message || String(error));
  process.exit(1);
});
