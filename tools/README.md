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
project. Costs use previously stored rates and are estimates, not billing.
Unpriced models are explicit. Missing sources retain their old freshness and
cause a nonzero result rather than a false all-fresh timestamp.

`publisherRefreshedAt` is separate from the aggregate metric timestamp.
Historical milestone prose is not republished as fresh project acceptance.
Public PR counts exclude the private archive.

Only index.html, status-data.js, .nojekyll and the reviewed image/video assets
are staged by a publishing run. Unrelated staged files block publication.
An operating-system lock prevents concurrent manual/hourly publishing.
Destinations must match the configured canonical origin and branch. Pushes
are non-force. A dry run refreshes private generated files but never modifies,
commits or pushes public files. It may fast-forward the configured clones.

The canonical page is https://kldsseeghosts.github.io/caelestia-windows/ .
The older https://kldsseeghosts.github.io/caelestia-windows-site/ remains a mirror.
Both use GitHub's branch-based Pages deployment. The hourly task preserves its
existing one-hour trigger, principal and background behavior.
