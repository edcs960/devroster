"""Bounded planning evidence and a declarative, executable-code-free mock schema."""

import json
import re
import uuid

DOCUMENTS = (
    "개발기획서",
    "요구사항명세서",
    "기능명세서",
    "화면정의서",
    "API명세서",
    "데이터정의서",
    "테이블정의서",
    "권한정의서",
    "정책정의서",
    "코드공통코드정의서",
    "테스트시나리오",
    "요구사항추적성매트릭스",
    "프로세스정의서",
    "비기능요구사항정의서",
    "프로그램목록",
    "용어사전",
    "기술설계서",
    "시스템다이어그램정의서",
    "시퀀스다이어그램",
    "클래스다이어그램",
    "갭분석및보완사항",
    "산출물목록",
)
KINDS = {
    "section",
    "heading",
    "text",
    "button",
    "input",
    "select",
    "table",
    "card",
    "metric",
    "tabs",
    "nav",
    "checkbox",
    "list",
}
LAYOUTS = {"dashboard", "form", "split", "list"}
TONES = {"indigo", "blue", "green", "neutral"}
IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")


class StudioError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def text(value, name, limit=120_000, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise StudioError(f"{name}을(를) 입력 길이 제한에 맞게 작성해주세요.")
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise StudioError("항목 ID 형식이 잘못되었습니다.")
    return value


def collection(value, name, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise StudioError(f"{name} 항목 수가 허용 범위를 벗어났습니다.")
    return value


def decode_output(output):
    output = text(output, "AI 응답", 1_500_000)
    if output.startswith("```json\n") and output.endswith("\n```"):
        output = output[8:-4]
    try:
        value = json.loads(output)
    except (ValueError, RecursionError) as exc:
        raise StudioError("AI 응답을 읽지 못했습니다. JSON 형식으로 다시 생성해주세요.") from exc
    if not isinstance(value, dict):
        raise StudioError("AI 응답은 명세서 객체여야 합니다.")
    return value


def validate_specs(value, source):
    if not isinstance(value, dict):
        raise StudioError("명세서 형식이 잘못되었습니다.")
    requirements = collection(value.get("requirements"), "요구사항", 200, 1)
    seen = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise StudioError("요구사항 항목 형식이 잘못되었습니다.")
        item_id = identifier(requirement.get("id"))
        if item_id in seen or not item_id.startswith("REQ-"):
            raise StudioError("요구사항 ID가 중복되거나 REQ- 형식이 아닙니다.")
        seen.add(item_id)
        for field in ("title", "description"):
            text(requirement.get(field), "요구사항 " + field, 8000)
        quote = text(requirement.get("source_quote"), "기획서 원문 근거", 8000)
        if quote not in source:
            raise StudioError(f"{item_id}의 원문 근거를 기획서에서 찾을 수 없습니다.")
        if requirement.get("priority") not in {"must", "should", "could"}:
            raise StudioError("요구사항 우선순위가 잘못되었습니다.")
        for criterion in collection(requirement.get("acceptance"), "수용 기준", 30, 1):
            text(criterion, "수용 기준", 4000)
    screens = collection(value.get("screens"), "화면", 40)
    screen_ids = set()
    for screen in screens:
        if not isinstance(screen, dict):
            raise StudioError("화면 정의 형식이 잘못되었습니다.")
        screen_id = identifier(screen.get("id"))
        if not screen_id.startswith("SCR-") or screen_id in screen_ids:
            raise StudioError("화면 ID는 중복 없는 SCR- 형식이어야 합니다.")
        screen_ids.add(screen_id)
        text(screen.get("title"), "화면 이름", 200)
        text(screen.get("purpose"), "화면 목적", 4000)
        references(screen.get("requirement_ids"), seen)
    value.setdefault("questions", [])
    question_ids = set()
    for question in collection(value.get("questions", []), "확인 질문", 40):
        if not isinstance(question, dict):
            raise StudioError("확인 질문 형식이 잘못되었습니다.")
        item_id = identifier(question.get("id"))
        if item_id in question_ids:
            raise StudioError("확인 질문 ID가 중복되었습니다.")
        question_ids.add(item_id)
        text(question.get("question"), "확인 질문", 4000)
        text(question.get("answer", ""), "질문 답변", 8000, False)
    docs = value.get("documents")
    if not isinstance(docs, dict) or set(docs) != set(DOCUMENTS):
        raise StudioError("필수 명세서 22종이 모두 있어야 합니다.")
    for name, content in docs.items():
        text(content, name, 60_000)
        if len(content.strip()) < 60 or not content.lstrip().startswith("#"):
            raise StudioError(f"{name}의 내용을 구체적으로 작성해주세요.")
    matrix = docs["요구사항추적성매트릭스"]
    if any(item not in matrix for item in seen | screen_ids):
        raise StudioError("추적성 매트릭스에 요구사항·화면 ID가 누락되었습니다.")
    if len(serialize(value).encode("utf-8")) > 900_000:
        raise StudioError("명세서 내용이 너무 큽니다. 범위를 나누어 생성해주세요.")
    return value


def references(value, allowed):
    values = collection(value, "연결된 요구사항", 200, 1)
    if any(not isinstance(item, str) or item not in allowed for item in values):
        raise StudioError("존재하지 않는 요구사항을 참조하고 있습니다.")
    return values


def validate_mock(value, specs):
    if not isinstance(value, dict) or set(value) - {"screens", "tone", "design_references"}:
        raise StudioError("목업은 화면 목록과 지정된 색상만 포함해야 합니다.")
    if value.get("tone", "indigo") not in TONES:
        raise StudioError("지원하지 않는 목업 색상입니다.")
    screens = collection(value.get("screens"), "목업 화면", 40, 1)
    expected = {s["id"] for s in specs["screens"]}
    actual = [identifier(s.get("id")) for s in screens if isinstance(s, dict)]
    if len(actual) != len(screens) or len(set(actual)) != len(actual) or set(actual) != expected:
        raise StudioError("목업 화면 ID와 확정 명세서의 화면 ID가 일치해야 합니다.")
    reqs = {r["id"] for r in specs["requirements"]}
    design_refs = value.get("design_references", [])
    collection(design_refs, "디자인 출처", 20)
    source_ids = set()
    for reference in design_refs:
        if not isinstance(reference, dict) or set(reference) != {"id", "provider", "source_digest"}:
            raise StudioError("디자인 출처 형식을 확인해주세요.")
        for field in ("id", "source_digest"):
            if not isinstance(reference[field], str) or not re.fullmatch(
                r"[a-f0-9]{64}", reference[field]
            ):
                raise StudioError("디자인 출처 해시를 확인해주세요.")
        if reference["provider"] not in {"figma", "stitch"}:
            raise StudioError("디자인 제공자를 확인해주세요.")
        source_ids.add(reference["id"])
    count = 0
    for screen in screens:
        if set(screen) - {"id", "title", "layout", "elements"}:
            raise StudioError("목업 화면에 지원하지 않는 실행 속성이 있습니다.")
        text(screen.get("title"), "목업 화면 이름", 200)
        if screen.get("layout") not in LAYOUTS:
            raise StudioError("지원하지 않는 목업 배치입니다.")
        ids = set()

        def nodes(elements, depth=0, ids=ids):
            nonlocal count
            if depth > 6:
                raise StudioError("목업 요소 중첩이 너무 깊습니다.")
            for element in collection(elements, "화면 요소", 150, 1 if depth == 0 else 0):
                count += 1
                if count > 600 or not isinstance(element, dict):
                    raise StudioError("목업 요소 수 또는 형식이 잘못되었습니다.")
                if set(element) - {
                    "id",
                    "kind",
                    "label",
                    "text",
                    "placeholder",
                    "options",
                    "columns",
                    "rows",
                    "children",
                    "requirement_ids",
                    "target_screen",
                    "reference_ids",
                }:
                    raise StudioError("목업에 임의 HTML·스타일·실행 코드를 넣을 수 없습니다.")
                item_id = identifier(element.get("id"))
                if item_id in ids or element.get("kind") not in KINDS:
                    raise StudioError("요소 ID가 중복되거나 지원하지 않는 요소입니다.")
                ids.add(item_id)
                text(element.get("label"), "요소 이름", 300)
                references(element.get("requirement_ids"), reqs)
                if "reference_ids" in element:
                    linked = collection(element["reference_ids"], "화면 참고 출처", 20)
                    if any(key not in source_ids for key in linked):
                        raise StudioError("연결되지 않은 화면 참고 자료입니다.")
                for field in ("text", "placeholder"):
                    if field in element:
                        text(element[field], "요소 내용", 8000, False)
                for field in ("columns", "options"):
                    if field in element:
                        for item in collection(element[field], "요소 선택지", 30):
                            text(item, "선택지", 500)
                if "rows" in element:
                    for row in collection(element["rows"], "샘플 행", 30):
                        for cell in collection(row, "샘플 셀", 30):
                            text(cell, "샘플 값", 500, False)
                if (
                    "target_screen" in element
                    and identifier(element["target_screen"]) not in expected
                ):
                    raise StudioError("목업 이동 대상이 존재하지 않습니다.")
                if "children" in element:
                    nodes(element["children"], depth + 1)

        nodes(screen.get("elements"))
    if len(serialize(value).encode("utf-8")) > 900_000:
        raise StudioError("목업 내용이 너무 큽니다. 화면 구성을 줄여주세요.")
    return value


def elements(mock, screen_id):
    screen = next((s for s in mock["screens"] if s["id"] == screen_id), None)
    if not screen:
        raise StudioError("화면을 찾을 수 없습니다.", 404)
    found = {}

    def visit(items):
        for item in items:
            found[item["id"]] = item
            visit(item.get("children", []))

    visit(screen["elements"])
    return found


def new_id(prefix):
    return prefix + uuid.uuid4().hex


def serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
