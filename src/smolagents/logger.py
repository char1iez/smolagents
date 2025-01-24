import json
from dataclasses import asdict, dataclass
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Dict, List

from rich.console import Console
from rich.rule import Rule
from rich.syntax import Syntax

from smolagents.models import MessageRole
from smolagents.utils import AgentError, make_json_serializable


if TYPE_CHECKING:
    from smolagents.models import ChatMessage


console = Console()


@dataclass
class Message:
    role: MessageRole
    content: str | list[dict]

    def dict(self):
        return asdict(self)


@dataclass
class ToolCall:
    name: str
    arguments: Any
    id: str

    def dict(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": make_json_serializable(self.arguments),
            },
        }


class AgentStepLog:
    raw: Any  # This is a placeholder for the raw data that the agent logs

    def dict(self):
        return asdict(self)

    def to_messages(self, **kwargs) -> List[Dict[str, Any]]:
        raise NotImplementedError


@dataclass
class ActionStep(AgentStepLog):
    agent_memory: List[Dict[str, str]] | None = None
    tool_calls: List[ToolCall] | None = None
    start_time: float | None = None
    end_time: float | None = None
    step_number: int | None = None
    error: AgentError | None = None
    duration: float | None = None
    llm_output: str | None = None
    observations: str | None = None
    observations_images: List[str] | None = None
    action_output: Any = None

    def dict(self):
        # We overwrite the method to parse the tool_calls and action_output manually
        return {
            "agent_memory": self.agent_memory,
            "tool_calls": [tc.dict() for tc in self.tool_calls] if self.tool_calls else [],
            "start_time": self.start_time,
            "end_time": self.end_time,
            "step": self.step_number,
            "error": self.error.dict() if self.error else None,
            "duration": self.duration,
            "llm_output": self.llm_output,
            "observations": self.observations,
            "action_output": make_json_serializable(self.action_output),
        }

    def to_messages(self, summary_mode: bool, return_memory: bool) -> List[Dict[str, Any]]:
        memory = []
        if self.agent_memory is not None and return_memory:
            message = Message(MessageRole.SYSTEM, self.agent_memory)
            memory.append(message.dict())
        if self.llm_output is not None and not summary_mode:
            message = Message(MessageRole.ASSISTANT, [{"type": "text", "text": self.llm_output.strip()}])
            memory.append(message.dict())

        if self.tool_calls is not None:
            message = Message(
                MessageRole.ASSISTANT, [{"type": "text", "text": str([tc.dict() for tc in self.tool_calls])}]
            )
            memory.append(message.dict())

        if self.error is not None:
            message_content = (
                "Error:\n"
                + str(self.error)
                + "\nNow let's retry: take care not to repeat previous errors! If you have retried several times, try a completely different approach.\n"
            )
            if self.tool_calls is None:
                tool_response_message = Message(MessageRole.ASSISTANT, [{"type": "text", "text": message_content}])
            else:
                tool_response_message = Message(
                    MessageRole.TOOL_RESPONSE, f"Call id: {self.tool_calls[0].id}\n{message_content}"
                )

            memory.append(tool_response_message.dict())
        else:
            if self.observations is not None and self.tool_calls is not None:
                tool_response_message = Message(
                    MessageRole.TOOL_RESPONSE,
                    f"Call id: {self.tool_calls[0].id}\nObservation:\n{self.observations}",
                )
                memory.append(tool_response_message.dict())
        if self.observations_images:
            thought_message_image = Message(
                MessageRole.USER,
                [{"type": "text", "text": "Here are the observed images:"}]
                + [
                    {
                        "type": "image",
                        "image": image,
                    }
                    for image in self.observations_images
                ],
            )
            memory.append(thought_message_image.dict())
        return memory


@dataclass
class PlanningStep(AgentStepLog):
    plan: str
    facts: str

    def to_messages(self, summary_mode: bool, **kwargs) -> List[Dict[str, str]]:
        memory = []
        thought_message = Message(MessageRole.ASSISTANT, f"[FACTS LIST]:\n{self.facts.strip()}")
        memory.append(thought_message.dict())

        if not summary_mode:
            thought_message = Message(MessageRole.ASSISTANT, f"[PLAN]:\n{self.plan.strip()}")
            memory.append(thought_message.dict())
        return memory


