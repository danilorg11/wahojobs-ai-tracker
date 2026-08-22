"""Deterministic checks for the OpenAI Structured Outputs subset used here."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re


_FORMAT_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_JSON_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)
_SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "anyOf",
        "description",
        "enum",
        "items",
        "properties",
        "required",
        "type",
    }
)
KNOWN_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "allOf",
        "contains",
        "format",
        "maxContains",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minContains",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "not",
        "oneOf",
        "pattern",
        "patternProperties",
        "propertyNames",
        "uniqueItems",
        "unevaluatedProperties",
    }
)
MAX_OBJECT_PROPERTIES = 100
MAX_OBJECT_NESTING = 5
MAX_ENUM_VALUES = 500
MAX_RESOURCE_STRING_CHARS = 15_000
MAX_LARGE_ENUM_STRING_CHARS = 7_500


@dataclass(frozen=True, slots=True)
class StructuredOutputsSchemaMetrics:
    object_property_count: int
    maximum_object_nesting: int
    unsupported_keyword_count: int
    enum_value_count: int
    resource_string_characters: int


def validate_responses_request_contract(url: object, body: object):
    """Validate the project-specific, non-streaming Responses request envelope."""

    if url != "https://api.openai.com/v1/responses":
        raise AssertionError("unexpected Responses endpoint")
    if type(body) is not dict:
        raise AssertionError("Responses request must be an object")
    chat_completions_fields = {
        "max_completion_tokens",
        "max_tokens",
        "messages",
        "reasoning_effort",
        "response_format",
    }
    if set(body) & chat_completions_fields:
        raise AssertionError("Chat Completions request fields are forbidden")
    if type(body.get("model")) is not str or not body["model"]:
        raise AssertionError("Responses model must be a non-empty string")
    if body.get("store") is not False:
        raise AssertionError("provider storage must be disabled")
    if body.get("tools") != []:
        raise AssertionError("profile extraction must not expose tools")
    if "tool_choice" in body:
        raise AssertionError("tool_choice must be absent when there are no tools")
    if type(body.get("max_output_tokens")) is not int or body["max_output_tokens"] < 1:
        raise AssertionError("max_output_tokens must be a positive integer")
    if body.get("reasoning") != {"effort": "low"}:
        raise AssertionError("unexpected reasoning configuration")
    if "temperature" in body or "top_p" in body:
        raise AssertionError("unsupported sampling controls must be absent")

    messages = body.get("input")
    if type(messages) is not list or not messages:
        raise AssertionError("Responses input must be a non-empty message list")
    for message in messages:
        if type(message) is not dict or set(message) != {"role", "content"}:
            raise AssertionError("invalid Responses input message")
        if message["role"] not in {"system", "developer", "user", "assistant"}:
            raise AssertionError("invalid Responses input role")
        content = message["content"]
        if type(content) is not list or not content:
            raise AssertionError("Responses message content must be non-empty")
        for item in content:
            if (
                type(item) is not dict
                or set(item) != {"type", "text"}
                or item["type"] != "input_text"
                or type(item["text"]) is not str
                or not item["text"]
            ):
                raise AssertionError("invalid Responses input_text content")

    text = body.get("text")
    if type(text) is not dict or set(text) != {"format"}:
        raise AssertionError("Responses text configuration is invalid")
    return validate_structured_outputs_format(text["format"])


def validate_structured_outputs_format(value: object):
    """Validate format metadata and the supported provider-schema subset."""

    if type(value) is not dict or set(value) != {"type", "name", "strict", "schema"}:
        raise AssertionError("invalid Structured Outputs format object")
    if value["type"] != "json_schema" or value["strict"] is not True:
        raise AssertionError("strict json_schema format is required")
    if type(value["name"]) is not str or _FORMAT_NAME.fullmatch(value["name"]) is None:
        raise AssertionError("invalid Structured Outputs schema name")
    return validate_structured_outputs_schema(value["schema"])


def validate_structured_outputs_schema(schema: object):
    """Reject schemas outside the bounded subset accepted by this project."""

    if type(schema) is not dict or schema.get("type") != "object" or "anyOf" in schema:
        raise AssertionError("Structured Outputs root must be an object")
    json.dumps(schema, ensure_ascii=False, allow_nan=False)

    state = {
        "properties": 0,
        "nesting": 0,
        "unsupported": 0,
        "enum_values": 0,
        "resource_chars": 0,
    }

    def walk(node, *, object_nesting=0, root=schema):
        if type(node) is not dict:
            raise AssertionError("schema nodes must be objects")
        unsupported = set(node) & KNOWN_UNSUPPORTED_SCHEMA_KEYWORDS
        state["unsupported"] += len(unsupported)
        if unsupported:
            raise AssertionError(
                "unsupported Structured Outputs keywords: "
                + ", ".join(sorted(unsupported))
            )
        unknown = set(node) - _SUPPORTED_SCHEMA_KEYWORDS
        if unknown:
            raise AssertionError(
                "unknown Structured Outputs keywords: " + ", ".join(sorted(unknown))
            )

        raw_types = node.get("type")
        if raw_types is None:
            types = ()
        elif type(raw_types) is str:
            types = (raw_types,)
        elif (
            type(raw_types) is list
            and raw_types
            and len(set(raw_types)) == len(raw_types)
            and all(type(item) is str for item in raw_types)
        ):
            types = tuple(raw_types)
        else:
            raise AssertionError("invalid schema type declaration")
        if any(item not in _JSON_TYPES for item in types):
            raise AssertionError("unsupported JSON type")

        next_object_nesting = object_nesting + (1 if "object" in types else 0)
        state["nesting"] = max(state["nesting"], next_object_nesting)
        if next_object_nesting > MAX_OBJECT_NESTING:
            raise AssertionError("Structured Outputs object nesting limit exceeded")

        properties = node.get("properties")
        if "object" in types:
            if type(properties) is not dict:
                raise AssertionError("object schema must define properties")
            if node.get("additionalProperties") is not False:
                raise AssertionError("object schema must set additionalProperties false")
            required = node.get("required")
            if type(required) is not list or set(required) != set(properties):
                raise AssertionError("object schema must require every property")
            if len(required) != len(set(required)):
                raise AssertionError("object schema required keys must be unique")
            state["properties"] += len(properties)
            state["resource_chars"] += sum(len(name) for name in properties)
            for name, child in properties.items():
                if type(name) is not str or not name:
                    raise AssertionError("invalid schema property name")
                walk(child, object_nesting=next_object_nesting, root=root)
        elif any(key in node for key in ("properties", "required", "additionalProperties")):
            raise AssertionError("object-only keywords require object type")

        if "items" in node:
            if "array" not in types:
                raise AssertionError("items requires array type")
            walk(node["items"], object_nesting=next_object_nesting, root=root)

        branches = node.get("anyOf")
        if branches is not None:
            if type(branches) is not list or not branches:
                raise AssertionError("anyOf must contain schema branches")
            for branch in branches:
                walk(branch, object_nesting=next_object_nesting, root=root)

        enum = node.get("enum")
        if enum is not None:
            if type(enum) is not list or not enum:
                raise AssertionError("enum must contain values")
            state["enum_values"] += len(enum)
            enum_chars = sum(len(str(item)) for item in enum)
            state["resource_chars"] += enum_chars
            if len(enum) > 250 and enum_chars > MAX_LARGE_ENUM_STRING_CHARS:
                raise AssertionError("large enum string limit exceeded")

        definitions = node.get("$defs")
        if definitions is not None:
            if type(definitions) is not dict:
                raise AssertionError("$defs must be an object")
            state["resource_chars"] += sum(len(name) for name in definitions)
            for definition in definitions.values():
                walk(definition, object_nesting=next_object_nesting, root=root)
        reference = node.get("$ref")
        if reference is not None:
            if (
                type(reference) is not str
                or not reference.startswith("#/$defs/")
                or reference.removeprefix("#/$defs/") not in root.get("$defs", {})
            ):
                raise AssertionError("unsupported schema reference")

    walk(schema)
    if state["properties"] > MAX_OBJECT_PROPERTIES:
        raise AssertionError("Structured Outputs property limit exceeded")
    if state["enum_values"] > MAX_ENUM_VALUES:
        raise AssertionError("Structured Outputs enum value limit exceeded")
    if state["resource_chars"] > MAX_RESOURCE_STRING_CHARS:
        raise AssertionError("Structured Outputs resource string limit exceeded")
    return StructuredOutputsSchemaMetrics(
        object_property_count=state["properties"],
        maximum_object_nesting=state["nesting"],
        unsupported_keyword_count=state["unsupported"],
        enum_value_count=state["enum_values"],
        resource_string_characters=state["resource_chars"],
    )


def structured_outputs_schema_accepts(schema: object, value: object) -> bool:
    """Small instance checker for the exact schema subset validated above."""

    validate_structured_outputs_schema(schema)

    def enum_contains(enum, candidate):
        return any(type(item) is type(candidate) and item == candidate for item in enum)

    def matches_type(type_name, candidate):
        if type_name == "null":
            return candidate is None
        if type_name == "boolean":
            return type(candidate) is bool
        if type_name == "integer":
            return type(candidate) is int
        if type_name == "number":
            return type(candidate) in (int, float) and type(candidate) is not bool
        if type_name == "string":
            return type(candidate) is str
        if type_name == "array":
            return type(candidate) is list
        if type_name == "object":
            return type(candidate) is dict
        return False

    def accepts(node, candidate, *, root=schema):
        reference = node.get("$ref")
        if reference is not None:
            return accepts(root["$defs"][reference.removeprefix("#/$defs/")], candidate)
        if "enum" in node and not enum_contains(node["enum"], candidate):
            return False
        branches = node.get("anyOf")
        if branches is not None and not any(
            accepts(branch, candidate, root=root) for branch in branches
        ):
            return False
        raw_types = node.get("type")
        types = (
            (raw_types,)
            if type(raw_types) is str
            else tuple(raw_types or ())
        )
        if types and not any(matches_type(item, candidate) for item in types):
            return False
        if type(candidate) is dict and "properties" in node:
            properties = node["properties"]
            if any(name not in candidate for name in node.get("required", ())):
                return False
            if node.get("additionalProperties") is False and any(
                name not in properties for name in candidate
            ):
                return False
            return all(
                name not in candidate or accepts(child, candidate[name], root=root)
                for name, child in properties.items()
            )
        if type(candidate) is list and "items" in node:
            return all(accepts(node["items"], item, root=root) for item in candidate)
        return True

    return accepts(schema, value)
