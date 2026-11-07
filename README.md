# qoder-acp — Qoder CLI as a Hermes model provider

Runs Hermes on a **Qoder** subscription by driving the official `qodercli`
binary in **ACP mode** (`qodercli --acp`) over stdio.

**Qoder is only the model. Hermes owns every tool.** Qoder's built-in tools are
switched off (`--tools ""`); Hermes' tool schemas travel into the prompt as
text, and the model returns calls as `<tool_call>{...}</tool_call>` blocks that
Hermes parses and executes with its own approvals and sandbox.

## Why ACP

Qoder published no OpenAI-compatible chat endpoint, so it cannot be a plain
`base_url`. But `qodercli --acp` speaks the standard **Agent Client Protocol**
over stdin/stdout, which is exactly the surface Hermes' bundled `copilot-acp`
provider already drives. This plugin is the Qoder analogue.

## Prerequisites

1. Qoder CLI installed and **signed in as the user Hermes runs as**:

   ```bash
   curl -fsSL https://qoder.com/install | bash
   qodercli login          # or: export QODER_PERSONAL_ACCESS_TOKEN=pt-...
   qodercli --version      # 1.1.84+
   ```

   `session/new` returns `-32000 Authentication required` until this is done.

2. Nothing else. The subprocess owns its own auth — Hermes never reads or copies
   Qoder credentials.

> `--acp` is a **hidden** flag: it works but is absent from `qodercli --help`.
> Hermes' generic `--acp` probe therefore reports "unsupported". This plugin
> does not use that probe.

## Use

One-off:

```bash
hermes chat -q "Summarize README.md" --provider qoder-acp --model DeepSeek-V4-Flash
```

In-session:

```text
/model DeepSeek-V4-Flash --provider qoder-acp
/model qoder                                    # alias
/model                                          # picker → "Qoder CLI (ACP)"
```

As the default, in `~/.hermes/config.yaml`:

```yaml
model:
  provider: qoder-acp
  default: DeepSeek-V4-Flash
```

## Models

The live list comes from `session/new` (Qoder's `configOptions`). The
`fallback_models` in `__init__.py` only cover the case where that probe cannot
run. Reasoning effort is not part of the model id here — set it with
`--reasoning-effort` / the session's own config, since ACP model selection is a
single `session/set_config_option` call.

## Environment overrides

| Variable | Purpose |
|---|---|
| `HERMES_QODER_ACP_COMMAND` | Path to the `qodercli` binary (else `QODER_CLI_PATH`, else PATH scan) |
| `HERMES_QODER_ACP_ARGS` | Full argv tail, shlex-split (default: `--acp --tools ""`) |
| `HERMES_QODER_ACP_EXTRA_ARGS` | Appended after the default args |
| `HERMES_QODER_ACP_TIMEOUT` | Per-request wall-clock seconds (default 900) |
| `HERMES_QODER_ACP_CWD` | Working directory for the ACP session (default: temp dir) |

## Disclosure

Inference is routed through Qoder's official `qodercli` and counts against
whatever Qoder plan that binary is signed in to. Automating a subscription
client may be subject to Qoder's terms for your account. Independent community
plugin — not affiliated with Qoder or Nous Research.
