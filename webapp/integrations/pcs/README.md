# ProCyclingStats (PCS)

This integration parses supplied PCS race result HTML and matches riders against
a Namespace's CDB. Parsing HTML does not fetch a remote page.

## Usage

Parse HTML without persisting results:

```python
from webapp.integrations.pcs import parse_pcs_race_results_html

parsed = parse_pcs_race_results_html(html, source_url=source_url)
```

To persist results, use the configured Flask application context and a Namespace:

```python
from webapp.integrations.pcs import PcsClient

client = PcsClient(namespace)
summary = client.sync_race_results_from_html(html=html, source_url=source_url)
```

`PcsClient` replaces existing PCS results for the same Namespace and race name,
stores parsed rows in the main database, and maintains race result mappings.
It commits by default; pass `commit=False` to leave the transaction to the caller.
`PcsRiderMatcher` reads the Namespace CDB to match riders, with a configurable
`fuzzy_threshold` (default `0.84`). Parsing requires BeautifulSoup, installed via
`beautifulsoup4` in the project requirements.
