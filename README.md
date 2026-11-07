# qoder-acp — run Hermes Agent on a Qoder subscription

A **Hermes model-provider plugin** that drives the official `qodercli` binary in
**ACP mode** (Agent Client Protocol over stdio). Qoder publishes no
OpenAI-compatible endpoint and there is no API key on the Hermes side, so the
signed-in CLI subprocess is the only door in.

**Qoder is only the model. Hermes owns every tool.**

```
Hermes agent loop
   |  builds the prompt + Hermes' own tool schemas (rendered as text)
   v
qodercli --acp --tools ""            <- stdio JSON-RPC (ACP)
   |  initialize -> session/new -> session/set_model -> session/prompt
   v
model answers; tool calls come back as <tool_call>{...}</tool_call> in the text
   |  Hermes parses the block, applies its own approvals + sandbox, executes
   v
Tool: result appended -> next session/prompt
```

Qoder's own tools are switched off (`--tools ""`), so nothing the model asks for
runs inside Qoder. Hermes runs it, under Hermes' permission prompts.

> **Why ACP and not a `base_url`:** Qoder publishes no OpenAI-compatible chat
> endpoint, but `qodercli --acp` speaks the Agent Client Protocol — the same
> surface Hermes' bundled `copilot-acp` provider drives. This plugin is the
> Qoder analogue of it.

---

## Contents

