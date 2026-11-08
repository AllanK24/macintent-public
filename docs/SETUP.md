# Setup

Run commands from the repository root. MacIntent targets **native Apple Silicon
macOS** and uses Python **3.12.14**. Python execution depends on that exact
managed runtime and the supported macOS sandbox; it fails closed if unavailable.
The native helper requires Apple's command-line developer tools/Swift SDK.

## 1. Project-local dependencies

```sh
./scripts/setup
```

Setup installs checksum-verified local uv and managed Python **3.12.14**, then syncs
the committed dependency lock into `.venv/`. It does not download models, read
a key, build/start the helper, activate a microphone or call Groq. Nothing is
installed globally and shell profiles are not changed.

For optional JavaScript/browser checks, install the pinned local Node tooling:

```sh
./scripts/setup --with-node
```

Project-local dependencies/configuration are not an OS sandbox. Do not put
credentials or private recordings in test fixtures.

## 2. Pinned local speech model

```sh
./scripts/download-model
```

This is an explicit multi-gigabyte public Hugging Face download into ignored
`models/parakeet-v2/`, pinned by revision and checked by size/hash. Custom model
code, other weight variants, remote inference and silent fallback are rejected.
Existing valid snapshots are reused; invalid/unfamiliar model contents are not
overwritten or repaired automatically. Inspect any interrupted partial download
before retrying. Model weights are not included in Git.

## 3. Build the recording helper

```sh
./scripts/build-native
```

The capture-only helper is built/ad-hoc signed under `.local/native/VoiceAgent.app/`.
Its bundle identifier is fixed for macOS microphone permission identity. Build
does not query or activate the microphone. No Accessibility, general Automation
or Full Disk Access permission is required. macOS permission acceptance remains
an operator-controlled step when recording is deliberately started.

## 4. Protected Groq credential

Put only your own Groq API key in a new root `api.txt`, set its permissions to
`0600`, then explicitly import it:

```sh
chmod 600 api.txt
./scripts/import-key
```

The importer validates private regular-file storage, migrates the key to
`.local/state/keys/groq-api-key` and removes the input after verification. It
never prints the key or makes a network request. Both paths are ignored by Git.
Never paste a key into source, screenshots, shell commands, issues or commits.

The planner uses the fixed Groq model and native schemas in source. Groq receives
only explicitly submitted text/review notes; its account/retention terms apply.

## 5. Start

```sh
./scripts/macintent
```

Open the printed literal `127.0.0.1` URL. Use an unused port rather than stopping
another instance. Connect checks prerequisites/prepares local Parakeet, while
Record, Send and each approval remain separate deliberate gestures. See
[Usage](USAGE.md) and [Security](../SECURITY.md).

## Development checks

Use the locked project-local environment. The unit checks use mocks and fresh
synthetic roots, not private workspace files or the microphone:

```sh
./scripts/dev uv run --locked --no-sync python -m pytest -q
./scripts/dev mise exec -- node --test tests/ui.test.mjs
```

An optional offline Chrome layout/control regression uses fully mocked fetches:

```sh
./scripts/dev mise exec -- node tests/browser-check.mjs --recovery-only --offline
```

That browser check requires Google Chrome at its normal macOS application path.
It checks actual DOM/CSS/JS wiring with test-only inline/CSP substitution; it is
not live HTTP/CSP, Safari-engine, microphone, Groq or action acceptance. No test
automatically grants recording or cloud/action permission.
