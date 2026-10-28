#!/usr/bin/env python3
"""Release pipeline for the Expo Android apps listed in all-app.json.

Signing keys come from the central .env next to this file (see .env.example). Release builds
are written to <app repo>/release/app-release.apk or app-release.aab.

Usage:
    python app.py                                   # interactive menu
    python app.py list                              # apps, versions and keystore status
    python app.py preflight <app> [--aab]           # checks only, no build
    python app.py keygen <app>                      # create the upload keystore if missing
    python app.py prebuild <app>                    # regenerate a stale android/ folder
    python app.py apk <app> [--skip-checks] [--prebuild]
    python app.py aab <app> [--bump | --no-bump] [--skip-checks] [--prebuild]
    python app.py verify <app> [apk|aab]            # check signature of the release file
    python app.py upload <app> [--track TRACK] [--notes TEXT]
                                                    # upload <PREFIX>_AAB_PATH to Google Play and publish

Google Play uploads need a service account key, see README.md.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APPS_FILE = ROOT / "all-app.json"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE_FILE = ROOT / ".env.example"
HISTORY_FILE = ROOT / "build-history.json"
SIGNING_INIT_SCRIPT = ROOT / "gradle" / "pipeline-signing.gradle"
KEYS_DIR = Path.home() / ".android-keys"
IS_WINDOWS = os.name == "nt"
MIN_JAVA_MAJOR = 17
MIN_FREE_DISK_GB = 5
RULE = "=" * 60

ARTIFACTS = {
    "apk": {
        "gradle_task": "assembleRelease",
        "output": Path("app/build/outputs/apk/release/app-release.apk"),
        "file_name": "app-release.apk",
    },
    "aab": {
        "gradle_task": "bundleRelease",
        "output": Path("app/build/outputs/bundle/release/app-release.aab"),
        "file_name": "app-release.aab",
    },
}
SIGNING_FIELDS = ("store_file", "key_alias", "store_password", "key_password")

PLAY_CREDENTIALS_VAR = "PLAY_SERVICE_ACCOUNT_JSON"
PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
PLAY_TRACKS = {
    "internal": "Internal testing",
    "alpha": "Closed testing",
    "beta": "Open testing",
    "production": "Production",
}
RELEASE_NOTES_LANGUAGE = "en-US"
MAX_RELEASE_NOTES = 500
UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024
# Play processes the whole bundle before answering the last chunk, which can take minutes.
PLAY_HTTP_TIMEOUT_SECONDS = 300
PLAY_ERROR_HINTS = [
    (r"has not been used in project|API .* is disabled",
     "Enable the Google Play Android Developer API in the service account's Cloud project (README step 2)."),
    (r"does not have permission|insufficient permission|permission denied",
     "Invite the service account in Play Console > Users and permissions with release permissions for this app "
     "(README step 4). New permissions can take up to 24 hours to apply."),
    (r"package not found|no application was found",
     "Check <PREFIX>_PACKAGE_NAME. The app must already exist in the Play developer account the service account was invited to."),
    (r"version code .*already been used",
     "Play has already seen this versionCode. Build a new AAB (menu option 5 bumps the version)."),
    (r"signed with the wrong key|not signed",
     "The AAB must be signed with the upload key registered in Play Console > Test and release > App integrity."),
    (r"invalid_grant|invalid jwt",
     "The JSON key was deleted/revoked, or this PC's clock is wrong. Create a new key (README step 3)."),
]


class PipelineError(Exception):
    pass


def info(message: str) -> None:
    print(f"\n👉 {message}", flush=True)


def posix(path: Path | str) -> str:
    return str(path).replace("\\", "/")


# ---------------------------------------------------------------- app config


@dataclass
class App:
    id: str
    name: str
    root: Path
    project: Path
    npm_dir: Path
    env_names: dict[str, str]
    keystore_name: str
    required_app_env: list[str] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)

    @property
    def android(self) -> Path:
        return self.project / "android"

    @property
    def app_json(self) -> Path:
        return self.project / "app.json"

    @property
    def build_gradle(self) -> Path:
        return self.android / "app" / "build.gradle"

    @property
    def release_dir(self) -> Path:
        return self.root / "release"

    def release_file(self, kind: str) -> Path:
        return self.release_dir / ARTIFACTS[kind]["file_name"]


def load_apps() -> dict[str, App]:
    try:
        data = json.loads(APPS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PipelineError(f"Could not read {APPS_FILE.name}: {err}")

    apps = {}
    for app_id, cfg in data.get("apps", {}).items():
        missing = [key for key in ("path", "env") if key not in cfg]
        missing += [f"env.{name}" for name in SIGNING_FIELDS if name not in cfg.get("env", {})]
        if missing:
            raise PipelineError(f"{APPS_FILE.name}: app '{app_id}' is missing {', '.join(missing)}")
        root = Path(cfg["path"])
        apps[app_id] = App(
            id=app_id,
            name=cfg.get("name", app_id),
            root=root,
            project=(root / cfg.get("project_dir", ".")).resolve(),
            npm_dir=(root / cfg.get("npm_dir", cfg.get("project_dir", "."))).resolve(),
            env_names=cfg["env"],
            keystore_name=cfg.get("keystore_name", f"{app_id}-upload.keystore"),
            required_app_env=cfg.get("required_app_env", []),
            checks=cfg.get("checks", []),
        )
    if not apps:
        raise PipelineError(f"No apps defined in {APPS_FILE.name}.")
    return apps


def get_app(apps: dict[str, App], app_id: str) -> App:
    if app_id in apps:
        return apps[app_id]
    matches = [app for key, app in apps.items() if key.lower() == app_id.lower()]
    if matches:
        return matches[0]
    raise PipelineError(f"Unknown app '{app_id}'. Known apps: {', '.join(apps)}")


# ---------------------------------------------------------------- .env files

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][\w.-]*)\s*=\s*(.*)$")


def parse_env(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        match = ENV_LINE.match(line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) > 1 and value[0] in "\"'" and value.endswith(value[0]):
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        values[match.group(1)] = value
    return values


def read_env_file(path: Path) -> dict[str, str]:
    return parse_env(path.read_text(encoding="utf-8")) if path.exists() else {}


def env_var(name: str) -> str:
    """Value from the central .env, falling back to the shell environment."""
    return (read_env_file(ENV_FILE).get(name) or os.environ.get(name, "")).strip()


def format_env_value(value: str) -> str:
    if not re.search(r"[\s#\"']", value):
        return value
    return f'"{value}"' if '"' not in value else f"'{value}'"


def upsert_env_file(path: Path, values: dict[str, str], header: str, overwrite: bool = True) -> None:
    """Set KEY=value lines in a .env file, appending missing keys under a `# header` comment."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for i, line in enumerate(lines):
        match = ENV_LINE.match(line)
        if match and match.group(1) in remaining:
            value = remaining.pop(match.group(1))
            if overwrite:
                lines[i] = f"{match.group(1)}={format_env_value(value)}"
    new_lines = [f"{key}={format_env_value(value)}" for key, value in remaining.items()]
    if new_lines and f"# {header}" in lines:
        end = lines.index(f"# {header}") + 1
        while end < len(lines) and ENV_LINE.match(lines[end]):
            end += 1
        lines[end:end] = new_lines
    elif new_lines:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"# {header}", *new_lines]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass
class Signing:
    store_file: str
    key_alias: str
    store_password: str
    key_password: str

    @property
    def missing(self) -> list[str]:
        return [name for name in SIGNING_FIELDS if not getattr(self, name)]


