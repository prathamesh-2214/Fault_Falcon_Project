"""Investigation stages: macro (which change?) then micro (which line? who is hit? how to fix?).

Macro: SCOPE -> CHANGE_TIMELINE -> SUSPECTS -> RUNTIME -> HYPOTHESIS
Micro: DIFF_ANALYSIS -> BLAST_RADIUS -> REMEDIATION -> CLOSE (RCA report)

Each stage has a one-sentence instruction appended to the user turn and a default next stage.
The engineer can jump to any stage (``stage diff``); the defaults drive the suggested flow.
"""

from __future__ import annotations

from ff import config

SCOPE = "SCOPE"
CHANGE_TIMELINE = "CHANGE_TIMELINE"
SUSPECTS = "SUSPECTS"
RUNTIME = "RUNTIME"
HYPOTHESIS = "HYPOTHESIS"
DIFF_ANALYSIS = "DIFF_ANALYSIS"
BLAST_RADIUS = "BLAST_RADIUS"
REMEDIATION = "REMEDIATION"
CLOSE = "CLOSE"
MACRO_STAGES: tuple[str, ...] = (SCOPE, CHANGE_TIMELINE, SUSPECTS, RUNTIME, HYPOTHESIS)
MICRO_STAGES: tuple[str, ...] = (DIFF_ANALYSIS, BLAST_RADIUS, REMEDIATION)
STAGES: tuple[str, ...] = MACRO_STAGES + MICRO_STAGES + (CLOSE,)

LABELS: dict[str, str] = {
    SCOPE: "Scope", CHANGE_TIMELINE: "Change timeline", SUSPECTS: "Suspects", RUNTIME: "Runtime evidence",
    HYPOTHESIS: "Root cause (macro)", DIFF_ANALYSIS: "Breaking line (micro)", BLAST_RADIUS: "Blast radius",
    REMEDIATION: "Remediation", CLOSE: "RCA report",
}

INSTRUCTIONS: dict[str, str] = {
    SCOPE: "Restate the symptom and the affected service in one sentence.",
    CHANGE_TIMELINE: "List the changes before the incident, newest first, and say which could explain the symptom.",
    SUSPECTS: "Examine the suspect change: what it changed, its tests and whether it fits the symptom.",
    RUNTIME: "Compare the error signatures with the baseline and say which change they point to.",
    HYPOTHESIS: "Name the one change that caused the incident, citing its id, or say no change explains it.",
    DIFF_ANALYSIS: "Inspect the diff hunks of the root-cause change and name the exact file and line that broke "
                   "production, citing the chg id and quoting the changed line.",
    BLAST_RADIUS: "List every affected service and why, using the topology and the anomalies in each service.",
    REMEDIATION: "Recommend the fix: rollback, config or terraform revert, or the code fix for the breaking line, "
                 "plus one follow-up action.",
    CLOSE: "Summarise the root cause, the breaking line, the ruled-out changes and the fix.",
}

DEFAULT_QUESTIONS: dict[str, str] = {
    SCOPE: "What is the symptom and which service is affected?",
    CHANGE_TIMELINE: "What changed before the incident?",
    SUSPECTS: "Could the suspect change explain the symptom?",
    RUNTIME: "What do the error signatures show compared with the baseline?",
    HYPOTHESIS: "What is the most likely root cause?",
    DIFF_ANALYSIS: "Which file and line in the root-cause change broke it?",
    BLAST_RADIUS: "Which services are affected and why?",
    REMEDIATION: "How do we fix it and prevent a repeat?",
    CLOSE: "Summarise the investigation.",
}

_NEXT = {SCOPE: CHANGE_TIMELINE, CHANGE_TIMELINE: SUSPECTS, SUSPECTS: RUNTIME, RUNTIME: HYPOTHESIS,
         HYPOTHESIS: DIFF_ANALYSIS, DIFF_ANALYSIS: BLAST_RADIUS, BLAST_RADIUS: REMEDIATION, REMEDIATION: CLOSE,
         CLOSE: CLOSE}


def normalise_stage(name: str) -> str | None:
    """'runtime', 'change-timeline', 'diff', 'blast' -> canonical stage name, or None."""
    key = name.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {"TIMELINE": CHANGE_TIMELINE, "CHANGES": CHANGE_TIMELINE, "SUSPECT": SUSPECTS,
               "HYPOTHESES": HYPOTHESIS, "ROOT_CAUSE": HYPOTHESIS, "MACRO": HYPOTHESIS, "DIFF": DIFF_ANALYSIS,
               "MICRO": DIFF_ANALYSIS, "LINE": DIFF_ANALYSIS, "BLAST": BLAST_RADIUS, "IMPACT": BLAST_RADIUS,
               "FIX": REMEDIATION, "REPORT": CLOSE, "RCA": CLOSE}
    key = aliases.get(key, key)
    return key if key in STAGES else None


def instruction(stage: str) -> str:
    return INSTRUCTIONS[stage]


def next_stage(stage: str, top_change_score: float | None = None,
               threshold: float = config.CHANGE_SCORE_THRESHOLD) -> str:
    """Default next stage; CHANGE_TIMELINE skips to RUNTIME when no change scored above ``threshold``."""
    if stage == CHANGE_TIMELINE and top_change_score is not None and top_change_score < threshold:
        return RUNTIME
    return _NEXT[stage]
