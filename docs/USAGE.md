# Using MacIntent

Run commands from the repository root after completing [Setup](SETUP.md).

## One request

1. Start `./scripts/macintent` and open the printed literal `127.0.0.1` address.
   Choose an unused port rather than stopping an existing instance.
2. Read the exact privacy/capability disclosure and choose **Connect**. Startup
   checks local prerequisites and prepares local speech recognition. It does
   not activate the microphone or make a cloud planning request.
3. Click **Record**. Stay quiet while **Preparing**, then speak only after
   **Listening**. Pause naturally to finish. The capture limit discards rather
   than truncates; the microphone button cancels/discards during preparation
   and recording, not manual stop-and-transcribe.
4. Review the unchanged original transcript. Add explicit corrections if needed.
   Up to eight ordered notes totaling 2,048 Unicode characters accompany the
   original; they are not silently merged or carried into later requests.
5. **Send** submits this recording's original plus notes to Groq. A validated
   read-only listing may run after planning. An explanation is inert; make a
   new complete request if clarification is needed.
6. Review exact paths and full content/code before each fresh write/run approval.
   Deletion is permanent—not Trash. Python creation does not approve execution.
   Approval IDs are one-use, request-bound and expiring; they are not broker tickets.
7. For workflows, first review the full plan, then approve each effect separately.
   Failure, decline or cancellation stops later steps; earlier effects remain.
8. Inspect the observed outcome. A safe schema does not guarantee that the model
   understood your intent or preserved literal text; exact human review matters.

## Fresh requests and recovery

After safe cancellation or an ordinary error, **Record** and **New request** work
on the same page without reload/reconnect. Both remain locked while cleanup is
in progress. Record directly clears the old view and starts a fresh request ID;
it never retries the previous request. Recreating stopped resources may take
longer than an already-ready recording.

Switching browser tabs or applications does not disconnect you. If the browser
marks the page hidden, active recording/planning/approval/execution stops safely.
Settled transcript review/results survive a short switch. A longer browser sleep
can expire the 15-second runtime lease and clear the old request. Returning
restores availability for that same consenting page and explains the cleared
view; it does not load models, record, send or approve automatically.

Explicit **Disconnect**, actual page departure or server shutdown requires a new
deliberate connection. Unknown effects, audit/contract faults or cleanup failures
stay blocked: stop and inspect before deliberately restarting. There is no rollback.

## Guided scenarios

The docked **Test scenarios** sidebar reserves space rather than covering the app.
Its references scroll independently, and its actual Preparing/Listening/Mic-off
cue stays visible. Narrow screens dock the reference pane below the main area.

Use a fresh unused synthetic folder name. References are not existence-checked
and never fill a request, enter planner context, record, submit, repair or approve.
Manual verdicts are in-memory notes, not automatic grading or evidence of effects.
Do not replay an unknown action or reset existing fixtures to force a pass.

## Limits and privacy

All workspace actions stay below `demo/workspace/`. There is no overwrite,
recursive/directory deletion, path escape, general shell or desktop automation.
Python is compute-only under separate exact approval and the supported OS
sandbox, with CPU/wall/memory/output bounds and no unrestricted fallback.

Audio stays local. Submitted text and review notes go to Groq; its retention
terms apply. Existing file contents, listings, script output, audit and credentials
are not uploaded automatically. Current-request speech/source/output are not
stored in browser storage or default logs. Approved files and metadata audit persist.

The server caches public assets at startup. Finish/cancel work and disconnect
before restarting an owned instance to load code changes. Reloading a page alone
does not update a running server. See [Security](../SECURITY.md).
