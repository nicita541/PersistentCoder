from __future__ import annotations

import re
from dataclasses import dataclass


# ==========================================
# LIMITS (framework-owned)
# ==========================================

DEFAULT_MAX_MEMORIES = 8
DEFAULT_MAX_MEMORY_CHARS = 4_000


# ==========================================
# MEMORY TYPES (long-term only)
# ==========================================

USER_RULE = "USER_RULE"
DECISION = "DECISION"
EXPERIENCE = "EXPERIENCE"
FACT = "FACT"

KNOWN_TYPES = frozenset(
    {USER_RULE, DECISION, EXPERIENCE, FACT}
)

# Ranking weights. USER_RULE is not ranked away: it is always kept
# first, so no relevance score can displace a user/safety rule.
TYPE_WEIGHT = {
    USER_RULE: 1_000.0,
    DECISION: 30.0,
    EXPERIENCE: 25.0,
    FACT: 15.0,
}

TERM_WEIGHT = 6.0
MAX_TERM_HITS = 5

MODULE_WEIGHT = 20.0


STOPWORDS = frozenset(
    {
        # Russian
        "и", "в", "во", "не", "что", "он", "на", "я", "с", "со",
        "как", "а", "то", "все", "она", "так", "его", "но", "да",
        "ты", "к", "у", "же", "вы", "за", "бы", "по", "только",
        "ее", "мне", "было", "вот", "от", "меня", "еще", "нет",
        "о", "из", "ему", "теперь", "когда", "даже", "ну", "вдруг",
        "ли", "если", "уже", "или", "ни", "быть", "был", "него",
        "до", "вас", "нибудь", "опять", "уж", "вам", "ведь", "там",
        "потом", "себя", "ничего", "ей", "может", "они", "тут",
        "где", "есть", "надо", "ней", "для", "мы", "тебя", "их",
        "чем", "была", "сам", "чтоб", "без", "будто", "чего",
        "раз", "тоже", "себе", "под", "будет", "ж", "тогда", "кто",
        "этот", "того", "потому", "этого", "какой", "совсем",
        "ним", "здесь", "этом", "один", "почти", "мой", "тем",
        "чтобы", "нее", "сейчас", "были", "куда", "зачем", "всех",
        "никогда", "можно", "при", "наконец", "два", "об", "другой",
        "хоть", "после", "над", "больше", "тот", "через", "эти",
        "нас", "про", "всего", "них", "какая", "много", "разве",
        "три", "эту", "моя", "впрочем", "хорошо", "свою", "этой",
        "перед", "иногда", "лучше", "чуть", "том", "нельзя",
        "такой", "им", "более", "всегда", "конечно", "всю", "между",
        # English
        "the", "and", "for", "are", "but", "not", "you", "all",
        "any", "can", "had", "her", "was", "one", "our", "out",
        "day", "get", "has", "him", "his", "how", "its", "new",
        "now", "old", "see", "two", "way", "who", "boy", "did",
        "use", "with", "this", "that", "from", "they", "will",
        "would", "there", "their", "what", "about", "into", "than",
        "then", "them", "these", "should", "must", "same", "only",
        "when", "have", "been", "file", "files", "code", "test",
        "tests", "task", "step", "plan", "make", "need",
    }
)


def _normalize(
    text: object,
) -> str:
    return re.sub(
        r"\s+",
        " ",
        str(text),
    ).strip()


def extract_terms(
    text: object,
) -> set[str]:
    """
    Deterministic token extraction.

    Keeps identifiers, paths and module names, and splits
    snake_case / dotted / CamelCase names into parts.
    """

    terms: set[str] = set()

    for token in re.findall(
        r"[A-Za-zА-Яа-я0-9_./\\:-]+",
        str(text or ""),
    ):
        lowered = token.casefold().strip(
            "._-/:\\"
        )

        if not lowered:
            continue

        if (
            len(lowered) >= 3
            and lowered not in STOPWORDS
        ):
            terms.add(lowered)

        for part in re.split(
            r"[._/:\\-]",
            lowered,
        ):
            if (
                len(part) >= 4
                and part not in STOPWORDS
            ):
                terms.add(part)

        for part in re.findall(
            r"[A-Z][a-z0-9]{3,}",
            token,
        ):
            if part.casefold() not in STOPWORDS:
                terms.add(part.casefold())

    return terms


@dataclass(frozen=True)
class MemoryQuery:
    """
    Everything the ranking may use. No second LLM, no embeddings.
    """

    global_goal: str = ""
    current_task: str = ""
    current_step: str = ""
    failure_context: str = ""
    project_context: str = ""
    extra_terms: tuple[str, ...] = ()

    def text(self) -> str:
        return "\n".join(
            part
            for part in (
                self.global_goal,
                self.current_task,
                self.current_step,
                self.failure_context,
                self.project_context,
            )
            if part
        )

    def terms(self) -> set[str]:
        terms = extract_terms(
            self.text()
        )

        for extra in self.extra_terms:
            terms.update(
                extract_terms(extra)
            )

        return terms


@dataclass
class MemorySelection:
    memories: list[dict[str, object]]
    scores: dict[int, float]

    considered: int = 0
    dropped: int = 0
    rule_count: int = 0
    used_chars: int = 0
    truncated: bool = False

    def ids(self) -> list[int]:
        return [
            int(memory["id"])
            for memory in self.memories
            if "id" in memory
        ]