def signing_for(app: App) -> Signing:
    env = read_env_file(ENV_FILE)
    values = {name: env.get(app.env_names[name]) or os.environ.get(app.env_names[name], "") for name in SIGNING_FIELDS}
    return Signing(**values)


def expected_package(app: App) -> str:
    """Package name the Play Console listing expects, from <PREFIX>_PACKAGE_NAME in .env."""
    var = app.env_names.get("package_name")
    return env_var(var) if var else ""


def ensure_env_example(app: App) -> None:
    values = {
        app.env_names["store_file"]: posix(KEYS_DIR / app.keystore_name),
        app.env_names["key_alias"]: "upload",
        app.env_names["store_password"]: "",
        app.env_names["key_password"]: "",
    }
    for optional in ("package_name", "aab_path"):
        if app.env_names.get(optional):
            values[app.env_names[optional]] = ""
    upsert_env_file(
        ENV_EXAMPLE_FILE,
        values,
        header=f"{app.name} ({app.id})",
        overwrite=False,
    )


def gradle_env(app: App, signing: Signing) -> dict[str, str]:
    """Environment for gradlew: the init script reads PIPELINE_UPLOAD_*, and apps whose own
    build.gradle expects e.g. MYAPP_UPLOAD_* as Gradle properties get ORG_GRADLE_PROJECT_*."""
    env = dict(os.environ)
    for name in SIGNING_FIELDS:
        value = getattr(signing, name)
        var = app.env_names[name]
        env[var] = value
        env[f"ORG_GRADLE_PROJECT_{var}"] = value
    env["PIPELINE_UPLOAD_STORE_FILE"] = signing.store_file
    env["PIPELINE_UPLOAD_KEY_ALIAS"] = signing.key_alias
    env["PIPELINE_UPLOAD_STORE_PASSWORD"] = signing.store_password
    env["PIPELINE_UPLOAD_KEY_PASSWORD"] = signing.key_password
    return env


# ---------------------------------------------------------------- tools


def run(cmd: list[str] | str, cwd: Path, env: dict[str, str] | None = None) -> None:
    shown = cmd if isinstance(cmd, str) else " ".join(str(part) for part in cmd)
    print(f"\n💻 $ {shown}   (in {cwd})\n", flush=True)
    code = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str)).returncode
    if code != 0:
        raise PipelineError(f"Command failed with exit code {code}: {shown}")


def capture(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, timeout: int = 120):
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def output_of(result) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}".strip() if result else ""


def which(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PipelineError(f"'{name}' was not found on PATH.")
    return path


def jdk_tool(name: str) -> str | None:
    exe = f"{name}.exe" if IS_WINDOWS else name
    java_home = os.environ.get("JAVA_HOME")
    if java_home and (Path(java_home) / "bin" / exe).exists():
        return str(Path(java_home) / "bin" / exe)
    return shutil.which(name)


def read_properties(path: Path) -> dict[str, str]:
    props = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "!")) and "=" in line:
                key, value = line.split("=", 1)
                props[key.strip()] = value.strip().replace("\\:", ":").replace("\\\\", "\\")
    return props


def android_sdk(app: App) -> Path | None:
    candidates = [
        read_properties(app.android / "local.properties").get("sdk.dir"),
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
    ]
    if IS_WINDOWS and os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk"))
    else:
        candidates += [str(Path.home() / "Library/Android/sdk"), str(Path.home() / "Android/Sdk")]
    for candidate in filter(None, candidates):
        if (Path(candidate) / "platforms").is_dir():
            return Path(candidate)
    return None


def build_tool(app: App, name: str) -> str | None:
    """Newest copy of an SDK build-tools binary (apksigner, aapt2, ...)."""
    sdk = android_sdk(app)
    if not sdk or not (sdk / "build-tools").is_dir():
        return None
    names = [f"{name}.exe", f"{name}.bat"] if IS_WINDOWS else [name]
    versions = sorted(
        (sdk / "build-tools").iterdir(),
        key=lambda d: [int(p) if p.isdigit() else 0 for p in re.split(r"[.-]", d.name)],
        reverse=True,
    )
    for version in versions:
        for exe in names:
            if (version / exe).exists():
                return str(version / exe)
    return None


def apksigner(app: App) -> str | None:
    return build_tool(app, "apksigner")


def gradlew(app: App) -> Path:
    return app.android / ("gradlew.bat" if IS_WINDOWS else "gradlew")


# ---------------------------------------------------------------- keystore helpers


