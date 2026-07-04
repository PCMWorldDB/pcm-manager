## Overview

The FirstCycling integration imports external cycling data into WorldDB so editors can work from a current race calendar, team list, rider list, and race results.

## Access

- Users need access to the Integrations area for FirstCycling to view this page.
- Users need run/download integration permission to start FirstCycling sync jobs.

## Selecting a Season

1. Open Settings, then select Integrations.
2. Select the FirstCycling provider tab.
3. Choose the season year and gender.
4. Select the season before running a sync.

> If no seasons are available, check the application `SEASONS` configuration before trying to sync data.

## Sync Types

- Race Calendar sync imports or refreshes races for the selected season.
- Teams sync imports or refreshes team records for the selected season.
- Riders sync imports or refreshes rider records for the selected season.
- Race Results sync imports completed race results for races overlapping the selected date range.

## Running a Sync

1. Select the sync view for the data you want to refresh.
2. Confirm the selected season.
3. For Race Results, enter a start date and end date.
4. Select Start Sync.
5. Refresh the page to follow queued or running work in Sync History.

## Sync History

Use Sync History to review when a job ran, who started it, the requested and synced counts, failed counts, and any status message.

> A queued or running sync may still be processing in the worker. Wait for the status to change before running the same sync again.
