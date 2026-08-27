"""Usage limits (session / weekly %) via Anthropic's OAuth usage endpoint.

Reads the Claude Code OAuth token fresh from the macOS Keychain each refresh
(Claude Code rotates it; the Keychain always has the current one) and makes a
read-only GET. The token goes nowhere except api.anthropic.com over HTTPS.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import urllib.request
from datetime import datetime

from ..state import LimitGauge, Limits
from .base import Collector

log = logging.getLogger(__name__)

_KEYCHAIN_SERVICE = "Claude Code-credentials"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_BETA_HEADER = "oauth-2025-04-20"
_TIMEOUT_S = 15


def _access_token() -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    return json.loads(out)["claudeAiOauth"]["accessToken"]


def _local_naive(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)
    except ValueError:
        return None


_KIND_LABELS = {"session": "SESSION", "weekly_all": "WEEK"}


def _label_for(entry: dict) -> str:
    """Scoped windows are named by their model; the rest by kind."""
    model = ((entry.get("scope") or {}).get("model") or {})
    name = model.get("display_name")
    if name:
        return f"WEEK {name}".upper()
    return _KIND_LABELS.get(entry.get("kind") or "", "LIMIT")


class LimitsCollector(Collector):
    name_ = "limits"
    cadence_s = 60.0

    def refresh(self) -> None:
        req = urllib.request.Request(_USAGE_URL, headers={
            "Authorization": f"Bearer {_access_token()}",
            "anthropic-beta": _BETA_HEADER,
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read())

        def gauge(section: dict | None, label: str) -> LimitGauge | None:
            if not section:
                return None
            return LimitGauge(
                label=label,
                used_pct=float(section.get("utilization") or 0.0),
                resets_at=_local_naive(section.get("resets_at")),
            )

        limits = Limits(
            session=gauge(data.get("five_hour"), "SESSION"),
            weekly=gauge(data.get("seven_day"), "WEEK"),
        )
        # Prefer the `limits` array where it overlaps: model-scoped weekly
        # windows (e.g. Fable) appear only there — the flat seven_day_* keys
        # are null for them — and it's what the desktop app and /usage show.
        for entry in data.get("limits") or []:
            g = LimitGauge(
                label=_label_for(entry),
                used_pct=float(entry.get("percent") or 0.0),
                resets_at=_local_naive(entry.get("resets_at")),
            )
            kind = entry.get("kind")
            if kind == "session":
                limits.session = g
            elif kind == "weekly_all":
                limits.weekly = g
            elif kind == "weekly_scoped":
                limits.weekly_scoped = g

        self.shared.update(limits=limits)
        log.debug("limits: session=%s weekly=%s scoped=%s(%s)",
                  limits.session and limits.session.used_pct,
                  limits.weekly and limits.weekly.used_pct,
                  limits.weekly_scoped and limits.weekly_scoped.label,
                  limits.weekly_scoped and limits.weekly_scoped.used_pct)

    def mark_stale(self) -> None:
        def apply(state) -> None:
            state.limits = dataclasses.replace(state.limits, stale=True)
        self.shared.mutate(apply)
