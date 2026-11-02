from __future__ import annotations

import argparse
import copy
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from captain_hook.snapshots.contracts import DOMAIN_REQUEST, DOMAIN_RESULT, HOST_SCHEMA

DIALECT = "https://json-schema.org/draft/2020-12/schema"


def core_refs(value: Any) -> Any:
    match value:
        case dict():
            return {key: core_refs(item) for key, item in value.items()}
        case list():
            return [core_refs(item) for item in value]
        case str() if value.startswith("#/$defs/"):
            return value.replace("#/$defs/", "#/$defs/Core_", 1)
        case _:
            return value


def compose(core_directory: Path) -> dict[str, dict[str, Any]]:
    sources = {
        name: json.loads((core_directory / f"{name}.schema.json").read_text())
        for name in ("request", "response", "context", "config", "tool_registry")
    }
    definitions: dict[str, Any] = {}
    roots: dict[str, Any] = {}
    for name, schema in sources.items():
        for key, value in schema.get("$defs", {}).items():
            prefixed = f"Core_{key}"
            renamed = core_refs(value)
            if prefixed in definitions and definitions[prefixed] != renamed:
                raise ValueError(f"inconsistent canonical core schema definition: {key}")
            definitions[prefixed] = renamed
        roots[name] = core_refs({key: value for key, value in schema.items() if key not in {"$defs", "$schema"}})
    request = DOMAIN_REQUEST.json_schema(by_alias=True)
    result = DOMAIN_RESULT.json_schema(by_alias=True)
    definitions.update(request.pop("$defs"))
    definitions.update(result.pop("$defs"))
    for name in ("PrepareReview", "PrepareCorrections", "PrepareHookView"):
        fields = definitions[name]["properties"]
        fields["view"] = {"$ref": "#/$defs/Core_View"}
        fields["limits"] = {"$ref": "#/$defs/Core_Limits"}
    definitions["PrepareClassifier"]["properties"].update(
        handle={"$ref": "#/$defs/Core_Handle"},
        classifier={"$ref": "#/$defs/Core_Classifier"},
        limits={"$ref": "#/$defs/Core_Limits"},
    )
    definitions["ClassifierResult"]["properties"]["classifier"] = {"$ref": "#/$defs/Core_Classifier"}
    definitions["PrepareCorrections"]["properties"]["anchors"]["items"] = {"$ref": "#/$defs/Core_EventRef"}
    definitions["PreparedCorrection"]["properties"]["anchor"] = {"$ref": "#/$defs/Core_EventRef"}
    domain_response = copy.deepcopy(roots["response"])
    for name in ("CompleteResponse", "IncompleteResponse"):
        definition = copy.deepcopy(definitions[f"Core_{name}"])
        definition["properties"]["data"] = {"anyOf": [definition["properties"]["data"], result]}
        definitions[f"Host_{name}"] = definition
    for variant in domain_response["oneOf"]:
        if variant["$ref"] in {"#/$defs/Core_CompleteResponse", "#/$defs/Core_IncompleteResponse"}:
            variant["$ref"] = variant["$ref"].replace("Core_", "Host_")
    domain_response.pop("discriminator", None)
    host = {}
    for name, field, value in (
        ("host-request", "request", {"oneOf": [roots["request"], request]}),
        ("host-response", "response", domain_response),
    ):
        host[name] = {
            "$schema": DIALECT,
            "$defs": definitions,
            "type": "object",
            "additionalProperties": False,
            "required": ["schema", field],
            "properties": {"schema": {"const": HOST_SCHEMA, "type": "string"}, field: value},
        }
    host["host-request"]["required"].append("tool_registry")
    host["host-request"]["properties"]["tool_registry"] = roots["tool_registry"]
    return sources | host


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--core-directory", type=Path, default=files("cc_transcript").joinpath("snapshot_schema"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, schema in compose(args.core_directory).items():
        path = args.directory / f"{name}.schema.json"
        contents = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        if args.check:
            if path.read_text() != contents:
                raise SystemExit(f"generated snapshot schema is stale: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)


if __name__ == "__main__":
    main()
