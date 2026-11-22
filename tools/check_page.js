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
if (!page.includes("data-release-download") || !page.includes("showRelease();"))
    throw new Error("Public release resolver missing");
if (!page.includes("account-wide proxy totals are excluded"))
    throw new Error("Usage scope disclosure missing");
// The owner chose to show recorded tokens and their API list price as headline
// figures. They stay partial, so the page must say so next to them, and must
// never present the null reconciled totals or a billing claim.
if (!page.includes("Both totals are partial") ||
        !page.includes("This is an estimate, not a bill") ||
        !page.includes("rates but incomplete usage") || !page.includes("Rate unavailable") ||
        page.includes('t.totalTokens') || page.includes('t.apiCostUsd') ||
        page.includes("It all ran on subscriptions."))
    throw new Error("Usage headline must carry its partial-coverage disclosure");
console.log(`PASS public page script and migration checks: ${checked} inline script`);
