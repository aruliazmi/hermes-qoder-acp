"""Qoder CLI (ACP) — Hermes model-provider registration.

Hermes drives the official ``qodercli`` binary in ACP mode on the user's Qoder
subscription. Qoder is only the model: its built-in tools are off, Hermes' tool
schemas travel in the prompt, and the returned ``<tool_call>`` blocks are
executed by Hermes under its own approvals. ``qoder_acp_client.py`` is the
request path.

Module names are prefixed (``qoder_``) on purpose: this plugin directory shares
sys.path with Hermes core and other plugins, and a bare ``client``/``catalog``
would collide with a same-named module and break the import.
"""
from __future__ import annotations

import logging

from providers import register_provider
from providers.base import ProviderProfile

# Dual import: the Hermes loader imports this directory as a package; a flat
# source tree under test does not.
try:
    from .qoder_catalog import (
        ALIASES,
        DEFAULT_MODEL,
        FALLBACK_MODELS,
        MODEL_NAMES,
        context_window,
        model_capabilities,
        resolve_model,
    )
except ImportError:
    from qoder_catalog import (
        ALIASES,
        DEFAULT_MODEL,
        FALLBACK_MODELS,
        MODEL_NAMES,
        context_window,
        model_capabilities,
        resolve_model,
    )

logger = logging.getLogger(__name__)

PROVIDER = "qoder-acp"
ACP_MARKER_BASE_URL = "acp://qoder"


class QoderACPProfile(ProviderProfile):
    """Qoder CLI over ACP — external process, no REST models endpoint."""

    def create_client(self, **client_kwargs):
        # Lazy import: keeps the ACP client module out of the plugin-load path
        # (the loader inspects this class, it does not run it).
        try:
            from .qoder_acp_client import QoderACPClient
        except ImportError:
            from qoder_acp_client import QoderACPClient
        return QoderACPClient(**client_kwargs)

    def get_model_context_length(self, model: str | None = None) -> int:
        return context_window(model)

    def fetch_models(self, **kwargs) -> list[str] | None:
        """Model ids advertised by a short-lived authenticated ``session/new``.

        ``api_key``/``base_url`` are ignored: the subprocess owns auth. None when
        the CLI is missing, not signed in, or the probe fails/times out — callers
        fall back to ``FALLBACK_MODELS``.
        """
        try:
            try:
                from .qoder_acp_client import QoderACPClient
            except ImportError:
                from qoder_acp_client import QoderACPClient
            client = QoderACPClient(acp_command=kwargs.get("command"), acp_args=kwargs.get("args"))
        except Exception:
            return None
        try:
            return client.list_models(timeout_seconds=20.0) or None
        except Exception:
            return None
        finally:
            client.close()

    def setup_status(self, **kwargs) -> dict:
        """Structural status: is the binary present, and does ACP talk?"""
        status: dict = {"configured": False, "logged_in": False}
        try:
            try:
                from .qoder_acp_client import QoderACPClient
            except ImportError:
                from qoder_acp_client import QoderACPClient
            client = QoderACPClient()
        except Exception as exc:
            status["error"] = str(exc)
            return status
        status["configured"] = True
        try:
            models = client.list_models(timeout_seconds=20.0)
            status["logged_in"] = bool(models)
            status["model_count"] = len(models or [])
        except Exception as exc:
            status["error"] = str(exc)
        finally:
            client.close()
        return status


profile = QoderACPProfile(
    name=PROVIDER,
    aliases=("qoder", "qodercli"),
    display_name="Qoder CLI (ACP)",
    description="Qoder subscription via the signed-in qodercli in ACP mode; Hermes owns tools",
    api_mode="chat_completions",  # the ACP subprocess uses chat_completions routing
    auth_type="external_process",
    supports_health_check=False,  # no REST /models endpoint; the ACP probe is the check
    env_vars=(),                  # the qodercli subprocess owns auth
    base_url=ACP_MARKER_BASE_URL,
    process_command="qodercli",
    process_args=("--acp", "--tools", ""),
    process_command_env_vars=("HERMES_QODER_ACP_COMMAND", "QODER_CLI_PATH"),
    process_args_env_var="HERMES_QODER_ACP_ARGS",
    default_aux_model=DEFAULT_MODEL,
    fallback_models=FALLBACK_MODELS,
    model_aliases={alias: resolve_model(alias) for alias in ALIASES},
    model_capabilities=model_capabilities(),
)

register_provider(profile)
