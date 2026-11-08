# MacIntent

Turn a spoken request into a reviewed, narrowly scoped action on your Mac.
MacIntent combines local speech recognition, Groq planning, and a local
approval-controlled execution broker in one dark-blue web interface.

**Requires an Apple Silicon Mac.** Audio stays local. Submitted text and review
notes go to Groq; review the privacy disclosure before connecting.

## What it does

- List directories and create folders or text notes.
- Copy a file or permanently delete one exact file.
- Create Python scripts and separately approve compute-only sandboxed runs.
- Review short workflows, with a fresh approval for each write or run.
- Keep guided scenario references open alongside recording and action review.

All file actions are confined to `demo/workspace/`. There is no overwrite,
general shell, unrestricted desktop control, or blanket approval.

## Use

After following [Setup](docs/SETUP.md), run from the repository root:

```sh
./scripts/macintent
```

Open **http://127.0.0.1:8093/**. Use an unused port if necessary, for example
`./scripts/macintent 8097`. Starting the server does not activate the microphone
or contact Groq.

1. **Connect** after reviewing the disclosure.
2. Click **Record**, stay quiet during Preparing, then speak after **Listening**.
3. Review the original transcript and any correction notes before **Send**.
4. Inspect exact paths, content or code and approve each write/run separately.
5. Read the observed outcome before starting another request.

Cancel and ordinary errors allow a fresh request after safe cleanup, without
refreshing or reconnecting. Switching tabs/apps does not disconnect your page;
hidden active work is stopped safely. No old recording, submission or approval
is automatically resumed. Unknown, audit or cleanup faults still require inspection.

## Privacy and safety

Parakeet transcribes on this Mac; Groq receives only the request text you submit
and its explicit review notes. Existing files, directory inventories, execution
output, audit data and credentials are not uploaded automatically. Groq's own
retention terms apply.

Audio, transcripts and generated output are not logged by default. Approved
files and metadata audit records intentionally persist. Cancellation is not
rollback, and an already dispatched effect may finish.

Python execution requires exact source/identity approval and the supported
compute-only OS sandbox: no user/workspace file I/O, network, subprocesses or
third-party packages. The app fails closed rather than using unrestricted execution.

See [Usage](docs/USAGE.md) and [Security](SECURITY.md) for limits and recovery.

## Development

Use the project-local environment and locked dependencies. Run commands from the
repository root; see [Setup](docs/SETUP.md) for development checks.

`src/` contains the current application, `web/` its interface, `native/` the
fixed-operation recording helper, and `tests/` the focused regression checks.
Models, credentials, runtime/cache data and workspace files are intentionally
excluded from Git.
