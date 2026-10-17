"""A discovery-driven Google API interface using this channel's OAuth token.

No endpoint wrappers or handwritten method/metric tables: the installed Google
client's official discovery documents define resource paths, schemas and verbs.
"""

import json
import uuid
from datetime import UTC, datetime

from googleapiclient.discovery import build
from googleapiclient.discovery_cache import get_static_doc
from googleapiclient.http import MediaFileUpload

from .channel import Channel
from .common import write_json_file
from .upload import get_credentials


def specification(
    api: str, version: str, pointer: str, query: str | None, offset: int, limit: int
) -> dict:
    raw = get_static_doc(api, version)
    if raw is None:
        raise ValueError(
            "No official discovery document in the installed Google client."
        )
    document = json.loads(raw)
    value = document
    if pointer:
        if not pointer.startswith("/"):
            raise ValueError("pointer must be a JSON Pointer starting with /.")
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
    if query is None:
        return {"pointer": pointer, "value": value}
    if offset < 0 or not 1 <= limit <= 200:
        raise ValueError("offset must be nonnegative and limit between 1 and 200.")
    matches = []

    def search(node, path):
        if isinstance(node, dict):
            for key, child in node.items():
                escaped = key.replace("~", "~0").replace("/", "~1")
                search(child, path + "/" + escaped)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                search(child, path + "/" + str(index))
        if not isinstance(node, (dict, list)) and (
            query.casefold() in path.casefold()
            or query.casefold() in str(node).casefold()
        ):
            matches.append({"pointer": path, "value": node})

    search(value, pointer)
    stop = min(offset + limit, len(matches))
    return {
        "matches": matches[offset:stop],
        "match_count": len(matches),
        "next_offset": stop if stop < len(matches) else None,
    }


def request(
    channel: Channel,
    api: str,
    version: str,
    method: str,
    parameters: dict,
    mutation: bool,
    media_path: str | None = None,
    observation: dict | None = None,
):
    service = build(
        api,
        version,
        credentials=get_credentials(channel.token_file),
        cache_discovery=False,
    )
    spec = service._rootDesc
    resource = service
    parts = method.split(".")
    for name in parts[:-1]:
        spec = spec["resources"][name]
        resource = getattr(resource, name)()
    method_spec = spec["methods"][parts[-1]]
    read_only = method_spec["httpMethod"] in {"GET", "HEAD", "OPTIONS"}
    if read_only == mutation:
        raise ValueError(
            "Use google_api_read for read verbs and google_api_write for mutations."
        )
    arguments = dict(parameters)
    # Transport controls cannot be supplied as ordinary API parameters.
    if "media_body" in arguments or "media_mime_type" in arguments:
        raise ValueError("Use media_path for an upload.")
    if media_path is not None:
        from pathlib import Path

        path = Path(media_path).resolve()
        if (
            not mutation
            or not path.is_relative_to(channel.dir.resolve())
            or not path.is_file()
            or any(
                part.startswith(".")
                for part in path.relative_to(channel.dir.resolve()).parts
            )
        ):
            raise ValueError("Media must be a file inside this channel's directory.")
        if not method_spec.get("mediaUpload"):
            raise ValueError("This method does not accept media uploads.")
        arguments["media_body"] = MediaFileUpload(str(path))
    operation = getattr(resource, parts[-1])(**arguments)
    if not mutation:
        return operation.execute(num_retries=0)
    # Persist intent BEFORE the external write. A lost response is ambiguous,
    # so the agent must inspect the remote state before retrying it.
    identity = uuid.uuid4().hex
    path = channel.dir / "api-writes" / f"{identity}.json"
    audit = {
        "id": identity,
        "api": api,
        "version": version,
        "method": method,
        "parameters": parameters,
        "media_path": media_path,
        "observation": observation,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "started",
    }
    write_json_file(path, audit)
    try:
        response = operation.execute(num_retries=0)
    except Exception as exc:
        audit.update(status="unconfirmed", error=f"{type(exc).__name__}: {exc}")
        write_json_file(path, audit)
        raise
    audit.update(
        status="succeeded", response=response, finished_at=datetime.now(UTC).isoformat()
    )
    write_json_file(path, audit)
    return {"response": response, "audit_id": identity}
