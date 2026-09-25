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

The offline window uses native Tk widgets and plain text, not WebKit, HTML or a
localhost web server. It has no URL-opening actions. Its only allowed Mach service
is WindowServer; preferences, pasteboard, launch services and network proxies
remain blocked. Closing the window exits the confined process and the launcher
terminates its scientific process group. Terminal chat has the same process
policy, although the user's external terminal application is outside our control.

Read grants cover installed runtime/code, the selected local model and the input
chosen at launch. HF model snapshot symlinks grant their resolved files individually.
Writes are confined to a new owner-only session folder, containing output, history,
HOME, temporary files and caches. Credentials, proxy settings, cloud endpoints,
Python paths and dynamic-library overrides are not inherited. A root-directory
read (the directory itself, not its subtree) lets dyld initialize.

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
that runtime, never by relaxing network denial. The simple offline UI does not
provide browser-based viewer interaction or the cloud UI's recovery controls.
For data requiring machine-level separation, a managed offline machine or VM
without a network adapter remains a stronger boundary. Linux/Windows backends,
isolated GPU inference and that VM deployment are future work.
