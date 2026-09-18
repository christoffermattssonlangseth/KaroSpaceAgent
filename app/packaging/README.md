# Packaging `KaroSpace Agent.app`

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
