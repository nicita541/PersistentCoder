from __future__ import annotations

import re


# Sentinel that is present in the global System Policy text
# (config/system_prompt.txt). Every semantic LLM call must carry it.
POLICY_MARKER = "SYSTEM POLICY"


def _normalize(
    text: object,
) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(text),
    ).strip()


def has_system_policy(
    messages: list[dict[str, str]] | None,
    policy: str | None,
) -> bool:
    """
    True if the global System Policy is already present in the
    messages (so it must not be duplicated).
    """

    if not policy or not policy.strip():
        return False

    needle = _normalize(policy)

    for message in messages or []:
        if str(message.get("role", "")) != "system":
            continue

        if needle in _normalize(
            message.get("content", "")
        ):
            return True

    return False


def merge_system_policy(
    messages: list[dict[str, str]] | None,
    policy: str | None,
) -> list[dict[str, str]]:
    """
    Return a copy of `messages` where the global System Policy is the
    first system instruction.

    Stage instructions never replace the policy: when a system message
    already exists it is merged into a single system message
    (policy first, stage instructions second). This keeps exactly one
    system message, which is what the local Qwen chat template
    expects, and it guarantees the policy is present in every
    semantic call.
    """

    prepared = [
        dict(message)
        for message in (messages or [])
    ]

    policy = (policy or "").strip()

    if not policy:
        return prepared

    if has_system_policy(prepared, policy):
        return prepared

    if (
        prepared
        and str(prepared[0].get("role", ""))
        == "system"
    ):
        stage_instructions = str(
            prepared[0].get("content", "")
        ).strip()

        prepared[0] = {
            "role": "system",
            "content": (
                f"{policy}\n\n{stage_instructions}"
                if stage_instructions
                else policy
            ),
        }

        return prepared

    return [
        {
            "role": "system",
            "content": policy,
        },
        *prepared,
    ]


class PolicyLLM:
    """
    Wraps the single LLM client of a runtime.

    Guarantees that EVERY semantic call (GoalAnalyzer, TaskDecomposer,
    DependencyBuilder, step/plan repair, CodingAgent, RepairAgent, ...)
    carries the global System Policy, without each stage having to
    remember it.
    """

    def __init__(
        self,
        llm,
        policy: str | None = None,
    ) -> None:
        if isinstance(llm, PolicyLLM):
            llm = object.__getattribute__(
                llm,
                "_llm",
            )

        self._llm = llm
        self.policy = policy or ""

    @property
    def policy_enforced(self) -> bool:
        return POLICY_MARKER in self.policy

    def chat(
        self,
        messages: list[dict[str, str]],
        **kwargs,
    ):
        return self._llm.chat(
            merge_system_policy(
                messages,
                self.policy,
            ),
            **kwargs,
        )

    def __getattr__(self, name: str):
        return getattr(
            object.__getattribute__(self, "_llm"),
            name,
        )


def enforce_system_policy(
    llm,
    policy: str | None,
):
    """
    Wrap `llm` so every semantic call carries the System Policy.

    An empty policy (explicitly disabled) returns the client
    untouched.
    """

    if not policy or not policy.strip():
        return llm

    if (
        isinstance(llm, PolicyLLM)
        and llm.policy
        == policy
    ):
        return llm

    return PolicyLLM(
        llm,
        policy,
    )
