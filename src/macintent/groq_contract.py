"""Current action schemas and bounded original-plus-review-note messages."""

from copy import deepcopy
import json

from . import workspace_contract
from .action_types import MAX_COMMAND, ActionError

EXPLANATION_TOOL = "explain_no_action"
MAX_EXPLANATION_CHARS = 1024
MAX_CORRECTIONS = 8
MAX_CORRECTION_TOTAL_CHARS = MAX_COMMAND
MAX_CORRECTION_CONTEXT_BYTES = 32 * 1024


def _action_tools() -> list[dict]:
    tools = deepcopy(workspace_contract.TOOLS)
    descriptions = {
        "copy_file": (
            "Copy one workspace file to a new destination file. Supply both full relative paths: "
            "source = source folder/filename; destination = destination folder/new filename, or the "
            "same basename if not renamed. Copying under a new name is this ONE action. "
            "The application checks existence, prevents overwrite, and asks approval."
        ),
        "create_text_file": (
            "Create one new nonblank .txt note. A request naming a file, its parent folder, and text "
            "to write is complete: filename is the basename, directory is the parent, content is the "
            "literal requested text. The application checks the parent and asks approval. "
            "If the text is absent, ask what to write instead of inventing a body."
        ),
    }
    for tool in tools:
        if tool["name"] in descriptions:
            tool["description"] = descriptions[tool["name"]]
    return tools


TOOLS = [
    *_action_tools(),
    {
        "name": EXPLANATION_TOOL,
        "description": (
            "Explain why no action can be selected: missing essential information, ambiguity, an unsupported/unsafe "
            "request, or multiple actions. Ask a specific complete clarification question, not a bare filename. "
            "No filesystem effects. Do not use for a clear supported action or merely repairable .txt extension formatting."
        ),
        "parameters": {
            "type": "object",
            "properties": {"reason": {
                "type": "string", "minLength": 1, "maxLength": MAX_EXPLANATION_CHARS,
                "description": (
                    "A complete user-facing explanation or specific clarification question naming what is missing. "
                    "For a file request without a body, ask what text it should contain and request a complete new request. "
                    "Never return only a filename, echo the request, or claim execution."
                ),
            }},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]


def transcript_messages(transcript: str, *, system_prompt: str, corrections: list[str] | None = None) -> list[dict[str, str]]:
    workspace_contract.validate_transcript(transcript)
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": transcript}]
    if corrections is not None:
        if type(corrections) is not list or len(corrections) > MAX_CORRECTIONS:
            raise ActionError("invalid_command")
        for correction in corrections:
            workspace_contract.validate_transcript(correction)
        if sum(map(len, corrections)) > MAX_CORRECTION_TOTAL_CHARS:
            raise ActionError("invalid_command")
        if corrections:
            context = json.dumps({"review_corrections": corrections}, ensure_ascii=True, separators=(",", ":"))
            if len(context.encode("utf-8")) > MAX_CORRECTION_CONTEXT_BYTES:
                raise ActionError("invalid_command")
            messages.append({"role": "user", "content": context})
    return messages
