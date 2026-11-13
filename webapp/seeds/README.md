# Namespace identity seeds and shared provider maps

`race_identity.yaml` and `rider_identity.yaml` are namespace seed datasets.
Their `races` / `riders` entries contain optional local `race_id` / `rider_id`,
reference `race_name` / `rider_name`, `firstcycling_id`, and `pcs_slug`.
A local ID selects a CDB row only during the confirmed initial CDB-import flow.
It is never copied into main. Null local IDs support external races/riders absent
from the CDB. Missing CDB rows are never inserted by these seeds.

```yaml
races:
  - race_id: null
    race_name: Example minor race
    firstcycling_id: 123456
    pcs_slug: example-minor-race
```

Namespace `race` and `rider` rows keep their own CDB ID, `first_cycling_id`, and
`pcs_slug`. Rider seeds fill missing fields independently; CDB values win over
seeds, and explicit seeds win over a name-generated PCS fallback. Race seeds use
the supplied mappings. Names remain labels and never deduplicate races.
Clones and later uploads do not repeat the initial seed projection.

The **main** `race_identity` and `rider_identity` tables contain only provider
IDs (`firstcycling_id`, `pcs_slug`) plus an internal ID and timestamps. They have
no WDBO IDs, CDB IDs, namespace IDs, names, or pointers to namespace objects.
There are two contribution paths:

1. Initial namespace seed loading queues complete FC/PCS seed pairs, including
   entries with no local CDB ID or no matching CDB object.
2. Applied namespace operations queue complete FC/PCS pairs from CDB rows.
   PostgreSQL triggers cover imports, approved attribute changes, and direct SQL
   updates. Proposals do not contribute until applied. Rider slugs generated from
   names carry `pcs_slug_is_assumed` and never contribute to main.

Contributions live in the **namespace** `identity_sync_outbox`, in the same
transaction as the local changes. A rollback removes them. Only after that
transaction commits does the application publish the pair to main. Main is
committed before the namespace outbox is marked published; interrupted delivery
can be replayed safely. Main outages preserve committed namespace work and leave
publication pending. Local CDB IDs, names, and namespace identifiers never enter
the provider map.

The publisher creates new pairs, reuses exact pairs, and merges compatible
incomplete provider records. Conflicting established links are preserved in main
and flagged `conflict` in the contributing namespace. It never replaces a shared
mapping based on namespace precedence, never deletes shared facts when a local
pointer is cleared, and never modifies another namespace.

Intermediate pairs replaced within one transaction are marked `superseded`
instead of being published. Explicit seed pairs remain publishable even when no
namespace CDB object exists for them.

Namespace identity fields use the existing attribute proposal/approval workflow
and field permissions. CDB race fields remain read-only. New schemas enable
`race.first_cycling_id`, `race.pcs_slug`, and `rider.first_cycling_id` as identity
fields. Existing field settings remain operator-controlled; enable these fields
through Field Configuration if required after deployment.

Authorized maintainers review/correct shared FC/PCS pairs at `/identities/race`
and `/identities/rider`; corrections are audited against the main mapping record,
without namespace object references. The page shows pending/conflicting pairs
from the selected namespace and provides a retry action. Alternatively:

```text
docker compose exec web flask namespace publish-identities NAMESPACE_SLUG
# After reviewing and resolving conflicts:
docker compose exec web flask namespace publish-identities NAMESPACE_SLUG --retry-conflicts
```

The race list combines namespace CDB races, season-specific FC editions, and
known PCS links using exact provider IDs. Unmatched records remain visible;
ambiguous mappings stay separate and are flagged. A PCS badge means a source
link, not downloaded results. PCS race scraping is separate.

## Deployment

Back up **main** and each namespace, pause application/worker writes, and connect
explicitly to each target database:

- Main: run `migrations/shared_identity_main.sql`. It also removes obsolete WDBO
  columns if the earlier identity design was installed. Main is not seeded at
  startup; provider maps are populated by namespace contributions.
- Each namespace: install earlier race identity storage columns with
  `migrations/cdb_race_identity_namespace.sql` if needed, then run
  `migrations/identity_sync_namespace.sql`. It installs the outbox and triggers,
  adds the rider assumed-slug marker, and removes obsolete shared-ID references.
- Deploy/restart and review field configuration and identity publication status.

No migration or retrospective namespace scan is executed automatically. Existing
name-generated slugs cannot be reliably identified from legacy values; review
those local identities before changing them into explicit source mappings.
