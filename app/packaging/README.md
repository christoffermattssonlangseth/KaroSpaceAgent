# Packaging `KaroSpace Agent.app`

For the separate offline-only app, see [Offline desktop app](#offline-desktop-app).
The existing build described below opens the cloud-provider UI.

Builds the `karospace-agent app` chat interface into a signed, notarized macOS
`.app` that opens with a normal double-click.

## What the `.app` is (and isn't)

It bundles the **chat UI + local web server + Python + all Python deps** into one
double-clickable window, plus the **`karospace-companion`** binary (see below). It
is a polished *front door* to the agent — not a self-contained scientific stack.
On every Mac that runs it, these must already be installed (the app orchestrates
them locally; the data never leaves the machine, so they can't be bundled
server-side):

- the **`claude` CLI** (`npm install -g @anthropic-ai/claude-code`) — the Agent SDK spawns it;
- **`karospace`** on PATH (Python; users keep it current with `pip install -U`);
- a **permitted credential** (Console sign-in or API key — see `karospace-agent auth`).

The app rehydrates PATH from your login shell on launch (`pathfix.py`), so it
finds these even though Finder starts it with a bare PATH.

### The bundled companion (and its rebuild coupling)

`karospace-companion` builds the spatial neighbor graph, which the agent now
computes **by default**, so the `.app` ships it inside the bundle instead of
asking each user to build Rust. `companion_bin()` resolves it automatically:
`KAROSPACE_COMPANION` override → the bundled copy (`_bundled_companion()`) → a
dev checkout's `../../KaroSpaceCompanion/target/release/`.

Two consequences to know:

- **It couples the companion's release to the app's.** Ship a new companion →
  rebuild + re-sign + re-notarize the `.app`. `karospace` is *not* coupled: it's
  Python on PATH and updates independently via pip. So only a **companion** change
  forces an app rebuild — and only when the companion's own behavior changes, not
  on every `karospace` bump (the app depends on its narrow `prepare` contract).
- **The binary is host-arch-specific.** `build_app.sh` bundles whatever
  `../../KaroSpaceCompanion/target/release/karospace-companion` was built for
  (Apple Silicon vs Intel). For a universal `.app`, build a universal2 companion
  and point `KAROSPACE_COMPANION_SRC` at it before building (see Universal2 below).

If the binary is absent at build time the spec builds **without** it (prints a
notice); the app still runs and degrades to direct export (no neighbor graph).
There is no automatic version compatibility check — `karospace` exposes no
`--version` to diff against — but the preflight prints the bundled companion's
version so drift is at least visible in the startup notes.

Build the companion first (once), from the companion repo:

```bash
cd ../../KaroSpaceCompanion && cargo build --release
```

## Build

From `app/`, in a venv with the app installed:

```bash
pip install -e '.[app]'

# unsigned local build (fine for trying it on your own machine):
./packaging/build_app.sh
# → dist/KaroSpace Agent.app   (open with right-click → Open the first time)
```

## Sign + notarize (for wider distribution)

You need an Apple Developer Program membership: a **Developer ID Application**
certificate in your Keychain, plus notarization credentials (an App Store
Connect API key `.p8`, recommended, or an app-specific password).

One-time — store the notary credentials:

```bash
xcrun notarytool store-credentials karospace-notary \
  --key /path/to/AuthKey_XXXX.p8 --key-id XXXX --issuer <issuer-uuid>
```

Then build signed and notarize:

```bash
export KAROSPACE_CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"
export KAROSPACE_NOTARY_PROFILE="karospace-notary"

./packaging/build_app.sh     # signs during the PyInstaller build
./packaging/notarize.sh      # re-signs deep, submits to Apple, staples the ticket
```

`spctl --assess` at the end should report *accepted*. Now zip or `.dmg` the
`.app` and distribute it — it opens with a plain double-click on any Mac.

## Files

| File | Role |
| --- | --- |
| `launch.py` | Frozen entry point — runs `karospace-agent app`. |
| `KaroSpaceAgent.spec` | PyInstaller spec (collects the page, uvicorn/pywebview submodules, builds the `.app`). |
| `entitlements.plist` | Hardened-runtime entitlements a frozen-Python app needs. |
| `build_app.sh` | Icon + PyInstaller build (unsigned, or signed if `KAROSPACE_CODESIGN_IDENTITY` is set). |
| `notarize.sh` | Deep-sign → notarize → staple. |

## Universal2 (Apple Silicon + Intel)

`build_app.sh` builds for the host arch. For a universal binary, install a
universal2 CPython and set `target_arch='universal2'` in the `EXE(...)` block of
the spec — every bundled wheel must also ship universal2 or the build fails. The
bundled companion must match too: build a universal2 `karospace-companion` (e.g.
`lipo`-merge an Apple-Silicon and an Intel `cargo build --release`) and point
`KAROSPACE_COMPANION_SRC` at it so the spec bundles the fat binary instead of the
host-arch one. Single-arch (Apple Silicon) is usually enough for a lab audience.

## Offline desktop app

On macOS Apple Silicon, with the offline runtime and model already installed:

```bash
cd app
python packaging/build_offline_app.py
open "dist/KaroSpace Offline.app"
```

Double-click **KaroSpace Offline.app**, then choose **Choose dataset…** or
**Start chat**. The chooser accepts `.h5ad`, `.rds`, `.RData`, and `.zarr` paths.
Selecting a dataset grants access for that session; choose a different input
by closing and reopening the app. The chat's **Stop and close** ends the session.

Use **Edit → Paste** or **⌘V** in the composer. `/tools` lists registered local
KaroSpace capabilities without relying on model-generated answers. `/inspect`
runs schema and structure inspection on the selected `.h5ad`/`.zarr` through
the existing privacy boundary and history; a default dataset launch offers this
inspection for review. It does not export or modify the dataset. Custom opening
requests still go to the model. R inputs and multi-table SpatialData may need
additional options through the tool loop.

A small (~0.5B) CPU model is not a reliable autonomous viewer planner: live
checks have shown invented tool names, incorrect arguments, and "Done." replies
with no tool call. The launcher therefore prefers the most capable
already-downloaded model (a ~3-4B instruct model, e.g.
`Qwen3-4B-Instruct-2507-4bit`), falling back to the 0.5B only when nothing
larger is present. When a dataset is selected, its schema is inspected
automatically (schema only, no data values) before the model's first turn, so it
starts grounded rather than guessing. A 4B model reasons far better but is slow
on CPU — it may take several minutes per turn, and a single tool-use check can
exceed three minutes on this setup. The registered export/preprocessing tools
are available, but a successful greeting or `/tools` response does not establish
that a complete model-driven export works. Check actual tool/history results.

This is a local bundle, ad-hoc signed for this Mac. It reuses the normal app's
HTML/CSS interface, with offline labels and private pipes replacing HTTP/SSE.
The coordinator includes a dataset chooser and fixed offline entry point. Its configuration
pins the existing source checkout, Python runtime and local model by absolute
path. No models, research data, credentials or scientific dependencies are
copied into the bundle. Moving the `.app` to Applications is supported on this
Mac; moving or deleting its runtime/model/checkout requires rebuilding. A
portable distribution and Developer ID signing/notarization are separate work.

Use `--python /path/to/python` and `--model /path/to/model` to build against
different existing installations. The default uses `.venv-offline` and the
locally available model, as `karospace-agent app --offline` does. The build
requires Xcode command-line tools and never downloads dependencies or models.
Existing `.app` destinations are not overwritten; use a new `--output` path
when rebuilding, then replace the older app yourself.

The native chooser passes only a path; it does not read dataset contents. The
model/tool worker uses the existing verified Seatbelt sandbox. The separate UI
helper uses App Sandbox with no network client/server entitlements, and verifies
a denied connection before loading the page. CSP blocks connections and remote
assets; navigation/new-window delegates prevent external browsing. The
coordinator denies its own networking after launching its children. No localhost
server, cloud provider or login shell is used. Normal launches discard raw logs.

The UI deliberately uses WebKit's deprecated in-process WebView API: this Mac
could not start WKWebView's auxiliary content processes in the restricted setup.
This is a compatibility limitation requiring revalidation after macOS updates;
it is not a supported modern-WebKit replacement for wider distribution. The
process-scope limits in [offline isolation](../../docs/design/offline-isolation.md)
still apply. The UI can access its app container; private browsing is enabled.

Validate the built executable with a synthetic message through the real shared
composer, checking the on-screen window, reply and both processes' restrictions:

```bash
"dist/KaroSpace Offline.app/Contents/MacOS/KaroSpaceOffline" --smoke-test
codesign --verify --deep --strict "dist/KaroSpace Offline.app"
```

The smoke test uses the normal session location and never loads a selected
research dataset. Scientific tools have separate synthetic worker checks.
`karospace-agent chat --offline` remains the terminal alternative.
