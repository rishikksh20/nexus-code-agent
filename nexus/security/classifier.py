"""Command and tool risk classification.

``RiskLevel`` mirrors the three tiers used by the bash classifier in
``nexus/tools/filesystem.py`` but adds a fourth ``DANGEROUS`` tier for
commands that are outright catastrophic (irreversible data loss, privilege
escalation, remote code execution, etc.).

``CommandClassifier`` wraps the existing :func:`classify_bash_risk` function
and adds pattern-based categorisation drawn from the reference agent.
"""
from __future__ import annotations

import re
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DANGEROUS = "dangerous"


# Patterns that indicate an outright catastrophic / irreversible command.
# These map any "high" classification to DANGEROUS.
_CATASTROPHIC_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*r[a-zA-Z]*|-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*)\b"),  # rm -rf / rm -fr
    re.compile(r"\bdd\b.*\bof="),          # dd to output file
    re.compile(r"\b(mkfs|fdisk|parted)\b"),# disk-formatting tools
    re.compile(r"\bshred\b"),              # secure wipe
    re.compile(r"\bkill\s+-9\b"),          # force-kill process
    re.compile(r"\b(curl|wget)\b.*\|\s*(ba)?sh"),  # pipe remote to shell
    re.compile(r"\bsudo\b"),               # privilege escalation
    re.compile(r"\bsu\b"),                 # switch user
    re.compile(r"\b(chmod|chown)\s+(-R|--recursive)\b"),  # recursive perm change
    re.compile(r">\s*/etc/"),              # overwrite system files
    re.compile(r">\s*/usr/"),
    re.compile(r">\s*/bin/"),
)

# Patterns that are definitely safe (read-only).
_SAFE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*cat\b"),
    re.compile(r"^\s*head\b"),
    re.compile(r"^\s*tail\b"),
    re.compile(r"^\s*grep\b"),
    re.compile(r"^\s*ls\b"),
    re.compile(r"^\s*echo\b"),
    re.compile(r"^\s*pwd\b"),
    re.compile(r"^\s*which\b"),
    re.compile(r"^\s*env\b"),
    re.compile(r"^\s*git\s+(status|log|diff|show|branch|tag|remote|fetch)\b"),
    re.compile(r"^\s*find\b"),
    re.compile(r"^\s*(python|python3|node)\b"),
)


class CommandClassifier:
    """Classify a shell command by risk level.

    Uses the three-tier bash classifier from ``nexus/tools/filesystem.py``
    as the foundation and promotes ambiguous HIGH classifications to
    DANGEROUS when they match known-catastrophic patterns.
    """

    @classmethod
    def classify(cls, command: str) -> RiskLevel:
        """Return the risk level for *command*."""
        from nexus.tools.filesystem import classify_bash_risk

        level = classify_bash_risk(command)
        if level == "high" and cls._is_catastrophic(command):
            return RiskLevel.DANGEROUS
        return RiskLevel(level)

    @classmethod
    def is_safe(cls, command: str) -> bool:
        """Return ``True`` if the command is clearly read-only / safe."""
        return any(pat.search(command) for pat in _SAFE_PATTERNS)

    @classmethod
    def is_catastrophic(cls, command: str) -> bool:
        """Return ``True`` if the command matches a catastrophic pattern."""
        return cls._is_catastrophic(command)

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    @classmethod
    def _is_catastrophic(cls, command: str) -> bool:
        return any(pat.search(command) for pat in _CATASTROPHIC_PATTERNS)
