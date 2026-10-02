#!/usr/bin/env python3
"""AndroidForge — Universal Android Project Detection.

Inspects an extracted Android source directory and identifies:

  • Project type (Gradle / Flutter / unknown)
  • Sub type (Java / Kotlin / Flutter / native-cmake)
  • Module structure (single-module / multi-module / legacy)
  • Build tool versions (AGP, Gradle, Kotlin, Java, NDK)
  • SDK levels (compileSdk, targetSdk, minSdk)
  • Indicator files present
  • Legacy indicators

Output:
  • JSON summary to stdout (or --output file)
  • Key fields written to $GITHUB_OUTPUT for use by the workflow

This script performs NO side effects on the project tree — it only reads.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Indicator files we look for at the project root
# ---------------------------------------------------------------------------
INDICATOR_FILES: dict[str, str] = {
    "settings.gradle": "gradle",
    "settings.gradle.kts": "gradle",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "gradlew": "gradle-wrapper",
    "gradlew.bat": "gradle-wrapper",
    "gradle-wrapper.properties": "gradle-wrapper",
    "AndroidManifest.xml": "android",
    "pubspec.yaml": "flutter",
    "local.properties": "android-local",
    "gradle.properties": "gradle-properties",
    "CMakeLists.txt": "native-cmake",
    "Android.mk": "native-ndk",
    "Application.mk": "native-ndk",
    "gradle/libs.versions.toml": "version-catalog",
}


# ---------------------------------------------------------------------------
# Version extraction patterns (applied to build.gradle / .kts / catalog)
# ---------------------------------------------------------------------------
AGP_PATTERNS = [
    r"classpath\s+['\"]com\.android\.tools\.build:gradle:([^'\"]+)['\"]",
    r"id\s+['\"]com\.android\.application['\"].*?version\s+['\"]([^'\"]+)['\"]",
    r"com\.android\.tools\.build:gradle:([^'\"\s]+)",
    r"androidGradlePlugin\s*=\s*['\"]([^'\"]+)['\"]",
    r"agp\s*=\s*['\"]([^'\"]+)['\"]",
    r"com-android-application\s*=\s*\{[^}]*version\s*=\s*['\"]([^'\"]+)['\"]",
]

KOTLIN_PATTERNS = [
    # Variable declarations first — these are explicit version pins.
    r"kotlinVersion\s*=\s*['\"]([^'\"]+)['\"]",
    r"kotlin_version\s*=\s*['\"]([^'\"]+)['\"]",
    r"\bkotlin\s*=\s*['\"]([^'\"]+)['\"]",
    r"org.jetbrains.kotlin.android\s*=\s*\{[^}]*version\s*=\s*['\"]([^'\"]+)['\"]",
    # Inline plugin references — exclude $ (Groovy variable references).
    r"org\.jetbrains\.kotlin:kotlin-gradle-plugin:([0-9][^'\"\s]*)",
    r"org\.jetbrains\.kotlin\.plugin:[^:]+:([0-9][^'\"\s]*)",
]

JAVA_PATTERNS = [
    r"sourceCompatibility\s*=?\s*(?:JavaVersion\.VERSION_)?(\w+)",
    r"targetCompatibility\s*=?\s*(?:JavaVersion\.VERSION_)?(\w+)",
    r"jvmTarget\s*=?\s*['\"](\d+)['\"]",
    r"JavaVersion\.VERSION_(\w+)",
]

NDK_PATTERNS = [
    r"ndkVersion\s*=?\s*['\"]([^'\"]+)['\"]",
]

SDK_PATTERNS = [
    (r"compileSdk\s*(?:=|:|\s)\s*(\d+)", "compile_sdk"),
    (r"compileSdkVersion\s+(\d+)", "compile_sdk"),
    (r"targetSdk\s*(?:=|:|\s)\s*(\d+)", "target_sdk"),
    (r"targetSdkVersion\s+(\d+)", "target_sdk"),
    (r"minSdk\s*(?:=|:|\s)\s*(\d+)", "min_sdk"),
    (r"minSdkVersion\s+(\d+)", "min_sdk"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def extract_version(content: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
        if m:
            v = m.group(1).strip()
            if v:
                return v
    return None


def normalize_java_version(raw: str | None) -> str | None:
    """Convert JavaVersion.VERSION_11 / 1_8 / VERSION_1_8 → '11' / '8'."""
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return raw
    if raw.startswith("1_") and raw[2:].isdigit():
        return raw.split("_", 1)[1]
    if "VERSION_" in raw:
        parts = raw.split("_")
        # VERSION_1_8 → ['VERSION','1','8'] → '8'
        if len(parts) >= 3 and parts[1] == "1":
            return parts[2]
        if len(parts) >= 2:
            return parts[-1]
    return raw


def find_project_root(extract_dir: Path) -> Path:
    """Locate the real project root inside an extracted ZIP tree.

    ZIPs are frequently structured as ``MyProject/`` → ``MyProject/settings.gradle``,
    so we look one or two levels deep for the first directory containing a
    settings.gradle / pubspec.yaml / build.gradle.
    """
    candidates: list[Path] = []

    def scan(d: Path, depth: int = 0) -> None:
        if depth > 3:
            return
        has_settings = (d / "settings.gradle").exists() or (d / "settings.gradle.kts").exists()
        has_pubspec = (d / "pubspec.yaml").exists()
        has_build_gradle = (d / "build.gradle").exists() or (d / "build.gradle.kts").exists()
        if has_settings or has_pubspec:
            candidates.append(d)
            return
        if has_build_gradle and depth > 0:
            candidates.append(d)
            return
        for sub in d.iterdir():
            if not sub.is_dir():
                continue
            if sub.name.startswith(".") or sub.name in {
                "build", "gradle", ".gradle", "__MACOSX", "node_modules", ".idea"
            }:
                continue
            scan(sub, depth + 1)

    scan(extract_dir)
    if not candidates:
        return extract_dir
    # Prefer settings.gradle / pubspec.yaml over build.gradle-only matches
    for c in candidates:
        if (c / "settings.gradle").exists() or (c / "settings.gradle.kts").exists() or (c / "pubspec.yaml").exists():
            return c
    return candidates[0]


def parse_gradle_wrapper(root: Path) -> dict[str, Any]:
    """Inspect gradle/wrapper/gradle-wrapper.properties for the Gradle version."""
    result: dict[str, Any] = {
        "present": False,
        "version": None,
        "distribution_url": None,
        "distribution_sha256": None,
        "uses_http": False,
    }
    props = root / "gradle" / "wrapper" / "gradle-wrapper.properties"
    gradlew = root / "gradlew"
    gradlew_bat = root / "gradlew.bat"
    wrapper_jar = root / "gradle" / "wrapper" / "gradle-wrapper.jar"

    if gradlew.exists() or gradlew_bat.exists() or props.exists():
        result["present"] = True
    if props.exists():
        result["present"] = True
        content = read_file(props)
        m = re.search(r"distributionUrl=([^\s]+)", content)
        if m:
            url = m.group(1).strip()
            # Replace ${BASEURL} which is used by some old wrappers
            url = url.replace("https\\://services.gradle.org/distributions", "https://services.gradle.org/distributions")
            url = url.replace("$BASEURL", "https://services.gradle.org/distributions")
            result["distribution_url"] = url
            result["uses_http"] = url.startswith("http://")
            v = re.search(r"gradle-([\d.]+)-\w+\.zip", url)
            if v:
                result["version"] = v.group(1)
        sha = re.search(r"distributionSha256Sum=([a-fA-F0-9]+)", content)
        if sha:
            result["distribution_sha256"] = sha.group(1)
    if not wrapper_jar.exists() and result["present"]:
        result["present"] = False
        result["missing_jar"] = True
    return result


def find_modules(root: Path) -> list[str]:
    """Discover modules declared in settings.gradle and on the filesystem."""
    modules: list[str] = []
    settings_file: Path | None = None
    for name in ("settings.gradle", "settings.gradle.kts"):
        if (root / name).exists():
            settings_file = root / name
            break
    if settings_file:
        content = read_file(settings_file)
        # Capture each include statement's arguments, then pull all quoted
        # names from those arguments. This handles all common syntaxes:
        #   include ':app'
        #   include ':app', ':lib1', ':lib2'
        #   include(':app')
        #   include ':app'\ninclude ':lib1'
        include_blocks = re.findall(r"include\s*([^;\n]+)", content, re.IGNORECASE)
        for block in include_blocks:
            for inc in re.findall(r"['\"]([^'\"]+)['\"]", block):
                mod = inc.lstrip(":")
                if mod and mod not in modules:
                    modules.append(mod)
    # Also scan filesystem for module build files
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        if sub.name in {"build", "gradle", ".gradle", ".idea", "node_modules"}:
            continue
        if (sub / "build.gradle").exists() or (sub / "build.gradle.kts").exists():
            if sub.name not in modules:
                modules.append(sub.name)
    return modules


def detect_native(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "has_native": False,
        "build_system": None,
        "has_cmake": False,
        "has_ndk_build": False,
        "cpp_files": 0,
    }
    for sub in [root, *(s for s in root.iterdir() if s.is_dir())]:
        if (sub / "CMakeLists.txt").exists():
            result["has_native"] = True
            result["has_cmake"] = True
            result["build_system"] = "cmake"
            break
        if (sub / "Android.mk").exists():
            result["has_native"] = True
            result["has_ndk_build"] = True
            result["build_system"] = "ndk-build"
            break
    # Count C/C++ source files (shallow scan)
    try:
        cpp_count = sum(1 for _ in root.rglob("*.cpp"))
        cpp_count += sum(1 for _ in root.rglob("*.c"))
        cpp_count += sum(1 for _ in root.rglob("*.cc"))
        cpp_count += sum(1 for _ in root.rglob("*.cxx"))
        result["cpp_files"] = cpp_count
        if cpp_count > 0 and not result["has_native"]:
            result["has_native"] = True
            result["build_system"] = "cmake"
            result["has_cmake"] = True
    except Exception:
        pass
    return result


def detect_flutter(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "is_flutter": False,
        "flutter_version_constraint": None,
        "has_android_dir": False,
    }
    pubspec = root / "pubspec.yaml"
    if pubspec.exists():
        result["is_flutter"] = True
        content = read_file(pubspec)
        # Look for "flutter:" key inside the environment: block (may have other keys in between).
        m = re.search(r"environment:[\s\S]*?flutter:\s*[\"']?([^\"'\n]+)", content)
        if m:
            result["flutter_version_constraint"] = m.group(1).strip()
        # Look for an android/ subdirectory
        if (root / "android").is_dir():
            result["has_android_dir"] = True
    return result


def scan_build_files(root: Path) -> dict[str, Any]:
    """Scan all build.gradle / .kts files and the version catalog for versions."""
    info: dict[str, Any] = {
        "agp_version": None,
        "kotlin_version": None,
        "java_version": None,
        "ndk_version": None,
        "compile_sdk": None,
        "target_sdk": None,
        "min_sdk": None,
    }

    build_files: list[Path] = []
    for name in ("build.gradle", "build.gradle.kts"):
        if (root / name).exists():
            build_files.append(root / name)
    for sub in root.iterdir():
        if not sub.is_dir():
            continue
        if sub.name in {"build", "gradle", ".gradle", ".idea", "node_modules"}:
            continue
        for name in ("build.gradle", "build.gradle.kts"):
            f = sub / name
            if f.exists():
                build_files.append(f)

    all_content = "\n".join(read_file(f) for f in build_files)

    # Version catalog (gradle/libs.versions.toml)
    catalog = root / "gradle" / "libs.versions.toml"
    catalog_content = ""
    if catalog.exists():
        catalog_content = read_file(catalog)
        combined = all_content + "\n" + catalog_content
    else:
        combined = all_content

    info["agp_version"] = (
        extract_version(catalog_content, AGP_PATTERNS)
        or extract_version(all_content, AGP_PATTERNS)
    )
    info["kotlin_version"] = (
        extract_version(catalog_content, KOTLIN_PATTERNS)
        or extract_version(all_content, KOTLIN_PATTERNS)
    )
    info["ndk_version"] = extract_version(combined, NDK_PATTERNS)
    info["java_version"] = normalize_java_version(extract_version(combined, JAVA_PATTERNS))

    for pattern, key in SDK_PATTERNS:
        m = re.search(pattern, combined, re.MULTILINE)
        if m and not info[key]:
            info[key] = m.group(1)

    # gradle.properties can also expose kotlin / agp versions
    gradle_props = root / "gradle.properties"
    if gradle_props.exists():
        content = read_file(gradle_props)
        if not info["kotlin_version"]:
            m = re.search(r"kotlinVersion\s*=\s*['\"]?([^'\"\n]+)", content)
            if m:
                info["kotlin_version"] = m.group(1).strip()
        if not info["agp_version"]:
            m = re.search(r"androidGradlePlugin\s*=\s*['\"]?([^'\"\n]+)", content)
            if m:
                info["agp_version"] = m.group(1).strip()
    return info


def detect_kotlin(root: Path, all_gradle_content: str) -> bool:
    """Heuristic: does this project use Kotlin at all?"""
    if re.search(r"kotlin[_-]android|kotlin-android|org\.jetbrains\.kotlin", all_gradle_content, re.IGNORECASE):
        return True
    try:
        for _ in root.rglob("*.kt"):
            return True
    except Exception:
        pass
    return False


def assess_legacy(result: dict[str, Any]) -> tuple[bool, list[str]]:
    """Decide whether the project should be treated as legacy."""
    is_legacy = False
    reasons: list[str] = []
    v = result.get("versions", {}) or {}
    wrapper = result.get("wrapper", {}) or {}

    if wrapper.get("version"):
        try:
            major = int(str(wrapper["version"]).split(".")[0])
            if major < 7:
                is_legacy = True
                reasons.append(f"Old Gradle wrapper: {wrapper['version']}")
        except (ValueError, IndexError):
            pass

    if v.get("agp_version"):
        agp = str(v["agp_version"])
        try:
            major = int(agp.split(".")[0])
            if major < 7:
                is_legacy = True
                reasons.append(f"Old Android Gradle Plugin: {agp}")
        except (ValueError, IndexError):
            pass

    if v.get("java_version"):
        jv = str(v["java_version"])
        try:
            jvn = int(jv)
            if jvn <= 11:
                is_legacy = True
                reasons.append(f"Targets Java {jv}")
        except ValueError:
            pass

    if v.get("compile_sdk"):
        try:
            cs = int(v["compile_sdk"])
            if cs < 30:
                is_legacy = True
                reasons.append(f"Low compileSdk: {cs}")
        except (ValueError, TypeError):
            pass

    if not wrapper.get("present"):
        is_legacy = True
        reasons.append("No Gradle wrapper present")
    return is_legacy, reasons


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="AndroidForge project detection")
    parser.add_argument("--root", required=True, help="Path to extracted project root")
    parser.add_argument("--output", default=None, help="Optional output file for JSON")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"ERROR: root path does not exist: {root}", file=sys.stderr)
        return 1

    project_root = find_project_root(root)

    # Indicator files (look at project root and one level deep)
    found_files: dict[str, str] = {}
    for rel, _ in INDICATOR_FILES.items():
        candidates = [project_root / rel, *[(sub / rel) for sub in project_root.iterdir() if sub.is_dir() and sub.name not in {"build", "gradle", ".gradle", ".idea", "node_modules"}]]
        for c in candidates:
            if c.exists():
                found_files[rel] = str(c.relative_to(project_root))
                break

    # Determine project type
    project_type = "unknown"
    sub_type: str | None = None
    if (project_root / "pubspec.yaml").exists():
        project_type = "flutter"
        sub_type = "flutter"
    elif (project_root / "settings.gradle").exists() or (project_root / "settings.gradle.kts").exists() or (project_root / "build.gradle").exists() or (project_root / "build.gradle.kts").exists():
        project_type = "gradle"

    # Collect gradle content for kotlin detection
    gradle_files: list[Path] = []
    for name in ("build.gradle", "build.gradle.kts"):
        f = project_root / name
        if f.exists():
            gradle_files.append(f)
    for sub in project_root.iterdir():
        if not sub.is_dir() or sub.name in {"build", "gradle", ".gradle", ".idea", "node_modules"}:
            continue
        for name in ("build.gradle", "build.gradle.kts"):
            f = sub / name
            if f.exists():
                gradle_files.append(f)
    gradle_content = "\n".join(read_file(f) for f in gradle_files)

    if project_type == "gradle" and sub_type is None:
        sub_type = "kotlin" if detect_kotlin(project_root, gradle_content) else "java"

    wrapper = parse_gradle_wrapper(project_root)
    modules = find_modules(project_root)
    native = detect_native(project_root)
    flutter = detect_flutter(project_root)
    versions = scan_build_files(project_root)

    if len(modules) > 1:
        structure = "multi-module"
    elif len(modules) == 1:
        structure = "single-module"
    elif project_type == "gradle":
        structure = "single-module"
    else:
        structure = "unknown"

    result: dict[str, Any] = {
        "project_root": str(project_root),
        "project_type": project_type,
        "sub_type": sub_type,
        "structure": structure,
        "indicator_files": found_files,
        "wrapper": wrapper,
        "modules": modules,
        "native": native,
        "flutter": flutter,
        "versions": versions,
        "is_legacy": False,
        "legacy_reasons": [],
    }
    is_legacy, legacy_reasons = assess_legacy(result)
    result["is_legacy"] = is_legacy
    result["legacy_reasons"] = legacy_reasons

    # Output JSON
    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output)
    else:
        print(output)

    # Write GITHUB_OUTPUT fields
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"project_root={project_root}\n")
            f.write(f"project_type={project_type}\n")
            f.write(f"sub_type={sub_type or ''}\n")
            f.write(f"structure={structure}\n")
            f.write(f"is_legacy={'true' if is_legacy else 'false'}\n")
            f.write(f"needs_gradle={'true' if project_type in ('gradle', 'flutter') else 'false'}\n")
            f.write(f"needs_flutter={'true' if flutter['is_flutter'] else 'false'}\n")
            f.write(f"has_native={'true' if native['has_native'] else 'false'}\n")
            f.write(f"wrapper_present={'true' if wrapper.get('present') else 'false'}\n")
            if wrapper.get("version"):
                f.write(f"gradle_version={wrapper['version']}\n")
            if versions.get("agp_version"):
                f.write(f"agp_version={versions['agp_version']}\n")
            if versions.get("kotlin_version"):
                f.write(f"kotlin_version={versions['kotlin_version']}\n")
            if versions.get("java_version"):
                f.write(f"java_version={versions['java_version']}\n")
            if versions.get("ndk_version"):
                f.write(f"ndk_version={versions['ndk_version']}\n")
            if versions.get("compile_sdk"):
                f.write(f"compile_sdk={versions['compile_sdk']}\n")
            # JSON payload for downstream steps
            f.write(f"json<<EOF\n{json.dumps(result)}\nEOF\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
