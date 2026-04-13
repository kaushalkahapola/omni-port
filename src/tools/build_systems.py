"""
build_systems.py — Build and test runner for the validation loop.

Strategy:
  1. For known projects, delegate to helpers/{project}/run_build.sh and
     run_tests.sh via Docker (mirrors the legacy evaluate harness).
  2. Fall back to bare Maven / Gradle when no helper is available.

Public API
----------
run_build(worktree_path, project_name)           → BuildResult
run_tests(worktree_path, project_name, ...)      → TestResult
detect_test_targets(worktree_path, project_name, changed_files) → TestTargetInfo
collect_test_results(worktree_path, project_name, target_classes, console_output) → dict
parse_compile_errors(output)                     → list[CompilerErrorDetail]
restore_repo_state(worktree_path)                → bool
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Path to the helpers/ directory relative to this file's repo root.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_HELPERS_ROOT = os.path.join(_REPO_ROOT, "helpers")

# Elasticsearch modules where targeted `--tests Class` against standard Gradle
# verification tasks is not supported (BWC matrix-only QA, etc.).
ELASTICSEARCH_HARNESS_EXCLUDED_TEST_MODULES: frozenset[str] = frozenset(
    {
        "x-pack/qa/rolling-upgrade",
    }
)

_TEST_SOURCE_DIRS = (
    "/src/test/java/",
    "/src/internalClusterTest/java/",
    "/src/javaRestTest/java/",
    "/src/yamlRestTest/java/",
    "/src/integTest/java/",
    "/src/integrationTest/java/",
)

_TEST_SUFFIXES = ("Test.java", "Tests.java", "IT.java", "TestCase.java")


# ──────────────────────────────────────────────────────────────────────────────
# Data classes
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class CompilerErrorDetail:
    file_path: str
    line: int
    message: str


@dataclass
class BuildResult:
    success: bool
    output: str
    mode: str  # e.g. "elasticsearch-helper", "gradle", "maven"


@dataclass
class TestResult:
    success: bool
    compile_error: bool
    output: str
    mode: str
    targets: dict[str, Any]
    test_state: dict[str, Any]


@dataclass
class TestTargetInfo:
    test_targets: list[str]       # ["module:pkg.ClassName", ...]
    source_modules: list[str]     # e.g. ["server", "x-pack/plugin/core"]
    all_modules: list[str]
    raw: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _resolve_valid_java_home() -> str | None:
    for key in ("JAVA_21_HOME", "JAVA_HOME"):
        candidate = os.getenv(key)
        if not candidate:
            continue
        java_bin = os.path.join(candidate, "bin", "java")
        if os.path.isdir(candidate) and os.path.isfile(java_bin):
            return candidate
    return None


def _detect_project_name(repo_path: str) -> str:
    return os.path.basename(repo_path).strip().lower()


def _helper_dir(project: str) -> str:
    return os.path.join(_HELPERS_ROOT, project.strip().lower())


def _has_helper(project: str) -> bool:
    return os.path.isdir(_helper_dir(project))


def _run_cmd(
    cmd: list[str],
    cwd: str,
    env: dict | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    print(f"  [build_systems] Running command: {' '.join(cmd)}")
    print(f"  [build_systems] Working directory: {cwd}")
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout chronologically
            text=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
        )
        output = (result.stdout or "").strip()
        print(f"  [build_systems] Command finished with return code: {result.returncode}")
        return {"success": result.returncode == 0, "returncode": result.returncode, "output": output}
    except subprocess.TimeoutExpired:
        print(f"  [build_systems] Command timed out after {timeout} seconds")
        return {"success": False, "returncode": -1, "output": "Command timed out"}
    except Exception as e:
        print(f"  [build_systems] Command failed with error: {str(e)}")
        return {"success": False, "returncode": -1, "output": str(e)}


def _get_current_head(repo_path: str) -> str:
    res = _run_cmd(["git", "rev-parse", "--short", "HEAD"], cwd=repo_path, timeout=10)
    if res["success"]:
        return res["output"].strip().splitlines()[0]
    return "worktree"


def _ensure_docker_image(project: str, repo_path: str) -> tuple[str | None, str | None]:
    """Build (or reuse) the per-project Docker builder image. Returns (tag, error)."""
    hdir = _helper_dir(project)
    dockerfile = os.path.join(hdir, "Dockerfile")
    if not os.path.exists(dockerfile):
        return None, f"Dockerfile not found for project {project}: {dockerfile}"

    env_key = f"{project.upper()}_BUILDER_IMAGE_TAG"
    tag = os.getenv(env_key, f"retrofit-{project.lower()}-builder:local")

    print(f"  [build_systems] Checking for Docker image: {tag}")
    inspect = _run_cmd(["docker", "image", "inspect", tag], cwd=repo_path, timeout=15)
    if inspect["success"]:
        print(f"  [build_systems] Docker image {tag} exists")
        return tag, None

    print(f"  [build_systems] Building Docker image: {tag} from {dockerfile}")
    build = _run_cmd(
        ["docker", "build", "-t", tag, "-f", dockerfile, hdir],
        cwd=repo_path,
        timeout=600,
    )
    if not build["success"]:
        print(f"  [build_systems] Failed to build Docker image {tag}")
        return None, f"Failed to build Docker image for {project} ({tag}): {build['output'][:500]}"
    print(f"  [build_systems] Docker image {tag} built successfully")
    return tag, None


def _filter_elasticsearch_test_targets(
    targets: list[str],
) -> tuple[list[str], list[str]]:
    kept, skipped = [], []
    for t in targets:
        mod = t.split(":", 1)[0] if ":" in t else ""
        if mod in ELASTICSEARCH_HARNESS_EXCLUDED_TEST_MODULES:
            skipped.append(t)
        else:
            kept.append(t)
    return kept, skipped


def _is_test_file(file_path: str) -> bool:
    p = (file_path or "").replace("\\", "/").lower()
    if any(d.lower() in p for d in _TEST_SOURCE_DIRS):
        return True
    filename = os.path.basename(p)
    return (
        any(filename.endswith(s.lower()) for s in _TEST_SUFFIXES)
        or (filename.startswith("test") and filename.endswith(".java"))
    )


def _find_module_for_path(repo_path: str, rel_path: str) -> str:
    """Walk up looking for a build file; return module path relative to repo root."""
    head = (rel_path or "").replace("\\", "/")
    while head:
        head = os.path.dirname(head)
        # Standard names first
        for bf in ("pom.xml", "build.gradle", "build.gradle.kts"):
            if os.path.exists(os.path.join(repo_path, head, bf)):
                return head
        
        # Check for any .gradle/.gradle.kts file (often [module-name].gradle in Hibernate ORM)
        try:
            full_dir = os.path.join(repo_path, head)
            if os.path.isdir(full_dir):
                for f in os.listdir(full_dir):
                    if (f.endswith(".gradle") or f.endswith(".gradle.kts")) and not f.startswith("settings.gradle"):
                        return head
        except Exception:
            pass

    for bf in ("pom.xml", "build.gradle", "build.gradle.kts"):
        if os.path.exists(os.path.join(repo_path, bf)):
            return ""
    return ""


def _is_gradle_repo(repo_path: str) -> bool:
    return os.path.exists(os.path.join(repo_path, "build.gradle")) or os.path.exists(
        os.path.join(repo_path, "build.gradle.kts")
    )


def _clear_junit_reports(repo_path: str) -> None:
    # Clear Maven surefire XML reports.
    for xml_path in glob.glob(
        os.path.join(repo_path, "**", "surefire-reports", "TEST-*.xml"), recursive=True
    ):
        try:
            os.remove(xml_path)
        except Exception:
            pass
    # Clear Gradle test-results directories (each module writes here).
    for test_results_dir in glob.glob(
        os.path.join(repo_path, "**", "build", "test-results"), recursive=True
    ):
        if os.path.isdir(test_results_dir):
            shutil.rmtree(test_results_dir, ignore_errors=True)
    # Clear target/test-results directories (Hibernate ORM Gradle custom output path
    # and other projects that redirect Gradle XML output under target/).
    # Note: Using a more inclusive pattern to ensure nested results are also cleared.
    for test_results_dir in glob.glob(
        os.path.join(repo_path, "**", "target", "test-results"), recursive=True
    ):
        if os.path.isdir(test_results_dir):
            shutil.rmtree(test_results_dir, ignore_errors=True)
    
    # Also explicitly clear any surefire-reports or failsafe-reports under target/
    for reports_dir in glob.glob(
        os.path.join(repo_path, "**", "target", "*-reports"), recursive=True
    ):
        if os.path.isdir(reports_dir):
            shutil.rmtree(reports_dir, ignore_errors=True)

    aggregate = os.path.join(repo_path, "build", "all-test-results")
    if os.path.isdir(aggregate):
        shutil.rmtree(aggregate, ignore_errors=True)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def restore_repo_state(repo_path: str) -> bool:
    """git reset --hard + git clean -fd on the given worktree."""
    print(f"  [build_systems] Restoring repo state in {repo_path}")
    try:
        subprocess.run(
            ["git", "reset", "--hard"],
            cwd=repo_path,
            capture_output=True,
            check=True,
            timeout=30,
        )
        result = subprocess.run(
            ["git", "clean", "-fd"],
            cwd=repo_path,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            subprocess.run(
                ["git", "clean", "-ffdX"],
                cwd=repo_path,
                capture_output=True,
                timeout=30,
            )
        print("  [build_systems] Repo state restored successfully")
        return True
    except Exception as e:
        print(f"  [build_systems] restore_repo_state warning: {e}")
        return False


def parse_compile_errors(output: str) -> list[CompilerErrorDetail]:
    """
    Extract structured compiler error locations from Maven/Gradle/javac output.

    Handles:
      - Maven javac:      [ERROR] /path/to/File.java:[line,col] error message
      - Gradle / javac:   /path/to/File.java:line: error:
      - Checkstyle:       [ERROR] /path/to/File.java:line:col: message [RuleName]
      - Maven checkstyle: [ERROR] /repo/path/File.java:line:col: message [RuleName]
    """
    errors: list[CompilerErrorDetail] = []
    seen: set[tuple[str, int]] = set()

    # Primary: javac-style  File.java:[line,col]: or File.java:line:
    javac_pattern = re.compile(
        r"([^:\s]+\.java):(?:\[(\d+),\d+\][ ]?:?|(\d+):)\s*(.*)"
    )
    # Secondary: checkstyle-style  [ERROR] /path/File.java:line:col: message [Rule]
    checkstyle_pattern = re.compile(
        r"\[ERROR\]\s+([^:\s]+\.java):(\d+):\d+:\s+(.*)"
    )

    for line in (output or "").splitlines():
        m = javac_pattern.search(line)
        if m and "error:" in line.lower():
            fp = m.group(1).replace("\\", "/")
            if fp.startswith("/repo/"):
                fp = fp[len("/repo/"):]
            lineno = int(m.group(2) or m.group(3) or 0)
            msg = m.group(4).strip()
            key = (fp, lineno)
            if key not in seen:
                seen.add(key)
                errors.append(CompilerErrorDetail(file_path=fp, line=lineno, message=msg))
            continue

        m2 = checkstyle_pattern.match(line.strip())
        if m2:
            fp = m2.group(1).replace("\\", "/")
            if fp.startswith("/repo/"):
                fp = fp[len("/repo/"):]
            lineno = int(m2.group(2))
            msg = m2.group(3).strip()
            key = (fp, lineno)
            if key not in seen:
                seen.add(key)
                errors.append(CompilerErrorDetail(file_path=fp, line=lineno, message=msg))

    return errors


def classify_build_failure(output: str) -> str:
    """
    Classify build failure into a coarse category for retry routing.

    Returns one of:
      "context_mismatch"   — malformed hunk, syntax error, patch artifact
      "api_mismatch"       — cannot find symbol, method cannot be applied
      "infrastructure"     — connection refused, daemon died, OOM
      "unknown"
    """
    text_lower = (output or "").lower()

    if (
        "not a statement" in text_lower
        or "class, interface, enum, or record expected" in text_lower
        or "malformed patch" in text_lower
        or "patch does not apply" in text_lower
    ):
        return "context_mismatch"

    if "cannot find symbol" in text_lower or (
        "method" in text_lower and "cannot be applied" in text_lower
    ):
        return "api_mismatch"

    if (
        "connection refused" in text_lower
        or "daemon disappeared" in text_lower
        or "out of memory" in text_lower
        or "java.lang.outofmemoryerror" in text_lower
    ):
        return "infrastructure"

    return "unknown"


def collect_test_results(
    repo_path: str,
    project: str,
    target_classes: list[str],
    console_output: str = "",
) -> dict[str, Any]:
    """
    Run helpers/collect_test_results.py to parse JUnit XML reports.
    Falls back to inline XML parsing if the helper script is unavailable.
    """
    helper_script = os.path.join(_HELPERS_ROOT, "collect_test_results.py")
    if os.path.exists(helper_script):
        console_file = ""
        if console_output:
            try:
                tf = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".log", delete=False, encoding="utf-8"
                )
                tf.write(console_output)
                tf.close()
                console_file = tf.name
            except Exception:
                pass

        abs_repo_path = os.path.abspath(repo_path)
        cmd = [
            "python3",
            helper_script,
            "--project", project,
            "--repo", abs_repo_path,
            "--target-classes", ",".join(sorted(set(target_classes))),
        ]
        if console_file:
            cmd += ["--console-file", console_file]

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            stdout_only = (proc.stdout or "").strip()
            if proc.returncode == 0 and stdout_only:
                return json.loads(stdout_only)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
            pass
        finally:
            if console_file:
                try:
                    os.unlink(console_file)
                except Exception:
                    pass

    # Inline fallback: scan common JUnit XML locations
    patterns = [
        os.path.join(repo_path, "**", "surefire-reports", "TEST-*.xml"),
        os.path.join(repo_path, "**", "build", "test-results", "**", "TEST-*.xml"),
        os.path.join(repo_path, "**", "target", "test-results", "**", "TEST-*.xml"),
        os.path.join(repo_path, "build", "all-test-results", "TEST-*.xml"),
        os.path.join(repo_path, "**/target/surefire-reports/*.xml"),
        os.path.join(repo_path, "**/JTwork/**/*.xml"),
    ]
    xml_paths: set[str] = set()
    for pat in patterns:
        xml_paths.update(glob.glob(pat, recursive=True))

    tc_set = set(target_classes)
    test_cases: dict[str, str] = {}
    class_acc: dict[str, list[str]] = {}

    for xp in sorted(xml_paths):
        try:
            root = ET.parse(xp).getroot()
        except Exception:
            continue
        for case in root.findall(".//testcase"):
            cls = (case.attrib.get("classname") or "").strip()
            name = (case.attrib.get("name") or "").strip()
            if not cls or not name:
                continue
            if tc_set:
                matched = False
                for target in tc_set:
                    if cls == target or cls.endswith("." + target):
                        matched = True
                        break
                if not matched:
                    continue
            if case.find("failure") is not None:
                status = "failed"
            elif case.find("error") is not None:
                status = "error"
            elif case.find("skipped") is not None:
                status = "skipped"
            else:
                status = "passed"
            test_cases[f"{cls}#{name}"] = status
            class_acc.setdefault(cls, []).append(status)

    classes: dict[str, str] = {}
    for cls, statuses in class_acc.items():
        if any(s in {"failed", "error"} for s in statuses):
            classes[cls] = "failed"
        elif all(s == "skipped" for s in statuses):
            classes[cls] = "skipped"
        else:
            classes[cls] = "passed"

    total = len(test_cases) or len(classes)
    src = test_cases if test_cases else classes
    return {
        "xml_reports": sorted(xml_paths),
        "target_classes": sorted(tc_set),
        "test_cases": test_cases,
        "classes": classes,
        "summary": {
            "passed": sum(1 for s in src.values() if s == "passed"),
            "failed": sum(1 for s in src.values() if s in {"failed", "error"}),
            "skipped": sum(1 for s in src.values() if s == "skipped"),
            "total": total,
        },
    }


def detect_test_targets(
    repo_path: str,
    project: str,
    changed_files: list[str] | None = None,
) -> TestTargetInfo:
    """
    Detect which tests to run based on changed files.

    Priority:
      1) If changed_files is provided (e.g. from target.patch), use path-based detection
         directly — ensures phase 0 and validation use the SAME targets regardless of
         worktree state.
      2) helpers/{project}/get_test_targets.py --worktree (git diff based)
      3) Infer from changed_files list (path-based heuristic, fallback)
    """
    normalized = project.strip().lower()
    print(f"  [build_systems] Detecting test targets for project {normalized}")

    # When caller explicitly provides changed_files (from target.patch), derive targets
    # directly from those paths.  This guarantees consistent targets between phase 0
    # (only test hunks applied) and validation (all hunks applied).
    if changed_files:
        print(f"  [build_systems] Using {len(changed_files)} explicit changed_files for target detection")
        test_targets: set[str] = set()
        source_modules: set[str] = set()
        all_modules: set[str] = set()
        ignored = {"web-console", "distribution", "docs", "examples", "benchmarks", "qa"}

        source_package_wildcards: set[str] = set()

        for rel_path in changed_files:
            p = (rel_path or "").replace("\\", "/")
            if not p:
                continue
            module = _find_module_for_path(repo_path, p)
            top_level = module.split("/")[0] if module else ""
            if top_level in ignored:
                continue
            if module:
                all_modules.add(module)
            if p.endswith(".java") and "/src/main/java/" in p and module:
                source_modules.add(module)
                # Derive a package wildcard from the source file path so that
                # tests exercising this production code are also run, even if
                # they aren't listed in target.patch.
                src_marker = "/src/main/java/"
                if src_marker in p:
                    pkg_path = p.split(src_marker, 1)[1]  # e.g. org/foo/bar/Baz.java
                    pkg = ".".join(pkg_path.replace(".java", "").split("/")[:-1])  # org.foo.bar
                    if pkg:
                        source_package_wildcards.add(f"{module}:{pkg}.*")
            if not _is_test_file(p):
                continue
            matched = next((d for d in _TEST_SOURCE_DIRS if d in p), None)
            if matched:
                class_part = p.split(matched, 1)[1].replace("/", ".").replace(".java", "")
                target = f"{module}:{class_part}" if module else class_part
                test_targets.add(target)

        # Add package-level wildcards for changed production files so that any
        # test in the same package (or a mirrored test package) is also run.
        # These complement the explicit test-file targets from target.patch.
        for wildcard in source_package_wildcards:
            test_targets.add(wildcard)

        targets_list = sorted(test_targets)
        if normalized == "elasticsearch" and targets_list:
            targets_list, _ = _filter_elasticsearch_test_targets(targets_list)

        explicit_count = len([t for t in targets_list if not t.endswith(".*")])
        wildcard_count = len([t for t in targets_list if t.endswith(".*")])
        print(f"  [build_systems] Explicit files detection found {explicit_count} targets + {wildcard_count} package wildcard(s)")
        return TestTargetInfo(
            test_targets=targets_list,
            source_modules=sorted(source_modules),
            all_modules=sorted(all_modules),
            raw={"source": "target_patch_files", "changed_files": sorted(set(changed_files))},
        )

    helper_script = os.path.join(_helper_dir(normalized), "get_test_targets.py")

    if os.path.exists(helper_script):
        print(f"  [build_systems] Using helper script: {helper_script}")
        res = _run_cmd(
            ["python3", helper_script, "--repo", os.path.abspath(repo_path), "--worktree"],
            cwd=repo_path,
            timeout=60,
        )
        if res["success"]:
            try:
                parsed = json.loads(res["output"])
                targets = sorted(set(
                    (parsed.get("modified") or []) + (parsed.get("added") or [])
                ))
                source_modules = sorted(set(parsed.get("source_modules") or []))
                all_modules = sorted(set(parsed.get("all_modules") or []))

                if normalized == "elasticsearch" and targets:
                    targets, skipped = _filter_elasticsearch_test_targets(targets)
                    if skipped:
                        parsed.setdefault("harness_excluded_test_targets", [])
                        parsed["harness_excluded_test_targets"] = sorted(
                            set(parsed["harness_excluded_test_targets"]) | set(skipped)
                        )

                print(f"  [build_systems] Helper found {len(targets)} targets")
                return TestTargetInfo(
                    test_targets=targets,
                    source_modules=source_modules,
                    all_modules=all_modules,
                    raw=parsed,
                )
            except json.JSONDecodeError:
                print("  [build_systems] Helper returned invalid JSON")
                pass

    # Path-based fallback
    print("  [build_systems] Using path-based fallback for test target detection")
    test_targets: set[str] = set()
    source_modules: set[str] = set()
    all_modules: set[str] = set()
    ignored = {"web-console", "distribution", "docs", "examples", "benchmarks", "qa"}

    for rel_path in changed_files or []:
        p = (rel_path or "").replace("\\", "/")
        if not p:
            continue
        module = _find_module_for_path(repo_path, p)
        top_level = module.split("/")[0] if module else ""
        if top_level in ignored:
            continue
        if module:
            all_modules.add(module)
        if p.endswith(".java") and "/src/main/java/" in p and module:
            source_modules.add(module)

        if not _is_test_file(p):
            continue

        matched = next((d for d in _TEST_SOURCE_DIRS if d in p), None)
        if matched:
            class_part = p.split(matched, 1)[1].replace("/", ".").replace(".java", "")
            target = f"{module}:{class_part}" if module else class_part
            test_targets.add(target)

    targets_list = sorted(test_targets)
    if normalized == "elasticsearch" and targets_list:
        targets_list, _ = _filter_elasticsearch_test_targets(targets_list)

    print(f"  [build_systems] Path-based detection found {len(targets_list)} targets")
    return TestTargetInfo(
        test_targets=targets_list,
        source_modules=sorted(source_modules),
        all_modules=sorted(all_modules),
        raw={"source": "changed_files", "changed_files": sorted(set(changed_files or []))},
    )


def run_build(repo_path: str, project: str = "", changed_files: list[str] | None = None) -> BuildResult:
    """
    Compile the project.

    Order:
      1. helpers/{project}/run_build.sh (Docker) — full compile + testClasses
      2. Gradle testClasses
      3. Maven compile test-compile
    """
    normalized = project.strip().lower() or _detect_project_name(repo_path)
    print(f"  [build_systems] Starting build for project {normalized} in {repo_path}")

    if _has_helper(normalized):
        hdir = _helper_dir(normalized)
        build_sh = os.path.join(hdir, "run_build.sh")
        if os.path.exists(build_sh):
            print(f"  [build_systems] Using project helper: {build_sh}")
            image_tag, err = _ensure_docker_image(normalized, repo_path)
            if image_tag:
                abs_repo_path = os.path.realpath(repo_path)
                env = os.environ.copy()
                env.update({
                    "PROJECT_NAME": normalized,
                    "PROJECT_DIR": abs_repo_path,
                    "TOOLKIT_DIR": hdir,
                    "BUILDER_IMAGE_TAG": image_tag,
                    "IMAGE_TAG": image_tag,
                    "COMMIT_SHA": _get_current_head(repo_path),
                    "WORKTREE_MODE": "1",
                })
                res = _run_cmd(["bash", build_sh], cwd=abs_repo_path, env=env, timeout=3600)
                print(f"  [build_systems] Build {'succeeded' if res['success'] else 'failed'} using {normalized}-helper")
                return BuildResult(
                    success=res["success"],
                    output=res["output"],
                    mode=f"{normalized}-helper",
                )
            else:
                print(f"  [build_systems] Docker image unavailable for {normalized}: {err}. Falling back.")

    env = os.environ.copy()
    java_home = _resolve_valid_java_home()
    if java_home:
        env["JAVA_HOME"] = java_home

    if _is_gradle_repo(repo_path):
        print("  [build_systems] Using Gradle build")
        cmd = ["./gradlew", "classes", "testClasses", "--no-daemon", "-x", "test"]
        mode = "gradle"
    else:
        print("  [build_systems] Using Maven build")
        cmd = [
            "mvn", "compile", "test-compile",
            "-Dmaven.javadoc.skip=true",
            "-Dcheckstyle.skip=true",
            "-Denforcer.skip=true",
            "-Dforbiddenapis.skip=true",
        ]
        mode = "maven"

    res = _run_cmd(cmd, cwd=repo_path, env=env, timeout=600)
    print(f"  [build_systems] Build {'succeeded' if res['success'] else 'failed'} using {mode}")
    return BuildResult(success=res["success"], output=res["output"], mode=mode)


def run_tests(
    repo_path: str,
    project: str = "",
    target_info: TestTargetInfo | None = None,
    changed_files: list[str] | None = None,
) -> TestResult:
    """
    Run targeted tests.

    Order:
      1. helpers/{project}/run_tests.sh (Docker) — if known project with Docker image
      2. Gradle --tests ClassName
      3. Maven -Dtest=ClassName
    """
    normalized = project.strip().lower() or _detect_project_name(repo_path)
    print(f"  [build_systems] Starting tests for project {normalized} in {repo_path}")

    if target_info is None:
        target_info = detect_test_targets(repo_path, normalized, changed_files)

    test_targets = list(target_info.test_targets)
    source_modules = list(target_info.source_modules)

    if not test_targets and not source_modules:
        print("  [build_systems] No relevant test targets or modules detected. Skipping tests.")
        return TestResult(
            success=True,
            compile_error=False,
            output="No relevant test targets/modules detected.",
            mode="skip",
            targets=target_info.__dict__,
            test_state={
                "xml_reports": [],
                "target_classes": [],
                "test_cases": {},
                "classes": {},
                "summary": {"passed": 0, "failed": 0, "skipped": 0, "total": 0},
            },
        )

    print(f"  [build_systems] Found {len(test_targets)} test targets and {len(source_modules)} source modules")
    _clear_junit_reports(repo_path)

    # Docker helper path
    if _has_helper(normalized):
        hdir = _helper_dir(normalized)
        test_sh = os.path.join(hdir, "run_tests.sh")
        if os.path.exists(test_sh):
            print(f"  [build_systems] Using project helper: {test_sh}")
            image_tag, err = _ensure_docker_image(normalized, repo_path)
            if image_tag:
                abs_repo_path = os.path.realpath(repo_path)
                target_classes_only = [
                    t.split(":", 1)[1] for t in test_targets if ":" in t
                ]
                env = os.environ.copy()
                env.update({
                    "PROJECT_NAME": normalized,
                    "PROJECT_DIR": abs_repo_path,
                    "BUILDER_IMAGE_TAG": image_tag,
                    "IMAGE_TAG": image_tag,
                    "COMMIT_SHA": _get_current_head(repo_path),
                    "WORKTREE_MODE": "1",
                    "TEST_TARGETS": " ".join(sorted(set(test_targets))) if test_targets else "NONE",
                    "TEST_TARGET_FILES": ",".join(
                        str(p) for p in (target_info.raw.get("changed_files") or []) if str(p).strip()
                    ),
                    "TEST_MODULES": "" if test_targets else ",".join(sorted(set(source_modules))),
                })
                res = _run_cmd(["bash", test_sh], cwd=abs_repo_path, env=env, timeout=900)
                output = res["output"]
                is_compile_error = (not res["success"]) and "compilation error" in output.lower()

                # Collect results
                target_classes_for_collection = [
                    t.split(":", 1)[1] if ":" in t else t for t in test_targets
                ]
                print(f"  [build_systems] Collecting results for {len(target_classes_for_collection)} classes")
                test_state = collect_test_results(
                    repo_path, normalized, target_classes_for_collection, output
                )
                print(f"  [build_systems] Tests {'succeeded' if res['success'] else 'failed'} using {normalized}-helper")
                return TestResult(
                    success=res["success"],
                    compile_error=is_compile_error,
                    output=output,
                    mode=f"{normalized}-helper",
                    targets=target_info.__dict__,
                    test_state=test_state,
                )
            else:
                print(f"  [build_systems] Docker image unavailable for {normalized}: {err}. Falling back.")

    # Direct Maven/Gradle fallback
    env = os.environ.copy()
    java_home = _resolve_valid_java_home()
    if java_home:
        env["JAVA_HOME"] = java_home

    compat_args = ["-Djdk.security.manager.allow.argLine="]

    if _is_gradle_repo(repo_path):
        print("  [build_systems] Using Gradle test runner")
        cmd = ["./gradlew", "test", "--no-daemon"]
        for t in test_targets:
            cls = t.split(":", 1)[1] if ":" in t else t
            cmd += ["--tests", cls]
        mode = "gradle-targeted" if test_targets else "gradle-module"
    else:
        print("  [build_systems] Using Maven test runner")
        module_set: set[str] = set(source_modules)
        test_classes: list[str] = []
        for t in test_targets:
            if ":" in t:
                mod, cls = t.split(":", 1)
                module_set.add(mod)
                test_classes.append(cls)
            else:
                test_classes.append(t)

        pl_arg = ",".join(sorted(module_set)) if module_set else "."
        base = [
            "mvn", "test",
            "-pl", pl_arg, "-am",
            "-DfailIfNoTests=false",
            "-Dsurefire.failIfNoSpecifiedTests=false",
            "-Dmaven.javadoc.skip=true",
            "-Dcheckstyle.skip=true",
            "-Dpmd.skip=true",
            "-Dforbiddenapis.skip=true",
            "-Denforcer.skip=true",
        ] + compat_args

        if test_classes:
            cmd = base + [f"-Dtest={','.join(sorted(set(test_classes)))}"]
            mode = "maven-targeted"
        else:
            cmd = base
            mode = "maven-module"

    res = _run_cmd(cmd, cwd=repo_path, env=env, timeout=900)
    output = res["output"]
    is_compile_error = (not res["success"]) and "compilation error" in output.lower()

    target_classes_for_collection = [
        t.split(":", 1)[1] if ":" in t else t for t in test_targets
    ]
    print(f"  [build_systems] Collecting results for {len(target_classes_for_collection)} classes")
    test_state = collect_test_results(
        repo_path, normalized, target_classes_for_collection, output
    )

    print(f"  [build_systems] Tests {'succeeded' if res['success'] else 'failed'} using {mode}")
    return TestResult(
        success=res["success"],
        compile_error=is_compile_error,
        output=output,
        mode=mode,
        targets=target_info.__dict__,
        test_state=test_state,
    )


def evaluate_test_transition(
    baseline: dict[str, Any] | None,
    patched: dict[str, Any],
    rename_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Determine whether patched run represents a valid backport signal vs baseline.

    Valid iff:
      - no pass → fail regressions
      - at least one fail → pass transition OR a newly observed passing test
    """
    baseline_state = (baseline or {}).get("test_state") or {}
    patched_state = (patched or {}).get("test_state") or {}

    baseline_cases = baseline_state.get("test_cases") or {}
    patched_cases = patched_state.get("test_cases") or {}

    # Class-level fallback when no case-level data
    if not baseline_cases and not patched_cases:
        baseline_cases = baseline_state.get("classes") or {}
        patched_cases = patched_state.get("classes") or {}

    baseline_targets = set(baseline_state.get("target_classes") or [])
    patched_targets = set(patched_state.get("target_classes") or [])
    expected_targets = baseline_targets | patched_targets

    if expected_targets and not baseline_cases and not patched_cases:
        return {
            "valid_backport_signal": False,
            "fail_to_pass": [],
            "pass_to_fail": [],
            "newly_passing": [],
            "reason": "Inconclusive: Relevant target tests were not observed in baseline or patched runs.",
        }

    def _translate(key: str, old_cls: str, new_cls: str) -> str:
        if key == old_cls or key.startswith(old_cls + "#"):
            return new_cls + key[len(old_cls):]
        return key

    def _lookup_patched(bkey: str) -> str | None:
        if bkey in patched_cases:
            return patched_cases[bkey]
        if rename_map:
            for old_cls, new_cls in rename_map.items():
                translated = _translate(bkey, old_cls, new_cls)
                if translated != bkey and translated in patched_cases:
                    return patched_cases[translated]
        return None

    def _baseline_key(pkey: str) -> str:
        if rename_map:
            for old_cls, new_cls in rename_map.items():
                rev = _translate(pkey, new_cls, old_cls)
                if rev != pkey:
                    return rev
        return pkey

    fail_to_pass = sorted(
        k for k, s in baseline_cases.items()
        if s in {"failed", "error"} and _lookup_patched(k) == "passed"
    )
    pass_to_fail = sorted(
        k for k, s in baseline_cases.items()
        if s == "passed" and _lookup_patched(k) in {"failed", "error"}
    )
    newly_passing = sorted(
        k for k, s in patched_cases.items()
        if s == "passed"
        and _baseline_key(k) not in baseline_cases
        and k not in baseline_cases
    )

    valid = (not pass_to_fail) and bool(fail_to_pass or newly_passing)
    if pass_to_fail:
        reason = "Invalid: Some tests regressed from pass to fail."
    elif fail_to_pass or newly_passing:
        reason = "Valid: Observed fail-to-pass and/or newly passing tests with no regressions."
    else:
        reason = "Invalid: No fail-to-pass or newly passing tests observed."

    return {
        "valid_backport_signal": valid,
        "fail_to_pass": fail_to_pass,
        "pass_to_fail": pass_to_fail,
        "newly_passing": newly_passing,
        "baseline_total": len(baseline_cases),
        "patched_total": len(patched_cases),
        "reason": reason,
    }
