# Integrations

Each external integration lives in its own Python package:

```text
integrations/
  cdb/
    __init__.py
    cdb_mappings.yaml
    README.md
  discord/
    __init__.py
    README.md
  firstcycling/
    __init__.py
    README.md
  pcs/
    __init__.py
    README.md
```

The existing implementation lives in each package's `__init__.py`, preserving
imports such as `from webapp.integrations.firstcycling import FirstCyclingClient`.
Additional Python modules, Markdown documentation, YAML mappings, and other
resources can live beside it as needed.

## Supported integrations

- [CDB](cdb/README.md): PCM SQLite inspection, updates, and field mappings.
- [Discord](discord/README.md): workflow notifications and approval images.
- [FirstCycling](firstcycling/README.md): API fetching and main database sync.
- [ProCyclingStats](pcs/README.md): result HTML parsing and Namespace CDB rider matching.

## Adding an integration

Create a package directory with `__init__.py` and a `README.md` describing its
purpose, configuration, usage, and persistence behavior. Keep public imports
available from the package if its implementation is later split into modules.

Add YAML files when needed; files are not automatically loaded as configuration.
Keep credentials and sensitive URL segments in environment variables, never in
committed resources. Encapsulate network and persistence behavior and redact
secrets from errors.
