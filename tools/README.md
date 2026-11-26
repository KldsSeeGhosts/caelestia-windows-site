# Hourly showcase metrics publisher

Run on the project owner's Windows workstation with an existing authenticated
GitHub CLI. This is not a GitHub Actions runner and makes no authentication
changes. Private configuration, SQLite data, transcripts and generated runtime
files stay outside this repository.

```
python -I tools/publish.py --config <private-absolute-config.json> --dry-run
python -I tools/publish.py --config <private-absolute-config.json>
python -B -m unittest discover -s tools -p test_metrics.py
```

The configuration names the current and historical workspace roots, public
repositories, public publishing clones and private runtime directory.
The reviewed template is versioned at `source/showcase.html`.
The publisher reads project-scoped T3 metadata, deduplicated Claude transcript
usage and contributor commit hashes, plus current public GitHub repository
counts. It never exports message text, workspace paths or credentials.
Account-wide proxy usage is excluded because it cannot be attributed to this
project. Usage coverage and counter semantics have not been fully reconciled.
The page headlines recorded token events and their known-rate API estimate
at the owner's request, next to a note that both are partial. Token events
include repeated cached-input processing, not unique text; known-rate
estimates use historical snapshots and are neither complete cost nor billing.
`source/model-rates.json` records dated public reference rates in USD per
million tokens. It supplements the existing historical snapshot with
explicit price-only aliases. Model usage stays grouped by the reported ID.
Rates are fixed references, not a reconstruction of price changes, plans,
speed tiers, or promotional discounts. The Gemini 4 Carbon entry uses the
published introductory rate on the snapshot date. The SWE-2 entries use
Cognition's non-promotional list prices, and the GLM coding-plan aliases
use the stored API reference rates rather than a subscription price.
Anthropic rates were checked against the published pricing page on the
snapshot date, including 1-hour cache writes and the fast-mode multiplier.
These committed rates win over rates carried forward in the previous
snapshot, so a wrong historical rate is corrected on the next run.

T3 usage is read from every provider turn in the project's threads. Delegated
subagent turns have no run attempt in T3, so they are joined by thread, not
by run. T3 input counters already include cache reads and cache writes, so
both are subtracted to get uncached input. Claude transcript requests are
deduplicated by message ID, keeping the largest count per channel, because
streamed and copied entries repeat a request with partial usage. Each request
is priced by its own cache duration and speed. Claude Code deletes old
transcripts, so seen request counts are kept in a private runtime ledger
(`claude-usage-ledger.json`, token counts only) and history never shrinks.
Missing model counters stay null, not zero. Synthetic records are excluded.
Known rates remain available when usage is incomplete, but no cost is
estimated without the counters and applicable rates. Missing sources retain their old freshness and
cause a nonzero result rather than a false all-fresh timestamp.

`publisherRefreshedAt` is separate from the aggregate metric timestamp.
Historical milestone prose is not republished as fresh project acceptance.
Public PR counts exclude the private archive.

Only index.html, release-download.js, status-data.js, .nojekyll and the reviewed
image/video assets are staged by a publishing run. The resolver selects the
exact public ZIP from the same metadata pointer as Nexus; the metrics
collector is independent of that pointer and the proxy dashboard.
Unrelated staged files block publication.
An operating-system lock prevents concurrent manual/hourly publishing. Before it
builds anything, the publisher fast-forwards the checkout it runs from
(`git pull --ff-only`) and, if new commits arrived, runs again with the new
code, so a merged template is never overwritten by an older copy. If that
checkout cannot fast-forward (no upstream, local commits or conflicting edits),
the run fails and publishes nothing. Set `"syncSource": false` in the private
config to turn this off.
Destinations must match the configured canonical origin and branch. Pushes
are non-force. A dry run refreshes private generated files but never modifies,
commits or pushes public files. It may fast-forward the configured clones.

The canonical page is https://kldsseeghosts.github.io/caelestia-windows/ .
The older https://kldsseeghosts.github.io/caelestia-windows-site/ remains a mirror.
Both use GitHub's branch-based Pages deployment. The hourly task preserves its
existing one-hour trigger, principal and background behavior.
