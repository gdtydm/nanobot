"""Native Antigravity messages and response assembly, independent of UI/runtime policy."""

from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from nanobot.providers.base import (
    LLMResponse,
    LLMUsage,
    ModelCommentary,
    ToolCallRequest,
    parse_tool_arguments,
)


def object_value(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Antigravity returned invalid {label}.")
    return cast(dict[str, Any], value)


def object_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"Antigravity returned invalid {label}.")
    return [object_value(item, label) for item in cast(list[object], value)]


def _content_parts(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if content is None:
        return []
    if not isinstance(content, list):
        raise ValueError("Antigravity message content must be text or content blocks.")
    result: list[dict[str, Any]] = []
    for value in cast(list[object], content):
        block = object_value(value, "content block")
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            result.append({"text": block["text"]})
        elif block.get("type") == "image_url":
            image = object_value(block.get("image_url"), "image")
            url = image.get("url")
            match = re.fullmatch(r"data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=\s]+)", url) if isinstance(url, str) else None
            if match is None:
                raise ValueError("Antigravity image inputs must use inline image data.")
            result.append({"inlineData": {"mimeType": match[1], "data": match[2]}})
        else:
            raise ValueError("This content block is not supported by Antigravity.")
    return result


def convert_messages(
    messages: list[dict[str, Any]], *, previous: list[dict[str, Any]] | None = None,
    system: dict[str, Any] | None = None,
    identity: str | None = None, model: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    contents = deepcopy(previous or [])
    systems: list[dict[str, Any]] = deepcopy(object_list(system.get("parts", []), "system parts")) if system else []
    call_names: dict[str, str] = {}
    for item in contents:
        for part in item.get("parts", []):
            raw_call = part.get("functionCall")
            if raw_call is not None:
                call = object_value(raw_call, "function call")
                if isinstance(call.get("id"), str) and isinstance(call.get("name"), str):
                    call_names[call["id"]] = call["name"]
    for message in messages:
        role = message.get("role")
        if role in {"system", "developer"}:
            systems.extend(_content_parts(message.get("content")))
            continue
        parts: list[dict[str, Any]]
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in call_names:
                raise ValueError("Antigravity tool result has no matching tool call in this conversation.")
            content = message.get("content")
            result = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            parts = [{"functionResponse": {"id": call_id, "name": call_names[call_id], "response": {"result": result}}}]
        elif role in {"user", "assistant"}:
            parts = _content_parts(message.get("content"))
            if role == "assistant":
                # Native state retains thought parts. For imported Chat history,
                # only replay thinking blocks that carry their original signature.
                thinking = object_list(message.get("thinking_blocks") or [], "thinking blocks")
                for block in thinking:
                    if block.get("signature") and isinstance(block.get("thinking"), str):
                        _check_signature_scope(block.get("antigravity"), identity, model)
                        parts.insert(0, {"text": block["thinking"], "thought": True, "thoughtSignature": block["signature"]})
                for tool in object_list(message.get("tool_calls") or [], "tool calls"):
                    fn = object_value(tool.get("function"), "tool call")
                    name, call_id = fn.get("name"), tool.get("id")
                    if not isinstance(name, str) or not isinstance(call_id, str):
                        raise ValueError("Antigravity tool calls require a name and an ID.")
                    call_names[call_id] = name
                    part: dict[str, Any] = {"functionCall": {
                        "id": call_id, "name": name, "args": parse_tool_arguments(fn.get("arguments", {})),
                    }}
                    extra = object_value(tool.get("extra_content") or {}, "tool metadata")
                    signature = object_value(extra.get("google") or {}, "Google tool metadata").get("thought_signature")
                    if signature:
                        _check_signature_scope(extra.get("antigravity"), identity, model)
                        part["thoughtSignature"] = signature
                    parts.append(part)
        else:
            raise ValueError("Unsupported Antigravity message role.")
        if not parts:
            continue
        native_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == native_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": native_role, "parts": parts})
    return contents, {"parts": systems} if systems else None


def _check_signature_scope(value: object, identity: str | None, model: str | None) -> None:
    if value is None:
        return
    scope = object_value(value, "signature scope")
    if scope.get("identity") != identity or scope.get("model") != model:
        raise ValueError("Start a new chat after changing the Antigravity account or model for signed tool history.")


def native_tool_schema(schema: object) -> dict[str, Any]:
    """Project JSON Schema into the Cloud Code Schema dialect.

    Only declaration schemas are rewritten; tool arguments/results retain their
    exact values. The gateway still validates calls against the original schema.
    Local refs and nullable type arrays occur in nanobot and MCP declarations.
    """
    root = object_value(schema, "tool schema")
    allowed = {"type", "properties", "required", "items", "description", "enum", "format", "nullable", "anyOf"}

    def convert(value: object, refs: tuple[str, ...] = (), depth: int = 0) -> dict[str, Any]:
        if depth > 64:
            raise ValueError("Antigravity tool schema exceeds the supported nesting depth.")
        node = deepcopy(object_value(value, "tool schema node"))
        ref = node.pop("$ref", None)
        if ref is not None:
            if not isinstance(ref, str) or not ref.startswith("#/") or ref in refs:
                raise ValueError("Antigravity tool schemas require non-recursive local references.")
            target: object = root
            for key in ref[2:].split("/"):
                target = object_value(target, "schema reference").get(key.replace("~1", "/").replace("~0", "~"))
            node = {**convert(target, (*refs, ref), depth + 1), **node}
        node.pop("$defs", None)
        node.pop("definitions", None)
        node.pop("$schema", None)
        node.pop("$id", None)
        node.pop("title", None)
        if "allOf" in node:
            branches = object_list(node.pop("allOf"), "allOf branches")
            for branch in branches:
                merged = convert(branch, refs, depth + 1)
                for key, item in merged.items():
                    if key == "properties":
                        node[key] = {**object_value(node.get(key, {}), "schema properties"), **object_value(item, "schema properties")}
                    elif key == "required":
                        existing: list[str] = node.get("required", [])
                        node[key] = list(dict.fromkeys([*existing, *item]))
                    elif key not in node:
                        node[key] = item
                    elif node[key] != item:
                        raise ValueError("Antigravity cannot represent conflicting allOf tool constraints.")
        types = node.get("type")
        if isinstance(types, list):
            alternatives = cast(list[object], types)
            if not alternatives or any(not isinstance(item, str) for item in alternatives):
                raise ValueError("Antigravity tool schema contains invalid types.")
            non_null = [item for item in alternatives if item != "null"]
            if "null" in alternatives:
                node["nullable"] = True
            if len(non_null) == 1:
                node["type"] = non_null[0]
            else:
                node.pop("type")
                node["anyOf"] = [{"type": item} for item in non_null]
        if "oneOf" in node:
            if "anyOf" in node:
                raise ValueError("Antigravity cannot combine oneOf and anyOf in a single tool schema node.")
            node["anyOf"] = node.pop("oneOf")
        if "properties" in node:
            node["properties"] = {
                key: convert(item, refs, depth + 1)
                for key, item in object_value(node["properties"], "schema properties").items()
            }
        if "items" in node:
            node["items"] = convert(node["items"], refs, depth + 1)
        if "anyOf" in node:
            node["anyOf"] = [convert(item, refs, depth + 1) for item in object_list(node["anyOf"], "schema alternatives")]
        if "enum" in node:
            enum: object = node["enum"]
            if not isinstance(enum, list):
                raise ValueError("Antigravity tool enum must be an array.")
            node["enum"] = [item if isinstance(item, str) else json.dumps(item) for item in cast(list[object], enum)]
        if "const" in node:
            constant = node.pop("const")
            node["enum"] = [constant if isinstance(constant, str) else json.dumps(constant)]
        hints = {key: node.pop(key) for key in list(node) if key not in allowed}
        if hints:
            description = node.get("description", "")
            node["description"] = str(description) + "\nJSON Schema constraints: " + json.dumps(hints, ensure_ascii=False)
        return node

    return convert(root)


def convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    for tool in tools or []:
        if tool.get("type") != "function":
            raise ValueError("Antigravity currently supports function tools.")
        fn = object_value(tool.get("function"), "tool declaration")
        name = fn.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,63}", name):
            raise ValueError("Antigravity function names must be valid identifiers of at most 64 characters.")
        declarations.append({
            "name": name, "description": fn.get("description") or "",
            "parameters": native_tool_schema(fn.get("parameters") or {"type": "object", "properties": {}}),
        })
    return [{"functionDeclarations": declarations}] if declarations else []


def tool_config(choice: str | dict[str, Any] | None) -> dict[str, Any]:
    if choice is None or choice == "auto":
        return {"functionCallingConfig": {"mode": "AUTO"}}
    if isinstance(choice, str) and choice in {"none", "required"}:
        return {"functionCallingConfig": {"mode": "NONE" if choice == "none" else "ANY"}}
    if isinstance(choice, dict):
        fn = object_value(choice.get("function"), "tool choice")
        if isinstance(fn.get("name"), str):
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [fn["name"]]}}
    raise ValueError("Unsupported Antigravity tool choice.")


