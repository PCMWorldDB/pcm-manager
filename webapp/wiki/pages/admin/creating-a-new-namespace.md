## Creating a New Namespace

> A Namespace represents one maintained CDB plus its namespace-specific workflow data. Check the selected season and gender before importing a CDB.


1. Open Settings, then select Namespaces.
2. Select Create Namespace.
3. Enter the namespace name, season, gender, description, PCM version, and initial status. The slug is generated from name and gender; it does not include the season or PCM version.

A namespace can be maintained across seasons and PCM versions. On its admin detail page, use **Current season and PCM version** to update these settings. They drive season queries and CDB mappings without changing the namespace URL, database, files, or workflow history. Upload a compatible CDB when moving to another PCM version before creating releases. Existing namespaces keep their original slugs, including any year, so their URLs and stored files remain accessible.
4. Optionally upload a PCM `.cdb` file or an already converted `.sqlite` file.
   Packed `.cdb` files are converted locally to SQLite before import; the
   original upload is preserved. Invalid or unreadable files are rejected
   before a namespace is created.
5. Review the generated slug, then submit the form.
6. After creation, open the namespace detail page and confirm provisioning and import messages.

To start from an existing namespace, select **Clone Existing Namespace** as the
creation method and choose an accessible source. Select the same PCM version as
the source; the name, season, description, gender, and initial status belong to
the new namespace.

**Copy Latest CDB Release and CDB Mapped tables** is mandatory. The clone imports
the latest published release, rather than the source's current working tables.
It owns copies of that release's CDB, rider SQL dump, overview HTML, and a SQLite
baseline for future releases. The source must have a complete published release
and compatible namespace schema. Cloning does not upgrade an existing schema.

Choose whether to copy completed **Rider Stats**, **Rider Attributes**, **Teams**,
and **Races** history; all four are selected initially. Race history includes
stages and race classes. History is limited to publication of the selected
release. Stat history must belong to a published release; pending proposals and
approved but unreleased stat work are excluded. Related lists, snapshots,
lifecycle events, and referenced published release metadata are copied as needed.
Only the latest release's files are copied. Source Discord threads, scheduled
jobs, Activity Monitor data, and shared application records are not copied.

Select **Preview Clone**, review the release and record counts, then select
**Confirm and clone namespace**. A changed source or changed settings requires
another preview. The destination database and storage directory must both be
new. Copied records retain their IDs inside the isolated new database, and ID
sequences are advanced before creation completes. History is never reapplied to
the released data. Cloning requires namespace creation/read and release
read/download permissions as well as access to the selected source.