- [1. Requirements](#1-requirements)
- [2. Install](#2-install)
  - [2.1 Install & sign in the Qoder CLI](#21-install--sign-in-the-qoder-cli)
  - [2.2 Drop the plugin into Hermes](#22-drop-the-plugin-into-hermes)
  - [2.3 Restart the gateway — required](#23-restart-the-gateway--required)
- [3. Verify](#3-verify)
- [4. Use](#4-use)
- [5. Models](#5-models)
- [6. Context window](#6-context-window)
- [7. Environment overrides](#7-environment-overrides)
- [8. Troubleshooting](#8-troubleshooting)
- [9. Files](#9-files)
- [10. Uninstall](#10-uninstall)
- [Disclosure](#disclosure)

---

## 1. Requirements

| Requirement | Notes |
|---|---|
| Hermes Agent | `manifest_version: 2` model-provider plugins |
| OS | linux, macos, windows |
| Python | 3.10+ (whatever Hermes already runs on) |
| Qoder CLI | `qodercli` **1.1.84+**, signed in as the same user Hermes runs as |

Nothing else. The `qodercli` subprocess owns its own auth — Hermes never reads
or copies Qoder credentials.

---

## 2. Install

### 2.1 Install & sign in the Qoder CLI

```bash
curl -fsSL https://qoder.com/install | bash
qodercli login                       # browser device flow
# headless instead:  export QODER_PERSONAL_ACCESS_TOKEN=...    # your token
```

Verify — this is the gate that actually breaks installs:

```bash
qodercli --version                    # 1.1.84 or newer
qodercli --acp --tools "" < /dev/null # starts with no "unknown option" error
```

Two facts about the CLI that decide the whole design:

- **`--acp` is hidden from `qodercli --help`.** It works. But Hermes' generic
  `--acp` probe shells `--help`, finds nothing, and reports "unsupported" — so
  this plugin writes the spawn directly instead of probing.
- **`--tools ""` disables every built-in tool.** With its real inventory empty,
  the model tends to conclude "no tools available" and answer with prose — which
  is why the plugin supplies its own tool-call preamble (see
  [Troubleshooting](#8-troubleshooting)).

If `qodercli` is not on `PATH` after install, the installer left it at one of:

```bash
~/.local/bin/qodercli
~/.qoder/bin/qodercli/qodercli
```

The plugin scans both. Pin it explicitly if you prefer:

```bash
export QODER_CLI_PATH=$HOME/.local/bin/qodercli
```

### 2.2 Drop the plugin into Hermes

`HERMES_HOME` defaults to `~/.hermes`. The directory name **must** be
`qoder-acp` — that is the provider name Hermes keys on.

```bash
git clone https://github.com/aruliazmi/hermes-qoder-acp.git
cd hermes-qoder-acp

DEST="${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/qoder-acp"
mkdir -p "$(dirname "$DEST")"
cp -r . "$DEST"
rm -rf "$DEST/.git"
```

Or symlink, if you want to edit the checkout in place:

```bash
DEST="${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/qoder-acp"
mkdir -p "$(dirname "$DEST")"
ln -s "$PWD" "$DEST"
```

### 2.3 Restart the gateway — required

Provider discovery runs **once per process**. A plugin created after the gateway
started is invisible to `/model` until you restart. The same applies to **code
edits**: the module is already imported, so editing `qoder_acp_client.py`
mid-session keeps running the OLD code even though `__init__.py` shows the new
one.

```bash
hermes gateway restart      # from a real shell, NOT from inside a Hermes turn
```

Restarting Hermes from inside a Hermes session is deliberately blocked — the
SIGTERM races the child and you get a half-dead gateway. Confirm the live code
after a restart:

```bash
cd "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/qoder-acp"
python3 -c "import qoder_acp_client as m; print(m._resolve_args())"
```

---

## 3. Verify

```bash
cd "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/qoder-acp"
python3 smoke_test.py
```

Five checks, in order:

1. the profile registers under `qoder-acp` + its aliases
2. the `qodercli` binary resolves
3. an authenticated `session/new` lists models
4. a plain completion round-trips (`PONG`)
5. a **tool call** comes back as `<tool_call>` and parses into `tool_calls`

Exits non-zero on the first failure. If check 5 is intermittent on your machine,
see [Troubleshooting](#8-troubleshooting) — that failure mode is understood.

Debug the raw prompt/response pair with:

```bash
export HERMES_QODER_ACP_DEBUG_DIR=/tmp/qdbg
```

Each request then drops `<ts>.prompt.txt` / `.response.txt` / `.reasoning.txt`.

---

## 4. Use

One-off:

```bash
hermes chat -q "Summarize README.md" --provider qoder-acp --model dfmodel
```

In-session:

```text
/model dfmodel --provider qoder-acp
/model qoder                        # alias
/model                              # picker -> "Qoder CLI (ACP)"
```

As the default, in `~/.hermes/config.yaml`:

```yaml
model:
  provider: qoder-acp
  default: dfmodel
```

`--model` accepts a Qoder **slug** (`dfmodel`, `qfmodel`, ...), a friendly
**alias** (`deepseek-flash`, `kimi`, `qwen-max`, ...), or the pretty name
(`DeepSeek-Flash`). Aliases and pretty names are resolved to slugs by
`qoder_catalog.resolve_model()`; the wire only accepts slugs.

---

## 5. Models

The live list comes from `session/new` (Qoder's `configOptions.models`). The
fallback list in `qoder_catalog.py` only covers the case where that probe cannot
run. Slugs are opaque; the human names below are what `session/new` reports.

| Slug | Reported name | Reasoning |
|---|---|---|
| `auto` | Auto | — |
| `ultimate` | Ultimate | yes |
| `fast` | Fast | — |
| `efficient` | Efficient | — |
| `smodel` | Sonus | yes |
| `cmodel` | Cantus | yes |
| `qmodel_38max` | Qwen3.8-Max | yes |
| `qfmodel` | Qwen3.8-Flash | yes |
| `kmodel_latest` | Kimi-K3.1-Pro | yes |
| `kmodel` | Kimi-K2.8-Preview | yes |
| `gmodel` | GLM-5.3 | yes |
| `gfmodel` | GLM-5.3-Flash | yes |
| `dmodel` | DeepSeek-V4-Pro | yes |
| `dfmodel` | DeepSeek-Flash | yes |
| `mmodel` | MiniMax-M3.1-Flash | yes |

Friendly aliases: `default`/`smart` → `auto`, `pro` → `ultimate`,
`cheap` → `efficient`, `qwen`/`qwen-flash` → `qfmodel`,
`qwen-max`/`qwen3.8` → `qmodel_38max`, `kimi`/`kimi-pro`/`kimi-k3` → `kmodel_latest`,
`glm` → `gmodel`, `glm-flash` → `gfmodel`, `deepseek`/`deepseek-pro` → `dmodel`,
`deepseek-flash`/`flash` → `dfmodel`, `minimax` → `mmodel`.

Reasoning effort is **not** part of the model id here — ACP model selection is a
single `session/set_model` call.

---

## 6. Context window

Qoder's `session/new` advertises **no** context window (only mode + model), so
Hermes' generic resolver falls back to 256K and would compact far too early.
Qoder exposes up to **1M** on supported models, and the only knob that raises it
is the CLI flag `--context-window <size>`.

Both sides must agree:

```python
# qoder_catalog.py — what Hermes BELIEVES
_DEFAULT_CONTEXT_WINDOW = 1_000_000      # env: HERMES_QODER_CONTEXT_WINDOW

# qoder_acp_client.py, in _resolve_args() — what qodercli is TOLD
args += ["--context-window", ctx]        # ctx = 1000000
```

- Miss the first: Hermes thinks the window is small and compacts too early.
- Miss the second: Hermes sends more history than the session really holds.

Effective compaction point = `context_window × compression.threshold`
(default `0.50`) → **500,000 tokens** at 1M. Confirm with:

```bash
python3 -c "from agent.model_metadata import get_model_context_length as g; print(g('dfmodel', provider='qoder-acp'))"
```

Non-restart override for the Hermes-side number only:

```bash
hermes config set model_overrides.qoder-acp._default.context_window 1000000
```

(The CLI flag still needs a restart.)

---

## 7. Environment overrides

| Variable | Purpose |
|---|---|
| `HERMES_QODER_ACP_COMMAND` | Path to the `qodercli` binary (else `QODER_CLI_PATH`, else PATH scan) |
| `HERMES_QODER_ACP_ARGS` | Full argv tail, shlex-split (default: `--acp --tools ""`) |
| `HERMES_QODER_ACP_EXTRA_ARGS` | Appended after the default args |
| `HERMES_QODER_ACP_TIMEOUT` | Per-request wall-clock seconds (default `900`) |
| `HERMES_QODER_ACP_TOOL_RETRIES` | Extra in-session nudges when a tool was offered but the model answered with prose (default `2`, `0` disables) |
| `HERMES_QODER_ACP_CWD` | Working directory for the ACP session (default: a temp dir) |
| `HERMES_QODER_ACP_DEBUG_DIR` | Dump every prompt/response/reasoning triple here |
| `HERMES_QODER_CONTEXT_WINDOW` | Context window declared to Hermes and passed to the CLI (`0` → fall back to 200K) |

---

## 8. Troubleshooting

**`-32000 Authentication required` (`authMethods: [qodercli-login]`)**
`qodercli` is installed but not signed in **as the user Hermes runs as**. Run
`qodercli login` in that account, or set `QODER_PERSONAL_ACCESS_TOKEN`.

**`qoder-acp` does not appear in `/model`**
Discovery is once-per-process. Restart the gateway
([2.3](#23-restart-the-gateway--required)). Confirm the directory is named
exactly `qoder-acp`.

**Edits to the client have no effect**
The module is imported once. Restart, then check with the `_resolve_args()`
snippet in [2.3](#23-restart-the-gateway--required).

**Model answers with prose ("I can't run that, no tools available") instead of a
tool call** — the known-hard part of any ACP provider, and it has a real cause:
`--tools ""` leaves Qoder with an empty tool inventory, and `qodercli` **injects
its own agent system prompt** on top of the session prompt, which lets the model
narrate its (empty) capabilities rather than call the tool from the text you
supplied. Measured on `dfmodel`, the same prompt misses roughly **1 in 6** runs.

This is handled in two layers:

1. **The preamble** declares the tool-call contract plainly and never mentions
   MCP or "no tools" — naming the empty inventory is what makes the model talk
   about it.
2. **The client retries.** When a request carries tools and the reply has no
   `<tool_call>`, the client re-prompts *in the same session* with a one-line
   nudge. Tune with `HERMES_QODER_ACP_TOOL_RETRIES` (default `2`; `0` disables).
   Measured: 5/6 → 6/6 tool calls with retries on.

Retries only fire when tools were offered, and never for plain chat turns.

If you still see misses, capture and measure:

```bash
# 1. capture the exact prompt + response
export HERMES_QODER_ACP_DEBUG_DIR=/tmp/qdbg
```

```bash
# 2. measure it — the same prompt, several times
cd "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/qoder-acp"
for i in 1 2 3 4 5; do
  python3 smoke_test.py 2>&1 | grep -E 'tool call parsed|completion round-trip'
done
```

**`qodercli` not found after install** — the installer writes
`~/.local/bin/qodercli` and `~/.qoder/entry/qoder`; the plugin scans both. Point
at it with `QODER_CLI_PATH` otherwise.

**Wrong working directory / files written to a temp dir** — the session runs in
`HERMES_QODER_ACP_CWD`, or a temp dir when unset. Hermes owns the filesystem
anyway: Qoder's `fs/*` requests are answered against the session cwd, and
permission requests are refused.

---

## 9. Files

```
plugin.yaml            kind: model-provider, manifest_version: 2
__init__.py            QoderACPProfile + register_provider()
qoder_acp_client.py    the ACP stdio shim — the whole request path
qoder_catalog.py       slugs, aliases, capabilities, context window
smoke_test.py          5 standalone checks
LICENSE                MIT
```

Notes for anyone adapting this to another ACP CLI:

- The client module is **prefixed** (`qoder_acp_client.py`, not `client.py`):
  the plugin dir shares `sys.path` with Hermes core, and a bare `client`/`catalog`
  name collides and breaks the import.
- The class sets `HERMES_SKIP_TRANSPORT_WRAP` / `HERMES_SKIP_ASYNC_WRAP` — the
  shim is already a complete client; Hermes must not wrap it.
- Assistant turns that issued a tool call have `content=None`; Hermes' generic
  formatter would drop both the turn and its `<tool_call>`. `format_messages_as_prompt()`
  re-emits the calls so a replayed tool loop stays coherent.

---

## 10. Uninstall

```bash
rm -rf "${HERMES_HOME:-$HOME/.hermes}/plugins/model-providers/qoder-acp"
hermes gateway restart
```

Then remove any `model.provider: qoder-acp` from `~/.hermes/config.yaml`.

---

## Disclosure

Inference is routed through Qoder's official `qodercli` and counts against
whatever Qoder plan that binary is signed in to. Automating a subscription client
may be subject to Qoder's terms for your account. Independent community plugin —
not affiliated with Qoder or Nous Research.

## License

MIT — see [LICENSE](LICENSE).
