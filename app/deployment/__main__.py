"""Package native wheels before handing the Python Worker to Pywrangler."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tomllib
import urllib.request
from pathlib import Path

from dotenv import dotenv_values

from app.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / "dist" / "worker"
WHEELS = ROOT / "dist" / "pyemscripten"
LOCK = Path(__file__).with_name("pylock.toml")
PYODIDE = "314.0.7"
NODE = "v24.22.2"
WRANGLER = "4.158.0"


def environment(*, authenticate: bool = False) -> dict[str, str]:
    values = dict(os.environ)
    node = ROOT / ".cache" / "cloudflare" / "node" / "bin"
    if node.is_dir():
        values["PATH"] = str(node) + os.pathsep + values.get("PATH", "")
    values.setdefault(
        "PYODIDE_XBUILDENV_PATH", str(ROOT / ".cache" / "cloudflare" / "xbuildenv")
    )
    values["WRANGLER_SEND_METRICS"] = "false"
    if authenticate:
        local = dotenv_values(ROOT / ".env.cloudflare")
        for key in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
            value = values.get(key) or local.get(key)
            if not value:
                raise SystemExit(f"Set {key} in the environment or .env.cloudflare")
            values[key] = value
    return values


def run(*arguments: str, cwd: Path = ROOT, authenticate: bool = False) -> None:
    subprocess.run(
        arguments, cwd=cwd, env=environment(authenticate=authenticate), check=True
    )


def wrangler(*arguments: str, authenticate: bool = False) -> None:
    run(
        "npx",
        "--yes",
        "wrangler@" + WRANGLER,
        *arguments,
        cwd=STAGE,
        authenticate=authenticate,
    )


def runtime_settings() -> dict[str, str]:
    local = dotenv_values(ROOT / ".env")
    return {
        key: value
        for name in Settings.model_fields
        if name
        not in {"documents_setup_url", "documents_database_url", "documents_pool_size"}
        if (key := "WRIGHT_ARCTIC_" + name.upper())
        if (value := os.environ.get(key, local.get(key))) is not None
    }


def setup() -> None:
    """Install the optional Linux development tools inside this checkout."""
    cache = ROOT / ".cache" / "cloudflare"
    node = cache / "node"
    if not node.exists():
        if os.uname().sysname != "Linux" or os.uname().machine != "x86_64":
            raise SystemExit(
                "Install Node 24 or newer on this platform, then run build."
            )
        cache.mkdir(parents=True, exist_ok=True)
        name = f"node-{NODE}-linux-x64"
        archive = cache / f"{name}.tar.xz"
        base = f"https://nodejs.org/dist/{NODE}/"
        with urllib.request.urlopen(base + "SHASUMS256.txt", timeout=30) as response:
            checksums = response.read().decode()
        expected = next(
            line.split()[0]
            for line in checksums.splitlines()
            if line.split()[-1] == archive.name
        )
        urllib.request.urlretrieve(base + archive.name, archive)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise SystemExit("Node archive checksum mismatch")
        with tarfile.open(archive) as package:
            package.extractall(cache, filter="data")
        node.symlink_to(name, target_is_directory=True)
    run("pyodide", "xbuildenv", "install", PYODIDE)
    run("pyodide", "xbuildenv", "install-emscripten")
    run(
        "uv",
        "run",
        "--directory",
        "engine",
        "rustup",
        "target",
        "add",
        "wasm32-unknown-emscripten",
    )


def build() -> None:
    """Cross-build the engine and resolve packages for the actual Worker runtime."""
    run("pyodide", "build", "engine", "-o", str(WHEELS), "-C", "build-args=--locked")
    wheels = list(
        WHEELS.glob("wright_arctic_engine-*-cp314-cp314-pyemscripten_2026_0_wasm32.whl")
    )
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one CPython 3.14 PyEmscripten engine wheel")
    wheel = wheels[0]
    if STAGE.exists():
        shutil.rmtree(STAGE)
    (STAGE / "wheels").mkdir(parents=True)
    shutil.copy2(wheel, STAGE / "wheels" / wheel.name)
    shutil.copytree(
        ROOT / "app",
        STAGE / "src" / "app",
        ignore=shutil.ignore_patterns("__pycache__", "tests", "deployment", "*.md"),
    )
    # The completion prompt is runtime data, not deployment documentation.
    prompt = Path("app/assets/prompts/completion.md")
    shutil.copy2(ROOT / prompt, STAGE / "src" / prompt)
    shutil.copy2(ROOT / "wrangler.toml", STAGE / "wrangler.toml")
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)["project"]
    # R2 bindings replace the S3 transport in Workers. Keep the SDK available
    # for the portable ASGI deployment without shipping its unused API models.
    project["dependencies"] = [
        item.replace("pyjwt[crypto]", "pyjwt")
        for item in project["dependencies"]
        if not item.startswith(("boto3", "psycopg"))
    ]
    project["dependencies"].extend(project["optional-dependencies"]["worker"])
    lines = ["[project]"]
    for key in ("name", "version", "requires-python", "dependencies"):
        lines.append(f"{key} = {json.dumps(project[key])}")
    lines.extend(
        [
            "",
            "[tool.uv]",
            "package = false",
            'required-version = ">=0.13.4"',
            "",
            "[tool.uv.sources]",
            "wright-arctic-engine = [",
            "  { path = "
            + json.dumps("wheels/" + wheel.name)
            + ", marker = \"sys_platform == 'emscripten'\" },",
            "  { path = "
            + json.dumps(str(ROOT / "engine"))
            + ", marker = \"sys_platform != 'emscripten'\" },",
            "]",
            "",
        ]
    )
    (STAGE / "pyproject.toml").write_text("\n".join(lines))
    if LOCK.exists():
        shutil.copy2(LOCK, STAGE / "pylock.toml")
    run("pywrangler", "sync", cwd=STAGE)
    shutil.copy2(STAGE / "pylock.toml", LOCK)
    # Wrangler's dry run reports bundle size but does not reject an oversized
    # Python bundle. Count conservatively before any upload or activation.
    size = sum(
        path.stat().st_size
        for directory in (STAGE / "src", STAGE / "python_modules")
        for path in directory.rglob("*")
        if path.is_file()
    )
    if size > 64 * 1024 * 1024:
        raise SystemExit(f"Worker exceeds the 64 MiB bundle limit: {size} bytes")
    wrangler("deploy", "--dry-run", "--outdir", "bundle")


def deploy() -> None:
    """Deploy a validated build and upload only the API's runtime settings."""
    if not (STAGE / "python_modules" / ".synced").exists():
        raise SystemExit("Run the worker build before deploying")
    wrangler("deploy", authenticate=True)
    runtime = runtime_settings()
    if runtime:
        payload = STAGE / ".runtime-secrets.json"
        descriptor = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(runtime, stream)
            wrangler("secret", "bulk", str(payload), authenticate=True)
        finally:
            payload.unlink(missing_ok=True)


def dev() -> None:
    values = environment(authenticate=True)
    local = dotenv_values(ROOT / ".env")
    database = os.environ.get(
        "WRIGHT_ARCTIC_DOCUMENTS_DATABASE_URL",
        local.get("WRIGHT_ARCTIC_DOCUMENTS_DATABASE_URL"),
    )
    if not database:
        raise SystemExit(
            "Set WRIGHT_ARCTIC_DOCUMENTS_DATABASE_URL for local Hyperdrive development"
        )
    values["CLOUDFLARE_HYPERDRIVE_LOCAL_CONNECTION_STRING_HYPERDRIVE"] = database
    payload = STAGE / ".dev.vars"
    descriptor = os.open(payload, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w") as stream:
            for key, value in runtime_settings().items():
                stream.write(key + "=" + json.dumps(value) + "\n")
        subprocess.run(
            ["npx", "--yes", "wrangler@" + WRANGLER, "dev"],
            cwd=STAGE,
            env=values,
            check=True,
        )
    finally:
        payload.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "build", "deploy", "dev"))
    arguments = parser.parse_args()
    match arguments.command:
        case "setup":
            setup()
        case "build":
            build()
        case "deploy":
            deploy()
        case "dev":
            dev()


if __name__ == "__main__":
    main()