@dataclass
class TaskStep(AgentStepLog):
    task: str
    task_images: List[str] | None = None

    def to_messages(self, summary_mode: bool, **kwargs) -> List[Dict[str, str]]:
        content = [{"type": "text", "text": f"New task:\n{self.task}"}]
        if self.task_images:
            for image in self.task_images:
                content.append({"type": "image", "image": image})

        message = Message(MessageRole.USER, content)
        return [message.dict()]


@dataclass
class SystemPromptStep(AgentStepLog):
    system_prompt: str

    def to_messages(self, summary_mode: bool, **kwargs) -> List[Dict[str, str]]:
        if not summary_mode:
            message = Message(MessageRole.SYSTEM, [{"type": "text", "text": self.system_prompt.strip()}])
            return [message.dict()]
        return []


class LogLevel(IntEnum):
    ERROR = 0  # Only errors
    INFO = 1  # Normal output (default)
    DEBUG = 2  # Detailed output


class AgentLogger:
    def __init__(self, level: LogLevel = LogLevel.INFO):
        self.level = level
        self.steps: List[ActionStep] = []
        self.chat_messages: List[ChatMessage] = []
        self.console = Console()

    def reset(self):
        self.steps = []

    def log(self, *args, level: str | LogLevel = LogLevel.INFO, **kwargs):
        """Logs a message to the console.

        Args:
            level (LogLevel, optional): Defaults to LogLevel.INFO.
        """
        if isinstance(level, str):
            level = LogLevel[level.upper()]
        if level <= self.level:
            self.console.print(*args, **kwargs)

    def log_step(self, step: AgentStepLog, position: int = None):
        """Logs an agent execution step for ulterior processing.

        Args:
            step (AgentStepLog)
            position (int, optional): Position at which to insert the item.
                Defaults to None, in which case item is added to the end of the list.
                Should only be used to insert the system prompt at the start of the list.
        """
        if position is None:
            self.steps.append(step)
        else:
            assert position == 0, "Position should only be 0 for system prompt."
            self.steps = [step] + self.steps[1:]  # we replace the system prompt

    def log_chat_messages(self, msg: "ChatMessage"):
        """Logs all raw model outputs throughout the run, for debugging purposes."""
        self.chat_messages.append(msg)

    def get_succinct_logs(self):
        return [{key: value for key, value in log.items() if key != "agent_memory"} for log in self.steps]

    def get_extended_logs(self) -> list[dict]:
        return {
            "steps": [step.dict() for step in self.steps],
            "chat_messages": [msg.dict() for msg in self.chat_messages],
        }

    def replay(self, with_memory: bool = False):
        """Prints a pretty replay of the agent's steps.

        Args:
            with_memory (bool, optional): If True, also displays the memory at each step. Defaults to False.
                Careful: will increase log length exponentially. Use only for debugging.
        """
        memory = []
        for step_log in self.logger.steps:
            memory.extend(step_log.to_messages(return_memory=with_memory))

        self.console.log("Replaying the agent's steps:")
        ix = 0
        for step in memory:
            role = step["role"].strip()
            if ix > 0 and role == "system":
                role == "memory"
            theme = "default"
            match role:
                case "assistant":
                    theme = "monokai"
                    ix += 1
                case "system":
                    theme = "monokai"
                case "tool-response":
                    theme = "github_dark"

            content = step["content"]
            try:
                content = eval(content)
            except Exception:
                content = [step["content"]]

            for substep_ix, item in enumerate(content):
                self.console.log(
                    Rule(
                        f"{role.upper()}, STEP {ix}, SUBSTEP {substep_ix + 1}/{len(content)}",
                        align="center",
                        style="orange",
                    ),
                    Syntax(
                        json.dumps(item, indent=4) if isinstance(item, dict) else str(item),
                        lexer="json",
                        theme=theme,
                        word_wrap=True,
                    ),
                )


__all__ = ["AgentLogger"]
