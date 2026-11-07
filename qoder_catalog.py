"""Qoder model catalog — fallback ids, aliases and declared capabilities.

The authoritative list comes from ``session/new`` (Qoder's ``models.availableModels``
/ ``configOptions``); this module only covers the case where that probe cannot run
(CLI missing, not signed in) and supplies friendly aliases + capabilities that
models.dev cannot know for an out-of-tree provider.

Qoder's model ids are opaque slugs (``dfmodel``, ``gmodel``, ...). The human
names in ``MODEL_NAMES`` are what ``session/new`` reports for each slug, kept
here for pickers and for resolution when only the pretty name is known.
"""

from __future__ import annotations

# Fallback ids in the order Qoder lists them (cheap/auto first).
FALLBACK_MODELS: tuple[str, ...] = (
    "auto",
    "efficient",
    "fast",
    "dfmodel",
    "gfmodel",
    "qfmodel",
    "mmodel",
    "dmodel",
    "gmodel",
    "kmodel",
    "kmodel_latest",
    "qmodel_38max",
    "cmodel",
    "smodel",
    "ultimate",
)

DEFAULT_MODEL = "dfmodel"

# slug -> human name (as reported by session/new). Used to resolve a pretty
# name back to a slug, since the wire only accepts slugs.
MODEL_NAMES: dict[str, str] = {
    "auto": "Auto",
    "ultimate": "Ultimate",
    "fast": "Fast",
    "efficient": "Efficient",
    "smodel": "Sonus",
    "cmodel": "Cantus",
    "qmodel_38max": "Qwen3.8-Max",
    "qfmodel": "Qwen3.8-Flash",
    "kmodel_latest": "Kimi-K3.1-Pro",
    "kmodel": "Kimi-K2.8-Preview",
    "gmodel": "GLM-5.3",
    "gfmodel": "GLM-5.3-Flash",
    "dmodel": "DeepSeek-V4-Pro",
    "dfmodel": "DeepSeek-Flash",
    "mmodel": "MiniMax-M3.1-Flash",
}

# Friendly alias -> slug.
ALIASES: dict[str, str] = {
    "default": "auto",
    "smart": "auto",
    "pro": "ultimate",
    "cheap": "efficient",
    "qwen": "qfmodel",
    "qwen-flash": "qfmodel",
    "qwen-max": "qmodel_38max",
    "qwen3.8": "qmodel_38max",
    "kimi": "kmodel_latest",
    "kimi-pro": "kmodel_latest",
    "kimi-k3": "kmodel_latest",
    "glm": "gmodel",
    "glm-flash": "gfmodel",
    "deepseek": "dmodel",
    "deepseek-pro": "dmodel",
    "deepseek-flash": "dfmodel",
    "flash": "dfmodel",
    "minimax": "mmodel",
}

# Slugs whose session/new description says "Reasoning".
_REASONING_SLUGS = {
    "ultimate", "smodel", "cmodel", "qmodel_38max", "qfmodel",
    "kmodel_latest", "kmodel", "gmodel", "gfmodel", "dmodel", "dfmodel", "mmodel",
}

# Qoder exposes 200K / 400K / 1M context options; declare the safe floor.
_DEFAULT_CONTEXT_WINDOW = 200_000


def resolve_model(name: str) -> str:
    """Map an alias or pretty name to a Qoder slug; pass unknown values through."""
    raw = (name or "").strip()
    if not raw:
        return DEFAULT_MODEL
    lowered = raw.lower()
    if lowered in ALIASES:
        return ALIASES[lowered]
    if lowered in MODEL_NAMES:
        return lowered
    for slug, pretty in MODEL_NAMES.items():
        if pretty.lower() == lowered:
            return slug
    return raw


def context_window(_model: str | None = None) -> int:
    return _DEFAULT_CONTEXT_WINDOW


def model_capabilities() -> dict[str, dict[str, object]]:
    """Capability declarations keyed by exact model id (slug + pretty + alias)."""
    keys = set(FALLBACK_MODELS) | set(MODEL_NAMES) | set(ALIASES)
    out: dict[str, dict[str, object]] = {}
    for key in keys:
        slug = resolve_model(key)
        out[key] = {
            "supports_reasoning": slug in _REASONING_SLUGS,
            "supports_vision": True,   # session/new advertises promptCapabilities.image
            "supports_tools": True,    # tools travel in-prompt and come back as <tool_call>
            "context_window": _DEFAULT_CONTEXT_WINDOW,
            "model_family": "qoder",
        }
    return out
