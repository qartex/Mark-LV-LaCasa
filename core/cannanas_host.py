"""Host-side Cannanas wiring: boot identity, confirmation grammar, fences.

Skeleton at this task; the plugin/hook wiring (config checks, adapter
lifecycle, prepare/confirm flow, degraded readiness) completes with the
plugin task. No network imports: the coordinator and grammar are pure host
state machines.
"""
from __future__ import annotations

import uuid

# Fresh per JARVIS process: supplied with every prepare/confirm as
# adapter_boot_id so a restart fences ordinary pending confirmations.
_ADAPTER_BOOT_ID = None


def adapter_boot_id() -> str:
    """Process-lifetime boot identity (uuid4), identical within one process."""
    global _ADAPTER_BOOT_ID
    if _ADAPTER_BOOT_ID is None:
        _ADAPTER_BOOT_ID = uuid.uuid4().hex
    return _ADAPTER_BOOT_ID


# Deterministic, bounded German confirmation grammar: exact bounded forms only.
# Everything outside these sets is 'none' — no fuzzy matching, no model
# interpretation. Grammar decisions belong to the host, never to Gemini.
_AFFIRM_FORMS = frozenset({
    'ja', 'ok', 'okay', 'bestätigen', 'bestätige', 'bitte bestätigen',
    'ja bitte', 'speichern', 'speichere',
})
_DENY_FORMS = frozenset({
    'nein', 'abbrechen', 'brich ab', 'abbruch', 'stop', 'stopp', 'stoppe',
    'stoppen',
})
_TRAILING_PUNCTUATION = '.,!?:;'


def _normalize(text) -> str:
    if not isinstance(text, str):
        return ''
    text = text.strip().lower()
    # Trim trailing punctuation only; never fuzzy-match inside the utterance.
    return text.rstrip(_TRAILING_PUNCTUATION)


def confirmation_grammar(text) -> str:
    """{'affirm' | 'deny' | 'none'} for one finalized utterance."""
    normalized = _normalize(text)
    if not normalized:
        return 'none'
    if normalized in _AFFIRM_FORMS:
        return 'affirm'
    if normalized in _DENY_FORMS:
        return 'deny'
    return 'none'