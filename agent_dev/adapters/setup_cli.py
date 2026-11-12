"""Interactive and repeatable setup using the same service as the web UI."""

import getpass
import json
import shlex
import sys
from pathlib import Path

from agent_dev.adapters.ai_providers import API_PROVIDERS, PROVIDERS
from agent_dev.adapters.environment import MASK
from agent_dev.adapters.presets import profile as preset_profile
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.domain.errors import DomainError, ErrorCode


def wizard(root):
    if not sys.stdin.isatty():
        raise DomainError(
            ErrorCode.INVALID_INPUT,
            "터미널에서 devroster setup을 실행하거나 --web / --from FILE을 사용해주세요.",
        )
    print("프로젝트 설정: Enter를 누르면 현재 값을 유지합니다. 로그인은 각 CLI에서 진행해주세요.")

    def ask(label, default=""):
        return input(f"{label} [{default}]: ").strip() or default

    selected = Path(ask("최상위 프로젝트 폴더", str(ProjectSetup(root).root))).expanduser()
    service = ProjectSetup(selected)
    value = service.read()
    value["name"] = ask("프로젝트 이름", value["name"])
    value["project_id"] = ask("프로젝트 ID", value["project_id"])
    print(
        "모듈을 비워두면 기존 구성을 유지합니다. 프론트·백은 상대 경로 또는 별도 저장소 절대 경로를 입력하세요."
    )
    modules = []
    choices = [("source", "공통 소스"), ("frontend", "프론트엔드"), ("backend", "백엔드")] + [
        (row["id"], row["id"])
        for row in value["modules"]
        if row["id"] not in {"source", "frontend", "backend"}
    ]
    for key, label in choices:
        existing = next((row for row in value["modules"] if row["id"] == key), None)
        path = input(
            f"{label} 폴더 [{existing['path'] if existing else '사용하지 않음'}]: "
        ).strip()
        if path == "-":
            continue
        if not path and not existing:
            continue
        preset = ask(
            label + " 기술 (generic/python/react/spring-boot/spring-boot-gradle)",
            (existing or {}).get("preset", "generic"),
        )
        defaults = preset_profile(preset)
        allowed = ask(
            label + " 수정 허용 경로 (쉼표 구분)",
            ",".join((existing or {}).get("allowed_paths", defaults["allowed_paths"])),
        )
        modules.append(
            {
                "id": key,
                "path": path or (existing or {})["path"],
                "preset": preset,
                "allowed_paths": [p.strip() for p in allowed.split(",")],
                "base_ref": ask(label + " Git 기준 ref", (existing or {}).get("base_ref", "HEAD")),
            }
        )
    if modules:
        value["modules"] = modules
        if ask("모듈 기술의 기본 검증 명령으로 바꾸기 (y/N)", "N").lower() == "y":
            value["verification"] = [
                dict(row, name=module["id"] + "_" + row["name"], module=module["id"])
                for module in modules
                for row in preset_profile(module["preset"])["verification"]["commands"]
            ]
    provider = ask("기본 생성 AI (" + "/".join(PROVIDERS) + ")", value["ai"]["provider"])
    if provider not in value["ai"]["runners"]:
        raise DomainError(ErrorCode.INVALID_INPUT, "지원 목록의 AI 도구를 선택해주세요.")
    value["ai"]["provider"] = provider
    for key, row in value["ai"]["runners"].items():
        row["enabled"] = (
            key == provider
            or ask(key + "도 사용하기 (y/N)", "y" if row["enabled"] else "N").lower() == "y"
        )
        if row["enabled"]:
            if key in API_PROVIDERS:
                row["base_url"] = ask(key + " API 주소", row["base_url"])
                row["credential_ref"] = ask(
                    key + " 인증 환경변수 이름 (인증 없는 로컬 서버는 '-')",
                    row["credential_ref"] or "-",
                )
                if row["credential_ref"] == "-":
                    row["credential_ref"] = ""
                row["max_output_tokens"] = int(
                    ask(key + " 출력 토큰 상한", str(row["max_output_tokens"]))
                )
                row["reasoning"] = ask(key + " 추론 옵션 (native/none)", row["reasoning"])
            else:
                row["command"] = ask(key + " 실행파일", row["command"])
            row["model"] = ask(key + " 모델 (API는 필수, CLI 기본은 '-')", row["model"])
            if row["model"] == "-":
                row["model"] = ""
    from agent_dev.domain.model_routing import routing_policy

    for key, row in value["ai"]["runners"].items():
        if not row["enabled"]:
            continue
        row["effort"] = ask(
            key + " 고정 추론 강도 (low/medium/high/xhigh/max, CLI 기본은 '-')",
            row.get("effort") or "-",
        )
        if row["effort"] == "-":
            row["effort"] = ""
        policy = routing_policy(row.get("routing"))
        policy["mode"] = ask(key + " 모델 선택 (fixed/auto 균형형)", policy["mode"])
        if policy["mode"] == "auto":
            policy["max_effort"] = ask(
                "자동 추론 강도 상한 (low/medium/high)", policy["max_effort"]
            )
            for tier, target in zip(("simple", "standard", "complex"), ("low", "medium", "high")):
                candidate = policy["profiles"][tier]
                candidate["model"] = ask(
                    f"{key} {tier} 작업 모델 (목표 {target})", candidate["model"] or row["model"]
                )
                levels = ask(
                    "이 모델이 지원하는 강도 (쉼표 구분, CLI 기본만 사용은 '-')",
                    ",".join(candidate["supported_efforts"]) or "-",
                )
                candidate["supported_efforts"] = (
                    [] if levels == "-" else [level.strip() for level in levels.split(",")]
                )
        row["routing"] = policy
    print("현재 검증 명령: " + ", ".join(row["name"] for row in value["verification"]))
    if ask("검증 명령 직접 입력하기 (y/N)", "N").lower() == "y":
        value["verification"] = []
        while True:
            name = input("검증 이름 (끝내려면 Enter): ").strip()
            if not name:
                break
            value["verification"].append(
                {
                    "name": name,
                    "module": ask("모듈 이름", value["modules"][0]["id"]),
                    "command": shlex.split(input("명령 (따옴표로 인수 구분, shell 사용 안함): ")),
                    "cwd": ask("모듈 기준 실행 폴더", "."),
                    "timeout_seconds": int(ask("제한시간(초)", "900")),
                    "required": True,
                }
            )
    for key, val in value["limits"].items():
        value["limits"][key] = int(ask("실행 예산 " + key, str(val)))
    for key, val in value["ports"].items():
        value["ports"][key] = int(ask(key + " 웹 포트", str(val)))
    print("환경변수 값은 화면에 표시하지 않습니다. 기존 이름 입력으로 변경, -이름으로 삭제합니다.")
    env = {row["key"]: row["value"] for row in value["environment"]}
    while True:
        key = input("환경변수 이름 (끝내려면 Enter): ").strip()
        if not key:
            break
        if key.startswith("-"):
            env.pop(key[1:], None)
        else:
            val = getpass.getpass("새 값 (Enter=기존 값 유지): ")
            env[key] = val or env.get(key, MASK)
    value["environment"] = [{"key": key, "value": val} for key, val in env.items()]
    value["create_directories"] = ask("없는 프로젝트 내부 폴더 생성 (y/N)", "N").lower() == "y"
    auth = ask("저장하며 CLI 로그인 상태 확인 (Y/n)", "Y").lower() != "n"
    return service.save(value, check_auth=auth)


