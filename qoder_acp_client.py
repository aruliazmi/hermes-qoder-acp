"""Qoder ACP transport — qodercli is only the model, Hermes owns every tool.

One Hermes chat-completions request = one short-lived ``qodercli --acp`` session:

1. The ACP server is spawned with ``--acp --tools ""`` so Qoder's own
   read/edit/execute tools are OFF (it has no tools that collide with ours; it
   must not touch the host).
2. Hermes' system prompt + a rendered tool catalog + the flattened transcript
   travel in as one ``session/prompt`` text turn.
3. ``session/update`` notifications stream back as text / thought chunks. Tool
   calls come back INSIDE the text as ``<tool_call>{...}</tool_call>`` blocks
   (ACP has no OpenAI tool channel) and are parsed out and handed to Hermes.
4. Any server->client request (permission, fs) is answered on this client's
   side — file access is confined to the session cwd, permission is refused.

Note: ``--acp`` is a HIDDEN qodercli flag — it works but is absent from
``qodercli --help``, so Hermes' generic ``--acp`` probe reports "unsupported".
This client never calls that probe.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import shlex
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from agent.acp_openai_bridge import (
    completion_to_stream_chunks as _completion_to_stream_chunks,
    extract_tool_calls_from_text as _extract_tool_calls_from_text,
)
from agent.copilot_acp_client import (
    _FS_HANDLERS,
    _ROLE_LABELS,
    _build_subprocess_env,
    _effective_timeout,
    _model_selection_request,
    _render_message_content,
    _session_model_ids,
)
from agent.acp_openai_bridge import render_tool_bridge_sections as _render_tool_bridge_sections

logger = logging.getLogger(__name__)

PROVIDER = "qoder-acp"
ACP_MARKER_BASE_URL = "acp://qoder"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_TOOL_RETRIES = 2
AUTH_REQUIRED_CODE = -32000

# Where the qoder installer drops the real binary, in priority order.
_FALLBACK_BINARIES = (
    "qodercli",
    "qoder",
    "~/.local/bin/qodercli",
    "~/.qoder/bin/qodercli/qodercli",
    "~/.qoder/entry/qoder",
)


class QoderACPAuthRequired(RuntimeError):
    """qodercli is installed but not signed in where Hermes runs it."""


def _resolve_command() -> str:
    for var in ("HERMES_QODER_ACP_COMMAND", "QODER_CLI_PATH"):
        if value := os.environ.get(var, "").strip():
            return value
    for candidate in _FALLBACK_BINARIES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        if (found := _which(candidate)) is not None:
            return found
    return "qodercli"


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _resolve_args() -> list[str]:
    if raw := os.environ.get("HERMES_QODER_ACP_ARGS", "").strip():
        args = shlex.split(raw)
    else:
        # --tools "" disables every built-in tool; Qoder must not act on the host.
        args = ["--acp", "--tools", ""]
    # Qoder's ACP session/new advertises no context window (mode + model only), so
    # the CLI flag is the only knob that actually raises it. Without this the
    # session runs at Qoder's default even though Hermes believes 1M.
    if "--context-window" not in args:
        ctx = (os.environ.get("HERMES_QODER_CONTEXT_WINDOW") or "").strip()
        if not ctx:
            try:
                from .qoder_catalog import _DEFAULT_CONTEXT_WINDOW as _dcw
            except ImportError:
                from qoder_catalog import _DEFAULT_CONTEXT_WINDOW as _dcw
            ctx = str(_dcw)
        if ctx and ctx != "0":
            args += ["--context-window", ctx]
    if extra := os.environ.get("HERMES_QODER_ACP_EXTRA_ARGS", "").strip():
        args.extend(shlex.split(extra))
    return args


def _resolve_timeout(timeout: Any) -> float:
    env = os.environ.get("HERMES_QODER_ACP_TIMEOUT", "").strip()
    if env:
        with contextlib.suppress(ValueError):
            return float(env)
    return _effective_timeout(timeout)


def _has_tool_call(text: str) -> bool:
    """True when the reply carries a parseable <tool_call> block.

    Uses the same extractor Hermes uses, so the retry condition matches what the
    OpenAI surface will actually hand back.
    """
    if not text or "<tool_call" not in text:
        return False
    calls, _cleaned = _extract_tool_calls_from_text(text)
    return bool(calls)


def _resolve_tool_retries() -> int:
    """How many extra in-session nudge prompts to spend recovering a lost call."""
    env = os.environ.get("HERMES_QODER_ACP_TOOL_RETRIES", "").strip()
    if env:
        with contextlib.suppress(ValueError):
            return max(0, int(env))
    return DEFAULT_TOOL_RETRIES


# Sent as the next turn in the SAME session when a tool was offered, no call came
# back, and the model answered with prose instead. Kept terse on purpose: the
# session already holds the full contract, so this only names the miss.
_TOOL_RETRY_NUDGE = (
    "You answered in prose. The tool you need IS available — your own tool "
    "inventory is unrelated to it and is not the one that runs. Reply now with "
    "ONLY the <tool_call>{...}</tool_call> block, nothing else."
)


def _jsonrpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


# Hermes' bundled ACP preamble tells the CLI it has its own tools. Qoder runs
# with `--tools ""`, so its REAL tool inventory is empty — and the model, told
# about a tool channel that "does not exist", tends to conclude it cannot act and
# answers with prose ("I tried but the backend returned Tool not found") instead
# of emitting the call. Two hard rules for this preamble:
#
#   1. Never mention MCP, tool registries, or "no tools available". Naming the
#      empty inventory in the prompt is what makes the model narrate it. The
#      prompt must simply DECLARE the contract and move on.
#   2. State the contract only when the request actually carries tools, or the
#      model invents calls (e.g. `clarify`) for plain chat turns.
#
# Empirically: an earlier verbose preamble (which explicitly argued "MCP is empty
# ON PURPOSE... 'No tools available' is false") made the model hallucinate
# "Tool \"terminal\" not found" on ~1/3 of runs; the plain contract below runs
# consistently. Keep it short and factual.
_PLAIN_PREAMBLE = (
    "You are the model backend for Hermes Agent. Hermes executes every tool; you never run "
    "anything yourself. This turn carries no tools — answer in plain text.",
)

_TOOL_PREAMBLE = (
    "You are the model backend for Hermes Agent. Hermes executes every tool; you never run "
    "anything yourself. Selecting a tool below and emitting the call IS you acting — Hermes "
    "runs it and returns the result on the next turn.",
    'To use a tool, answer with ONLY a <tool_call>{...}</tool_call> block holding one JSON '
    'object: {"id": "<unique>", "type": "function", "function": {"name": "<tool name>", '
    '"arguments": "<JSON-encoded arguments as a string>"}}.',
    "Use only a tool from the list below. When no tool is needed, answer normally in plain text.",
)


def _render_assistant_message(message: dict[str, Any]) -> str:
    """Render one assistant turn so a replayed tool loop stays coherent.

    Hermes' generic formatter (``copilot_acp_client._format_messages_as_prompt``)
    renders a message from its ``content`` alone, and an assistant message that
    issued a tool call has ``content=None`` — so it drops BOTH the assistant turn
    and the ``<tool_call>`` that produced the following ``Tool:`` result. On the
    next request the model then sees a tool result with no requester and refuses
    it ("that output isn't mine"). Re-emit the calls as the same
    ``<tool_call>`` text the model is told to speak.
    """
    text = _render_message_content(message.get("content"))
    lines: list[str] = [text] if text else []
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else getattr(call, "function", None)
        name = (fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)) or ""
        args = (fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)) or "{}"
        call_id = (call.get("id") if isinstance(call, dict) else getattr(call, "id", None)) or "call_1"
        if not isinstance(args, str):
            try:
                args = json.dumps(args)
            except Exception:
                args = "{}"
        if name:
            lines.append(
                '<tool_call>{"id": %s, "type": "function", "function": {"name": %s, "arguments": %s}}</tool_call>'
                % (json.dumps(str(call_id)), json.dumps(str(name)), json.dumps(args))
            )
    return "\n".join(lines).strip()


def format_messages_as_prompt(
    messages: list[dict[str, Any]] | None,
    model: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> str:
    """Like the bundled ACP formatter, but with tool-call history preserved."""
    preamble = _TOOL_PREAMBLE if tools else _PLAIN_PREAMBLE
    sections: list[str] = [*preamble, *_render_tool_bridge_sections(tools, tool_choice)]
    transcript: list[str] = []
    for message in (m for m in (messages or []) if isinstance(m, dict)):
        role = str(message.get("role") or "unknown").strip().lower()
        if role == "assistant":
            rendered = _render_assistant_message(message)
        else:
            rendered = _render_message_content(message.get("content"))
        if rendered:
            transcript.append(f"{_ROLE_LABELS.get(role, 'Context')}:\n{rendered}")
    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


class QoderACPClient:
    """Minimal OpenAI-client-compatible facade over ``qodercli --acp``."""

    # This shim drives an ACP subprocess over stdio: it is already a complete
    # client (never re-dispatch through a wire adapter) and async-safe as-is.
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        acp_command: str | None = None,
        acp_args: list[str] | None = None,
        acp_cwd: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        **_,
    ):
        self.api_key = api_key or PROVIDER
        self.base_url = base_url or ACP_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or _resolve_command()
        self._acp_args = list(acp_args or args or _resolve_args())
        self._temp_dir: tempfile.TemporaryDirectory | None = None
        cwd = acp_cwd or os.environ.get("HERMES_QODER_ACP_CWD", "").strip()
        if cwd:
            self._acp_cwd = str(Path(cwd).resolve())
        else:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="hermes_qoder_")
            self._acp_cwd = self._temp_dir.name
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))
        self.is_closed = False
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._active_process_lock = threading.Lock()

    # ── process lifecycle ───────────────────────────────────────────────

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    def _release_process(self, proc: subprocess.Popen[str]) -> None:
        with self._active_process_lock:
            self._active_processes.discard(proc)
            if not self._active_processes:
                self.is_closed = True
        self._terminate_process(proc)

    def close(self) -> None:
        with self._active_process_lock:
            procs, self._active_processes = tuple(self._active_processes), set()
        self.is_closed = True
        for proc in procs:
            self._terminate_process(proc)
        if self._temp_dir is not None:
            with contextlib.suppress(Exception):
                self._temp_dir.cleanup()
            self._temp_dir = None

    def __enter__(self) -> "QoderACPClient":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _spawn(self) -> subprocess.Popen[str]:
        try:
            proc = subprocess.Popen(
                [self._acp_command, *self._acp_args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._acp_cwd,
                env=_build_subprocess_env(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Could not start the Qoder ACP command {self._acp_command!r}. Install Qoder CLI "
                "(curl -fsSL https://qoder.com/install | bash) or set HERMES_QODER_ACP_COMMAND."
            ) from exc
        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError("Qoder ACP process did not expose stdin/stdout pipes.")
        with self._active_process_lock:
            self._active_processes.add(proc)
            self.is_closed = False
        return proc

    # ── ACP session ─────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _session(
        self, timeout_seconds: float, *, allow_file_requests: bool = True
    ) -> Iterator[tuple[dict[str, Any], Callable[..., Any]]]:
        """Start one ACP process; yield its ``session/new`` result + request callable."""
        proc = self._spawn()
        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=60)
        write_lock = threading.Lock()

        def _pump(stream, sink) -> None:
            for line in stream or ():
                sink(line)

        def _decode(line: str) -> dict[str, Any]:
            try:
                return json.loads(line)
            except Exception:
                return {"raw": line.rstrip("\n")}

        threading.Thread(target=_pump, args=(proc.stdout, lambda line: inbox.put(_decode(line))), daemon=True).start()
        stderr_pump = threading.Thread(
            target=_pump, args=(proc.stderr, lambda line: stderr_tail.append(line.rstrip("\n"))), daemon=True
        )
        stderr_pump.start()

        request_ids = iter(range(1, 1 << 62))
        session_deadline = time.monotonic() + timeout_seconds

        def _write(payload: dict[str, Any]) -> None:
            with write_lock:
                if proc.stdin is None or proc.poll() is not None:
                    raise RuntimeError("Qoder ACP process is no longer writable.")
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()

        def _answer(msg: dict[str, Any]) -> None:
            """Reply to a server->client request; never let it stall the turn."""
            message_id = msg.get("id")
            method = msg.get("method")
            if method == "session/request_permission":
                # Qoder's tools are off; a permission ask means something is off-script.
                response = _jsonrpc_result(message_id, {"outcome": {"outcome": "cancelled"}})
            elif method in _FS_HANDLERS:
                if not allow_file_requests:
                    response = _jsonrpc_error(message_id, -32601, "File access is unavailable during model discovery.")
                else:
                    try:
                        response = _jsonrpc_result(message_id, _FS_HANDLERS[method](msg.get("params") or {}, self._acp_cwd))
                    except Exception as exc:
                        response = _jsonrpc_error(message_id, -32602, str(exc))
            else:
                response = _jsonrpc_error(message_id, -32601, f"ACP client method {method!r} is not supported by Hermes yet.")
            _write(response)

        def _request(
            method: str,
            params: dict[str, Any],
            *,
            text_parts: list[str] | None = None,
            reasoning_parts: list[str] | None = None,
        ) -> Any:
            request_id = next(request_ids)
            _write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            while time.monotonic() < session_deadline and proc.poll() is None:
                try:
                    msg = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._handle_server_message(msg, text_parts=text_parts, reasoning_parts=reasoning_parts, answer=_answer):
                    continue
                if msg.get("id") != request_id:
                    continue
                if "error" in msg:
                    err = msg.get("error") or {}
                    if err.get("code") == AUTH_REQUIRED_CODE:
                        raise QoderACPAuthRequired(
                            "Qoder CLI is not signed in where Hermes runs it. Run `qodercli login` "
                            "(or set QODER_PERSONAL_ACCESS_TOKEN) as the user Hermes runs as, then retry."
                        )
                    raise RuntimeError(f"Qoder ACP {method} failed: {err.get('message') or err}")
                return msg.get("result")
            if proc.poll() is not None:
                stderr_pump.join(timeout=1.0)
                stderr_text = "\n".join(stderr_tail).strip()
                if "Automatic authentication" in stderr_text or "Authentication required" in stderr_text:
                    raise QoderACPAuthRequired(
                        "Qoder CLI is not signed in where Hermes runs it. Run `qodercli login` "
                        "(or set QODER_PERSONAL_ACCESS_TOKEN) as the user Hermes runs as, then retry."
                    )
                raise RuntimeError(f"Qoder ACP process exited early: {stderr_text or f'exit code {proc.returncode}'}")
            raise TimeoutError(f"Timed out waiting for Qoder ACP response to {method}.")

        try:
            _request(
                "initialize",
                {
                    "protocolVersion": 1,
                    # Qoder advertises fs/prompt capabilities but Hermes owns the
                    # filesystem: no fs capability is offered, so Qoder asks and we decline.
                    "clientCapabilities": {},
                    "clientInfo": {"name": "hermes-agent", "title": "Hermes Agent", "version": "0.0.0"},
                },
            )
            session = _request("session/new", {"cwd": self._acp_cwd, "mcpServers": []}) or {}
            if not str(session.get("sessionId") or "").strip():
                raise RuntimeError(f"Qoder ACP did not return a sessionId: {session}")
            yield session, _request
        finally:
            self._release_process(proc)

    @staticmethod
    def _handle_server_message(
        msg: dict[str, Any],
        *,
        text_parts: list[str] | None,
        reasoning_parts: list[str] | None,
        answer: Callable[[dict[str, Any]], None],
    ) -> bool:
        """Consume a server->client message; True when handled (notification or request answered)."""
        method = msg.get("method")
        if not isinstance(method, str):
            return False
        if method == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            content = update.get("content") or {}
            chunk_text = str(content.get("text") or "") if isinstance(content, dict) else ""
            if chunk_text:
                kind = str(update.get("sessionUpdate") or "").strip()
                if kind == "agent_message_chunk" and text_parts is not None:
                    text_parts.append(chunk_text)
                elif kind == "agent_thought_chunk" and reasoning_parts is not None:
                    reasoning_parts.append(chunk_text)
            return True
        if msg.get("id") is None:
            return True  # an id-less notification we do not model
        answer(msg)
        return True

    # ── OpenAI surface ──────────────────────────────────────────────────

    def list_models(self, *, timeout_seconds: float = 20.0) -> list[str]:
        """Model ids advertised by a short-lived authenticated ACP session."""
        with self._session(timeout_seconds, allow_file_requests=False) as (session, _request):
            return _session_model_ids(session)

    def _run_prompt(
        self,
        prompt_text: str,
        *,
        timeout_seconds: float,
        model: str | None = None,
        expect_tool_call: bool = False,
    ) -> tuple[str, str]:
        requested_model = str(model or "").strip()
        with self._session(timeout_seconds) as (session, _request):
            session_id = str(session.get("sessionId") or "").strip()
            if requested_model and requested_model != PROVIDER:
                selection = _model_selection_request(session, requested_model)
                if selection is not None:
                    _request(*selection)
                else:
                    advertised = _session_model_ids(session)
                    if requested_model not in advertised:
                        logger.warning(
                            "Qoder ACP does not offer model %r (advertised: %s); using the session default.",
                            requested_model, ", ".join(advertised) or "none",
                        )

            def _one_prompt(text: str) -> tuple[str, str]:
                text_parts: list[str] = []
                reasoning_parts: list[str] = []
                _request(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                    text_parts=text_parts,
                    reasoning_parts=reasoning_parts,
                )
                return "".join(text_parts), "".join(reasoning_parts)

            response_text, reasoning = _one_prompt(prompt_text)

            # The model is nondeterministic about the tool-call contract: told a
            # tool exists, it still sometimes answers with prose — often inventing
            # "Tool <name> not found" / "no tools available". Re-prompting in the
            # SAME session (so it sees its own refusal) with a one-line nudge
            # recovers the call cheaply. Never retry when no tools were offered,
            # and never more than HERMES_QODER_ACP_TOOL_RETRIES times.
            if expect_tool_call and not _has_tool_call(response_text):
                retries = _resolve_tool_retries()
                for attempt in range(retries):
                    logger.debug("qoder-acp: no tool call in reply, retry %d/%d", attempt + 1, retries)
                    retry_text, retry_reasoning = _one_prompt(_TOOL_RETRY_NUDGE)
                    if _has_tool_call(retry_text):
                        response_text, reasoning = retry_text, retry_reasoning
                        break
            return response_text, reasoning

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **_,
    ) -> Any:
        prompt_text = format_messages_as_prompt(messages or [], model=model, tools=tools, tool_choice=tool_choice)
        response_text, reasoning = self._run_prompt(
            prompt_text,
            timeout_seconds=_resolve_timeout(timeout),
            model=model,
            expect_tool_call=bool(tools),
        )
        if debug_dir := os.environ.get("HERMES_QODER_ACP_DEBUG_DIR", "").strip():
            try:
                import time as _t
                d = Path(debug_dir)
                d.mkdir(parents=True, exist_ok=True)
                stem = f"{int(_t.time()*1000)}"
                (d / f"{stem}.prompt.txt").write_text(prompt_text, encoding="utf-8")
                (d / f"{stem}.response.txt").write_text(response_text, encoding="utf-8")
                (d / f"{stem}.reasoning.txt").write_text(reasoning, encoding="utf-8")
            except Exception:
                logger.debug("qoder-acp debug dump failed", exc_info=True)
        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
        message = SimpleNamespace(
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=reasoning or None,
            reasoning_content=reasoning or None,
            reasoning_details=None,
        )
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
            usage=SimpleNamespace(
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
            model=model or PROVIDER,
        )
        return _completion_to_stream_chunks(completion) if stream else completion


# Alias kept explicit for readers of the bundled copilot-acp equivalent.
Client = QoderACPClient
