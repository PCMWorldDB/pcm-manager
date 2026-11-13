# Namespace field configuration

## Existing namespaces

The operator must review and run `field_level_configuration_namespace.sql`
against each namespace database. The script creates the configuration table and
adds `stat_changes.is_applied_immediately` and the namespace field audit table.
Back up the target database first.
Keep workers and web writes stopped during the installation and seed.

Populate defaults using the namespace's PCM version.

```sh
docker compose run --rm web flask namespace seed-field-configuration NAMESPACE_SLUG --yes
```

Restart the web and worker processes after installation. Main RBAC initialization
creates field resources and their initial grants. No main schema script is needed.
Review role grants on the role detail page before enabling user edits.

New namespace provisioning installs and seeds these settings automatically.
Seeding fills missing rows and preserves existing settings. Legacy namespaces
without installed settings continue using YAML defaults for field behavior.
Field resource permissions apply to legacy namespaces as well.

## Defaults and behavior

- Names are internal database column names. The rider `birth_date` column has the
  resource slug `rider:date_of_birth`. All rider stat columns use `rider:stat`.
- Resource slugs and primary-key identity mappings are fixed. Settings are
  namespace-specific. Role grants belong to main and apply across namespaces.
- The `admin` role initially receives field update and approve permissions.
  Stat editors receive `rider:stat:update` and stat approvers receive
  `rider:stat:approve`. Database Editor receives update permission for every
  non-stat field resource. Database Maintainer receives update and approve
  permission for every field resource, including stats. Existing workflow
  permissions still apply to stat edits.
  Subsequent RBAC synchronization preserves field grants and revocations.
- Import defaults come from the selected PCM version in `cdb_mappings.yaml`.
  Manual edit defaults come from `editable_fields.yaml`. Approval is required
  by default. Application-managed fields are excluded, and identity is read-only.
- Current manual editors support riders and teams. Lookup objects expose import
  configuration. Their fields remain read-only in the manual workflow.
- CDB source settings control imports. Release/export column mappings retain
  the version's export configuration.
- Clearing “Requires approval” applies new edits during save. Attribute changes
  retain an applied event. Stat changes remain available to release selection.
  Immediately applied stats are read-only in their existing unreleased scope.
  Mixed stat lists retain pending fields for the existing approval workflow.
- Changing an approval setting leaves existing pending proposals for review.
  Changing import configuration invalidates an outstanding CDB review.
- Updates from the field configuration UI are recorded with namespace, object,
  field, actor, and changed before/after values in the main Audit Log.

Configuration updates and their audit events commit in the same namespace
transaction. Committed events are projected into the main Audit Log. A failed
projection retains the event in the namespace and is retried when field settings
or the Audit Log are opened. Projection deduplicates by namespace and event ID.

## Logged-in users

Role memberships and resource/action grants are loaded from main and cached in
Flask's request-local `g` object. A changed assignment or grant takes effect on
the user's next request. An already-authorized request may finish under its
existing permission snapshot.

The selected namespace's field settings are read from its database during edits
and approvals. Writes take shared locks on the relevant configuration rows so
a configuration update waits for actions already using that configuration.
After the update commits, subsequent actions use the new settings. Open browser
pages show updated controls after refresh. Every save and approval enforces
current permissions and field settings on the server.

Role grants apply across namespaces. Namespace configuration can still make
fields read-only or require approval. New role definitions are installed by
normal RBAC initialization or `flask seed-roles`, and users must be assigned their
roles through user management.
