from __future__ import annotations
from typing import List, Dict, Optional, Any
from utils.mcp_client import get_client
from utils.validation_models import (
    HunkValidationResult,
    HunkValidationError,
    HunkValidationErrorType,
    PatchValidationResult,
    PatchRetryContext,
)
from utils.file_operations_models import (
    StructuredPatchHunk,
    TextFilePayload,
    EditFileOutput,
)
import tempfile
import os
import subprocess
import json
import re
import glob
import shutil
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
from utils.file_operations import edit_file, extract_hunk_context_from_diff

# Load environment variables from .env file
load_dotenv()

# Elasticsearch modules where targeted `--tests Class` against standard Gradle
# verification tasks is not supported (BWC matrix-only QA, etc.). The harness
# still runs other co-detected targets (e.g. plugin internalClusterTest).
ELASTICSEARCH_HARNESS_EXCLUDED_TEST_MODULES: frozenset[str] = frozenset(
    {
        "x-pack/qa/rolling-upgrade",
    }
)


def _filter_elasticsearch_harness_test_targets(
    test_targets: List[str],
) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    skipped: list[str] = []
    for t in test_targets or []:
        s = str(t or "").strip()
        if not s or ":" not in s:
            if s:
                kept.append(s)
            continue
        mod, _ = s.split(":", 1)
        if mod in ELASTICSEARCH_HARNESS_EXCLUDED_TEST_MODULES:
            skipped.append(s)
        else:
            kept.append(s)
    return kept, skipped


def _resolve_valid_java_home() -> str | None:
    """
    Returns a usable JAVA_HOME path if configured, otherwise None.

    Preference order:
      1) JAVA_21_HOME (if valid)
      2) JAVA_HOME (if valid)
    """
    candidates = [os.getenv("JAVA_21_HOME"), os.getenv("JAVA_HOME")]
    for candidate in candidates:
        if not candidate:
            continue
        java_bin = os.path.join(candidate, "bin", "java")
        if os.path.isdir(candidate) and os.path.isfile(java_bin):
            return candidate
    return None


def _clean_spotbugs_output(output: str) -> str:
    """
    Clean SpotBugs output by removing verbose noise while keeping important findings.
    - Keeps bug findings (lines with bug codes like H B, M V, L C, etc.)
    - Removes 'missing classes' list (usually noise for validation)
    - Truncates very long outputs
    """
    if not output:
        return output

    lines = output.split("\n")
    cleaned_lines = []
    in_missing_section = False

    for line in lines:
        # Detect start of "missing classes" section
        if "The following classes needed for analysis were missing:" in line:
            in_missing_section = True
            continue

        # Skip lines in missing classes section
        if in_missing_section:
            # End of missing section when we hit an empty line or a bug finding
            if not line.strip() or re.match(r"^[HML]\s+[A-Z]", line):
                in_missing_section = False
                if not line.strip():
                    continue
            else:
                continue

        # Keep bug findings and summary lines
        if re.match(r"^[HML]\s+[A-Z]", line) or not line.strip():
            cleaned_lines.append(line)

    result = "\n".join(cleaned_lines)

    # Truncate if still too long (keep first and last findings)
    if len(result) > 5000:
        parts = result.split("\n\n")
        if len(parts) > 10:
            result = (
                "\n\n".join(parts[:5])
                + "\n\n...[TRUNCATED]...\n\n"
                + "\n\n".join(parts[-5:])
            )

    return result.strip() if result.strip() else "SpotBugs completed with no findings."


def classify_test_failure_signal(
    *,
    output_text: str,
    transition_reason: str = "",
    success: bool | None = None,
    compile_error: bool = False,
) -> Dict[str, Any]:
    """Classify test-stage failures into code vs infrastructure categories."""
    out = str(output_text or "")
    out_lower = out.lower()
    reason = str(transition_reason or "")
    reason_lower = reason.lower()

    if (
        "retrofittest_runner_config_unresolved" in out_lower
        or "cannot locate tasks that match" in out_lower
        or "task 'test' is ambiguous" in out_lower
    ):
        return {
            "category": "test_runner_config",
            "infrastructure_failure": True,
            "infrastructure_inconclusive": True,
            "reason": "Gradle test task resolution failed (ambiguous/missing task).",
        }

    if "inconclusive: relevant target tests were not observed" in reason_lower:
        return {
            "category": "inconclusive_tests_observed_none",
            "infrastructure_failure": True,
            "infrastructure_inconclusive": True,
            "reason": "Relevant target tests were not observed in baseline/patched runs.",
        }

    if compile_error:
        return {
            "category": "context_mismatch",
            "infrastructure_failure": False,
            "infrastructure_inconclusive": False,
            "reason": "Test execution failed due to compilation errors.",
        }

    if success is False and (
        "connection refused" in out_lower
        or "timed out" in out_lower
        or "daemon disappeared" in out_lower
        or "could not resolve" in out_lower
        and "dependency" in out_lower
    ):
        return {
            "category": "test_infrastructure",
            "infrastructure_failure": True,
            "infrastructure_inconclusive": True,
            "reason": "Test execution failed due to infrastructure/runtime issues.",
        }

    return {
        "category": "unknown",
        "infrastructure_failure": False,
        "infrastructure_inconclusive": False,
        "reason": "No infrastructure-specific signal detected.",
    }