def usage_from_native(value: object) -> LLMUsage | None:
    if value is None:
        return None
    usage = object_value(value, "usage")

    def count(key: str) -> int:
        value = usage.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("Antigravity returned invalid token usage.")
        return value

    output = count("candidatesTokenCount") + count("thoughtsTokenCount")
    return LLMUsage.reported(
        input_tokens=count("promptTokenCount"), output_tokens=output,
        total_tokens=count("totalTokenCount"),
        cache_read_tokens=count("cachedContentTokenCount") if "cachedContentTokenCount" in usage else None,
    )


@dataclass
class NativeResponse:
    parts: list[dict[str, Any]] = field(default_factory=list)
    finish: str | None = None
    usage: LLMUsage | None = None
    blocked: bool = False

    def feed(self, envelope: object) -> tuple[list[str], list[str], list[dict[str, Any]]]:
        root = object_value(envelope, "response")
        if "error" in root:
            raise ValueError("Antigravity returned an error inside the response stream.")
        response = object_value(root.get("response", root), "generation response")
        if "usageMetadata" in response:
            self.usage = usage_from_native(response["usageMetadata"])
        feedback = response.get("promptFeedback")
        if feedback is not None and object_value(feedback, "prompt feedback").get("blockReason"):
            self.blocked = True
        candidates = response.get("candidates", [])
        if not isinstance(candidates, list):
            raise ValueError("Antigravity returned invalid candidates.")
        text: list[str] = []
        thoughts: list[str] = []
        calls: list[dict[str, Any]] = []
        for raw in cast(list[object], candidates[:1]):
            candidate = object_value(raw, "candidate")
            if candidate.get("finishReason"):
                self.finish = str(candidate["finishReason"])
            content = object_value(candidate.get("content", {}), "candidate content")
            raw_parts = content.get("parts", [])
            if not isinstance(raw_parts, list):
                raise ValueError("Antigravity returned invalid response parts.")
            for value in cast(list[object], raw_parts):
                part = deepcopy(object_value(value, "response part"))
                if "functionCall" in part:
                    fn = object_value(part["functionCall"], "function call")
                    if not isinstance(fn.get("name"), str) or not fn["name"]:
                        raise ValueError("Antigravity returned a tool call without a name.")
                    if not isinstance(fn.get("id"), str) or not fn["id"]:
                        fn["id"] = "call_" + uuid.uuid4().hex
                    if not isinstance(fn.get("args", {}), dict):
                        raise ValueError("Antigravity returned invalid tool arguments.")
                    calls.append({"id": fn["id"], "type": "function", "function": {
                        "name": fn["name"], "arguments": json.dumps(fn.get("args", {}), ensure_ascii=False),
                    }})
                if isinstance(part.get("text"), str):
                    if part["text"].startswith("This version of Antigravity is no longer supported"):
                        raise ValueError("Antigravity requires a newer client protocol. Update nanobot to continue.")
                    (thoughts if part.get("thought") else text).append(part["text"])
                if not any(key in part for key in ("text", "thoughtSignature", "functionCall")):
                    raise ValueError("Antigravity returned an unsupported response part.")
                # A terminal signature-only chunk belongs to the preceding part.
                if set(part) == {"thoughtSignature"} and self.parts:
                    self.parts[-1]["thoughtSignature"] = part["thoughtSignature"]
                elif (
                    "text" in part and self.parts and "text" in self.parts[-1]
                    and not self.parts[-1].get("thoughtSignature")
                    and bool(part.get("thought")) == bool(self.parts[-1].get("thought"))
                ):
                    previous_text = self.parts[-1]["text"]
                    self.parts[-1].update(part)
                    self.parts[-1]["text"] = previous_text + part["text"]
                else:
                    self.parts.append(part)
        return text, thoughts, calls

    def result(self, *, clean_eof: bool = False, capture_tool_commentary: bool = False) -> LLMResponse:
        # Cloud Code PA legitimately omits finishReason on cleanly framed
        # streams (including tool calls). A transport/read failure never calls
        # this with clean_eof=True, so truncated calls cannot be executed.
        complete_output = any(
            "functionCall" in part or (part.get("text") and not part.get("thought"))
            for part in self.parts
        )
        if not self.finish and not self.blocked and not (clean_eof and complete_output):
            return LLMResponse(
                content="Antigravity stream ended before its terminal response.", finish_reason="error",
                error_kind="connection", error_should_retry=True, usage=self.usage,
            )
        finish = self.finish or ("SAFETY" if self.blocked else "STOP")
        if self.blocked or finish in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"}:
            return LLMResponse(content=None, finish_reason="content_filter", usage=self.usage)
        calls: list[ToolCallRequest] = []
        for part in self.parts:
            if "functionCall" in part:
                fn = part["functionCall"]
                signature = part.get("thoughtSignature")
                calls.append(ToolCallRequest(
                    id=fn["id"], name=fn["name"], arguments=fn.get("args", {}),
                    extra_content={"google": {"thought_signature": signature}} if signature else None,
                ))
        reason = "length" if finish == "MAX_TOKENS" else "tool_calls" if calls else "stop"
        if finish not in {"STOP", "MAX_TOKENS"}:
            return LLMResponse(content=f"Antigravity stopped with {finish}.", finish_reason="error", error_should_retry=False)
        commentary: tuple[ModelCommentary, ...] = ()
        content_parts = self.parts
        if capture_tool_commentary and reason == "tool_calls":
            first_call = next(index for index, part in enumerate(self.parts) if "functionCall" in part)
            progress = "".join(p.get("text", "") for p in self.parts[:first_call] if not p.get("thought")).strip()
            if progress:
                commentary = (ModelCommentary(progress),)
                content_parts = self.parts[first_call:]
        return LLMResponse(
            content="".join(p.get("text", "") for p in content_parts if not p.get("thought")) or None,
            commentary=commentary,
            reasoning_content="".join(p.get("text", "") for p in self.parts if p.get("thought")) or None,
            thinking_blocks=[
                {"type": "thinking", "thinking": part["text"], "signature": part["thoughtSignature"]}
                for part in self.parts if part.get("thought") and part.get("text") and part.get("thoughtSignature")
            ] or None,
            tool_calls=calls, finish_reason=reason, usage=self.usage,
        )
