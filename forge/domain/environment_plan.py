"""Each exec input holds only its own languages, independent of other steps."""

from collections.abc import Mapping
from typing import Any

from forge.domain.environment import EnvironmentLock
from forge.domain.run_plan.models import RunPlan
from forge.domain.run_plan.records import EXEC_USE
from forge.domain.run_task.models import EntrantInput


def languages_in(inputs: Mapping[str, Any], entrant: Mapping[str, EntrantInput]) -> set[str]:
    found: set[str] = set()
    language = inputs.get("language", {})
    if "value" in language:
        found.add(language["value"])
    if "entrant" in language:
        entry = entrant.get(language["entrant"])
        if entry is not None:
            found.update(
                entry.form.options if entry.form.options is not None else (entry.options or ())
            )
    if "step" in language:
        from forge.domain.environment.tools import LANGUAGES

        found.update(LANGUAGES)
    for program in inputs.get("programs", {}).get("programs", {}).values():
        found.update(languages_in(program, entrant))
    return {"python" if each == "ipynb" else each for each in found}


def for_inputs(
    lock: EnvironmentLock, inputs: Mapping[str, Any], entrant: Mapping[str, EntrantInput]
) -> dict[str, Any]:
    wanted = languages_in(inputs, entrant)
    platforms = {
        platform: {
            language: entry.model_dump()
            for language, entry in languages.items()
            if language in wanted
        }
        for platform, languages in lock.platforms.items()
    }
    if not any(platforms.values()):
        return dict(inputs)
    return {
        **inputs,
        "environment": {
            "environment": {
                "version": lock.version,
                "prefix": lock.prefix,
                "platforms": platforms,
            }
        },
    }


def with_environment(
    plan: RunPlan, lock: EnvironmentLock, entrant: Mapping[str, EntrantInput]
) -> RunPlan:
    amended = {}
    for name, run_step in plan.steps.items():
        if run_step.primitive != EXEC_USE:
            amended[name] = run_step
            continue
        amended[name] = run_step.model_copy(
            update={
                "inputs": for_inputs(lock, run_step.inputs, entrant)
                if run_step.inputs is not None
                else None,
                "items": {
                    run: item.model_copy(update={"inputs": for_inputs(lock, item.inputs, entrant)})
                    for run, item in run_step.items.items()
                }
                if run_step.items is not None
                else None,
            }
        )
    return plan.model_copy(update={"steps": amended})


def languages_of(plan: RunPlan, entrant: Mapping[str, EntrantInput]) -> set[str]:
    return {
        language
        for step in plan.steps.values()
        if step.primitive == EXEC_USE
        for ports in (
            [step.inputs]
            if step.inputs is not None
            else [item.inputs for item in (step.items or {}).values()]
        )
        for language in languages_in(ports, entrant)
    }
