"""Safe File Janitor sandbox tools."""

import copy
import json
import time
from pathlib import Path

from app.config import ROOT
from app.models import (
    FileRecord,
    FindDuplicatesInput,
    InspectFileInput,
    ListFilesInput,
    MoveFileInput,
    ToolResult,
)


class ToolTimeout(Exception):
    pass


class ToolExecutionError(Exception):
    pass


def load_sandbox() -> list[FileRecord]:
    """Load a fresh copy for every Arena run."""

    path = ROOT / "data" / "sample_data.json"

    data = json.loads(path.read_text(encoding="utf-8"))

    return [
        FileRecord.model_validate(record)
        for record in copy.deepcopy(data.get("records", []))
    ]


def _safe_path(path: str) -> bool:
    """Only allow paths inside our virtual sandbox."""

    if not path:
        return False

    if not path.startswith("/"):
        return False

    if ".." in Path(path).parts:
        return False

    return True


def list_files(
    files: list[FileRecord],
    args: ListFilesInput,
) -> ToolResult:

    if not _safe_path(args.path):
        return ToolResult(
            success=False,
            message="Rejected unsafe path.",
        )

    if args.path == "/":
        selected = files
    else:
        selected = [
            f for f in files
            if f.path.startswith(args.path.rstrip("/") + "/")
        ]

    return ToolResult(
        success=True,
        message=f"Found {len(selected)} file(s).",
        data={
            "files": [
                f.model_dump()
                for f in selected
            ]
        },
    )


def inspect_file(
    files: list[FileRecord],
    args: InspectFileInput,
) -> ToolResult:

    if not _safe_path(args.path):
        return ToolResult(
            success=False,
            message="Rejected unsafe path.",
        )

    for file in files:
        if file.path == args.path:
            return ToolResult(
                success=True,
                message=f"File found: {file.name}",
                data={
                    "file": file.model_dump()
                },
            )

    return ToolResult(
        success=False,
        message="File was not found.",
    )


def find_duplicates(
    files: list[FileRecord],
    args: FindDuplicatesInput,
) -> ToolResult:

    if not _safe_path(args.path):
        return ToolResult(
            success=False,
            message="Rejected unsafe path.",
        )

    selected = [
        f for f in files
        if args.path == "/"
        or f.path.startswith(args.path.rstrip("/") + "/")
    ]

    groups = {}

    for file in selected:
        groups.setdefault(file.checksum, []).append(file)

    duplicates = [
        group
        for group in groups.values()
        if len(group) > 1
    ]

    return ToolResult(
        success=True,
        message=f"Found {len(duplicates)} duplicate group(s).",
        data={
            "duplicate_groups": [
                [
                    file.model_dump()
                    for file in group
                ]
                for group in duplicates
            ]
        },
    )


def move_file(
    files: list[FileRecord],
    args: MoveFileInput,
) -> ToolResult:

    if not _safe_path(args.source):
        return ToolResult(
            success=False,
            message="Rejected unsafe source path.",
        )

    if not _safe_path(args.destination):
        return ToolResult(
            success=False,
            message="Rejected unsafe destination path.",
        )

    source_file = None

    for file in files:
        if file.path == args.source:
            source_file = file
            break

    if source_file is None:
        return ToolResult(
            success=False,
            message="Source file was not found.",
        )

    # Destination must represent a directory.
    destination = args.destination.rstrip("/")
    last = destination.split("/")[-1]
    if last == source_file.name or last.endswith("." + source_file.file_type):
        return ToolResult(
            success=False,
            message="Destination must be a folder, not a file path. Example: /sandbox/pdf",
        )
    if destination == "":
        destination = "/"

    new_path = destination + "/" + source_file.name

    for file in files:
        if file.path == new_path:
            return ToolResult(
                success=False,
                message="A file with the same name already exists at the destination.",
            )

    source_file.path = new_path

    return ToolResult(
        success=True,
        message=f"Moved {source_file.name} to {new_path}.",
        data={
            "file": source_file.model_dump()
        },
    )


TOOL_INPUTS = {
    "list_files": ListFilesInput,
    "inspect_file": InspectFileInput,
    "find_duplicates": FindDuplicatesInput,
    "move_file": MoveFileInput,
}


TOOLS = {
    "list_files": list_files,
    "inspect_file": inspect_file,
    "find_duplicates": find_duplicates,
    "move_file": move_file,
}