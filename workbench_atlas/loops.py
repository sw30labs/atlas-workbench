"""Ralph Loop — iterative QA with LLM-as-a-Judge.

Named after the "Ralph" pattern (also called Tenacity Loops): iterate on
a task until a quality threshold is met or max iterations are exhausted.
Each iteration: generate → judge → (if failing) reflect & regenerate.

The implementation uses Tenacity for bounded retries. The LLM-as-a-Judge
(`judge.py`) provides the quality signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .judge import JudgeResult, judge_scenario
from .llm import LLMError, OMLXClient

logger = logging.getLogger(__name__)
PROMPT_VERSION = "scenario-reflection-v1"


@dataclass
class RalphLoopResult:
    """Result of a Ralph Loop iteration cycle."""

    final_score: float
    passed: bool
    iterations: int
    feedback: str
    history: list[JudgeResult] = field(default_factory=list)
    # True when the loop aborted because the judge itself failed —
    # not a verdict on scenario quality.
    error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_score": self.final_score,
            "passed": self.passed,
            "iterations": self.iterations,
            "feedback": self.feedback,
            "history": [r.to_dict() for r in self.history],
            "error": self.error,
        }


REFLECT_PROMPT = """You are improving a Atlas Workbench risk scenario. The judge gave this feedback:

Score: {score}
Criteria: {criteria}
Feedback: {feedback}

Current scenario:
Title: {title}
Description: {desc}
Preparation Strategy: {prep}

Improve the scenario description and preparation strategy to address the judge's feedback.
Return JSON: {{"description": "...", "preparation_strategy": [...], "improvement_notes": "..."}}"""


def ralph_loop(
    scenario: dict[str, Any],
    client: OMLXClient,
    *,
    threshold: float = 0.75,
    max_iterations: int = 3,
    improve_fn: Callable[[dict[str, Any], JudgeResult, OMLXClient], dict[str, Any]] | None = None,
    final_judge_model: str | None = None,
    scenario_id: str | None = None,
) -> RalphLoopResult:
    """Run a Ralph Loop on a scenario: judge → reflect → improve → re-judge.

    The loop iterates until the judge score meets threshold or max_iterations
    is reached. If ``final_judge_model`` is supplied and differs from the
    loop judge, one final independent judgment is performed on the improved
    scenario and used as the authoritative pass/fail verdict.

    Args:
        scenario: The scenario to evaluate (mutated in place; a dict is
            returned from the improvement step).
        client: OMLX client for LLM calls.
        threshold: Minimum judge score to pass (default 0.75).
        max_iterations: Max loop iterations (default 3).
        improve_fn: Optional custom improvement function. If None, uses
                    the default reflect-and-improve prompt. Custom functions
                    do not receive ``scenario_id`` and must tag telemetry
                    themselves if they want attribution.
        final_judge_model: Optional independent model for the final verdict.
        scenario_id: Optional id tagging judge/improve telemetry so tokens
            attribute to the scenario under judgment.
    """
    history: list[JudgeResult] = []

    for iteration in range(1, max_iterations + 1):
        # Judge the current state
        result = judge_scenario(scenario, client, threshold=threshold,
                                scenario_id=scenario_id)
        result.iteration = iteration
        history.append(result)

        if result.error:
            # Judge outage/misbehavior — abort, don't treat as a rejection
            logger.warning(f"Ralph Loop aborted at iteration {iteration}: {result.feedback}")
            return RalphLoopResult(
                final_score=result.score,
                passed=False,
                iterations=iteration,
                feedback=result.feedback,
                history=history,
                error=True,
            )

        logger.info(
            f"Ralph Loop iteration {iteration}: score={result.score:.2f} "
            f"passed={result.passed}"
        )

        if result.passed:
            break

        # If not last iteration, try to improve
        if iteration < max_iterations:
            if improve_fn:
                scenario = improve_fn(scenario, result, client)
            else:
                scenario = _default_improve(scenario, result, client,
                                            scenario_id=scenario_id)
    else:
        # Did not pass after all iterations
        last = history[-1]
        return RalphLoopResult(
            final_score=last.score,
            passed=False,
            iterations=max_iterations,
            feedback=last.feedback,
            history=history,
        )

    # Optional independent final judgment to reduce self-evaluation bias.
    if final_judge_model and final_judge_model != client.settings.llm_model_judge:
        logger.info(f"Running final independent judge: {final_judge_model}")
        final = judge_scenario(
            scenario, client, threshold=threshold, model=final_judge_model,
            scenario_id=scenario_id,
        )
        final.iteration = len(history) + 1
        history.append(final)
        if final.error:
            # Independent judge failed — fall back to loop result rather than
            # rejecting a scenario because of a backend issue.
            logger.warning(f"Final judge failed, using loop verdict: {final.feedback}")
            return RalphLoopResult(
                final_score=history[-2].score,
                passed=True,
                iterations=len(history),
                feedback=history[-2].feedback,
                history=history,
            )
        return RalphLoopResult(
            final_score=final.score,
            passed=final.passed,
            iterations=len(history),
            feedback=final.feedback,
            history=history,
        )

    # No independent final judge requested
    last = history[-1]
    return RalphLoopResult(
        final_score=last.score,
        passed=last.passed,
        iterations=len(history),
        feedback=last.feedback,
        history=history,
    )


def _default_improve(
    scenario: dict[str, Any], judge_result: JudgeResult, client: OMLXClient,
    scenario_id: str | None = None,
) -> dict[str, Any]:
    """Default improvement function: ask LLM to revise based on judge feedback."""
    prompt = REFLECT_PROMPT.format(
        score=judge_result.score,
        criteria=judge_result.criteria,
        feedback=judge_result.feedback,
        title=scenario.get("title", ""),
        desc=scenario.get("description", ""),
        prep=scenario.get("preparation_strategy", ""),
    )
    try:
        improvement = client.chat_json(
            [{"role": "user", "content": prompt}],
            model=client.settings.llm_model,
            temperature=0.3,  # slightly creative for improvement
            scenario_id=scenario_id,
        )
        if "description" in improvement:
            scenario["description"] = improvement["description"]
        if "preparation_strategy" in improvement:
            scenario["preparation_strategy"] = improvement["preparation_strategy"]
        if "improvement_notes" in improvement:
            existing = scenario.get("additional_information", "")
            scenario["additional_information"] = (
                f"{existing}\nImprovement: {improvement['improvement_notes']}"
                if existing
                else f"Improvement: {improvement['improvement_notes']}"
            )
    except LLMError as e:
        logger.warning(f"Improvement LLM call failed: {e}")
    except Exception as e:
        logger.error(f"Improvement attempt failed unexpectedly: {e}", exc_info=True)

    return scenario
