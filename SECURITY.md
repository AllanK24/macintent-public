# Security and privacy

MacIntent is a supervised local application, not an unrestricted computer-control
agent. Review every proposed action and do not expose the server to a network.

## Boundaries

- The web server binds to literal loopback. Commands require its session token,
  page identity, matching origin and bounded, closed JSON bodies.
- Audio is local and memory-only. Recording requires an explicit click and ends
  before transcription, planning or approvals. Bounded capture discards on its
  limit rather than submitting truncated speech.
- Only deliberately submitted request text and ordered review notes go to the
  fixed Groq planning endpoint. No silent provider fallback or history upload.
- Model output is untrusted. Closed schemas, workspace path/identity checks,
  exclusive creation and exact expiring one-use approvals precede effects.
- Workspace tools cannot target source, credentials, model files, runtime state
  or audit data. File deletion is permanent and single-file only.
- Python creation is not execution. Running requires its own captured-source
  approval, identity revalidation, resource limits and supported compute-only
  OS sandbox. Standard-library imports or a working directory alone are not isolation.
- Metadata audit intent precedes effects and finalized outcomes precede later
  workflow steps. No rollback, automatic effect retry or unrestricted fallback.

## Cancellation and returning pages

Cancel and ordinary failures unlock a new request only after owned cleanup.
Stopped resources are not reused and old stop events are never cleared.
Switching away cancels active work but does not revoke that page's connection.
The independent lease still releases sleeping/abandoned runtime resources.
Returning may reclaim only that same page's existing consent, not restart work.
Explicit Disconnect, real departure, shutdown, another owner or a safety fault
prevents reclamation as appropriate. Stale/replayed approvals cannot execute.

An action already dispatched may complete even after cancellation. Stop on
unknown effects, audit faults or cleanup failures; inspect the workspace and
audit before deliberately restarting. Do not blindly retry a mutation.

## Data handling

Do not commit credentials, audio, private speech, workspace files or audit databases.
The credential importer uses protected local storage. Keep recordings and raw
provider errors out of bug reports. If reporting an issue, share the visible
closed error code and a synthetic reproduction rather than private content.

Application confinement and project-local dependencies are not a complete OS
sandbox. Python runs use a separate enforceable sandbox; macOS may still maintain
its own permission or system-managed metadata. Groq's retention terms apply to
submitted text.
