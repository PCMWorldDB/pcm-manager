# FirstCycling Activity Monitor integration

`activity_profile.py` is a pure, offline normalizer for an already obtained
FirstCycling rider JSON payload containing `description` and `allResults`. It
does not call FirstCycling and does not write to the monitor database.

`normalize_activity_payload(...)` validates the response rider ID against the
selected namespace rider's expected FC ID before mapping result rows to
`ActivityMonitorResult`-shaped dictionaries. Invalid responses and ID
mismatches return explicit statuses with no rows. A successful response is
always marked `partial` coverage: no recent FC result must never be treated as
proof of inactivity.

PCM Manager's existing `FirstCyclingClient.get_rider()` uses the configured
`rider/{id}.json` endpoint for its application data sync. The Activity Monitor
normalizer targets the separate `/dora/rider/{id}.json` `allResults` shape used
by the Arie Activity Monitor. A future network adapter must establish that
endpoint's availability and error handling before calling this normalizer.