class ValidationToolkit:
    def test_and_restore(self, test_classes: List[str]) -> Dict:
        """
        Runs targeted tests and restores repo state if tests fail or compile error.
        Returns test result dict.
        """
        result = self.run_targeted_tests(test_classes)
        if not result.get("success") or result.get("compile_error"):
            self.restore_repo_state()
            return result

    def __init__(self, target_repo_path: str):
        self.target_repo_path = target_repo_path
        self.client = get_client()

    def _is_known_project_with_helper(self, project: str) -> bool:
        project_dir = self._get_project_helper_dir(project)
        return os.path.isdir(project_dir)

    def _find_module_for_path(self, rel_path: str) -> str:
        """Find nearest parent directory containing a build file for a repo-relative path."""
        head = (rel_path or "").replace("\\", "/")
        while head:
            head, _ = os.path.split(head)
            for build_file in ("pom.xml", "build.gradle", "build.gradle.kts"):
                if os.path.exists(
                    os.path.join(self.target_repo_path, head, build_file)
                ):
                    return head
        # Check root
        for build_file in ("pom.xml", "build.gradle", "build.gradle.kts"):
            if os.path.exists(os.path.join(self.target_repo_path, build_file)):
                return ""
        return ""

    def _clear_previous_junit_reports(self) -> None:
        """Remove stale JUnit XML files so each run reflects only current execution."""
        try:
            for xml_path in glob.glob(
                os.path.join(
                    self.target_repo_path, "**", "surefire-reports", "TEST-*.xml"
                ),
                recursive=True,
            ):
                try:
                    os.remove(xml_path)
                except Exception:
                    pass

            aggregate_dir = os.path.join(
                self.target_repo_path, "build", "all-test-results"
            )
            if os.path.isdir(aggregate_dir):
                shutil.rmtree(aggregate_dir, ignore_errors=True)
        except Exception:
            # Best effort cleanup only.
            pass

    def _collect_junit_xml_paths(self) -> list[str]:
        """Collect JUnit XML report file paths from common Maven/Gradle output locations."""
        paths = set(
            glob.glob(
                os.path.join(
                    self.target_repo_path, "**", "surefire-reports", "TEST-*.xml"
                ),
                recursive=True,
            )
        )
        paths.update(
            glob.glob(
                os.path.join(
                    self.target_repo_path, "build", "all-test-results", "TEST-*.xml"
                ),
                recursive=True,
            )
        )
        # Gradle test results (Elasticsearch, Spring Framework, etc.)
        paths.update(
            glob.glob(
                os.path.join(
                    self.target_repo_path,
                    "**",
                    "build",
                    "test-results",
                    "**",
                    "TEST-*.xml",
                ),
                recursive=True,
            )
        )
        return sorted(paths)

    def _detect_project_name(self) -> str:
        return os.path.basename(self.target_repo_path).strip().lower()

    def _extract_test_state_with_project_helper(
        self,
        target_info: Optional[Dict[str, Any]] = None,
        console_output: str = "",
    ) -> Dict[str, Any] | None:
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        helper_script = os.path.join(
            root_dir, "evaluate", "helpers", "collect_test_results.py"
        )
        if not os.path.exists(helper_script):
            return None

        info = target_info or {}
        target_classes = [
            t.split(":", 1)[1] for t in (info.get("test_targets") or []) if ":" in t
        ]
        target_classes_arg = ",".join(sorted(set(target_classes)))

        console_file = os.path.join(self.target_repo_path, "build", "test-console.log")
        try:
            os.makedirs(os.path.dirname(console_file), exist_ok=True)
            with open(console_file, "w", encoding="utf-8") as f:
                f.write(console_output or "")
        except Exception:
            console_file = ""

        cmd = [
            "python3",
            helper_script,
            "--project",
            self._detect_project_name(),
            "--repo",
            self.target_repo_path,
            "--target-classes",
            target_classes_arg,
        ]
        if console_file:
            cmd.extend(["--console-file", console_file])

        result = self._run_cmd_capture(cmd, cwd=self.target_repo_path)
        if not result.get("success"):
            return None

        try:
            return json.loads(result.get("output", "{}"))
        except json.JSONDecodeError:
            return None

    def _extract_test_state(
        self, target_info: Optional[Dict[str, Any]] = None, console_output: str = ""
    ) -> Dict[str, Any]:
        """
        Parse JUnit XML files and return test-case and class-level statuses.

        Status values: passed | failed | error | skipped
        """
        helper_state = self._extract_test_state_with_project_helper(
            target_info=target_info, console_output=console_output
        )
        if helper_state is not None:
            return helper_state

        info = target_info or {}
        target_classes = {
            t.split(":", 1)[1] for t in (info.get("test_targets") or []) if ":" in t
        }

        xml_paths = self._collect_junit_xml_paths()
        test_case_status: dict[str, str] = {}
        class_status_acc: dict[str, list[str]] = {}

        for xml_path in xml_paths:
            try:
                tree = ET.parse(xml_path)
                root = tree.getroot()
            except Exception:
                continue

            for case in root.findall(".//testcase"):
                classname = (case.attrib.get("classname") or "").strip()
                testname = (case.attrib.get("name") or "").strip()
                if not classname or not testname:
                    continue

                if target_classes and classname not in target_classes:
                    continue

                if case.find("failure") is not None:
                    status = "failed"
                elif case.find("error") is not None:
                    status = "error"
                elif case.find("skipped") is not None:
                    status = "skipped"
                else:
                    status = "passed"

                test_key = f"{classname}#{testname}"
                test_case_status[test_key] = status
                class_status_acc.setdefault(classname, []).append(status)

        class_status: dict[str, str] = {}
        for classname, statuses in class_status_acc.items():
            if any(s in {"failed", "error"} for s in statuses):
                class_status[classname] = "failed"
            elif all(s == "skipped" for s in statuses):
                class_status[classname] = "skipped"
            else:
                class_status[classname] = "passed"

        return {
            "xml_reports": xml_paths,
            "target_classes": sorted(target_classes),
            "test_cases": test_case_status,
            "classes": class_status,
            "summary": {
                "passed": sum(1 for s in test_case_status.values() if s == "passed"),
                "failed": sum(
                    1 for s in test_case_status.values() if s in {"failed", "error"}
                ),
                "skipped": sum(1 for s in test_case_status.values() if s == "skipped"),
                "total": len(test_case_status),
            },
        }

    @staticmethod
    def build_test_rename_map_from_aux_hunks(
        developer_aux_hunks: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """
        Extract a rename map {old_class_fqn: new_class_fqn} from developer
        auxiliary hunks that represent RENAMED test files.

        This is used by Phase 0 (and the validation agent) to map the baseline
        test run (targeting the old class name that exists before the patch) to
        the post-patch test run (targeting the new class name).
        """
        test_source_sets = (
            "/src/test/java/",
            "/src/internalClusterTest/java/",
            "/src/javaRestTest/java/",
            "/src/yamlRestTest/java/",
            "/src/integTest/java/",
            "/src/integrationTest/java/",
        )

        def _path_to_fqn(path: str) -> Optional[str]:
            p = (path or "").replace("\\", "/")
            matched = next((td for td in test_source_sets if td in p), None)
            if not matched:
                return None
            try:
                return p.split(matched, 1)[1].replace("/", ".").replace(".java", "")
            except Exception:
                return None

        rename_map: Dict[str, str] = {}
        for h in developer_aux_hunks or []:
            if h.get("file_operation") != "RENAMED":
                continue
            old_path = h.get("old_target_file") or ""
            new_path = h.get("target_file") or ""
            if "test" not in new_path.lower() and "test" not in old_path.lower():
                continue
            old_fqn = _path_to_fqn(old_path)
            new_fqn = _path_to_fqn(new_path)
            if old_fqn and new_fqn and old_fqn != new_fqn:
                rename_map[old_fqn] = new_fqn
        return rename_map

    def evaluate_test_state_transition(
        self,
        baseline_test_result: Optional[Dict[str, Any]],
        patched_test_result: Optional[Dict[str, Any]],
        rename_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate whether patched tests represent a valid transition.

        Valid iff:
          - no pass -> fail transitions
          - and at least one fail -> pass transition OR a newly observed passing test

        Args:
            baseline_test_result: Test result from the baseline run (pre-patch).
            patched_test_result:  Test result from the post-patch run.
            rename_map:           Optional dict mapping old_class_fqn -> new_class_fqn.
                                  When provided, test cases from the old class (baseline)
                                  are matched against the corresponding new class (patched)
                                  for cross-class fail->pass and newly_passing detection.
                                  This is needed when a test file is renamed in the patch
                                  (e.g. SupervisorTest -> LagStatsTest).
        """
        baseline_state = (baseline_test_result or {}).get("test_state") or {}
        patched_state = (patched_test_result or {}).get("test_state") or {}

        baseline_cases = baseline_state.get("test_cases") or {}
        patched_cases = patched_state.get("test_cases") or {}
        baseline_target_classes = set(baseline_state.get("target_classes") or [])
        patched_target_classes = set(patched_state.get("target_classes") or [])
        expected_target_classes = baseline_target_classes | patched_target_classes

        # If no case-level data exists, use class-level status as fallback.
        if not baseline_cases and not patched_cases:
            baseline_cases = baseline_state.get("classes") or {}
            patched_cases = patched_state.get("classes") or {}

        # If relevant targets were requested but no target-level observations were
        # collected in either run, treat this as inconclusive (usually runner routing
        # or discovery issues), not as a semantic fail/pass signal.
        if expected_target_classes and not baseline_cases and not patched_cases:
            return {
                "valid_backport_signal": False,
                "fail_to_pass": [],
                "pass_to_fail": [],
                "newly_passing": [],
                "baseline_total": 0,
                "patched_total": 0,
                "reason": "Inconclusive: Relevant target tests were not observed in baseline or patched runs.",
            }

        # ------------------------------------------------------------------
        # For fail->pass: check baseline keys (may be old_cls) against patched
        # keys (may be new_cls) via rename_map translation.
        # ------------------------------------------------------------------
        def _translate_key(key: str, old_cls: str, new_cls: str) -> str:
            """Replace the class prefix in a test-case key."""
            if key == old_cls or key.startswith(old_cls + "#"):
                return new_cls + key[len(old_cls) :]
            # Also handle dot-separated method names (fallback)
            if key.startswith(old_cls + "."):
                return new_cls + key[len(old_cls) :]
            return key

        # For fail->pass: check baseline keys (may be old_cls) against patched keys (may be new_cls)
        def _lookup_patched_status(baseline_key: str) -> Optional[str]:
            if baseline_key in patched_cases:
                return patched_cases[baseline_key]
            if rename_map:
                for old_cls, new_cls in rename_map.items():
                    translated = _translate_key(baseline_key, old_cls, new_cls)
                    if translated != baseline_key and translated in patched_cases:
                        return patched_cases[translated]
            return None

        fail_to_pass = sorted(
            case
            for case, old_status in baseline_cases.items()
            if old_status in {"failed", "error"}
            and _lookup_patched_status(case) == "passed"
        )
        pass_to_fail = sorted(
            case
            for case, old_status in baseline_cases.items()
            if old_status == "passed"
            and _lookup_patched_status(case) in {"failed", "error"}
        )

        # For newly_passing: cases that appear in patched but not in baseline.
        # When rename_map is present, also translate patched new_cls keys to old_cls
        # and check whether they existed in baseline (they should be absent there).
        def _baseline_key_for_patched(patched_key: str) -> str:
            """Return the equivalent baseline key, accounting for renames."""
            if rename_map:
                for old_cls, new_cls in rename_map.items():
                    reverse = _translate_key(patched_key, new_cls, old_cls)
                    if reverse != patched_key:
                        return reverse
            return patched_key

        newly_passing = sorted(
            case
            for case, new_status in patched_cases.items()
            if new_status == "passed"
            and _baseline_key_for_patched(case) not in baseline_cases
            and case not in baseline_cases
        )

        valid = (not pass_to_fail) and bool(fail_to_pass or newly_passing)

        if pass_to_fail:
            reason = "Invalid: Some tests regressed from pass to fail."
        elif fail_to_pass or newly_passing:
            reason = "Valid: Observed fail-to-pass and/or newly passing relevant tests with no regressions."
        else:
            reason = "Invalid: No fail-to-pass or newly passing relevant tests were observed."

        return {
            "valid_backport_signal": valid,
            "fail_to_pass": fail_to_pass,
            "pass_to_fail": pass_to_fail,
            "newly_passing": newly_passing,
            "baseline_total": len(baseline_cases),
            "patched_total": len(patched_cases),
            "reason": reason,
        }

    def _get_project_helper_dir(self, project: str) -> str:
        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        return os.path.join(root_dir, "evaluate", "helpers", project.strip().lower())

    def _get_current_head(self) -> str:
        res = self._run_cmd_capture(
            ["git", "rev-parse", "--short", "HEAD"], cwd=self.target_repo_path
        )
        if res.get("success"):
            return (res.get("output") or "worktree").strip().splitlines()[0]
        return "worktree"

    def _ensure_project_builder_image(
        self, project: str
    ) -> tuple[str | None, str | None]:
        helper_dir = self._get_project_helper_dir(project)
        dockerfile = os.path.join(helper_dir, "Dockerfile")
        if not os.path.exists(dockerfile):
            return (
                None,
                f"Helper Dockerfile not found for project {project}: {dockerfile}",
            )

        image_tag = os.getenv(
            f"{project.upper()}_BUILDER_IMAGE_TAG",
            f"retrofit-{project.lower()}-builder:local",
        )

        inspect_res = self._run_cmd_capture(
            ["docker", "image", "inspect", image_tag], cwd=self.target_repo_path
        )
        if inspect_res.get("success"):
            return image_tag, None

        build_res = self._run_cmd_capture(
            ["docker", "build", "-t", image_tag, "-f", dockerfile, helper_dir],
            cwd=self.target_repo_path,
        )
        if not build_res.get("success"):
            return (
                None,
                f"Failed to build helper image for {project} ({image_tag}): {build_res.get('output', '')}",
            )

        return image_tag, None

    def _run_cmd_capture(
        self, cmd: List[str], cwd: Optional[str] = None, env: Optional[dict] = None
    ) -> Dict[str, Any]:
        """Run a command and return success plus combined output."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=cwd or self.target_repo_path,
                env=env,
            )
            output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            return {
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "output": output,
            }
        except Exception as e:
            return {"success": False, "returncode": 1, "output": str(e)}

    def detect_relevant_test_targets(self, project: str = "") -> Dict[str, Any]:
        """
        Detect relevant test targets/modules from current worktree diff.

        For Druid, delegates to evaluate helper script and returns:
          {
            "test_targets": ["module:pkg.ClassTest", ...],
            "source_modules": ["processing", ...],
            "all_modules": [...],
            "raw": <helper_json>
          }
        """
        normalized_project = (project or "").strip().lower()
        if not normalized_project:
            normalized_project = self._detect_project_name()

        if not self._is_known_project_with_helper(normalized_project):
            return {
                "test_targets": [],
                "source_modules": [],
                "all_modules": [],
                "raw": {},
            }

        helper_dir = self._get_project_helper_dir(normalized_project)
        helper_script = os.path.join(helper_dir, "get_test_targets.py")
        if not os.path.exists(helper_script):
            return {
                "test_targets": [],
                "source_modules": [],
                "all_modules": [],
                "raw": {"error": f"Helper script not found: {helper_script}"},
            }

        helper_res = self._run_cmd_capture(
            ["python3", helper_script, "--repo", self.target_repo_path, "--worktree"],
            cwd=self.target_repo_path,
        )
        if not helper_res.get("success"):
            return {
                "test_targets": [],
                "source_modules": [],
                "all_modules": [],
                "raw": {"error": helper_res.get("output", "helper failed")},
            }

        try:
            parsed = json.loads(helper_res.get("output", "{}"))
        except json.JSONDecodeError:
            parsed = {"error": f"Invalid helper JSON: {helper_res.get('output', '')}"}

        test_targets = sorted(
            set((parsed.get("modified") or []) + (parsed.get("added") or []))
        )
        source_modules = sorted(set(parsed.get("source_modules") or []))
        all_modules = sorted(set(parsed.get("all_modules") or []))

        if normalized_project == "elasticsearch" and test_targets:
            kept, skipped = _filter_elasticsearch_harness_test_targets(test_targets)
            test_targets = sorted(set(kept))
            if skipped:
                parsed = dict(parsed)
                prev = set(parsed.get("harness_excluded_test_targets") or [])
                parsed["harness_excluded_test_targets"] = sorted(prev | set(skipped))

        return {
            "test_targets": test_targets,
            "source_modules": source_modules,
            "all_modules": all_modules,
            "raw": parsed,
        }

    def detect_relevant_test_targets_from_changed_files(
        self, changed_files: List[str], *, project: str = ""
    ) -> Dict[str, Any]:
        """
        Infer relevant tests/modules directly from changed file paths.
        This avoids relying on worktree diff state during Phase 0 baseline runs.
        """
        ignored_modules = {
            "web-console",
            "distribution",
            "docs",
            "examples",
            "benchmarks",
            "qa",
        }

        test_targets = set()
        source_modules = set()
        all_modules = set()

        test_source_sets = (
            "/src/test/java/",
            "/src/internalClusterTest/java/",
            "/src/javaRestTest/java/",
            "/src/yamlRestTest/java/",
            "/src/integTest/java/",
            "/src/integrationTest/java/",
        )
        test_suffixes = ("Test.java", "Tests.java", "IT.java", "TestCase.java")

        for rel_path in changed_files or []:
            p = (rel_path or "").replace("\\", "/")
            if not p:
                continue

            module_path = self._find_module_for_path(p)
            top_level = module_path.split("/")[0] if module_path else ""
            if top_level in ignored_modules:
                continue

            if module_path:
                all_modules.add(module_path)

            if p.endswith(".java") and "src/main/java/" in p:
                if module_path:
                    source_modules.add(module_path)

            # Support both XXXTest.java (Crate/Druid) and TestXXX.java (HBase) patterns
            filename = os.path.basename(p)
            is_test_file = p.endswith(test_suffixes) or (
                filename.startswith("Test") and p.endswith(".java")
            )

            # Find the matching test directory
            matched_test_dir = next((td for td in test_source_sets if td in p), None)

            if is_test_file and matched_test_dir:
                try:
                    class_path = p.split(matched_test_dir, 1)[1]
                    class_name = class_path.replace("/", ".").replace(".java", "")
                    test_targets.add(f"{module_path}:{class_name}")
                except Exception:
                    continue

        raw: Dict[str, Any] = {
            "source": "changed_files",
            "changed_files": sorted(set(changed_files or [])),
        }
        out_targets = sorted(test_targets)
        np = (project or "").strip().lower()
        if np == "elasticsearch" and out_targets:
            kept, skipped = _filter_elasticsearch_harness_test_targets(out_targets)
            out_targets = sorted(set(kept))
            if skipped:
                raw["harness_excluded_test_targets"] = sorted(set(skipped))

        return {
            "test_targets": out_targets,
            "source_modules": sorted(source_modules),
            "all_modules": sorted(all_modules),
            "raw": raw,
        }

    def run_relevant_tests(
        self, project: str = "", target_info: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Run relevant tests based on detected targets/modules.

        Priority:
          1) If exact test classes are known, run those in the corresponding modules.
          2) Else run module-level tests for source-touched modules.
          3) Else skip.
        """
        info = target_info or self.detect_relevant_test_targets(project)
        test_targets = list(info.get("test_targets") or [])
        source_modules = list(info.get("source_modules") or [])

        normalized_project = (
            project or ""
        ).strip().lower() or self._detect_project_name()

        if normalized_project == "elasticsearch" and test_targets:
            kept, skipped = _filter_elasticsearch_harness_test_targets(test_targets)
            test_targets = kept
            if skipped:
                raw_info = info.get("raw")
                if isinstance(raw_info, dict):
                    prev = set(raw_info.get("harness_excluded_test_targets") or [])
                    raw_info["harness_excluded_test_targets"] = sorted(
                        prev | set(skipped)
                    )

        if not test_targets and not source_modules:
            return {
                "success": True,
                "compile_error": False,
                "output": "No relevant test targets/modules detected.",
                "failed_tests": [],
                "mode": "skip",
                "targets": info,
                "test_state": {
                    "xml_reports": [],
                    "target_classes": [],
                    "test_cases": {},
                    "classes": {},
                    "summary": {"passed": 0, "failed": 0, "skipped": 0, "total": 0},
                },
            }

        self._clear_previous_junit_reports()

        if self._is_known_project_with_helper(normalized_project):
            helper_script = os.path.join(
                self._get_project_helper_dir(normalized_project), "run_tests.sh"
            )
            if os.path.exists(helper_script):
                image_tag, image_err = self._ensure_project_builder_image(
                    normalized_project
                )
                if image_tag:
                    helper_env = os.environ.copy()
                    helper_env.update(
                        {
                            "PROJECT_NAME": normalized_project,
                            "PROJECT_DIR": self.target_repo_path,
                            "BUILDER_IMAGE_TAG": image_tag,
                            "IMAGE_TAG": image_tag,  # For backward compatibility with some scripts
                            "COMMIT_SHA": self._get_current_head(),
                            "WORKTREE_MODE": "1",
                            "TEST_TARGETS": " ".join(sorted(set(test_targets)))
                            if test_targets
                            else "NONE",
                            # Optional source file hints for helper scripts to pick
                            # the correct Gradle test task (e.g., internalClusterTest).
                            "TEST_TARGET_FILES": ",".join(
                                str(p)
                                for p in (
                                    (info.get("raw") or {}).get("changed_files") or []
                                )
                                if str(p).strip()
                            ),
                            # If explicit class targets are available, helpers should
                            # run those directly and not fall back to module mode.
                            "TEST_MODULES": ""
                            if test_targets
                            else (
                                ",".join(sorted(set(source_modules)))
                                if source_modules
                                else ""
                            ),
                        }
                    )
                    print(
                        f"    Agent 4: Executing {normalized_project} helper test script: {helper_script}"
                    )
                    result = self._run_cmd_capture(
                        ["bash", helper_script],
                        cwd=self.target_repo_path,
                        env=helper_env,
                    )
                    output_text = result.get("output", "")
                    return {
                        "success": bool(result.get("success")),
                        "compile_error": (not bool(result.get("success")))
                        and ("compilation error" in output_text.lower()),
                        "output": output_text,
                        "failed_tests": [],
                        "mode": f"{normalized_project}-helper-script",
                        "targets": info,
                        "test_state": self._extract_test_state(info, output_text),
                    }
                else:
                    print(
                        f"    Agent 4 Warning: {normalized_project} helper image unavailable for tests. Falling back. Details: {image_err}"
                    )

        is_gradle = os.path.exists(os.path.join(self.target_repo_path, "build.gradle"))
        java_compat_args = ["-Djdk.security.manager.allow.argLine="]

        if is_gradle:
            cmd = ["gradle", "test"]
            for t in test_targets:
                try:
                    _, cls = t.split(":", 1)
                    cmd.extend(["--tests", cls])
                except ValueError:
                    continue
            mode = "gradle-targeted" if test_targets else "gradle-module"
        else:
            module_set = set(source_modules)
            test_classes = []
            for t in test_targets:
                try:
                    module, cls = t.split(":", 1)
                    if module:
                        module_set.add(module)
                    if cls:
                        test_classes.append(cls)
                except ValueError:
                    continue

            module_list = sorted(module_set)
            if test_classes:
                cmd = [
                    "mvn",
                    "test",
                    "-pl",
                    ",".join(module_list),
                    "-am",
                    f"-Dtest={','.join(sorted(set(test_classes)))}",
                    "-DfailIfNoTests=false",
                    "-Dsurefire.failIfNoSpecifiedTests=false",
                    "-Dmaven.javadoc.skip=true",
                    "-Dcheckstyle.skip=true",
                    "-Dpmd.skip=true",
                    "-Dforbiddenapis.skip=true",
                    "-Denforcer.skip=true",
                ] + java_compat_args
                mode = "maven-targeted"
            else:
                cmd = [
                    "mvn",
                    "test",
                    "-pl",
                    ",".join(module_list),
                    "-am",
                    "-DfailIfNoTests=false",
                    "-Dsurefire.failIfNoSpecifiedTests=false",
                    "-Dmaven.javadoc.skip=true",
                    "-Dcheckstyle.skip=true",
                    "-Dpmd.skip=true",
                    "-Dforbiddenapis.skip=true",
                    "-Denforcer.skip=true",
                ] + java_compat_args
                mode = "maven-module"

        print(f"    Agent 4: Executing relevant test command: {' '.join(cmd)}")
        env = os.environ.copy()
        resolved_java_home = _resolve_valid_java_home()
        if resolved_java_home:
            env["JAVA_HOME"] = resolved_java_home

        cmd_res = self._run_cmd_capture(cmd, cwd=self.target_repo_path, env=env)
        output_text = cmd_res.get("output", "")
        return {
            "success": bool(cmd_res.get("success")),
            "compile_error": (not bool(cmd_res.get("success")))
            and ("compilation error" in output_text.lower()),
            "output": output_text,
            "failed_tests": [],
            "mode": mode,
            "targets": info,
            "test_state": self._extract_test_state(info, output_text),
        }

    # ------------------------------------------------------------------
    # Existing Methods
    # ------------------------------------------------------------------

    def compile_files(self, file_paths: List[str]) -> Dict:
        """
        Compiles the specified files using the Analysis Engine's compile tool.
        If it fails, falls back to module-level Maven/Gradle compilation.
        """
        res = self.client.call_tool(
            "compile",
            {"target_repo_path": self.target_repo_path, "file_paths": file_paths},
        )

        # Check if successful (handle both success and status=success)
        is_success = res.get("success") or res.get("status") == "success"
        if is_success:
            return {
                "success": True,
                "output": res.get("output", "Targeted compilation successful."),
            }

        # If targeted compile fails, try module-level compile
        print(
            f"    Agent 4: Targeted compile failed, falling back to module-level build..."
        )

        # Deduce module by finding closest build file for each file
        modules = set()
        is_gradle = os.path.exists(os.path.join(self.target_repo_path, "build.gradle"))

        for fp in file_paths:
            # Find the directory containing the closest build file
            current_dir = os.path.dirname(fp)
            build_file = "build.gradle" if is_gradle else "pom.xml"

            found_module = None
            while current_dir and current_dir != "":
                if os.path.exists(
                    os.path.join(self.target_repo_path, current_dir, build_file)
                ):
                    found_module = current_dir
                    break
                parent = os.path.dirname(current_dir)
                if parent == current_dir:  # Root
                    break
                current_dir = parent

            if found_module:
                modules.add(found_module)

        java_compat_args = ["-Djdk.security.manager.allow.argLine="]

        if not modules:
            # Fallback to root build
            return self.run_build_script()

        is_gradle = os.path.exists(os.path.join(self.target_repo_path, "build.gradle"))
        java_compat_args = ["-Djdk.security.manager.allow.argLine="]

        if is_gradle:
            # Gradle: pass all tasks together
            gradle_tasks = []
            for mod in modules:
                gradle_mod = ":" + mod.replace(os.sep, ":")
                gradle_tasks.append(f"{gradle_mod}:classes")
            cmd = ["gradle"] + gradle_tasks
        else:
            # Maven: pass all modules with -pl
            mod_list = ",".join(modules)
            # Use test-compile to ensure both main and test sources are compiled
            # Skip FMPP and other slow plugins that aren't needed for basic compilation check
            # Force Java 21 for SpotBugs compatibility (Java 25 not supported by SpotBugs)
            # Skip forbiddenapis, checkstyle, and pmd to avoid version-specific check failures
            cmd = [
                "mvn",
                "test-compile",
                "-pl",
                mod_list,
                "-am",
                "-DskipTests",
                "-Dfmpp.skip=true",
                "-Dmaven.compiler.release=21",
                "-Dmaven.compiler.source=21",
                "-Dmaven.compiler.target=21",
                "-Dforbiddenapis.skip=true",
                "-Dcheckstyle.skip=true",
                "-Dpmd.skip=true",
            ] + java_compat_args

        print(f"    Agent 4: Executing fallback build: {' '.join(cmd)}")
        try:
            compile_env = os.environ.copy()
            resolved_java_home = _resolve_valid_java_home()
            if resolved_java_home:
                compile_env["JAVA_HOME"] = resolved_java_home

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.target_repo_path,
                env=compile_env,
            )
            if result.returncode == 0:
                return {
                    "success": True,
                    "output": "Module-level compilation successful.",
                    "modules": list(modules),
                }
            else:
                return {"success": False, "output": result.stderr or result.stdout}
        except Exception as e:
            return {"success": False, "output": str(e)}

    def get_module_class_paths(self, modules: List[str]) -> List[str]:
        """
        Returns a list of class paths for the given modules.
        """
        class_paths = []
        for mod in modules:
            # Check Maven
            cp = os.path.join(self.target_repo_path, mod, "target", "classes")
            if os.path.exists(cp):
                class_paths.append(cp)
            # Check Gradle
            cp = os.path.join(
                self.target_repo_path, mod, "build", "classes", "java", "main"
            )
            if os.path.exists(cp):
                class_paths.append(cp)

        # Also check root just in case
        root_maven = os.path.join(self.target_repo_path, "target", "classes")
        if os.path.exists(root_maven) and root_maven not in class_paths:
            class_paths.append(root_maven)
        root_gradle = os.path.join(
            self.target_repo_path, "build", "classes", "java", "main"
        )
        if os.path.exists(root_gradle) and root_gradle not in class_paths:
            class_paths.append(root_gradle)

        return class_paths

    def get_project_classpath(self) -> List[str]:
        """
        Retrieves the project's classpath using Maven or Gradle.
        """
        is_gradle = os.path.exists(os.path.join(self.target_repo_path, "build.gradle"))

        if is_gradle:
            # For Gradle, use the 'dependencies' or similar task.
            # A more robust way is to use a custom gradle script snippet.
            # For now, let's try to get a basic classpath.
            cmd = [
                "gradle",
                "properties",
                "-q",
            ]  # This is a placeholder; getting classpath from gradle is harder
            # For brevity and since most targets are Maven, we'll return empty for Gradle for now
            # or try to find common jar locations.
            return []
        else:
            # Maven: use dependency:build-classpath
            tmp_cp = os.path.join(tempfile.gettempdir(), "retrofit_cp.txt")
            cmd = [
                "mvn",
                "dependency:build-classpath",
                f"-Dmdep.outputFile={tmp_cp}",
                "-DskipTests",
            ]
            try:
                print(f"    Agent 4: Calculating project classpath...")
                subprocess.run(
                    cmd,
                    cwd=self.target_repo_path,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                if os.path.exists(tmp_cp):
                    with open(tmp_cp, "r") as f:
                        cp_string = f.read().strip()
                    os.unlink(tmp_cp)
                    return cp_string.split(os.pathsep)
            except Exception as e:
                print(f"    Agent 4 Warning: Failed to get classpath: {e}")
        return []

    def run_spotbugs(
        self,
        compiled_classes_paths: List[str],
        source_path: Optional[str] = None,
        target_files: List[str] = None,
    ) -> Dict[str, Any]:
        """
        Runs SpotBugs using direct JAR invocation with Java 21.
        Requires JAVA_HOME to be set to Java 21.

        Args:
            compiled_classes_paths: Paths to compiled class directories
            source_path: Path to source code directory
            target_files: List of target files to analyze (for focused analysis)
        """
        # Determine classes to analyze
        class_names = []
        if target_files:
            for f in target_files:
                if "src/main/java/" in f:
                    cls = (
                        f.split("src/main/java/")[1]
                        .replace(".java", "")
                        .replace("/", ".")
                    )
                    class_names.append(cls)
                elif "src/test/java/" in f:
                    cls = (
                        f.split("src/test/java/")[1]
                        .replace(".java", "")
                        .replace("/", ".")
                    )
                    class_names.append(cls)

        # Direct JAR Invocation with SpotBugs 4.9.3
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        spotbugs_jar = os.path.join(base_dir, "tools", "spotbugs", "spotbugs-4.9.3.jar")
        resolved_java_home = _resolve_valid_java_home()

        if os.path.exists(spotbugs_jar):
            print(
                f"    Agent 4: Executing SpotBugs via direct JAR invocation ({spotbugs_jar})..."
            )
            aux_cp = self.get_project_classpath()

            # Build the command for SpotBugs 4.9.3+ using wildcard classpath for dependencies
            # Uses Java 21 from JAVA_21_HOME env variable
            spotbugs_dir = os.path.dirname(spotbugs_jar)
            java_bin = (
                os.path.join(resolved_java_home, "bin", "java")
                if resolved_java_home
                else "java"
            )
            cmd = [
                java_bin if os.path.exists(java_bin) else "java",
                "-cp",
                os.path.join(spotbugs_dir, "*"),
                "edu.umd.cs.findbugs.LaunchAppropriateUI",
                "-textui",
                "-effort:max",
                "-low",  # Report all confidence levels (low, medium, high)
                "-noClassOk",
            ]

            if class_names:
                cmd.extend(["-onlyAnalyze", ",".join(class_names)])

            # Include project's compiled classes in aux classpath to ensure visibility
            all_aux = list(aux_cp)
            for p in compiled_classes_paths:
                if p not in all_aux:
                    all_aux.append(p)

            if all_aux:
                cmd.extend(["-auxclasspath", os.pathsep.join(all_aux)])

            if source_path:
                cmd.extend(["-sourcepath", source_path])

            # Target classes for analysis (directories containing .class files)
            for p in compiled_classes_paths:
                cmd.append(p)

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=300
                )
                output = result.stdout + "\n" + result.stderr
                # SpotBugs returns 0 if no bugs found, >0 if bugs found
                passed = result.returncode == 0

                # Clean output - remove noise, keep findings
                cleaned_output = _clean_spotbugs_output(output)

                # Check for critical errors that indicate the tool didn't run properly
                # Ignore "Exception in thread" from missing classes (not a fatal error)
                has_fatal_error = (
                    "Exception in thread" in output
                    and "Could not instantiate" in output
                )
                has_fatal_error = has_fatal_error or (
                    "Exception" in output and "ClassNotFoundException" in output
                )
                has_fatal_error = has_fatal_error or (
                    "Error" in output and "BUILD FAILURE" in output
                )

                if has_fatal_error:
                    print(
                        f"    Agent 4 Warning: JAR invocation encountered errors, falling back..."
                    )
                else:
                    return {
                        "success": passed,
                        "output": cleaned_output,  # Return cleaned output
                        "raw_output": output,  # Keep raw for debugging
                        "method": "direct-jar",
                        "return_code": result.returncode,
                    }
            except subprocess.TimeoutExpired:
                print(
                    f"    Agent 4 Warning: Direct JAR SpotBugs timed out, falling back..."
                )
            except Exception as e:
                print(f"    Agent 4 Warning: Direct JAR SpotBugs failed: {e}")

        # 2. Maven Plugin Attempt (Fallback)
        is_maven = os.path.exists(os.path.join(self.target_repo_path, "pom.xml"))
        if is_maven:
            # Use SpotBugs Maven plugin as fallback (has known issues with -xml:withMessages)
            maven_env = os.environ.copy()
            resolved_java_home = _resolve_valid_java_home()
            if resolved_java_home:
                maven_env["JAVA_HOME"] = resolved_java_home

            cmd = [
                "mvn",
                "com.github.spotbugs:spotbugs-maven-plugin:4.9.0.0:spotbugs",
                "-Dspotbugs.effort=Max",
                "-Dspotbugs.threshold=Low",
                "-DskipTests",
                "-Djdk.security.manager.allow.argLine=",
                "-Dmaven.compiler.release=21",
                "-Dfmpp.skip=true",
                "-Dforbiddenapis.skip=true",
                "-Dcheckstyle.skip=true",
                "-Dpmd.skip=true",
            ]

            # If we have specific modules from compilation, limit scope with -pl
            if compiled_classes_paths:
                modules_to_scan = set()
                for cp in compiled_classes_paths:
                    parts = cp.replace("\\", "/").split("/")
                    if "target" in parts and "classes" in parts:
                        idx = parts.index("target")
                        if idx > 0:
                            repo_parts = self.target_repo_path.replace("\\", "/").split(
                                "/"
                            )
                            module_parts = parts[:idx]
                            if len(module_parts) > len(repo_parts):
                                relative_module = "/".join(
                                    module_parts[len(repo_parts) :]
                                )
                                modules_to_scan.add(relative_module)

                if modules_to_scan:
                    cmd.extend(["-pl", ",".join(modules_to_scan)])
                    cmd.append("-am")

            print(f"    Agent 4: Executing SpotBugs via Maven plugin: {' '.join(cmd)}")
            try:
                result = subprocess.run(
                    cmd,
                    cwd=self.target_repo_path,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=maven_env,
                )
                output = result.stdout + "\n" + result.stderr

                # Check for execution errors
                if "MojoExecutionException" in output or "BUILD FAILURE" in output:
                    print(
                        f"    Agent 4 Warning: Maven SpotBugs failed, trying full scan..."
                    )
                    cmd_fallback = [
                        "mvn",
                        "com.github.spotbugs:spotbugs-maven-plugin:4.9.0.0:spotbugs",
                        "-Dspotbugs.effort=Max",
                        "-Dspotbugs.threshold=Low",
                        "-DskipTests",
                        "-Djdk.security.manager.allow.argLine=",
                        "-Dmaven.compiler.release=21",
                        "-Dfmpp.skip=true",
                        "-Dforbiddenapis.skip=true",
                        "-Dcheckstyle.skip=true",
                        "-Dpmd.skip=true",
                    ]
                    print(
                        f"    Agent 4: Executing full project SpotBugs scan: {' '.join(cmd_fallback)}"
                    )
                    result = subprocess.run(
                        cmd_fallback,
                        cwd=self.target_repo_path,
                        capture_output=True,
                        text=True,
                        timeout=600,
                        env=maven_env,
                    )
                    output = result.stdout + "\n" + result.stderr

                # Check again for critical failures
                if "MojoExecutionException" in output or "BUILD FAILURE" in output:
                    print(f"    Agent 4 Warning: Maven SpotBugs full scan also failed")
                    return {
                        "success": True,
                        "output": output,
                        "method": "maven-plugin-failed",
                        "note": "SpotBugs execution failed, assuming clean by default",
                    }
                else:
                    # Parse output for bug count - SpotBugs Maven plugin outputs "Total bugs: X"
                    # Also check for individual bug reports like "BUG: ..."
                    bug_match = re.search(r"Total bugs: (\d+)", output)
                    if bug_match:
                        bug_count = int(bug_match.group(1))
                        passed = bug_count == 0
                        return {
                            "success": passed,
                            "output": output,
                            "method": "maven-plugin",
                            "bug_count": bug_count,
                        }
                    # Fallback: check for "No bugs found" or individual bug patterns
                    has_bugs = (
                        "BUG:" in output or "High: " in output or "Medium: " in output
                    )
                    passed = (
                        not has_bugs
                        or "No bugs found" in output
                        or "Total bugs: 0" in output
                    )
                    return {
                        "success": passed,
                        "output": output,
                        "method": "maven-plugin",
                    }
            except subprocess.TimeoutExpired:
                print(f"    Agent 4 Warning: Maven SpotBugs timed out, falling back...")
            except Exception as e:
                print(f"    Agent 4 Warning: Maven SpotBugs failed: {e}")

        # 3. Fallback to programmatic tool (Analysis Engine)
        print("    Agent 4: Falling back to programmatic SpotBugs tool...")
        aux_classpath = self.get_project_classpath()
        args = {
            "compiled_classes_paths": compiled_classes_paths,
            "aux_classpath": aux_classpath,
        }
        if source_path:
            args["source_path"] = source_path

        return self.client.call_tool("spotbugs", args)

    def write_trace(self, trace_content: str, filename: str = "validation_trace.md"):
        """
        Writes the validation trace to a file.
        """
        with open(filename, "w", encoding="utf-8") as f:
            f.write(trace_content)
        print(f"Trace saved to {filename}")

    # ------------------------------------------------------------------
    # Phase 3 New Methods: Hunk Application
    # ------------------------------------------------------------------

    def apply_hunk_dry_run(self, target_file_path: str, hunk_text: str) -> Dict:
        """
        Validates that a generated unified-diff hunk can apply cleanly to the
        target file without actually modifying it.

        Writes the hunk_text to a temporary .patch file, runs
        `git apply --check <patch>` in the target repo, then cleans up.

        Args:
            target_file_path: Relative path of the target file within the repo
                              (used to prefix the hunk header if needed).
            hunk_text:        A unified diff hunk string  — MUST start with @@.

        Returns:
            {"success": bool, "output": str}
        """
        # Build a minimal patch file that git can understand
        patch_content = self._build_patch_file(target_file_path, hunk_text)

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(patch_content)
                tmp_path = tmp.name

            result = subprocess.run(
                [
                    "git",
                    "apply",
                    "--check",
                    "--recount",
                    "--whitespace=nowarn",
                    tmp_path,
                ],
                capture_output=True,
                text=True,
                cwd=self.target_repo_path,
            )
            if result.returncode == 0:
                return {"success": True, "output": result.stdout or "Clean apply."}

            # Fallback to patch --dry-run
            patch_result = subprocess.run(
                [
                    "patch",
                    "-p1",
                    "--dry-run",
                    "--batch",
                    "--forward",
                    "--reject-file=-",
                    "--ignore-whitespace",
                    "-i",
                    tmp_path,
                ],
                capture_output=True,
                text=True,
                cwd=self.target_repo_path,
            )
            if patch_result.returncode == 0:
                return {
                    "success": True,
                    "output": patch_result.stdout or "Clean apply via patch fallback.",
                }

            # ------------------------------------------------------------------
            # Final Fallback: CLAW-style exact string matching (dry-run)
            # ------------------------------------------------------------------
            try:
                context = extract_hunk_context_from_diff(hunk_text)
                if context and context.old_string:
                    target_file_abs = os.path.join(
                        self.target_repo_path, target_file_path
                    )
                    if os.path.exists(target_file_abs):
                        with open(
                            target_file_abs, "r", encoding="utf-8", errors="replace"
                        ) as f:
                            content = f.read()
                        if context.old_string in content:
                            return {
                                "success": True,
                                "output": "Clean apply via CLAW exact-string dry-run.",
                            }
            except Exception:
                pass

            return {"success": False, "output": result.stderr or result.stdout}

        except Exception as e:
            return {"success": False, "output": f"Exception during dry-run: {e}"}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def apply_hunk_dry_run_structured(
        self,
        hunk_id: str,
        target_file_path: str,
        hunk_text: str,
        context_lines: Optional[List[str]] = None,
    ) -> HunkValidationResult:
        """
        Validates a hunk and returns a structured HunkValidationResult (CLAW-inspired).

        Instead of returning a simple dict with success/failure, this returns a rich
        HunkValidationResult object with error metadata, context, and suggestions.

        Args:
            hunk_id: Identifier for this hunk (for tracking)
            target_file_path: Relative path of the target file
            hunk_text: The unified diff hunk text
            context_lines: Optional surrounding code for error context

        Returns:
            HunkValidationResult with detailed error information if validation fails
        """
        result = self.apply_hunk_dry_run(target_file_path, hunk_text)

        if result.get("success"):
            # Success case
            return HunkValidationResult(
                hunk_id=hunk_id,
                is_error=False,
                validation_output=result.get("output"),
                applied_successfully=True,
                context_lines=context_lines or [],
            )

        # Failure case: create structured error with categorization and suggestions
        error_output = result.get("output", "Unknown error")
        error_type, suggestions = self._categorize_validation_error(
            error_output, hunk_text
        )

        error = HunkValidationError(
            error_type=error_type,
            message=error_output,
            context_lines=context_lines or [],
            suggestions=suggestions,
            raw_output=error_output,
        )

        return HunkValidationResult(
            hunk_id=hunk_id,
            is_error=True,
            error=error,
            validation_output=error_output,
            applied_successfully=False,
            context_lines=context_lines or [],
        )

    def _categorize_validation_error(self, error_output: str, hunk_text: str) -> tuple:
        """
        Categorize a validation error and suggest fixes.

        Returns:
            (HunkValidationErrorType, List[str] of suggestions)
        """
        suggestions = []

        # Check for specific error patterns
        if "malformed patch" in error_output.lower():
            suggestions.append("Check hunk header format (should start with @@)")
            suggestions.append("Verify line numbers match target file")
            return HunkValidationErrorType.MALFORMED_PATCH, suggestions

        if (
            "context.*does not match" in error_output.lower()
            or "patch does not apply" in error_output.lower()
        ):
            suggestions.append(
                "Target file may have different code at the hunk location"
            )
            suggestions.append("Review line numbers - target code may have changed")
            suggestions.append("Check if required dependencies or imports are present")
            return HunkValidationErrorType.CONTEXT_MISMATCH, suggestions

        if "No such file" in error_output or "not found" in error_output.lower():
            suggestions.append("Target file does not exist - may need CREATE operation")
            suggestions.append("Check file path mapping from source to target")
            return HunkValidationErrorType.CONTEXT_MISMATCH, suggestions

        if "Hunk #" in error_output and "FAILED" in error_output:
            # GNU patch indicates which hunk failed
            suggestions.append("One or more hunks failed to apply")
            suggestions.append("Check if file was modified or has different structure")
            return HunkValidationErrorType.APPLY_FAILED, suggestions

        if "offset" in error_output.lower() or "adjust" in error_output.lower():
            suggestions.append("Hunk line numbers have shifted due to prior changes")
            suggestions.append(
                "Consider reordering hunks or recalculating line numbers"
            )
            return HunkValidationErrorType.LINE_OFFSET_ERROR, suggestions

        # Default: unknown error
        suggestions.append("Review hunk content and target file carefully")
        suggestions.append("Run git diff to see exact differences in target repo")
        return HunkValidationErrorType.UNKNOWN, suggestions

    def apply_adapted_hunks(self, code_hunks: list, test_hunks: list) -> Dict:
        """
        Applies the full set of adapted code and test hunks to the target repo
        using a resilient multi-pass strategy.

        Strategy:
            1) Build one unified diff section per file with hunks in bottom-to-top order.
            2) Try strict git apply with recount.
            3) Retry git apply with whitespace-tolerant flags.
            4) Fallback to GNU patch dry-run + apply (fuzzy matching/offset support).

        Args:
            code_hunks: List of AdaptedHunk dicts for code changes.
            test_hunks: List of AdaptedHunk dicts for test changes.

        Returns:
            {"success": bool, "output": str, "applied_files": list[str]}
        """
        all_hunks = list(code_hunks) + list(test_hunks)
        if not all_hunks:
            return {
                "success": False,
                "output": "No hunks to apply.",
                "applied_files": [],
            }

        def _normalize_rel_path(path: str) -> str:
            p = (path or "").strip().replace("\\", "/").lstrip("/")
            while p.startswith("a/") or p.startswith("b/"):
                p = p[2:]
            if p == "dev/null":
                return ""
            return p

        def _is_full_file_creation_hunk(hunk_text: str) -> bool:
            """
            Heuristic: hunk contains only added lines (plus header), which usually
            means a full-file create patch. If target file already exists, re-applying
            this as MODIFIED is typically wrong and should be skipped.
            """
            if not hunk_text or not hunk_text.strip():
                return False
            lines = hunk_text.splitlines()
            if not lines or not lines[0].startswith("@@"):
                return False
            body = lines[1:]
            if not body:
                return False
            saw_added = False
            for line in body:
                if line.startswith("+"):
                    saw_added = True
                    continue
                # Any context/removal means this is not a pure file-creation hunk.
                if line.startswith(" ") or line.startswith("-"):
                    return False
            return saw_added

        def _is_full_git_diff(hunk_text: str) -> bool:
            return bool((hunk_text or "").lstrip().startswith("diff --git"))

        normalized_hunks = []

        # Ensure all hunks have insertion_line set; normalize and preflight operations.
        for raw_h in all_hunks:
            h = dict(raw_h)
            target_file = _normalize_rel_path(h.get("target_file", ""))
            h["target_file"] = target_file

            if not h.get("insertion_line"):
                hunk_text = h.get("hunk_text", "")
                try:
                    match = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)", hunk_text)
                    if match:
                        h["insertion_line"] = int(match.group(1))
                except Exception:
                    h["insertion_line"] = 0  # Fallback to 0 if extraction fails

            original_op = (h.get("file_operation", "MODIFIED") or "MODIFIED").upper()
            if h.get("file_operation_required") is False:
                h["file_operation"] = None
                normalized_hunks.append(h)
                continue

            file_path_full = os.path.normpath(
                os.path.join(self.target_repo_path, target_file)
            )
            file_exists = os.path.exists(file_path_full) and os.path.isfile(
                file_path_full
            )

            old_target_file = _normalize_rel_path(
                h.get("old_target_file") or h.get("mainline_file") or ""
            )
            h["old_target_file"] = old_target_file or None
            old_file_path_full = (
                os.path.normpath(os.path.join(self.target_repo_path, old_target_file))
                if old_target_file
                else ""
            )
            old_exists = bool(old_target_file and os.path.isfile(old_file_path_full))

            hunk_text = h.get("hunk_text", "")
            corrected_op = original_op

            if original_op == "ADDED" and file_exists:
                # Full-file create hunks should be no-op if file already exists.
                if _is_full_file_creation_hunk(hunk_text):
                    corrected_op = None
                    h["_skip_reason"] = "added_file_already_present_with_full_content"
                    print(
                        f"  Validation: {target_file} marked ADDED but already exists "
                        "with full-create hunk → skipping"
                    )
                else:
                    corrected_op = "MODIFIED"
                    print(
                        f"  Validation: {target_file} marked ADDED but exists → correcting to MODIFIED"
                    )
            elif original_op == "DELETED" and not file_exists:
                corrected_op = None
                print(
                    f"  Validation: {target_file} marked DELETED but doesn't exist → skipping"
                )
            elif original_op == "MODIFIED" and not file_exists:
                # If old path exists and differs, this is likely a rename flow.
                if old_exists and old_target_file and old_target_file != target_file:
                    corrected_op = "RENAMED"
                    print(
                        f"  Validation: {target_file} marked MODIFIED but missing; old path exists "
                        f"({old_target_file}) → correcting to RENAMED"
                    )
                else:
                    corrected_op = "ADDED"
                    print(
                        f"  Validation: {target_file} marked MODIFIED but doesn't exist → correcting to ADDED"
                    )
            elif original_op == "RENAMED":
                if old_exists and not file_exists:
                    corrected_op = "RENAMED"
                elif not old_exists and file_exists:
                    corrected_op = "MODIFIED"
                    print(
                        f"  Validation: {target_file} marked RENAMED but old path missing "
                        f"({old_target_file}) → correcting to MODIFIED"
                    )
                elif old_exists and file_exists:
                    corrected_op = "MODIFIED"
                    print(
                        f"  Validation: {target_file} marked RENAMED but old and new both exist "
                        "→ correcting to MODIFIED"
                    )
                else:
                    # Heuristic rescue: some upstream patch parsers may misclassify
                    # pure file additions as RENAMED when source path is /dev/null.
                    # If we have hunk content, treat this as ADDED so test/code files
                    # are actually created during validation.
                    has_hunks_for_file = bool((hunk_text or "").strip())
                    if has_hunks_for_file:
                        corrected_op = "ADDED"
                        print(
                            f"  Validation: {target_file} marked RENAMED but neither old nor new exists "
                            "with non-empty hunks → correcting to ADDED"
                        )
                    else:
                        corrected_op = None
                        print(
                            f"  Validation: {target_file} marked RENAMED but neither old nor new exists "
                            "→ skipping"
                        )

            h["file_operation"] = corrected_op
            if corrected_op != original_op:
                h["_file_operation_corrected"] = True

            normalized_hunks.append(h)

        # Group hunks by target file, then sort each file's hunks.
        # We sort them bottom-to-top (reverse=True) and apply them ONE BY ONE.
        # This prevents earlier hunks from shifting line numbers for later hunks.
        hunks_by_file = {}
        for h in normalized_hunks:
            target_file = _normalize_rel_path(h.get("target_file", "unknown"))
            if target_file not in hunks_by_file:
                hunks_by_file[target_file] = []
            hunks_by_file[target_file].append(h)

        for target_file in hunks_by_file:
            hunks_by_file[target_file].sort(
                key=lambda h: h.get("insertion_line", 0), reverse=True
            )
            insertion_lines = [
                h.get("insertion_line") for h in hunks_by_file[target_file]
            ]
            print(
                f"  Validation: {target_file} - applying {len(hunks_by_file[target_file])} "
                f"hunk(s) one by one in bottom-to-top order: {insertion_lines}"
            )

        applied_files = []
        all_errors = []

        for target_file in hunks_by_file:
            file_operations = set()
            old_file_path = None
            skip_file = False

            for h in hunks_by_file[target_file]:
                file_op = h.get("file_operation")
                if file_op is None:
                    skip_file = True
                    break
                file_operations.add(file_op.upper())
                if file_op.upper() == "RENAMED" and not old_file_path:
                    old_file_path = h.get("old_target_file") or h.get("mainline_file")

            if skip_file:
                print(f"  Validation: Skipping {target_file}")
                continue

            if not old_file_path:
                for h in hunks_by_file[target_file]:
                    candidate_old = _normalize_rel_path(
                        h.get("old_target_file") or h.get("mainline_file") or ""
                    )
                    if candidate_old and candidate_old != target_file:
                        old_file_path = candidate_old
                        break

            # Determine the operation type
            if len(file_operations) == 1:
                file_operation = next(iter(file_operations))
            elif "RENAMED" in file_operations:
                file_operation = "RENAMED"
            elif "ADDED" in file_operations and "DELETED" not in file_operations:
                file_operation = "ADDED"
            elif "DELETED" in file_operations and "ADDED" not in file_operations:
                file_operation = "DELETED"
            else:
                file_operation = "MODIFIED"

            file_has_full_git_diff = any(
                _is_full_git_diff(h.get("hunk_text", ""))
                for h in hunks_by_file.get(target_file, [])
            )

            # Execute structural operation BEFORE applying hunks, but only for
            # legacy bare-@@ hunks. Full git-diff payloads already encode file
            # operations in their own headers.
            if (
                file_operation in ["RENAMED", "ADDED", "DELETED"]
                and not file_has_full_git_diff
            ):
                try:
                    mark_as_preapplied = True
                    if file_operation == "RENAMED":
                        if not old_file_path:
                            raise ValueError("old_file_path is required for RENAMED")
                        src = os.path.normpath(
                            os.path.join(
                                self.target_repo_path,
                                _normalize_rel_path(old_file_path),
                            )
                        )
                        dst = os.path.normpath(
                            os.path.join(self.target_repo_path, target_file)
                        )
                        if not os.path.exists(src):
                            raise ValueError(
                                f"rename source does not exist: {old_file_path}"
                            )
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.replace(src, dst)
                    elif file_operation == "DELETED":
                        src = os.path.normpath(
                            os.path.join(self.target_repo_path, target_file)
                        )
                        if os.path.exists(src):
                            os.remove(src)
                    elif file_operation == "ADDED":
                        # For ADDED files with hunks, do NOT pre-create the file;
                        # git/patch add semantics expect the path to be absent.
                        # Pre-creating here causes "would create file ... already exists".
                        dst = os.path.normpath(
                            os.path.join(self.target_repo_path, target_file)
                        )
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        has_hunks_for_file = any(
                            bool((hh.get("hunk_text") or "").strip())
                            for hh in hunks_by_file.get(target_file, [])
                        )
                        if not has_hunks_for_file and not os.path.exists(dst):
                            with open(dst, "w", encoding="utf-8"):
                                pass
                        # Critical: for ADDED files with hunks, keep first patch header as ADDED
                        # (do not downgrade to MODIFIED based on pre-applied bookkeeping).
                        if has_hunks_for_file:
                            mark_as_preapplied = False

                    if mark_as_preapplied and target_file not in applied_files:
                        applied_files.append(target_file)
                    print(f"  Validation: {file_operation} operation for {target_file}")
                except ValueError as e:
                    return {
                        "success": False,
                        "output": f"Invalid {file_operation} operation for {target_file}: {e}",
                        "applied_files": [],
                    }

            # Build a combined patch for all hunks belonging to this file.
            # Using a single git apply call per file allows git to handle hunk
            # dependencies and line offsets internally.
            combined_file_patch = ""
            first_hunk_line = 0

            for h in hunks_by_file[target_file]:
                hunk_text = h.get("hunk_text", "")
                if not hunk_text or not hunk_text.strip():
                    continue

                if not first_hunk_line:
                    first_hunk_line = h.get("insertion_line", 0)

                hunk_text = hunk_text if hunk_text.endswith("\n") else hunk_text + "\n"

                if _is_full_git_diff(hunk_text):
                    combined_file_patch += hunk_text
                else:
                    # Treat subsequent hunks on the same file as MODIFIED
                    # so we don't regenerate "new file" diff headers for the same file.
                    current_file_op = (
                        "MODIFIED" if target_file in applied_files else file_operation
                    )

                    try:
                        patch_part = self._build_patch_file(
                            target_file,
                            hunk_text,
                            file_operation=current_file_op,
                            old_file_path=old_file_path
                            if current_file_op == "RENAMED"
                            else None,
                        )
                        combined_file_patch += patch_part
                        if target_file not in applied_files:
                            applied_files.append(target_file)
                    except ValueError as e:
                        self.restore_repo_state()
                        return {
                            "success": False,
                            "output": f"Invalid hunk format for {target_file}: {e}",
                            "applied_files": [],
                        }

            if combined_file_patch:
                result = self._apply_patch_with_fallbacks(
                    combined_file_patch, [target_file]
                )
                if not result.get("success"):
                    self.restore_repo_state()
                    return {
                        "success": False,
                        "output": f"Patch application failed for {target_file} at line {first_hunk_line}:\n{result.get('output')}",
                        "applied_files": [],
                    }
                if target_file not in applied_files:
                    applied_files.append(target_file)

        return {
            "success": True,
            "output": "All hunks applied successfully.",
            "applied_files": applied_files,
        }

    def _apply_patch_with_fallbacks(
        self, patch_content: str, applied_files: list[str]
    ) -> Dict:
        """
        Apply patch content with increasingly permissive strategies.
        """
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".patch",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(patch_content)
                tmp_path = tmp.name

            attempts = [
                (
                    "git-apply-strict",
                    ["git", "apply", "--recount", "--whitespace=nowarn", tmp_path],
                ),
                (
                    "git-apply-whitespace-tolerant",
                    [
                        "git",
                        "apply",
                        "--recount",
                        "--ignore-space-change",
                        "--ignore-whitespace",
                        "--whitespace=nowarn",
                        tmp_path,
                    ],
                ),
            ]

            all_errors = []
            for name, cmd in attempts:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=self.target_repo_path,
                )
                if result.returncode == 0:
                    return {
                        "success": True,
                        "output": result.stdout or f"Applied successfully via {name}.",
                        "applied_files": applied_files,
                        "apply_strategy": name,
                    }

                err = result.stderr or result.stdout or "unknown apply failure"
                all_errors.append(f"[{name}] {err.strip()}")

            patch_dry_run_cmd = [
                "patch",
                "-p1",
                "--dry-run",
                "--batch",
                "--forward",
                "--reject-file=-",
                "--ignore-whitespace",
                "-i",
                tmp_path,
            ]
            patch_apply_cmd = [
                "patch",
                "-p1",
                "--batch",
                "--forward",
                "--reject-file=-",
                "--ignore-whitespace",
                "-i",
                tmp_path,
            ]

            dry_run = subprocess.run(
                patch_dry_run_cmd,
                capture_output=True,
                text=True,
                cwd=self.target_repo_path,
            )

            if dry_run.returncode == 0:
                patch_apply = subprocess.run(
                    patch_apply_cmd,
                    capture_output=True,
                    text=True,
                    cwd=self.target_repo_path,
                )
                if patch_apply.returncode == 0:
                    return {
                        "success": True,
                        "output": patch_apply.stdout
                        or "Applied successfully via patch fallback.",
                        "applied_files": applied_files,
                        "apply_strategy": "gnu-patch-fallback",
                    }

                self.restore_repo_state()
                patch_err = (
                    patch_apply.stderr or patch_apply.stdout or "patch apply failed"
                )
                all_errors.append(f"[gnu-patch-apply] {patch_err.strip()}")
            else:
                patch_err = dry_run.stderr or dry_run.stdout or "patch dry-run failed"
                all_errors.append(f"[gnu-patch-dry-run] {patch_err.strip()}")

            # ------------------------------------------------------------------
            # Strategy 4: CLAW-style exact string matching (Final Fallback)
            # ------------------------------------------------------------------
            # If git and patch both failed, try parsing the hunk into old/new strings
            # and applying it via exact matching. This handles cases where line
            # numbers are totally wrong but the code context is unique.
            try:
                hunk_lines: list[str] = []
                in_hunk = False
                for line in str(patch_content or "").splitlines():
                    if line.startswith("@@"):
                        in_hunk = True
                    if in_hunk:
                        if line.startswith("diff --git") and hunk_lines:
                            break
                        hunk_lines.append(line)

                hunk_text = "\n".join(hunk_lines).strip()
                context = (
                    extract_hunk_context_from_diff(hunk_text) if hunk_text else None
                )
                if context and context.old_string and context.new_string:
                    # We need the target file path. For multi-file patches this is
                    # complex, but here we usually have one file in patch_content.
                    # Try to extract it from the patch header.
                    target_file_match = re.search(
                        r"^\+\+\+ b/(.*)", patch_content, re.MULTILINE
                    )
                    if target_file_match:
                        target_file_rel = target_file_match.group(1).strip()
                        target_file_abs = os.path.join(
                            self.target_repo_path, target_file_rel
                        )

                        success, edit_out = edit_file(
                            path=target_file_abs,
                            old_string=context.old_string,
                            new_string=context.new_string,
                            replace_all=False,
                        )
                        if success:
                            return {
                                "success": True,
                                "output": "Applied successfully via CLAW exact-string fallback.",
                                "applied_files": applied_files,
                                "apply_strategy": "claw-exact-match",
                            }
                        else:
                            err_msg = (
                                edit_out.get("error", "Unknown edit error")
                                if isinstance(edit_out, dict)
                                else "Edit failed"
                            )
                            all_errors.append(f"[claw-exact-match] {err_msg}")
            except Exception as e:
                all_errors.append(f"[claw-exact-match-exception] {e}")

            return {
                "success": False,
                "output": "\n\n".join(all_errors),
                "applied_files": [],
            }
        except Exception as e:
            return {
                "success": False,
                "output": f"Exception during apply: {e}",
                "applied_files": [],
            }
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # ------------------------------------------------------------------
    # Phase 4 New Methods: Testing and Repo Management
    # ------------------------------------------------------------------

    def run_build_script(self) -> Dict:
        """
        Runs the build script for the target repo.
        Detects if it's Maven (pom.xml) or Gradle (build.gradle).
        """
        is_gradle = os.path.exists(os.path.join(self.target_repo_path, "build.gradle"))
        project_name = os.path.basename(self.target_repo_path).strip().lower()

        if project_name == "druid":
            helper_script = os.path.join(
                self._get_project_helper_dir("druid"), "run_build.sh"
            )
            if os.path.exists(helper_script):
                image_tag, image_err = self._ensure_project_builder_image("druid")
                if image_tag:
                    helper_env = os.environ.copy()
                    helper_env.update(
                        {
                            "PROJECT_DIR": self.target_repo_path,
                            "BUILDER_IMAGE_TAG": image_tag,
                            "IMAGE_TAG": image_tag,
                            "COMMIT_SHA": self._get_current_head(),
                            "WORKTREE_MODE": "1",
                        }
                    )
                    print(
                        f"    Agent 4: Executing Druid helper build script: {helper_script}"
                    )
                    result = self._run_cmd_capture(
                        ["bash", helper_script],
                        cwd=self.target_repo_path,
                        env=helper_env,
                    )
                    return {
                        "success": bool(result.get("success")),
                        "output": result.get("output", ""),
                        "mode": "druid-helper-script",
                    }
                else:
                    print(
                        f"    Agent 4 Warning: Druid helper image unavailable. Falling back. Details: {image_err}"
                    )
        elif self._is_known_project_with_helper(project_name):
            # Generic helper-based build for projects with a run_build.sh (e.g., crate)
            helper_script = os.path.join(
                self._get_project_helper_dir(project_name), "run_build.sh"
            )
            if os.path.exists(helper_script):
                image_tag, image_err = self._ensure_project_builder_image(project_name)
                if image_tag:
                    helper_env = os.environ.copy()
                    helper_env.update(
                        {
                            "PROJECT_NAME": project_name,
                            "PROJECT_DIR": self.target_repo_path,
                            "TOOLKIT_DIR": self._get_project_helper_dir(project_name),
                            "BUILDER_IMAGE_TAG": image_tag,
                            "IMAGE_TAG": image_tag,
                            "COMMIT_SHA": self._get_current_head(),
                            "WORKTREE_MODE": "1",
                        }
                    )
                    print(
                        f"    Agent 4: Executing {project_name} helper build script: {helper_script}"
                    )
                    result = self._run_cmd_capture(
                        ["bash", helper_script],
                        cwd=self.target_repo_path,
                        env=helper_env,
                    )
                    return {
                        "success": bool(result.get("success")),
                        "output": result.get("output", ""),
                        "mode": f"{project_name}-helper-script",
                    }
                else:
                    print(
                        f"    Agent 4 Warning: {project_name} helper image unavailable. Falling back. Details: {image_err}"
                    )

        if is_gradle:
            cmd = ["gradle", "testClasses"]
        else:
            # Druid can fail on `mvn clean compile` because some modules depend on
            # classifier artifacts (e.g., tests-jar) that are not produced at compile phase.
            # Build only touched modules at package phase so required artifacts exist.
            if project_name == "druid":
                target_info = self.detect_relevant_test_targets(project="druid")
                modules = sorted(
                    set(
                        target_info.get("all_modules")
                        or target_info.get("source_modules")
                        or []
                    )
                )
                if modules:
                    cmd = [
                        "mvn",
                        "clean",
                        "package",
                        "-pl",
                        ",".join(modules),
                        "-am",
                        "-DskipTests",
                        "-DskipITs",
                        "-DfailIfNoTests=false",
                        "-Dmaven.javadoc.skip=true",
                        "-Dcheckstyle.skip=true",
                        "-Dpmd.skip=true",
                        "-Dforbiddenapis.skip=true",
                        "-Denforcer.skip=true",
                        "-Drat.skip=true",
                        "-Dweb.console.skip=true",
                    ]
                else:
                    cmd = [
                        "mvn",
                        "clean",
                        "package",
                        "-DskipTests",
                        "-DskipITs",
                        "-DfailIfNoTests=false",
                        "-Dmaven.javadoc.skip=true",
                        "-Dcheckstyle.skip=true",
                        "-Dpmd.skip=true",
                        "-Dforbiddenapis.skip=true",
                        "-Denforcer.skip=true",
                        "-Drat.skip=true",
                        "-Dweb.console.skip=true",
                        "-pl",
                        "!:distribution",
                    ]
            else:
                cmd = ["mvn", "clean", "compile"]

        try:
            print(f"    Agent 4: Executing build command: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.target_repo_path,
            )
            return {
                "success": result.returncode == 0,
                "output": (
                    (result.stdout or "") + "\n" + (result.stderr or "")
                ).strip(),
            }
        except Exception as e:
            return {"success": False, "output": f"Exception building repo: {e}"}

    def run_targeted_tests(self, test_classes: List[str]) -> Dict:
        """
        Runs specific unit tests in the target repository.
        Detects Maven vs Gradle automatically, or uses project helper script if available.

        Args:
            test_classes: List of test class names to run.

        Returns:
            {"success": bool, "compile_error": bool, "output": str, "failed_tests": list[str]}
        """
        if not test_classes:
            return {
                "success": True,
                "compile_error": False,
                "output": "No tests to run.",
                "failed_tests": [],
            }

        project_name = self._detect_project_name()
        if self._is_known_project_with_helper(project_name):
            helper_script = os.path.join(
                self._get_project_helper_dir(project_name), "run_tests.sh"
            )
            if os.path.exists(helper_script):
                image_tag, image_err = self._ensure_project_builder_image(project_name)
                if image_tag:
                    helper_env = os.environ.copy()
                    helper_env.update(
                        {
                            "PROJECT_NAME": project_name,
                            "PROJECT_DIR": self.target_repo_path,
                            "TOOLKIT_DIR": self._get_project_helper_dir(project_name),
                            "BUILDER_IMAGE_TAG": image_tag,
                            "IMAGE_TAG": image_tag,
                            "COMMIT_SHA": self._get_current_head(),
                            "WORKTREE_MODE": "1",
                        }
                    )

                    cmd = ["bash", helper_script] + test_classes
                    print(
                        f"    Agent 4: Executing {project_name} helper test script: {' '.join(cmd)}"
                    )
                    result = self._run_cmd_capture(
                        cmd, cwd=self.target_repo_path, env=helper_env
                    )

                    success = bool(result.get("success"))
                    output = result.get("output", "")
                    compile_error = (not success) and (
                        "compilation error" in output.lower()
                        or "build failed with an exception" in output.lower()
                    )

                    return {
                        "success": success,
                        "compile_error": compile_error,
                        "output": output,
                        "failed_tests": [],
                    }
                else:
                    print(
                        f"    Agent 4 Warning: {project_name} helper image unavailable for testing. Falling back. Details: {image_err}"
                    )

        is_gradle = os.path.exists(os.path.join(self.target_repo_path, "build.gradle"))

        # Fix for Java 18+ / Java 25 security manager issues
        java_compat_args = ["-Djdk.security.manager.allow.argLine="]

        if is_gradle:
            cmd = ["gradle", "test"]
            for cls in test_classes:
                cmd.extend(["--tests", cls])
        else:
            test_args = ",".join(test_classes)
            cmd = ["mvn", "test", f"-Dtest={test_args}"] + java_compat_args

        try:
            print(f"    Agent 4: Executing fallback test command: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=self.target_repo_path,
            )
            success = result.returncode == 0
            output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
            compile_error = (not success) and ("compilation error" in output.lower())

            return {
                "success": success,
                "compile_error": compile_error,
                "output": output,
                "failed_tests": [],
            }
        except Exception as e:
            return {
                "success": False,
                "compile_error": False,
                "output": f"Exception running tests: {e}",
                "failed_tests": [],
            }

    def create_patch_retry_context(
        self,
        patch_id: str,
        failed_hunk_ids: List[str],
        target_file_path: str,
        assembly_error_messages: List[str],
        suggestions: Optional[List[str]] = None,
    ) -> PatchRetryContext:
        """
        Create a PatchRetryContext for when Phase 4 (Validation) encounters failures.
        This context is passed back to Phase 3 (Hunk Generator) for LLM-driven retry.

        Follows CLAW's pattern of rich error feedback rather than silent failures.

        Args:
            patch_id: ID of the patch being retried
            failed_hunk_ids: List of hunk IDs that failed
            target_file_path: Path to the file being patched
            assembly_error_messages: Error messages from patch assembly
            suggestions: Optional list of fix suggestions

        Returns:
            PatchRetryContext with full context for retry
        """
        try:
            # Read the target file to provide full context to LLM
            full_path = os.path.join(self.target_repo_path, target_file_path)
            if os.path.exists(full_path) and os.path.isfile(full_path):
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    file_content = f.read()
            else:
                file_content = f"[File not found at {target_file_path}]"
        except Exception as e:
            file_content = f"[Error reading file: {e}]"

        return PatchRetryContext(
            patch_id=patch_id,
            failed_hunks=[],  # Would be populated with HunkValidationResult objects by caller
            target_file_path=target_file_path,
            target_file_content=file_content,
            assembly_error_messages=assembly_error_messages,
            suggestions_from_phase4=suggestions or [],
        )

    def restore_repo_state(self) -> bool:
        """
        Restores the target repository to a clean state, reverting any uncommitted patches.
        Handles git errors gracefully with fallback strategies.
        """
        try:
            # Attempt to reset and clean with increasing aggressiveness
            subprocess.run(
                ["git", "reset", "--hard"],
                cwd=self.target_repo_path,
                capture_output=True,
                check=True,
            )

            # Try initial git clean (most common case)
            result = subprocess.run(
                ["git", "clean", "-fd"], cwd=self.target_repo_path, capture_output=True
            )
            if result.returncode == 0:
                return True

            # If git clean fails, try with force flag and ignore errors
            result = subprocess.run(
                ["git", "clean", "-ffdX"],
                cwd=self.target_repo_path,
                capture_output=True,
            )
            if result.returncode == 0:
                return True

            # If even that fails, log it but don't fail hard - try to continue anyway
            print(
                f"ValidationToolkit: Warning - git clean had issues (exit code {result.returncode}). Continuing anyway."
            )
            # This is not a hard failure; patch application may still succeed
            return True

        except subprocess.CalledProcessError as e:
            print(f"ValidationToolkit: Error during git reset: {e}")
            return False
        except Exception as e:
            print(f"ValidationToolkit: Unexpected error restoring repo state: {e}")
            return False

    # ------------------------------------------------------------------
    # Private Helpers
    # ------------------------------------------------------------------

    def _build_patch_file(
        self,
        target_file_path: str,
        hunk_text: str,
        file_operation: str = "MODIFIED",
        old_file_path: str = None,
    ) -> str:
        """
        Wraps a hunk in a minimal unified diff file header so `git apply`
        can understand it. The hunk_text must already start with @@ ... @@.

        For file-only operations (rename, pure add, pure delete) with no hunk_text,
        generates an appropriate diff header without body hunks.

        Args:
            target_file_path: New path (for renames) or target path
            hunk_text: Unified diff hunks (may be empty for structural operations)
            file_operation: "ADDED" | "DELETED" | "MODIFIED" | "RENAMED"
            old_file_path: Old path (for renames)
        """
        # Normalize path separators and strip optional diff prefixes.
        p = (target_file_path or "").strip().replace("\\", "/").lstrip("/")
        while p.startswith("a/") or p.startswith("b/"):
            p = p[2:]

        if not p:
            raise ValueError("target_file_path is empty")

        normalized_op = (file_operation or "MODIFIED").upper()
        old_p = (
            (old_file_path or "").strip().replace("\\", "/").lstrip("/")
            if old_file_path
            else p
        )
        while old_p.startswith("a/") or old_p.startswith("b/"):
            old_p = old_p[2:]

        # Handle structural operations (no hunks)
        if not hunk_text or not hunk_text.strip():
            if normalized_op == "RENAMED":
                header = (
                    f"diff --git a/{old_p} b/{p}\n"
                    f"similarity index 100%\n"
                    f"rename from {old_p}\n"
                    f"rename to {p}\n"
                )
                return header + "\n"
            elif normalized_op == "ADDED":
                header = (
                    f"diff --git a/{p} b/{p}\n"
                    f"new file mode 100644\n"
                    f"index 0000000..0000000\n"
                    f"--- /dev/null\n"
                    f"+++ b/{p}\n"
                )
                return header + "\n"
            elif normalized_op == "DELETED":
                header = (
                    f"diff --git a/{p} b/{p}\n"
                    f"deleted file mode 100644\n"
                    f"index 0000000..0000000\n"
                    f"--- a/{p}\n"
                    f"+++ /dev/null\n"
                )
                return header + "\n"
            else:
                # MODIFIED with no hunks doesn't make sense, but handle gracefully
                raise ValueError(
                    f"MODIFIED operation requires hunks, but hunk_text is empty"
                )

        # Normal case: file operation with hunks
        if not hunk_text.startswith("@@"):
            raise ValueError(f"Hunk must start with @@, got: {hunk_text[:50]}")

        # Ensure hunk ends with newline if it doesn't already
        body = hunk_text if hunk_text.endswith("\n") else hunk_text + "\n"

        # Add trailing newline for proper separation between files in combined patches
        if not body.endswith("\n\n"):
            body = body.rstrip("\n") + "\n"

        if normalized_op == "ADDED":
            header = (
                f"diff --git a/{p} b/{p}\n"
                f"new file mode 100644\n"
                f"index 0000000..0000000 100644\n"
                f"--- /dev/null\n"
                f"+++ b/{p}\n"
            )
        elif normalized_op == "DELETED":
            header = (
                f"diff --git a/{p} b/{p}\n"
                f"deleted file mode 100644\n"
                f"index 0000000..0000000 100644\n"
                f"--- a/{p}\n"
                f"+++ /dev/null\n"
            )
        elif normalized_op == "RENAMED":
            header = (
                f"diff --git a/{old_p} b/{p}\n"
                f"rename from {old_p}\n"
                f"rename to {p}\n"
                f"--- a/{old_p}\n"
                f"+++ b/{p}\n"
            )
        else:
            header = (
                f"diff --git a/{p} b/{p}\n"
                f"index 0000000..0000000 100644\n"
                f"--- a/{p}\n"
                f"+++ b/{p}\n"
            )

        return header + body

    def _extract_hunk_context(self, hunk_text: str) -> tuple[str, str]:
        """
        Extract the old_string and new_string from a unified diff hunk.

        This parses a standard unified diff hunk and reconstructs the exact
        strings that need to match for the patch to apply.

        Args:
            hunk_text: Unified diff hunk starting with @@

        Returns:
            Tuple of (old_string, new_string) - exact text to match and replace
        """
        lines = hunk_text.strip().split("\n")

        if not lines or not lines[0].startswith("@@"):
            return "", ""

        old_lines_list = []
        new_lines_list = []

        for line in lines[1:]:
            if line.startswith("-") and not line.startswith("---"):
                # Removed line
                old_lines_list.append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                # Added line
                new_lines_list.append(line[1:])
            elif line.startswith(" "):
                # Context line
                old_lines_list.append(line[1:])
                new_lines_list.append(line[1:])
            elif line.startswith("\\"):
                # "\ No newline at end of file" marker
                continue

        old_string = "\n".join(old_lines_list)
        new_string = "\n".join(new_lines_list)

        return old_string, new_string

    def apply_hunk_with_claw_approach(
        self,
        target_file_path: str,
        hunk_text: str,
    ) -> Dict:
        """
        Apply CLAW's approach to hunk application:
        1. Pre-validate that old_string exists BEFORE mutation
        2. Use exact string matching (no fuzzy)
        3. Return structured patch for verification

        This solves line number offset issues by using exact matching
        instead of relying on line number calculations.

        Args:
            target_file_path: Relative path to file (from repo root)
            hunk_text: Unified diff hunk text

        Returns:
            Dict with success status and details
        """
        # Step 1: Normalize path
        target_file = (target_file_path or "").strip().replace("\\", "/").lstrip("/")
        while target_file.startswith("a/") or target_file.startswith("b/"):
            target_file = target_file[2:]

        full_path = os.path.normpath(os.path.join(self.target_repo_path, target_file))

        # Step 2: Read the file
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                original_content = f.read()
        except Exception as e:
            return {
                "success": False,
                "file_path": target_file,
                "error": "file_read_error",
                "message": f"Could not read file: {str(e)}",
            }

        # Step 3: Extract old/new strings from hunk
        old_string, new_string = self._extract_hunk_context(hunk_text)

        if not old_string:
            return {
                "success": False,
                "file_path": target_file,
                "error": "hunk_parse_error",
                "message": "Could not parse hunk to extract context",
            }

        # Step 4: Pre-validate (CLAW approach - BEFORE mutation)
        if old_string not in original_content:
            return {
                "success": False,
                "file_path": target_file,
                "error": "context_not_found",
                "message": f"Could not find exact context in {target_file}",
                "suggestions": [
                    "Context lines may have changed",
                    "Check whitespace and indentation",
                    "The file may have been modified since the patch was generated",
                ],
                "old_string_preview": old_string[:100] + "..."
                if len(old_string) > 100
                else old_string,
            }

        # Step 5: Apply change
        updated_content = original_content.replace(old_string, new_string, 1)

        # Step 6: Write to disk
        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(updated_content)
        except Exception as e:
            return {
                "success": False,
                "file_path": target_file,
                "error": "file_write_error",
                "message": f"Could not write file: {str(e)}",
            }

        return {
            "success": True,
            "file_path": target_file,
            "message": "Hunk applied successfully",
        }

    def apply_adapted_hunks_claw_style(
        self, code_hunks: list, test_hunks: list
    ) -> Dict:
        """
        Apply hunks using CLAW's proven approach:
        - Extract exact strings from hunk context
        - Pre-validate before mutation
        - Apply one hunk at a time
        - Return structured output with detailed results

        This replaces the git apply fallback approach with exact string matching,
        which is more reliable for generated patches.

        Args:
            code_hunks: List of AdaptedHunk dicts for code changes
            test_hunks: List of AdaptedHunk dicts for test changes

        Returns:
            Dict with success status and results
        """
        all_hunks = list(code_hunks) + list(test_hunks)
        if not all_hunks:
            return {
                "success": False,
                "message": "No hunks to apply",
                "results": [],
                "applied_files": [],
            }

        results = []
        applied_files = []
        failed_count = 0

        # Apply each hunk
        for hunk in all_hunks:
            target_file = hunk.get("target_file")
            hunk_text = hunk.get("hunk_text", "")
            hunk_id = hunk.get("hunk_id", "unknown")

            if not hunk_text or not hunk_text.strip():
                # Skip empty hunks (structural operations only)
                results.append(
                    {
                        "hunk_id": hunk_id,
                        "file": target_file,
                        "status": "skipped",
                        "reason": "empty_hunk",
                    }
                )
                continue

            # Apply using CLAW approach
            result = self.apply_hunk_with_claw_approach(target_file, hunk_text)

            if result["success"]:
                results.append(
                    {
                        "hunk_id": hunk_id,
                        "file": target_file,
                        "status": "applied",
                    }
                )
                if target_file not in applied_files:
                    applied_files.append(target_file)
            else:
                results.append(
                    {
                        "hunk_id": hunk_id,
                        "file": target_file,
                        "status": "failed",
                        "error": result.get("error"),
                        "message": result.get("message"),
                        "suggestions": result.get("suggestions", []),
                    }
                )
                failed_count += 1

        # Build summary
        if failed_count > 0:
            return {
                "success": False,
                "message": f"{failed_count} hunk(s) failed to apply",
                "results": results,
                "applied_files": applied_files,
                "failed_count": failed_count,
            }

        return {
            "success": True,
            "message": "All hunks applied successfully",
            "results": results,
            "applied_files": applied_files,
        }
