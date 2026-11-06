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

