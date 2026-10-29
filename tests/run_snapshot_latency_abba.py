import hashlib
import json
import os
import platform
import subprocess
import sys
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path.cwd()
OUTPUT = Path(os.environ["RUNNER_TEMP"]) / "capt-sdk-latency-abba"
SOURCES = {
    "14.45.14": "baee94b927589c8994338146afe93bd840acd422",
    "14.45.15": "5f169fb6042994c51d9a66f806975f685faf85c1",
}
WHEELS = {
    "14.45.14": (
        "https://files.pythonhosted.org/packages/cf/72/875e9b5185758c41cb82c69b9f5e5c4e14e127b5758d3c06d192ce6cf333/cc_transcript-14.45.14-cp313-abi3-macosx_11_0_x86_64.whl",
        "cb0c737ac254a91101cd036ea6a1e7ea9413e07d1daf96b63d5a568f3a6d9e01",
    ),
    "14.45.15": (
        "https://files.pythonhosted.org/packages/6f/1a/e66c4f99e0cb414cf7361b97a47d304723d763db951c024db157bea72ccc/cc_transcript-14.45.15-cp313-abi3-macosx_11_0_x86_64.whl",
        "7f3203eed4cdd7ce736610fb66885865c231c3928226bdf3e73700e20b871fcf",
    ),
}


def command(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def installed_packages():
    result = command(
        "uv", "pip", "list", "--python", sys.executable, "--format", "json", capture_output=True, text=True
    )
    return sorted(
        (entry["name"], entry["version"]) for entry in json.loads(result.stdout) if entry["name"] != "cc-transcript"
    )


def main():
    assert platform.machine() == "x86_64"
    assert sys.version_info[:2] == (3, 13)
    OUTPUT.mkdir()
    original = {name: (ROOT / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    baseline_packages = installed_packages()
    wheel_receipts = {}
    manifests = {}
    for version, (url, expected) in WHEELS.items():
        wheel = OUTPUT / url.rsplit("/", 1)[1]
        with urllib.request.urlopen(url) as response:
            wheel.write_bytes(response.read())
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == expected
        with zipfile.ZipFile(wheel) as archive:
            native = archive.read("cc_transcript/_native.abi3.so")
        wheel_receipts[version] = {
            "url": url,
            "wheel_sha256": expected,
            "native_sha256": hashlib.sha256(native).hexdigest(),
            "path": str(wheel),
        }
        manifests[version] = {
            name: command("git", "show", SOURCES[version] + ":" + name, capture_output=True).stdout for name in original
        }
    outcomes = []
    try:
        for ordinal, version in enumerate(("14.45.14", "14.45.15", "14.45.15", "14.45.14"), 1):
            variant = str(ordinal) + "-" + version
            for name, data in manifests[version].items():
                (ROOT / name).write_bytes(data)
            project = tomllib.loads((ROOT / "pyproject.toml").read_text())
            lock = tomllib.loads((ROOT / "uv.lock").read_text())
            assert "cc-transcript[judge]==" + version in project["project"]["dependencies"]
            assert (
                next(package for package in lock["package"] if package["name"] == "cc-transcript")["version"] == version
            )
            command(
                "uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                "--no-deps",
                "--reinstall",
                wheel_receipts[version]["path"],
            )
            command("uv", "pip", "install", "--python", sys.executable, "--no-deps", "--reinstall", "--editable", ".")
            assert installed_packages() == baseline_packages
            receipt_path = OUTPUT / (variant + ".json")
            env = dict(
                os.environ,
                CAPTAIN_ABBA_VERSION=version,
                CAPTAIN_ABBA_VARIANT=variant,
                CAPTAIN_ABBA_NATIVE_SHA256=wheel_receipts[version]["native_sha256"],
                CAPTAIN_ABBA_RECEIPT=str(receipt_path),
            )
            result = subprocess.run(
                (
                    "uv",
                    "run",
                    "--no-sync",
                    "pytest",
                    "-n",
                    "0",
                    "-s",
                    "tests/test_snapshot_client.py::test_foreground_graph_latency_stays_bounded_while_registry_warms",
                ),
                env=env,
            )
            receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else None
            outcomes.append(
                {
                    "ordinal": ordinal,
                    "sdk_version": version,
                    "returncode": result.returncode,
                    "receipt": receipt,
                    "wheel": wheel_receipts[version],
                }
            )
            (OUTPUT / "outcomes.json").write_text(json.dumps(outcomes, indent=2) + "\n")
    finally:
        for name, data in original.items():
            (ROOT / name).write_bytes(data)
        (OUTPUT / "experiment.json").write_text(
            json.dumps(
                {
                    "consumer_source": SOURCES["14.45.15"],
                    "diagnostic_head": command(
                        "git", "rev-parse", "HEAD", capture_output=True, text=True
                    ).stdout.strip(),
                    "platform": platform.platform(),
                    "machine": platform.machine(),
                    "python": sys.version,
                    "runner_image": os.environ["ImageVersion"],
                    "dependencies_except_sdk": baseline_packages,
                    "wheel_receipts": wheel_receipts,
                    "outcomes": outcomes,
                },
                indent=2,
            )
            + "\n"
        )
    assert len(outcomes) == 4
    assert all(outcome["receipt"] is not None and len(outcome["receipt"]["samples"]) == 30 for outcome in outcomes)
    raise SystemExit(int(any(outcome["returncode"] != 0 for outcome in outcomes)))


if __name__ == "__main__":
    main()