class MemoryRetriever:
    """
    Retrieval layer of the existing Memory OS.

    Ranks already-persisted, ACTIVE memories for ONE semantic call.
    It never writes, never loads a model and never needs a vector DB:

      - USER_RULE always first (safety rules cannot be out-ranked);
      - keyword / identifier / module overlap with the current
        goal, task, step, failure and project context;
      - memory type, importance, confidence;
      - recency (newer ids win ties);
      - source.
    """

    def __init__(
        self,
        *,
        max_memories: int = DEFAULT_MAX_MEMORIES,
        max_memory_chars: int = DEFAULT_MAX_MEMORY_CHARS,
    ) -> None:
        if max_memories < 1:
            raise ValueError(
                "max_memories must be >= 1"
            )

        if max_memory_chars < 1:
            raise ValueError(
                "max_memory_chars must be >= 1"
            )

        self.max_memories = max_memories
        self.max_memory_chars = max_memory_chars

    @staticmethod
    def _type(
        memory: dict[str, object],
    ) -> str:
        return str(
            memory.get("type", "")
        ).upper()

    @staticmethod
    def _int(
        value: object,
        default: int = 0,
    ) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _float(
        value: object,
        default: float = 0.0,
    ) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def score(
        self,
        memory: dict[str, object],
        *,
        terms: set[str],
        total: int,
        position: int,
    ) -> float:
        memory_type = self._type(memory)

        score = TYPE_WEIGHT.get(
            memory_type,
            5.0,
        )

        score += self._int(
            memory.get("importance"),
            50,
        ) * 0.5

        score += self._float(
            memory.get("confidence"),
            1.0,
        ) * 10.0

        content_terms = extract_terms(
            memory.get("content", "")
        )

        content_terms.update(
            extract_terms(
                memory.get("why") or ""
            )
        )

        hits = len(
            terms & content_terms
        )

        score += (
            min(hits, MAX_TERM_HITS)
            * TERM_WEIGHT
        )

        # Same path / module name mentioned by memory and by the
        # current task is a strong relevance signal.
        def _path_like(items: set[str]) -> set[str]:
            return {
                item
                for item in items
                if "/" in item or "." in item
            }

        if (
            _path_like(terms)
            & _path_like(content_terms)
        ):
            score += MODULE_WEIGHT

        if hits == 0 and terms:
            score -= 10.0

        # Recency: newer records rank higher (deterministic, no clock).
        if total > 1:
            score += (
                5.0
                * position
                / (total - 1)
            )

        if (
            str(memory.get("source", ""))
            .upper()
            == "USER"
        ):
            score += 2.0

        return score

    def _ranked(
        self,
        active: list[dict[str, object]],
        terms: set[str],
    ) -> tuple[
        list[dict[str, object]],
        dict[int, float],
    ]:
        scores: dict[int, float] = {}

        for position, memory in enumerate(active):
            scores[
                self._int(memory.get("id"), -1)
            ] = self.score(
                memory,
                terms=terms,
                total=len(active),
                position=position,
            )

        rules = [
            memory
            for memory in active
            if self._type(memory) == USER_RULE
        ]

        others = [
            memory
            for memory in active
            if self._type(memory) != USER_RULE
        ]

        rules.sort(
            key=lambda memory: (
                -self._int(
                    memory.get("importance"),
                    50,
                ),
                -self._int(
                    memory.get("id"),
                    -1,
                ),
            )
        )

        others.sort(
            key=lambda memory: (
                -scores[
                    self._int(memory.get("id"), -1)
                ],
                -self._int(
                    memory.get("id"),
                    -1,
                ),
            )
        )

        return rules + others, scores

    def select(
        self,
        memories: list[dict[str, object]] | None,
        query: MemoryQuery | None = None,
        *,
        max_memories: int | None = None,
        max_memory_chars: int | None = None,
    ) -> MemorySelection:
        """
        Choose the memories relevant to ONE call, bounded by
        max_memories / max_memory_chars.
        """

        limit = max_memories or self.max_memories

        budget = (
            max_memory_chars
            or self.max_memory_chars
        )

        query = query or MemoryQuery()
        terms = query.terms()

        active = [
            memory
            for memory in (memories or [])
            if str(
                memory.get("status", "ACTIVE")
            ).upper()
            == "ACTIVE"
        ]

        ordered, scores = self._ranked(
            active,
            terms,
        )

        selected: list[dict[str, object]] = []
        used = 0
        truncated = False

        for memory in ordered:
            if len(selected) >= limit:
                truncated = True
                break

            cost = _memory_cost(memory)

            if used + cost > budget:
                # A USER_RULE is never pushed out by the budget
                # while nothing at all has been selected yet.
                if (
                    self._type(memory) == USER_RULE
                    and not selected
                ):
                    selected.append(memory)
                    used += cost
                    continue

                truncated = True
                continue

            selected.append(memory)
            used += cost

        selected_scores = {
            self._int(memory.get("id"), -1): scores[
                self._int(memory.get("id"), -1)
            ]
            for memory in selected
        }

        return MemorySelection(
            memories=selected,
            scores=selected_scores,
            considered=len(active),
            dropped=max(
                0,
                len(active) - len(selected),
            ),
            rule_count=sum(
                1
                for memory in selected
                if self._type(memory) == USER_RULE
            ),
            used_chars=used,
            truncated=truncated,
        )


def _memory_cost(
    memory: dict[str, object],
) -> int:
    return (
        len(str(memory.get("content", "")))
        + len(str(memory.get("why") or ""))
        + 32
    )
