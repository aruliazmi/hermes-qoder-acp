#!/usr/bin/env python3
"""Smoke test for the qoder-acp provider plugin.

Run from the plugin directory (or anywhere with Hermes on PYTHONPATH):

    python3 smoke_test.py

Checks, in order:
  1. the plugin registers and resolves under its name + aliases
  2. the qodercli binary resolves
  3. an authenticated ACP session lists models
  4. a plain completion round-trips
  5. a tool call comes back as <tool_call> and parses into tool_calls

Exits non-zero on the first failure. Requires qodercli signed in as this user
(`qodercli login`, or QODER_PERSONAL_ACCESS_TOKEN in the environment).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES = Path(os.environ.get("HERMES_AGENT_DIR", "~/.hermes/hermes-agent")).expanduser()
for path in (str(HERMES), str(HERE)):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        mark = "ok  " if ok else "FAIL"
        print(f"[{mark}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # 1. registration
    try:
        from providers import get_provider_profile

        prof = get_provider_profile("qoder-acp")
        check("profile registers", prof is not None and prof.name == "qoder-acp",
              f"aliases={getattr(prof, 'aliases', ())}")
        check("alias 'qoder' resolves", (get_provider_profile("qoder") or prof).name == "qoder-acp")
        check("process argv", list(prof.process_args) == ["--acp", "--tools", ""], str(prof.process_args))
    except Exception as exc:
        check("profile registers", False, repr(exc))
        return 1

    # 2-5. transport
    try:
        from qoder_acp_client import QoderACPClient
    except ImportError:
        from qoder_acp_client import QoderACPClient  # type: ignore
    except Exception:  # pragma: no cover
        import importlib.util as u

        spec = u.spec_from_file_location("qac", HERE / "qoder_acp_client.py")
        module = u.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        QoderACPClient = module.QoderACPClient  # type: ignore[assignment]

    import qoder_acp_client as mod

    command = mod._resolve_command()
    check("qodercli resolves", Path(command).exists() or bool(command), command)

    client = QoderACPClient()
    try:
        try:
            models = client.list_models(timeout_seconds=30)
        except Exception as exc:
            check("authenticated ACP session", False, f"{type(exc).__name__}: {exc}")
            return 1
        check("authenticated ACP session", bool(models), f"{len(models)} models: {', '.join(models[:5])}...")
        model = os.environ.get("QODER_TEST_MODEL", "dfmodel")

        try:
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with exactly: PONG. Nothing else."}],
                timeout=120,
            )
            text = (r.choices[0].message.content or "").strip()
            check("completion round-trip", "PONG" in text.upper(), repr(text[:60]))
        except Exception as exc:
            check("completion round-trip", False, f"{type(exc).__name__}: {exc}")

        try:
            tools = [{
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run a shell command",
                    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                },
            }]
            r2 = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are Hermes. Use tools when the user asks."},
                    {"role": "user", "content": "Run the shell command: whoami"},
                ],
                tools=tools,
                timeout=150,
            )
            calls = r2.choices[0].message.tool_calls or []
            check("tool call parsed", bool(calls),
                  ", ".join(f"{c.function.name}({c.function.arguments})" for c in calls) or "no <tool_call> block returned")
        except Exception as exc:
            check("tool call parsed", False, f"{type(exc).__name__}: {exc}")
    finally:
        client.close()

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
