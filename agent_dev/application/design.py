"""Design sufficiency is explicit; an incomplete document cannot enable implementation."""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class SufficiencyResult:
    sufficient: bool
    missing: tuple[str, ...]
    question: str


class DesignAssessment:
    # Labels accept existing Korean and English deliverable conventions.
    required: ClassVar[dict[str, tuple[str, ...]]] = {
        "scope": ("scope", "범위"),
        "acceptance": ("acceptance", "수용 기준", "기대 동작", "검증 기준"),
        "constraints": ("constraints", "제약", "정책", "안전 경계"),
    }

    def assess(self, content: str) -> SufficiencyResult:
        headings = [
            line.lower().strip() for line in content.splitlines() if line.lstrip().startswith("#")
        ]
        missing = tuple(
            name
            for name, labels in self.required.items()
            if not any(label in heading for label in labels for heading in headings)
        )
        questions = {
            "scope": "구현할 범위와 범위 밖 항목을 설계 문서에 적어주세요.",
            "acceptance": "성공을 확인할 수용 기준을 설계 문서에 적어주세요.",
            "constraints": "구현 시 지켜야 할 제약과 정책을 설계 문서에 적어주세요.",
        }
        return SufficiencyResult(not missing, missing, questions[missing[0]] if missing else "")
