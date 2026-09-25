# Offline isolation

Status: an opt-in macOS offline app and terminal chat are implemented. Start
with `karospace-agent app --offline` or `karospace-agent chat --offline`.
Ordinary Claude/Codex sessions continue to use cloud models. The standalone
`isolation-check` diagnostic alone does not switch a session offline.

The intended restricted-data mode must process data without an OpenAI or
Anthropic connection, including connections made by dependencies and descendant
processes. Sanitizing tool replies remains useful, but is not OS isolation.

## Enforced launch

`offline.py` starts a separate process under a fixed Seatbelt policy. Before it
opens a dataset or loads a model, that same process proves network denial,
inherited child-process denial, and blocked reads/writes outside its grants.
Startup fails if any check fails. It never retries without the sandbox or with
a cloud provider. Unsupported operating systems refuse this mode.

The app runs an already-downloaded MLX model in process, using eager CPU kernels.
GPU and JIT compilation are disabled to avoid compiler services and code caches.
The launcher neither downloads a model nor connects to an existing Ollama server.
Model tokenizers use local files only and remote model code is disabled.
Models up to roughly 600 million parameters are dequantized in memory for
faster CPU kernels; larger models remain quantized to bound added memory use.

The packaged desktop app reuses the normal HTML/CSS interface. Its separate
App Sandbox UI helper has no network client/server entitlements and proves a
denied connection before loading the bundled page. In-process WebKit WebView
renders it without a localhost server; CSP denies connections/remote resources,
and navigation/new-window delegates prevent external browsing. This deprecated
renderer is a current-Mac compatibility choice requiring revalidation after OS
updates: modern WKWebView helper processes failed in the restricted prototype.
The UI can access its own app container; private browsing is enabled. OS window
services remain part of the trusted desktop, not a machine-wide boundary.

A coordinator chooses the input path, starts the separately confined model/UI
processes, then denies its own networking and filesystem data access. It relays
private pipes only. Closing the UI or choosing Stop terminates the worker and
its scientific process group. The legacy CLI Tk surface and terminal chat keep
the original Seatbelt policy; external terminal apps are outside our control.
The packaged shared interface is the preferred desktop surface on this Mac.

Read grants cover installed runtime/code, the selected local model and the input
chosen at launch. HF model snapshot symlinks grant their resolved files individually.
Writes are confined to a new owner-only session folder, containing output, history,
HOME, temporary files and caches. Credentials, proxy settings, cloud endpoints,
Python paths and dynamic-library overrides are not inherited. A root-directory
read (the directory itself, not its subtree) lets dyld initialize.
One separate short-lived directory under `/private/tmp` permits only the
synthetic isolation-check files and socket. Its short path avoids macOS's Unix
socket path-length limit when the normal session directory is in a long home
path. It is removed when the session ends; network denial still applies there.

The local model may invoke only an explicit set of registered KaroSpace tools.
GEO acquisition is absent. Schema validation and `privacy.Boundary` still apply
to tool calls/results. Every scientific child inherits the OS policy, so analytics
that need internet access fail locally. Generated viewers are never auto-opened.

## Standalone diagnostic

`karospace-agent isolation-check` launches a synthetic Python probe under macOS
`sandbox-exec`, with network operations and Mach service lookup denied. The
profile applies to descendants. The diagnostic proves IPv4/IPv6 TCP and UDP,
loopback and Unix socket denials in both the original and a child process,
while checking that ordinary local file access still works. It opens no dataset,
loads no model, and does not run provider authentication. Only permission-denied
errors count; connection refusal, timeouts, unsupported sockets, malformed output
and a missing sandbox all fail the check. There is no unprotected fallback.

The standalone diagnostic is process-scoped and changes no machine-wide firewall
settings. Its minimal probe profile allows local file access; the offline app uses
the additional filesystem restrictions described above. macOS Seatbelt is a
deprecated interface, so capability checks remain mandatory after OS updates.

## Scope and remaining work

This confines the app's process tree, not the entire computer. Other applications,
screen sharing, filesystem backup/sync agents, a compromised OS/runtime, or a user
manually copying outputs elsewhere remain outside the boundary. The storage
policy and donation agreement still matter; no software check establishes consent.

CPU inference is slower than hosted or GPU inference. Use a suitable small local
MLX model. Additional native dependencies such as an independently installed R
runtime may need explicit read grants; failures must be resolved by qualifying
that runtime, never by relaxing network denial. The shared offline interface
provides local previews/history/recovery over pipes; generated viewers stay on
disk and are not opened by the offline UI.
For data requiring machine-level separation, a managed offline machine or VM
without a network adapter remains a stronger boundary. Linux/Windows backends,
isolated GPU inference and that VM deployment are future work.