def normalize_digest(value: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", value).upper()


def pretty_digest(value: str) -> str:
    return ":".join(value[i : i + 2] for i in range(0, len(value), 2))


def keytool_env(signing: Signing) -> dict[str, str]:
    return dict(os.environ, PIPELINE_KT_STOREPASS=signing.store_password, PIPELINE_KT_KEYPASS=signing.key_password)


def keystore_details(signing: Signing):
    """Run `keytool -list -v` for the configured alias. Returns the CompletedProcess or None."""
    keytool = jdk_tool("keytool")
    if not keytool:
        return None
    return capture(
        [keytool, "-list", "-v", "-keystore", signing.store_file, "-alias", signing.key_alias,
         "-storepass:env", "PIPELINE_KT_STOREPASS"],
        env=keytool_env(signing),
    )


def keystore_aliases(signing: Signing) -> list[str]:
    keytool = jdk_tool("keytool")
    result = capture(
        [keytool, "-list", "-keystore", signing.store_file, "-storepass:env", "PIPELINE_KT_STOREPASS"],
        env=keytool_env(signing),
    )
    return [line.split(",")[0].strip() for line in output_of(result).splitlines() if re.search(r"Entry,?\s*$", line)]


def cert_digest(text: str, algo: str) -> str | None:
    match = re.search(rf"{algo}:\s*([0-9A-Fa-f:]+)", text)
    return normalize_digest(match.group(1)) if match else None


def artifact_sha256(app: App, path: Path) -> tuple[str | None, str]:
    """Return (SHA-256 of the signing certificate, signer owner text) for an .apk or .aab."""
    if path.suffix == ".apk":
        signer = apksigner(app)
        if signer:
            result = capture([signer, "verify", "--print-certs", str(path)])
            text = output_of(result)
            if result is None or result.returncode != 0:
                return None, text.splitlines()[0] if text else "apksigner failed"
            digest = re.search(r"certificate SHA-256 digest:\s*([0-9a-fA-F]+)", text)
            owner = re.search(r"certificate DN:\s*(.+)", text)
            return (normalize_digest(digest.group(1)) if digest else None), (owner.group(1).strip() if owner else "")
    keytool = jdk_tool("keytool")
    result = capture([keytool, "-printcert", "-jarfile", str(path)]) if keytool else None
    text = output_of(result)
    owner = re.search(r"Owner:\s*(.+)", text)
    return cert_digest(text, "SHA256"), (owner.group(1).strip() if owner else "")


def _varint(data: bytes, i: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, i
        shift += 7


def _proto_fields(data: bytes):
    """Yield (field number, wire type, value) for each field of a protobuf message."""
    i = 0
    while i < len(data):
        key, i = _varint(data, i)
        wire = key & 7
        if wire == 0:
            value, i = _varint(data, i)
        elif wire == 2:
            length, i = _varint(data, i)
            value, i = data[i : i + length], i + length
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            value, i = data[i : i + size], i + size
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield key >> 3, wire, value


def aab_package_name(path: Path) -> str | None:
    # base/manifest/AndroidManifest.xml in an AAB is an aapt2 XmlNode proto:
    # XmlNode.element(1) -> XmlElement.attribute(4) -> XmlAttribute{namespace_uri(1), name(2), value(3)}.
    with zipfile.ZipFile(path) as bundle:
        manifest = bundle.read("base/manifest/AndroidManifest.xml")
    element = next((v for f, w, v in _proto_fields(manifest) if f == 1 and w == 2), b"")
    for field_no, wire, value in _proto_fields(element):
        if field_no == 4 and wire == 2:
            attr = {f: v for f, w, v in _proto_fields(value) if w == 2}
            if attr.get(2) == b"package" and not attr.get(1):
                return attr.get(3, b"").decode("utf-8")
    return None


def apk_package_name(app: App, path: Path) -> str | None:
    aapt2 = build_tool(app, "aapt2") or build_tool(app, "aapt")
    result = capture([aapt2, "dump", "badging", str(path)]) if aapt2 else None
    match = re.search(r"package: name='([^']+)'", output_of(result))
    return match.group(1) if match else None


def artifact_package_name(app: App, path: Path) -> str | None:
    try:
        return apk_package_name(app, path) if path.suffix == ".apk" else aab_package_name(path)
    except (OSError, KeyError, ValueError, IndexError, zipfile.BadZipFile):
        return None


def verify_package(app: App, path: Path) -> str | None:
    """Fail if the built file's package differs from <PREFIX>_PACKAGE_NAME. Returns the package."""
    actual = artifact_package_name(app, path)
    expected = expected_package(app)
    print(f"Package     {path.name}: {actual or 'unknown'}   expected: {expected or 'not set in .env'}")
    if expected and actual and actual != expected:
        raise PipelineError(
            f"{path.name} has package {actual}, but Play Console expects {expected}. "
            f"Fix android.package in the Expo config and run: python app.py prebuild {app.id}"
        )
    if expected and not actual:
        raise PipelineError(f"Could not read the package name from {path.name}.")
    if expected:
        print(f"✅ MATCH: package name is {actual}.")
    return actual


# ---------------------------------------------------------------- versions


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def write_json(path: Path, data: dict, original_text: str) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if "\r\n" in original_text:
        text = text.replace("\n", "\r\n")
    write_text(path, text)


def gradle_values(app: App) -> dict[str, str | None]:
    text = read_text(app.build_gradle) if app.build_gradle.exists() else ""
    get = lambda pattern: (m.group(1) if (m := re.search(pattern, text)) else None)
    return {
        "applicationId": get(r"applicationId\s+['\"]([^'\"]+)['\"]"),
        "versionCode": get(r"versionCode\s+(\d+)"),
        "versionName": get(r"versionName\s+['\"]([^'\"]*)['\"]"),
    }


def read_versions(app: App) -> tuple[str, int]:
    expo = json.loads(read_text(app.app_json)).get("expo", {})
    code = expo.get("android", {}).get("versionCode") or gradle_values(app)["versionCode"] or 1
    return expo.get("version", "1.0.0"), int(code)


def next_patch_version(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise PipelineError(f"app.json version '{version}' is not in MAJOR.MINOR.PATCH format.")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def sync_gradle_versions(app: App, version: str, code: int) -> None:
    gradle = read_text(app.build_gradle)
    gradle, code_hits = re.subn(r"versionCode\s+\d+", f"versionCode {code}", gradle)
    gradle, name_hits = re.subn(r"versionName\s+['\"][^'\"]*['\"]", f'versionName "{version}"', gradle)
    if not code_hits or not name_hits:
        raise PipelineError(f"Could not find versionCode/versionName in {app.build_gradle}")
    write_text(app.build_gradle, gradle)


def bump_versions(app: App, last_code: int | None) -> dict[Path, str]:
    """Bump patch version and versionCode in app.json, package.json, package-lock.json and
    android/app/build.gradle. Returns the original contents so a failed build can be undone."""
    package_json = app.project / "package.json"
    package_lock = app.project / "package-lock.json"
    files = [app.app_json, package_json, package_lock, app.build_gradle]
    originals = {path: read_text(path) for path in files if path.exists()}

    old_version, old_code = read_versions(app)
    new_version = next_patch_version(old_version)
    new_code = max(old_code, last_code or 0) + 1

    config = json.loads(originals[app.app_json])
    config["expo"]["version"] = new_version
    config["expo"].setdefault("android", {})["versionCode"] = new_code
    write_json(app.app_json, config, originals[app.app_json])

    for path in (package_json, package_lock):
        if path not in originals:
            continue
        data = json.loads(originals[path])
        if data.get("version") != old_version:
            continue
        data["version"] = new_version
        if data.get("packages", {}).get("", {}).get("version") == old_version:
            data["packages"][""]["version"] = new_version
        write_json(path, data, originals[path])

    if app.build_gradle.exists():
        sync_gradle_versions(app, new_version, new_code)
    print(f"\n🔼 Version bumped: {old_version} -> {new_version}, versionCode {old_code} -> {new_code}")
    return originals


def restore_files(originals: dict[Path, str]) -> None:
    for path, text in originals.items():
        write_text(path, text)
    print("\n↩️  Build failed, version bump reverted.")


def load_history() -> dict:
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def last_aab_code(app: App) -> int | None:
    """Highest versionCode built or uploaded to Play so far."""
    entry = load_history().get(app.id, {})
    codes = [e.get("versionCode") for e in (entry.get("aab"), entry.get("play_upload")) if e]
    codes = [code for code in codes if isinstance(code, int)]
    return max(codes) if codes else None


def record_build(app: App, kind: str, sha256: str, package: str | None) -> None:
    history = load_history()
    version, code = read_versions(app)
    history.setdefault(app.id, {})[kind] = {
        "package": package,
        "version": version,
        "versionCode": code,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "sha256": pretty_digest(sha256),
    }
    HISTORY_FILE.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")


def should_bump(app: App, mode: bool | None) -> bool:
    """First AAB keeps the current version; later ones need a versionCode Play hasn't seen."""
    if mode is not None:
        return mode
    last = last_aab_code(app)
    if last is not None:
        return read_versions(app)[1] <= last
    return app.release_file("aab").exists()


# ---------------------------------------------------------------- preflight


class Report:
    def __init__(self) -> None:
        self.problems: list[str] = []
        self.warnings: list[str] = []

    @staticmethod
    def _hint(hint: str | list[str] | None) -> None:
        for line in [hint] if isinstance(hint, str) else hint or []:
            print(f"       ↳ {line}")

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def ok(self, message: str) -> None:
        print(f"  ✅ {message}")

    def warn(self, message: str, hint: str | list[str] | None = None) -> None:
        self.warnings.append(message)
        print(f"  ⚠️  {message}")
        self._hint(hint)

    def fail(self, message: str, hint: str | list[str] | None = None) -> None:
        self.problems.append(message)
        print(f"  ❌ {message}")
        self._hint(hint)


def expo_public_config(app: App) -> dict | None:
    npx = shutil.which("npx")
    if not npx or not (app.npm_dir / "node_modules").exists():
        return None
    result = capture([npx, "expo", "config", "--json", "--type", "public"], cwd=app.project, timeout=180)
    text = result.stdout if result and result.returncode == 0 else ""
    try:
        return json.loads(text[text.index("{"):]) if "{" in text else None
    except json.JSONDecodeError:
        return None


def check_project(app: App, prebuild: bool, report: Report) -> str | None:
    """Checks the project layout. Returns android.package from the resolved Expo config."""
    report.section("📁 Project")
    if not app.root.is_dir():
        report.fail(f"App path not found: {app.root}", f"Fix 'path' for {app.id} in {APPS_FILE.name}.")
        return None
    if not app.project.is_dir():
        report.fail(f"Project folder not found: {app.project}", f"Fix 'project_dir' for {app.id} in {APPS_FILE.name}.")
        return None

    for path in {app.project / "package.json", app.npm_dir / "package.json"}:
        try:
            json.loads(path.read_text(encoding="utf-8"))
            report.ok(f"{posix(path.relative_to(app.root))}")
        except (OSError, json.JSONDecodeError) as err:
            report.fail(f"{path} is missing or invalid", str(err))

    try:
        version, code = read_versions(app)
    except (OSError, json.JSONDecodeError) as err:
        report.fail("app.json is missing or invalid", str(err))
        return None
    if re.fullmatch(r"\d+\.\d+\.\d+", version):
        report.ok(f"app.json version {version}, versionCode {code}")
    else:
        report.fail(f"app.json version '{version}' is not MAJOR.MINOR.PATCH")

    config = expo_public_config(app)
    if config is None:
        package = json.loads(read_text(app.app_json)).get("expo", {}).get("android", {}).get("package")
        report.warn("Could not resolve the Expo config (npx expo config); using app.json only")
    else:
        package = config.get("android", {}).get("package")
        if config.get("version") != version:
            report.warn(f"Resolved Expo config version {config.get('version')} differs from app.json {version}",
                        "app.config.* overrides the version, so bumps written to app.json are ignored.")
    if not package:
        report.fail("No android.package set in the Expo config")
    elif package.startswith(("com.anonymous.", "com.example.")):
        report.fail(f"android.package '{package}' is a placeholder", "Set a real package id before publishing.")
    else:
        report.ok(f"android.package {package}")

    if not app.android.is_dir():
        report.warn("android/ folder not found", "It will be generated with: npx expo prebuild --platform android")
        return package
    missing = [p for p in (gradlew(app), app.build_gradle, app.android / "settings.gradle") if not p.exists()]
    if missing:
        hint = "It will be regenerated (--prebuild)." if prebuild else "Rerun with --prebuild to regenerate it."
        (report.warn if prebuild else report.fail)(
            f"Android project incomplete, missing: {', '.join(posix(p.relative_to(app.project)) for p in missing)}", hint
        )
        return package
    report.ok("Android project and Gradle wrapper")

    gradle = gradle_values(app)
    if package and gradle["applicationId"] and gradle["applicationId"] != package:
        message = f"build.gradle applicationId {gradle['applicationId']} differs from the Expo config ({package})"
        if prebuild:
            report.warn(message, "--prebuild will regenerate android/.")
        else:
            report.fail(message, f"android/ is stale. Fix: python app.py prebuild {app.id}  (menu option 10)")
    elif gradle["applicationId"]:
        report.ok(f"build.gradle applicationId {gradle['applicationId']}")
    if gradle["versionCode"] != str(code) or gradle["versionName"] != version:
        report.warn(
            f"build.gradle has versionCode {gradle['versionCode']} / versionName {gradle['versionName']}",
            f"It will be synced to app.json ({code} / {version}) before building.",
        )

    if not (app.npm_dir / "node_modules").is_dir():
        report.warn("node_modules not installed", "npm install runs before the build.")
    return package


def check_package_name(app: App, kind: str, config_package: str | None, prebuild: bool, report: Report) -> None:
    """Play Console rejects uploads whose package differs from the listing's, so pin it in .env."""
    report.section("📦 Package name")
    var = app.env_names.get("package_name")
    expected = expected_package(app)
    if not expected:
        message = f"{var or 'package_name'} is not set in {ENV_FILE.name}" if var else \
            f"No env.package_name configured for {app.id} in {APPS_FILE.name}"
        hint = "Set it to the package name shown in Play Console (or the one you will publish under)."
        (report.fail if kind == "aab" else report.warn)(message, hint)
        return
    if not re.fullmatch(r"[A-Za-z][\w]*(\.[A-Za-z][\w]*)+", expected):
        report.fail(f"{var}={expected} is not a valid Android package name")
        return
    report.ok(f"{var}={expected}")

    if config_package and config_package != expected:
        report.fail(
            f"Expo config android.package is {config_package}, but Play expects {expected}",
            [f"Set android.package to '{expected}' in app.json / app.config.*,",
             f"then run: python app.py prebuild {app.id}  (menu option 10)"],
        )
    elif config_package:
        report.ok(f"Expo config android.package matches ({config_package})")

    application_id = gradle_values(app)["applicationId"] if app.build_gradle.exists() else None
    if application_id and application_id != expected:
        message = f"android/app/build.gradle applicationId is {application_id}, but Play expects {expected}"
        if prebuild and config_package == expected:
            report.warn(message, "--prebuild will regenerate android/ from the Expo config.")
        else:
            report.fail(message, f"Fix the Expo config if needed, then run: python app.py prebuild {app.id}")
    elif application_id:
        report.ok(f"build.gradle applicationId matches ({application_id})")


def check_toolchain(app: App, report: Report) -> None:
    report.section("⚙️  Toolchain")
    for tool in ("node", "npm", "npx"):
        if not shutil.which(tool):
            report.fail(f"{tool} not found on PATH", "Install Node.js LTS.")
    node = capture([shutil.which("node") or "node", "--version"])
    if node and node.returncode == 0:
        report.ok(f"Node.js {node.stdout.strip()}")

    java_home = os.environ.get("JAVA_HOME")
    java_exe = "java.exe" if IS_WINDOWS else "java"
    if java_home and not (Path(java_home) / "bin" / java_exe).exists():
        report.fail(f"JAVA_HOME points to an invalid JDK: {java_home}", f"Set JAVA_HOME to a JDK {MIN_JAVA_MAJOR}+.")
    else:
        java = jdk_tool("java")
        result = capture([java, "-version"]) if java else None
        match = re.search(r'version "(\d+)(?:\.(\d+))?', output_of(result))
        if not match:
            report.fail("Java not found", f"Install JDK {MIN_JAVA_MAJOR}+ and set JAVA_HOME.")
        else:
            major = int(match.group(2)) if match.group(1) == "1" else int(match.group(1))
            if major < MIN_JAVA_MAJOR:
                report.fail(f"Java {major} found, Gradle needs JDK {MIN_JAVA_MAJOR}+")
            else:
                report.ok(f"Java {major}")
    if not jdk_tool("keytool"):
        report.fail("keytool not found", "It ships with the JDK; set JAVA_HOME.")

    sdk = android_sdk(app)
    if not sdk:
        report.fail("Android SDK not found", "Set ANDROID_HOME, or sdk.dir in android/local.properties.")
    else:
        report.ok(f"Android SDK ({sdk})")
        if not (sdk / "licenses" / "android-sdk-license").exists():
            report.warn("Android SDK licenses not accepted", "Run: sdkmanager --licenses")
        if not apksigner(app):
            report.warn("apksigner not found in SDK build-tools", "APK signature verification will use keytool.")

    if not SIGNING_INIT_SCRIPT.exists():
        report.fail(f"Signing init script missing: {SIGNING_INIT_SCRIPT}")

    free_gb = shutil.disk_usage(app.root).free / 1024**3
    if free_gb < MIN_FREE_DISK_GB:
        report.warn(f"Only {free_gb:.1f} GB free on the build drive", "Android release builds can need several GB.")
    else:
        report.ok(f"{free_gb:.0f} GB free disk space")


def check_app_env(app: App, report: Report) -> None:
    if not app.required_app_env:
        return
    report.section("🌎 App environment variables")
    files = [".env.production.local", ".env.local", ".env.production", ".env"]
    sources = [(name, read_env_file(app.project / name)) for name in files]
    for key in app.required_app_env:
        source = "shell environment" if os.environ.get(key) else next((n for n, v in sources if v.get(key)), None)
        if source:
            report.ok(f"{key} (from {source})")
        else:
            report.fail(f"{key} is not set", f"Add it to {posix(app.project / '.env')}; it is baked into the JS bundle.")


def check_signing(app: App, apps: dict[str, App], report: Report) -> Signing | None:
    report.section(f"🔐 Release signing ({ENV_FILE.name})")
    signing = signing_for(app)
    if signing.missing:
        names = ", ".join(app.env_names[name] for name in signing.missing)
        report.fail(f"Missing in {posix(ENV_FILE)}: {names}",
                    f"Run: python app.py keygen {app.id}   (or fill them in, see .env.example)")
        return None
    report.ok(f"{app.env_names['store_file'].rsplit('_STORE_FILE', 1)[0]}_* variables set")

    store = Path(signing.store_file)
    if not store.is_absolute():
        report.warn(f"{app.env_names['store_file']} is a relative path", "Use an absolute path with forward slashes.")
    if not store.is_file():
        report.fail(f"Keystore not found: {store}", [
            "If this app was ever published, restore that exact keystore to this path.",
            f"Only for a brand-new app, create one with: python app.py keygen {app.id}",
        ])
        return None
    report.ok(f"Keystore exists ({posix(store)})")
    resolved = store.resolve()
    if resolved.is_relative_to(app.android.resolve()):
        report.fail("Keystore is inside android/, which `expo prebuild --clean` deletes",
                    f"Move it to {posix(KEYS_DIR)} and update {app.env_names['store_file']}.")
    elif resolved.is_relative_to(app.root.resolve()):
        report.warn("Keystore is inside the app repo", "Keep it outside the repo so it never gets committed.")

    shared = [
        other.id for other in apps.values()
        if other.id != app.id and (s := signing_for(other).store_file) and Path(s).resolve() == resolved
    ]
    if shared:
        report.warn(f"Same keystore is also used by: {', '.join(shared)}")

    result = keystore_details(signing)
    text = output_of(result)
    if result is None:
        report.fail("keytool could not be run")
        return None
    if result.returncode != 0:
        if re.search(r"password was incorrect|password verification failed", text, re.I):
            report.fail(f"{app.env_names['store_password']} is wrong for this keystore")
        elif re.search(r"does not exist", text, re.I):
            aliases = keystore_aliases(signing)
            report.fail(f"Alias '{signing.key_alias}' not found in keystore",
                        f"Aliases in this keystore: {', '.join(aliases)}" if aliases else None)
        else:
            report.fail("keytool could not read the keystore", text.splitlines()[0] if text else None)
        return None
    report.ok(f"Keystore opens and alias '{signing.key_alias}' exists")

    owner = re.search(r"Owner:\s*(.+)", text)
    if owner and re.search(r"CN=Android Debug", owner.group(1), re.I):
        report.fail("Keystore holds the Android debug certificate", "Play Console rejects debug-signed builds.")
    valid_until = re.search(r"until:\s*(.+)", text)
    sha1 = cert_digest(text, "SHA1")
    if sha1:
        report.ok(f"Upload key SHA1 {pretty_digest(sha1)}")
    if valid_until:
        report.ok(f"Certificate valid until {valid_until.group(1).strip()}")

    certreq = capture(
        [jdk_tool("keytool"), "-certreq", "-keystore", signing.store_file, "-alias", signing.key_alias,
         "-storepass:env", "PIPELINE_KT_STOREPASS", "-keypass:env", "PIPELINE_KT_KEYPASS"],
        env=keytool_env(signing),
    )
    if certreq and re.search(r"not supported for PKCS12", certreq.stderr or ""):
        # keytool silently uses the store password here, but Gradle uses the key password and fails.
        report.fail(f"{app.env_names['key_password']} differs from the store password on a PKCS12 keystore",
                    "PKCS12 keystores use the store password for the key too.")
    elif certreq and certreq.returncode == 0:
        report.ok("Key password unlocks the private key")
    else:
        report.fail(f"{app.env_names['key_password']} cannot unlock the private key",
                    output_of(certreq).splitlines()[0] if output_of(certreq) else None)
    return signing


def check_git(app: App, report: Report) -> None:
    git = shutil.which("git")
    result = capture([git, "-C", str(app.root), "status", "--porcelain"]) if git else None
    if not result or result.returncode != 0:
        return
    report.section("🌿 Git")
    changes = [line for line in result.stdout.splitlines() if line.strip()]
    if changes:
        report.warn(f"{len(changes)} uncommitted change(s) in {app.root.name}",
                    "Commit first so the release maps to a known commit.")
    else:
        report.ok("Working tree clean")


def check_version_plan(app: App, bump_mode: bool | None, report: Report) -> None:
    report.section("🏷️  Play Store version")
    version, code = read_versions(app)
    last = last_aab_code(app)
    if should_bump(app, bump_mode):
        new_code = max(code, last or 0) + 1
        report.ok(f"Next iteration: will bump {version} -> {next_patch_version(version)}, versionCode {code} -> {new_code}")
    elif last is not None and code <= last:
        report.fail(f"versionCode {code} was already used by a previous AAB (last: {last})",
                    "Play Console rejects reused versionCodes. Drop --no-bump.")
    else:
        report.ok(f"First AAB: building {version} (versionCode {code}) without bumping")


def preflight(app: App, apps: dict[str, App], kind: str, prebuild: bool = False,
              bump_mode: bool | None = None) -> Signing | None:
    report = Report()
    print(f"\n{RULE}\n  🔍 PREFLIGHT: {app.name} ({kind.upper()})\n{RULE}")
    config_package = check_project(app, prebuild, report)
    check_package_name(app, kind, config_package, prebuild, report)
    check_toolchain(app, report)
    check_app_env(app, report)
    signing = check_signing(app, apps, report)
    check_git(app, report)
    if kind == "aab" and app.app_json.exists():
        check_version_plan(app, bump_mode, report)

    note = f" ({len(report.warnings)} warning(s))" if report.warnings else ""
    if report.problems:
        print(f"\n{RULE}\n  ❌ PREFLIGHT FAILED: {len(report.problems)} problem(s){note}\n{RULE}")
        return None
    print(f"\n{RULE}\n  ✅ PREFLIGHT PASSED{note}\n{RULE}")
    return signing


# ---------------------------------------------------------------- key generation


def confirm(prompt: str, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().lower()
    return default if not answer else answer in ("y", "yes")


def prompt_password(label: str) -> str:
    while True:
        first = getpass.getpass(f"{label} (min 6 chars, hidden): ")
        if len(first) < 6:
            print("Too short.")
            continue
        if getpass.getpass("Repeat it: ") == first:
            return first
        print("Passwords do not match.")


def keygen(app: App) -> None:
    """Create the upload keystore for an app if it does not exist yet, and save its values to .env."""
    signing = signing_for(app)
    store = Path(signing.store_file or KEYS_DIR / app.keystore_name)
    ensure_env_example(app)

    if store.exists():
        print(f"🔑 Keystore already exists: {posix(store)}")
        if not signing.store_file:
            upsert_env_file(ENV_FILE, {app.env_names["store_file"]: posix(store)}, f"{app.name} ({app.id})")
            print(f"Saved {app.env_names['store_file']} to {ENV_FILE.name}.")
        if signing.missing:
            names = ", ".join(app.env_names[name] for name in signing.missing if name != "store_file")
            if names:
                print(f"Fill in {names} in {posix(ENV_FILE)}.")
        return

    if not sys.stdin.isatty():
        raise PipelineError(f"Keystore {posix(store)} does not exist. Run `python app.py keygen {app.id}` in a terminal.")
    print(f"\n🔑 No upload keystore found for {app.name} at {posix(store)}.")
    print("⚠️  If this app is ALREADY on Google Play, stop: restore the original keystore instead.")
    print("A new key cannot sign updates for an existing listing without an upload-key reset.")
    if not confirm("Create a NEW upload keystore?"):
        raise PipelineError("Keystore creation cancelled.")

    alias = signing.key_alias or input("Key alias [upload]: ").strip() or "upload"
    if signing.store_password and confirm(f"Use the password already set in {app.env_names['store_password']}?", True):
        password = signing.store_password
    else:
        password = prompt_password("Keystore password")
    common_name = input(f"Certificate name (CN) [{app.name}]: ").strip() or app.name

    keytool = jdk_tool("keytool")
    if not keytool:
        raise PipelineError("keytool not found. Install a JDK and set JAVA_HOME.")
    store.parent.mkdir(parents=True, exist_ok=True)
    # PKCS12 keystores use one password for the store and the key, so both get the same value.
    env = dict(os.environ, PIPELINE_KT_STOREPASS=password, PIPELINE_KT_KEYPASS=password)
    dname = "CN=" + common_name.replace(",", "\\,")
    run([keytool, "-genkeypair", "-v", "-storetype", "PKCS12", "-keystore", str(store), "-alias", alias,
         "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000", "-dname", dname,
         "-storepass:env", "PIPELINE_KT_STOREPASS", "-keypass:env", "PIPELINE_KT_KEYPASS"], cwd=ROOT, env=env)

    upsert_env_file(
        ENV_FILE,
        {
            app.env_names["store_file"]: posix(store),
            app.env_names["key_alias"]: alias,
            app.env_names["store_password"]: password,
            app.env_names["key_password"]: password,
        },
        header=f"{app.name} ({app.id})",
    )
    details = output_of(keystore_details(signing_for(app)))
    sha1 = cert_digest(details, "SHA1")
    info(f"Created {posix(store)} and saved its {app.env_names['store_file'].rsplit('_STORE_FILE', 1)[0]}_* "
         f"values to {ENV_FILE.name}.")
    if sha1:
        print(f"🔏 Upload key SHA1: {pretty_digest(sha1)}")
    print(f"💾 Back up {store.name} and its password somewhere safe. Losing it means a Play upload-key reset.")


# ---------------------------------------------------------------- build


def verify_artifact(app: App, path: Path, signing: Signing) -> str:
    """Fail unless `path` is signed with the configured upload key. Returns the cert SHA-256."""
    expected = cert_digest(output_of(keystore_details(signing)), "SHA256")
    actual, owner = artifact_sha256(app, path)
    print(f"Upload key  {posix(signing.store_file)} (alias {signing.key_alias})")
    print(f"  SHA-256: {pretty_digest(expected) if expected else 'unknown'}")
    print(f"{path.name:<11} signed by {owner or 'unknown'}")
    print(f"  SHA-256: {pretty_digest(actual) if actual else 'unknown'}")
    if re.search(r"CN=Android Debug", owner or "", re.I):
        raise PipelineError(f"{path.name} is signed with the Android debug key.")
    if not expected or not actual:
        raise PipelineError(f"Could not read the signing certificate of {path.name} or the keystore.")
    if expected != actual:
        raise PipelineError(f"{path.name} is NOT signed with the {app.id} upload key.")
    print("✅ MATCH: signed with the upload key.")
    return actual


def build(app: App, apps: dict[str, App], kind: str, bump_mode: bool | None = None,
          skip_checks: bool = False, prebuild: bool = False) -> None:
    artifact = ARTIFACTS[kind]
    started = time.time()
    info(f"{app.name}: release {kind.upper()} build")

    keygen(app)
    signing = preflight(app, apps, kind, prebuild, bump_mode)
    if not signing:
        raise PipelineError("Preflight failed. Fix the issues above; nothing was built.")

    run([which("npm"), "install", "--no-audit", "--no-fund"], cwd=app.npm_dir)
    if app.checks and not skip_checks:
        info("Code checks")
        for check in app.checks:
            run(check, cwd=app.npm_dir)

    originals = bump_versions(app, last_aab_code(app)) if kind == "aab" and should_bump(app, bump_mode) else None
    try:
        if prebuild or not app.build_gradle.exists():
            prebuild_android(app)
        version, code = read_versions(app)
        sync_gradle_versions(app, version, code)

        output = app.android / artifact["output"]
        output.unlink(missing_ok=True)
        info(f"Building {artifact['file_name']} {version} (versionCode {code}) with Gradle")
        run(
            [str(gradlew(app)), "--init-script", str(SIGNING_INIT_SCRIPT),
             # Gradle does not see JS changes in workspace packages; always rebuild the JS bundle.
             "createBundleReleaseJsAndAssets", "--rerun", artifact["gradle_task"]],
            cwd=app.android,
            env=gradle_env(app, signing),
        )
        if not output.exists():
            raise PipelineError(f"Gradle finished but {output} was not found.")

        info("Verifying signature and package name")
        sha256 = verify_artifact(app, output, signing)
        package = verify_package(app, output)
    except (PipelineError, KeyboardInterrupt):
        if originals:
            restore_files(originals)
        raise

    app.release_dir.mkdir(exist_ok=True)
    dest = app.release_file(kind)
    shutil.copy2(output, dest)
    record_build(app, kind, sha256, package)
    size_mb = dest.stat().st_size / 1024**2
    minutes = (time.time() - started) / 60
    info(f"🎉 Built {dest.name} {version} (versionCode {code}) in {minutes:.1f} min -> {posix(dest)} ({size_mb:.1f} MB)")
    if kind == "aab" and sys.stdin.isatty() and confirm("\n☁️  Upload it to Google Play Console now?"):
        upload_to_play(app, dest)


def prebuild_android(app: App) -> None:
    info(f"Regenerating {posix(app.android)} from the Expo config (expo prebuild)")
    run([which("npx"), "expo", "prebuild", "--platform", "android", "--no-install"], cwd=app.project)


def verify_release(app: App, kind: str) -> None:
    path = app.release_file(kind)
    if not path.exists():
        raise PipelineError(f"{posix(path)} not found. Build it first.")
    signing = signing_for(app)
    if signing.missing or not Path(signing.store_file).exists():
        raise PipelineError(f"Signing values for {app.id} are incomplete in {ENV_FILE.name}.")
    verify_artifact(app, path, signing)
    verify_package(app, path)


def list_apps(apps: dict[str, App]) -> None:
    history = load_history()
    print(f"\n{'App':<18} {'Version':<14} {'Keystore':<14} {'Last AAB':<20} Path")
    for app in apps.values():
        try:
            version, code = read_versions(app)
            version_text = f"{version} ({code})"
        except (OSError, json.JSONDecodeError):
            version_text = "?"
        signing = signing_for(app)
        if not signing.store_file:
            key_status = "❌ not set"
        elif not Path(signing.store_file).exists():
            key_status = "❌ missing"
        else:
            key_status = "✅ ok" if not signing.missing else "⚠️ incomplete"
        last = history.get(app.id, {}).get("aab")
        last_text = f"{last['version']} ({last['versionCode']})" if last else "-"
        print(f"{app.id:<18} {version_text:<14} {key_status:<14} {last_text:<20} {posix(app.project)}")


def open_release_folder(app: App) -> None:
    app.release_dir.mkdir(exist_ok=True)
    if IS_WINDOWS:
        os.startfile(app.release_dir)
    else:
        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(app.release_dir)])


# ---------------------------------------------------------------- Google Play upload


def configured_aab(app: App) -> Path:
    var = app.env_names.get("aab_path")
    if not var:
        raise PipelineError(f"No env.aab_path configured for {app.id} in {APPS_FILE.name}.")
    value = env_var(var)
    if not value:
        built = app.release_dir / "app-release.aab"
        example = posix(built) if built.is_file() else "C:/path/to/app-release.aab"
        raise PipelineError(f"{var} is not set in {posix(ENV_FILE)}, so there is no .aab to upload for {app.id}.\n"
                            f"       ↳ Add this line to {ENV_FILE.name}: {var}={example}")
    path = Path(value)
    if path.suffix.lower() != ".aab":
        raise PipelineError(f"{var}={value} is not an .aab file.")
    if not path.is_file():
        raise PipelineError(f"{var} points to a missing file: {posix(path)}")
    return path


def play_service_account() -> tuple[Path, str]:
    """The service account key file from .env and its client_email."""
    value = env_var(PLAY_CREDENTIALS_VAR)
    if not value:
        raise PipelineError(f"{PLAY_CREDENTIALS_VAR} is not set in {posix(ENV_FILE)}. See README.md to create the key.")
    path = Path(value)
    if not path.is_file():
        raise PipelineError(f"{PLAY_CREDENTIALS_VAR} points to a missing file: {posix(path)}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise PipelineError(f"{posix(path)} is not a valid JSON key file: {err}")
    if data.get("type") != "service_account" or not data.get("client_email"):
        raise PipelineError(f'{posix(path)} is not a service account key (expected "type": "service_account").')
    return path, data["client_email"]


def ask_track() -> str:
    labels = [f"{label} ({track})" for track, label in PLAY_TRACKS.items()]
    choice = choose("Select Play track", labels)
    if not choice:
        raise PipelineError("Upload cancelled: no Play track was selected (pressed Enter at the track prompt).")
    return list(PLAY_TRACKS)[labels.index(choice)]


def describe_play_error(err: Exception) -> str:
    """Readable message for a Google API, auth or network error, plus a fix hint when known."""
    message = f"{type(err).__name__}: {err}"
    status = getattr(getattr(err, "resp", None), "status", None)
    content = getattr(err, "content", None)
    if status and content:
        try:
            message = f"HTTP {status}: {json.loads(content)['error']['message']}"
        except (ValueError, KeyError, TypeError):
            message = f"HTTP {status}: {content.decode('utf-8', 'replace')[:500]}"
    hints = [hint for pattern, hint in PLAY_ERROR_HINTS if re.search(pattern, message, re.I)]
    return "\n".join([message, *(f"       ↳ {hint}" for hint in hints)])


def record_upload(app: App, package: str, code: int, track: str, status: str, path: Path) -> None:
    history = load_history()
    history.setdefault(app.id, {})["play_upload"] = {
        "package": package,
        "versionCode": code,
        "track": track,
        "status": status,
        "file": posix(path),
        "uploaded_at": datetime.now().isoformat(timespec="seconds"),
    }
    HISTORY_FILE.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")


def upload_to_play(app: App, aab: Path | None = None, track: str | None = None, notes: str | None = None) -> None:
    """Upload an .aab (default: <PREFIX>_AAB_PATH) and publish it on a track of an existing Play app."""
    progress = {"step": "checking the setup"}
    try:
        result, next_step = _upload_to_play(app, aab, track, notes, progress)
    except (PipelineError, KeyboardInterrupt):
        print(f"\n{RULE}\n  ❌ UPLOAD FAILED while {progress['step']}\n{RULE}")
        raise
    print(f"\n{RULE}\n  ✅ UPLOADED TO GOOGLE PLAY\n{RULE}")
    for label, value in result.items():
        print(f"  {label:<12} {value}")
    print(f"\n  {next_step}")


def _upload_to_play(app: App, aab: Path | None, track: str | None, notes: str | None,
                    progress: dict[str, str]) -> tuple[dict[str, str], str]:
    try:
        from google.oauth2 import service_account
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build as discovery_build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload, build_http
    except ImportError:
        raise PipelineError(f"Google API libraries are not installed. Run: pip install -r {posix(ROOT / 'requirements.txt')}")

    started = time.time()
    info(f"{app.name}: upload .aab to Google Play Console")
    key_file, client_email = play_service_account()
    aab = aab or configured_aab(app)
    print(f"AAB         {posix(aab)} ({aab.stat().st_size / 1024**2:.1f} MB)")
    print(f"Account     {client_email}")
    package = verify_package(app, aab)
    if not package:
        raise PipelineError(f"Could not read the package name from {aab.name}.")
    signing = signing_for(app)
    if signing.missing or not Path(signing.store_file).is_file():
        print(f"⚠️  Signing values for {app.id} are incomplete in {ENV_FILE.name}; skipping the local upload-key check.")
    else:
        verify_artifact(app, aab, signing)

    if not track:
        progress["step"] = "choosing the Play track"
        if not sys.stdin.isatty():
            raise PipelineError(f"No track given. Pass --track {'|'.join(PLAY_TRACKS)}.")
        track = ask_track()
    if notes is None and sys.stdin.isatty():
        notes = input(f"Release notes ({RELEASE_NOTES_LANGUAGE}, Enter to skip): ").strip()
    if notes and len(notes) > MAX_RELEASE_NOTES:
        raise PipelineError(f"Release notes are {len(notes)} characters; Play allows {MAX_RELEASE_NOTES}.")

    edits = edit_id = None
    try:
        progress["step"] = "signing in to Google Play"
        info(f"Opening a Play Console edit for {package}")
        credentials = service_account.Credentials.from_service_account_file(str(key_file), scopes=[PLAY_SCOPE])
        # build_http() stops httplib2 from treating the resumable upload's 308 replies as redirects.
        base_http = build_http()
        base_http.timeout = PLAY_HTTP_TIMEOUT_SECONDS
        http = AuthorizedHttp(credentials, http=base_http)
        edits = discovery_build("androidpublisher", "v3", http=http, cache_discovery=False).edits()
        edit_id = edits.insert(packageName=package, body={}).execute(num_retries=3)["id"]

        progress["step"] = "uploading the .aab"
        info(f"Uploading {aab.name}")
        media = MediaFileUpload(str(aab), mimetype="application/octet-stream",
                                chunksize=UPLOAD_CHUNK_BYTES, resumable=True)
        request = edits.bundles().upload(packageName=package, editId=edit_id, media_body=media)
        bundle = None
        while bundle is None:
            status, bundle = request.next_chunk(num_retries=3)
            if status:
                print(f"\r  ⬆️  {status.progress():.0%}", end="", flush=True)
        print("\r  ⬆️  100%")
        code = bundle["versionCode"]

        progress["step"] = f"publishing the release on the '{track}' track"
        info(f"Publishing versionCode {code} on the '{track}' track")
        release = {"versionCodes": [str(code)], "status": "completed"}
        if notes:
            release["releaseNotes"] = [{"language": RELEASE_NOTES_LANGUAGE, "text": notes}]
        update_track = lambda: edits.tracks().update(
            packageName=package, editId=edit_id, track=track, body={"track": track, "releases": [release]}
        ).execute(num_retries=3)
        try:
            update_track()
        except HttpError as err:
            # Apps that were never published on Play only accept draft releases.
            if not re.search(r"status draft may be created on draft app", describe_play_error(err), re.I):
                raise
            print("⚠️  Play only accepts draft releases until this app's first release is published; saving as draft.")
            release["status"] = "draft"
            update_track()

        progress["step"] = "committing the edit"
        info("Committing the edit")
        sent_for_review = True
        try:
            edits.commit(packageName=package, editId=edit_id).execute(num_retries=3)
        except HttpError as err:
            # Apps with managed publishing or a rejected update refuse automatic review submission.
            if "changesNotSentForReview" not in describe_play_error(err):
                raise
            edits.commit(packageName=package, editId=edit_id, changesNotSentForReview=True).execute(num_retries=3)
            sent_for_review = False
        edit_id = None
    except BaseException as err:
        if edit_id:
            try:
                edits.delete(packageName=package, editId=edit_id).execute()
            except Exception:
                pass
        if isinstance(err, Exception):
            raise PipelineError(describe_play_error(err)) from err
        raise

    track_page = f"Play Console > Test and release > {PLAY_TRACKS.get(track, track)}"
    if release["status"] == "draft":
        next_step = f"Saved as a draft. Publish it in {track_page} > Edit release > Save and publish."
    elif not sent_for_review:
        next_step = ("Play did not send it for review automatically (managed publishing or a previous rejection). "
                     "Open Play Console > Publishing overview and send the changes for review.")
    elif track == "internal":
        next_step = "Published. Internal testers get it within a few minutes."
    else:
        next_step = "Published and sent to Google for review. It goes live once the review passes."

    record_upload(app, package, code, track, release["status"], aab)
    result = {
        "App": app.name,
        "Package": package,
        "versionCode": str(code),
        "Track": track,
        "Status": release["status"],
        "File": posix(aab),
        "Time": f"{time.time() - started:.0f} s",
    }
    return result, next_step


# ---------------------------------------------------------------- CLI / menu


def choose(prompt: str, items: list[str]) -> str | None:
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item}")
    while True:
        answer = input(f"{prompt} [1-{len(items)}, Enter to cancel]: ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print(f"⚠️  '{answer}' is not a valid choice. Type a number from 1 to {len(items)}, or press Enter to cancel.")


MENU = {
    "1": ("📋 List apps", None),
    "2": ("🔍 Preflight checks (APK)", lambda app, apps: preflight(app, apps, "apk")),
    "3": ("🔍 Preflight checks (AAB)", lambda app, apps: preflight(app, apps, "aab")),
    "4": ("📱 Build release .apk", lambda app, apps: build(app, apps, "apk")),
    "5": ("📦 Build signed .aab for Play Store (auto version bump)", lambda app, apps: build(app, apps, "aab")),
    "6": ("🔑 Create upload keystore (if missing)", lambda app, apps: keygen(app)),
    "7": ("🔏 Verify signature of release/app-release.aab", lambda app, apps: verify_release(app, "aab")),
    "8": ("🔏 Verify signature of release/app-release.apk", lambda app, apps: verify_release(app, "apk")),
    "9": ("📂 Open release folder", lambda app, apps: open_release_folder(app)),
    "10": ("🔄 Regenerate android/ (expo prebuild, fixes stale android/)", lambda app, apps: prebuild_android(app)),
    "11": ("☁️  Upload .aab to Google Play Console and publish (<PREFIX>_AAB_PATH)", lambda app, apps: upload_to_play(app)),
}


def menu() -> None:
    while True:
        print(f"\n{RULE}\n  🚀 Apps pipeline\n{RULE}")
        for key, (label, _) in MENU.items():
            print(f"  {key}. {label}")
        print("  0. Exit")
        try:
            choice = input("Select an option: ").strip()
            if choice in ("0", "q", "exit"):
                return
            if choice not in MENU:
                print("Invalid option.")
                continue
            apps = load_apps()
            label, action = MENU[choice]
            if action is None:
                list_apps(apps)
                continue
            app_id = choose("Select app", list(apps))
            if app_id:
                action(apps[app_id], apps)
        except PipelineError as err:
            print(f"\n❌ Error: {err}")
        except KeyboardInterrupt:
            print("\n🛑 Cancelled")
        except EOFError:
            print()
            return


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build signed Android releases for the apps in all-app.json.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="list apps, versions and keystore status")

    p = sub.add_parser("preflight", help="run preflight checks only")
    p.add_argument("app")
    p.add_argument("--aab", action="store_true", help="include Play Store version checks")

    sub.add_parser("keygen", help="create the upload keystore if missing").add_argument("app")
    sub.add_parser("prebuild", help="regenerate android/ with expo prebuild").add_argument("app")

    for kind in ("apk", "aab"):
        b = sub.add_parser(kind, help=f"build a signed release .{kind}")
        b.add_argument("app")
        b.add_argument("--skip-checks", action="store_true", help="skip the app's typecheck/test commands")
        b.add_argument("--prebuild", action="store_true", help="regenerate android/ with expo prebuild first")
        if kind == "aab":
            group = b.add_mutually_exclusive_group()
            group.add_argument("--bump", dest="bump", action="store_true", default=None, help="force a version bump")
            group.add_argument("--no-bump", dest="bump", action="store_false", help="never bump the version")

    v = sub.add_parser("verify", help="check the signature of the release file")
    v.add_argument("app")
    v.add_argument("kind", nargs="?", choices=["aab", "apk"], default="aab")

    u = sub.add_parser("upload", help="upload <PREFIX>_AAB_PATH to Google Play and publish it on a track")
    u.add_argument("app")
    u.add_argument("--track", help=f"{', '.join(PLAY_TRACKS)} or a custom closed track (asked if omitted)")
    u.add_argument("--notes", help=f"release notes ({RELEASE_NOTES_LANGUAGE}, max {MAX_RELEASE_NOTES} chars)")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    # Emoji output crashes on Windows consoles that default to a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    args = parse_args(argv)
    if not args.command:
        menu()
        return 0
    try:
        apps = load_apps()
        if args.command == "list":
            list_apps(apps)
            return 0
        app = get_app(apps, args.app)
        if args.command == "preflight":
            return 0 if preflight(app, apps, "aab" if args.aab else "apk") else 1
        if args.command == "keygen":
            keygen(app)
        elif args.command == "prebuild":
            prebuild_android(app)
        elif args.command == "verify":
            verify_release(app, args.kind)
        elif args.command == "upload":
            upload_to_play(app, track=args.track, notes=args.notes)
        else:
            build(app, apps, args.command, getattr(args, "bump", None), args.skip_checks, args.prebuild)
        return 0
    except PipelineError as err:
        print(f"\n❌ Error: {err}")
    except KeyboardInterrupt:
        print("\n🛑 Cancelled")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
