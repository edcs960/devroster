from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_dev.domain.legacy import parse_task_status
from agent_dev.domain.status import TaskStatus


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    display_name: str
    role_file: str
    code_allowed: bool
    summary: str
    required_context: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineStep:
    step_id: str
    agent_id: str
    output_file: str
    prompt_file: str
    run_condition: str = "always"


@dataclass(frozen=True)
class IssueDecision:
    handoff_status: str
    issue_type: str
    owning_agent_id: str
    root_cause: str
    reentry_step_id: str
    summary: str


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    priority: str
    status: str
    requirement_ids: list[str]
    scope: list[str]
    related_documents: list[str]
    acceptance_criteria: list[str]
    depends_on: list[str] = field(default_factory=list)
    workstreams: list[str] = field(default_factory=list)
    system_name: str = ""

    @property
    def domain_status(self) -> TaskStatus:
        """Read-only canonical projection; storage and execution keep legacy strings."""
        return parse_task_status(self.status)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            task_id=data["id"],
            title=data["title"],
            priority=data.get("priority", "medium"),
            status=data.get("status", "ready"),
            requirement_ids=list(data.get("requirement_ids", [])),
            scope=list(data.get("scope", [])),
            related_documents=list(data.get("related_documents", [])),
            acceptance_criteria=list(data.get("acceptance_criteria", [])),
            depends_on=list(data.get("depends_on", [])),
            workstreams=list(data.get("workstreams", [])),
            system_name=str(data.get("system_name", "")).strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "title": self.title,
            "priority": self.priority,
            "status": self.status,
            "requirement_ids": self.requirement_ids,
            "scope": self.scope,
            "related_documents": self.related_documents,
            "acceptance_criteria": self.acceptance_criteria,
            "depends_on": self.depends_on,
            "workstreams": self.workstreams,
            "system_name": self.system_name,
        }
