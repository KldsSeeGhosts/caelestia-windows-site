const fs = require("node:fs");
const vm = require("node:vm");
const page = fs.readFileSync(process.argv[2], "utf8");
let checked = 0;
for (const match of page.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (match[1].includes("src=") || match[1].includes("application/ld+json"))
        continue;
    new vm.Script(match[2]);
    checked++;
}
if (checked === 0)
    throw new Error("No inline application script found");
if (!page.includes("Download Windows preview") || page.includes("RELEASED = false"))
    throw new Error("Public preview availability is stale");
if (!page.includes("account-wide proxy totals are excluded"))
    throw new Error("Usage scope disclosure missing");
if (!page.includes("No verified project-wide token total or cost is available.") ||
        page.includes('num(t.totalTokens') || page.includes('num(t.apiCostUsd') ||
        page.includes("It all ran on subscriptions."))
    throw new Error("Unreconciled usage must not appear as an authoritative headline");
console.log(`PASS public page script and migration checks: ${checked} inline script`);