def run(args):
    service = ProjectSetup(args.root)
    if args.copy and not args.source:
        raise DomainError(ErrorCode.INVALID_INPUT, "--copy는 --from FILE과 함께 사용해주세요.")
    if args.web:
        from agent_dev.adapters.command_line import web_server

        return web_server("setup")(
            args.root, args.port if args.port is not None else service.read()["ports"]["setup"]
        )
    if args.show:
        return service.read()
    if args.check:
        return service.check(check_auth=args.check_auth)
    if getattr(args, "preview_task", None) is not None:
        return service.preview(service.read(), {"title": args.preview_task}, args.preview_phase)
    if args.source:
        source = Path(args.source)
        if source.stat().st_size > 1_000_000:
            raise DomainError(ErrorCode.INVALID_INPUT, "설정 파일은 1MB 이하로 줄여주세요.")
        value = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "설정 파일은 설정 객체를 담은 JSON이어야 합니다."
            )
        if args.copy:
            target = service.read()
            origin = Path(value.get("root", str(service.root))).expanduser().resolve()
            for module in value["modules"]:
                path = Path(module["path"]).expanduser()
                if path.is_absolute() and path.resolve().is_relative_to(origin):
                    module["path"] = path.resolve().relative_to(origin).as_posix()
            value.update(
                root=target["root"],
                configuration_root=target["configuration_root"],
                project_id=target["project_id"],
                name=target["name"],
                revision=target["revision"],
                profile_version=target["profile_version"],
                create_directories=True,
            )
            target_environment = {row["key"]: row["value"] for row in target["environment"]}
            for row in value.get("environment", []):
                target_environment.setdefault(row["key"], "")
            value["environment"] = [
                {"key": key, "value": val} for key, val in target_environment.items()
            ]
        result = service.save(value, check_auth=args.check_auth)
        if args.copy and value.get("environment"):
            result["warnings"].append(
                "환경변수 값은 복사하지 않았습니다. 웹·터미널 설정에서 새 프로젝트의 값을 입력해주세요."
            )
        return result
    return wizard(args.root)


LOCATION_OPERATIONS = ("show", "plan", "apply", "recover")


def _invalid(message):
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def settings_location(args):
    """devroster settings location show|plan DEST --scope S|apply PLAN_ID|recover ID --action A."""
    from agent_dev.adapters import storage_migration
    from agent_dev.adapters.workspace_locator import location_report

    operation, target = args.operation, args.target
    if operation not in LOCATION_OPERATIONS:
        _invalid("사용할 수 있는 동작: " + ", ".join(LOCATION_OPERATIONS))
    if operation == "show":
        return location_report(args.root)
    if not target:
        _invalid("대상(새 위치 또는 이전 ID)을 입력해주세요.")
    if operation == "plan":
        if not args.scope:
            _invalid("--scope workspace 또는 --scope management를 지정해주세요.")
        source = args.root if args.scope == "workspace" else storage_migration.management_root()
        return storage_migration.plan_move(source, Path(target), args.scope)
    if operation == "apply":
        expected = args.expected_revision
        if expected is None:
            expected = storage_migration.journal(target)["registry_revision"]
        return storage_migration.apply_move(target, expected_registry_revision=expected)
    if not args.recover_action:
        _invalid("--action resume 또는 --action rollback을 지정해주세요.")
    return storage_migration.recover_move(target, args.recover_action)
