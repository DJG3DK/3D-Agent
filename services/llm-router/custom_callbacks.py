"""Routing visibility callback.

Appends one JSON line per completed request to logs/routing.jsonl, recording
which underlying model actually answered (return_raw_model_name=true makes
response.model the real deployment, not "smart-router") plus cost/tokens.
This is what the review dashboard's "Router" tab reads to show live model
usage, since litellm's own spend/usage API requires a Postgres DB we don't
have set up.
"""

import json
import os
import time

from litellm.integrations.custom_logger import CustomLogger

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_PATH = os.path.join(LOG_DIR, "routing.jsonl")
MAX_LINES = 5000  # trim on rotation so this can't grow unbounded


def _trim_if_needed():
    try:
        if os.path.getsize(LOG_PATH) < 5_000_000:  # only bother checking size past 5MB
            return
        with open(LOG_PATH) as f:
            lines = f.readlines()
        if len(lines) > MAX_LINES:
            with open(LOG_PATH, "w") as f:
                f.writelines(lines[-MAX_LINES:])
    except FileNotFoundError:
        pass


class RoutingLogger(CustomLogger):
    # complexity_router classifies by text content alone and has no concept
    # of message modality (checked its source — zero mention of image/vision
    # anywhere), so an image sent to a text-only pool member would silently
    # fail or get ignored upstream. Intercept here, before the classifier
    # ever sees the request, and force a known vision-capable model — kimi-k3.
    # Paired with model_info.supports_vision: true on the smart-router entry
    # in config.yaml so OpenHands stops refusing client-side before even
    # trying; don't remove one without the other.
    #
    # 2026-08-16: most of the pool actually takes images now (gpt-4o-mini,
    # mistral-small-3.2, claude-haiku-4.5, grok-4.3, gemini-3.1-pro-preview,
    # gpt-5.3-codex — checked OpenRouter's own catalog), so hard-coding
    # kimi-k3 as the target is more conservative/expensive than it needs to
    # be; a cheap SIMPLE-tier image could route as cheaply as a text one.
    # Flagging as a real future optimization, not implemented here.
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            if data.get("model") != "smart-router":
                return data
            has_image = any(
                isinstance(m.get("content"), list)
                and any(
                    isinstance(block, dict) and block.get("type") == "image_url"
                    for block in m["content"]
                )
                for m in data.get("messages", [])
            )
            if has_image:
                data["model"] = "kimi-k3"
                metadata = data.setdefault("metadata", {})
                metadata["routing_decision"] = {
                    "tier": "VISION",
                    "cause": "image_input_override",
                    "matched_keyword": None,
                    "classifier_model": None,
                }
        except Exception:
            pass
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            requested = kwargs.get("model")
            model = getattr(response_obj, "model", None) or requested
            usage = getattr(response_obj, "usage", None)
            metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
            routing_decision = metadata.get("routing_decision") or {}
            entry = {
                "ts": time.time(),
                "requested_model": requested,
                "routed_model": model,
                "tier": routing_decision.get("tier"),
                "cause": routing_decision.get("cause"),
                "matched_keyword": routing_decision.get("matched_keyword"),
                "classifier_model": routing_decision.get("classifier_model"),
                "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                "cost": (metadata.get("hidden_params") or {}).get("response_cost") or kwargs.get("response_cost"),
                "duration_s": (end_time - start_time).total_seconds() if start_time and end_time else None,
            }
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
            _trim_if_needed()
        except Exception:
            pass

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        try:
            entry = {
                "ts": time.time(),
                "requested_model": kwargs.get("model"),
                "routed_model": None,
                "error": True,
            }
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
            _trim_if_needed()
        except Exception:
            pass


routing_logger = RoutingLogger()
