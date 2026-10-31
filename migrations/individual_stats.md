Individual stat edits
====================

Target: each **namespace database**, never `main`. No schema change is required.
An individual edit/snapshot has both `race_result_id` and `stat_list_id` NULL;
`cdb_cyclist_id` identifies the independent review scope. Race results and lists
retain their existing references. Individual submissions use a namespace and rider
specific audit resource and must be resubmitted after any save.

For installations with legacy `Rider profile #<id>` lists:

1. Back up the selected namespace database and stop web/worker writes.
2. Review [the conversion SQL](./convert_profile_lists_to_individual.sql) and the
   matching lists/items in that database. Multi-rider or renamed lists are excluded.
3. Run the script manually against that namespace database. It refuses ambiguous
   scopes or collisions with existing individual edits. Resolve any such collision
   manually before retrying; do not delete records to force conversion.
4. Restart services. Resubmit converted pending edits for approval. Existing
   approval/release IDs and stat values are preserved; historical release artifacts
   are unchanged. Newly generated overviews label these edits `Individual`.

This script is not run by application startup or namespace provisioning.
