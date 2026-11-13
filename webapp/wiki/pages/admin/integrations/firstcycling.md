## Overview

The FirstCycling integration imports external cycling data into WorldDB so editors can work from a current race calendar, team list, rider list, and race results.

## Access

- Users with FirstCycling read access can view its task groups.
- Users with FirstCycling download permission can start sync jobs.

## Sync Types

- Race Calendar sync imports or refreshes races for the selected season.
- Teams sync imports or refreshes team records for the selected season.
- Riders sync imports or refreshes rider records for the selected season.
- Race Results sync imports completed race results for races overlapping the selected date range.

## Running a Sync

1. Open Settings → External Data Integrations → Manual Syncs.
2. Choose an FC tab and select its season year and gender. Teams and Riders also accept an optional record limit.
3. For FC Race Results, enter the start and end dates.
4. Select Start Sync and open History to follow the run.

> Available season and gender combinations come from the application `SEASONS` configuration.

## History

Filter by provider, status, origin, priority, sync type, or season. Select a group to review its counts, status message, and individual task outcomes. Users with external call group read access can also see shared queue groups for the selected namespace and global groups.

> A queued or running sync may still be processing in the worker. Wait for the status to change before running the same sync again.

## Scheduler

Use the **Job** tab to view scheduled jobs and select **Edit** to update one. To add a job, open **Create New Job**, choose a type, and give the job a name. Enable it to run daily at its saved time. The worker timezone determines the daily run time and what Today and Yesterday mean.

Race Results uses the saved FirstCycling season, gender, and relative date range.
Rider Identity Check selects the first eligible riders with unknown PCS identity
from the chosen namespace. Its rider limit sets the PCS task group size.
Rider Activity Check uses one selected rider with a PCS slug and FirstCycling ID
and queues one group for each provider.

Use **Run Now** to try the current scheduler form values and review the outcome
in History. Use **Save Job** to persist the settings.
