from __future__ import annotations

import argparse
import ast
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

REQUIRED = {
    "TranscriptStore": {
        "request",
        "borrow_snapshot",
        "prepare_classifier",
        "submit_classifier",
        "publish_projection",
        "resume_projection",
        "register_tool_registry",
        "discard_response",
    },
    "TranscriptSnapshot": {
        "activity",
        "capture",
        "hydrate",
        "mine_json",
        "classifier_facts",
        "source_facts",
        "prose_rows",
    },
}


def api(path: str) -> dict:
    return json.loads(subprocess.check_output(["gh", "api", path]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    receipt = json.loads(Path(__file__).parents[1].joinpath("snapshot-candidate.json").read_text())
    if receipt["pending_api_changes"]:
        raise SystemExit(f"native candidate requires a new tested artifact: {receipt['pending_api_changes']}")
    if receipt["artifact_id"] is None or receipt["artifact_digest"] is None:
        raise SystemExit(f"native candidate artifact is not verified yet; waiting for source run {receipt['run_id']}")
    repository = receipt["repository"]
    metadata = api(f"repos/{repository}/actions/artifacts/{receipt['artifact_id']}")
    expected = (receipt["artifact_name"], receipt["artifact_digest"], receipt["run_id"], receipt["head_sha"])
    actual = (
        metadata["name"],
        metadata["digest"],
        metadata["workflow_run"]["id"],
        metadata["workflow_run"]["head_sha"],
    )
    if metadata["expired"] or actual != expected:
        raise SystemExit("snapshot candidate artifact no longer matches its pinned receipt")
    commit = api(f"repos/{repository}/git/commits/{receipt['merge_sha']}")
    if commit["tree"]["sha"] != receipt["tree_sha"] or receipt["head_sha"] not in {
        item["sha"] for item in commit["parents"]
    }:
        raise SystemExit("snapshot candidate merge tree differs from its pinned receipt")
    args.directory.mkdir(parents=True, exist_ok=True)
    archive_path = args.directory / "candidate.zip"
    with archive_path.open("wb") as output:
        subprocess.run(
            ["gh", "api", f"repos/{repository}/actions/artifacts/{receipt['artifact_id']}/zip"],
            stdout=output,
            check=True,
        )
    with archive_path.open("rb") as source:
        digest = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
    if digest != receipt["artifact_digest"]:
        raise SystemExit("snapshot candidate archive digest differs from its pinned receipt")
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if (
            len(entries) != 1
            or not entries[0].filename.endswith(".whl")
            or Path(entries[0].filename).name != entries[0].filename
        ):
            raise SystemExit("snapshot candidate must contain exactly one wheel")
        wheel_path = args.directory / entries[0].filename
        with archive.open(entries[0]) as source, wheel_path.open("wb") as destination:
            shutil.copyfileobj(source, destination)
    with wheel_path.open("rb") as source:
        wheel_digest = hashlib.file_digest(source, "sha256").hexdigest()
    if wheel_path.name != receipt["wheel_name"] or wheel_digest != receipt["wheel_sha256"]:
        raise SystemExit("snapshot candidate wheel differs from its pinned receipt")
    with zipfile.ZipFile(wheel_path) as wheel:
        tree = ast.parse(wheel.read("cc_transcript/snapshots.py"))
        classes = {
            node.name: {method.name for method in node.body if isinstance(method, ast.FunctionDef)}
            for node in tree.body
            if isinstance(node, ast.ClassDef)
        }
        missing = {
            name: sorted(methods - classes.get(name, set()))
            for name, methods in REQUIRED.items()
            if methods - classes.get(name, set())
        }
        if missing:
            raise SystemExit(
                f"pinned candidate predates required snapshot APIs; update tested artifact receipt: {missing}"
            )
        store = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TranscriptStore")
        registration = next(
            node for node in store.body if isinstance(node, ast.FunctionDef) and node.name == "register_tool_registry"
        )
        if "context" not in {argument.arg for argument in registration.args.kwonlyargs}:
            raise SystemExit("pinned candidate lacks request-scoped tool registry admission")
        for schema in ("request", "response", "context", "config", "tool_registry"):
            json.loads(wheel.read(f"cc_transcript/snapshot_schema/{schema}.schema.json"))
    print(json.dumps(receipt | {"wheel": wheel_path.name}, sort_keys=True))


if __name__ == "__main__":
    main()
