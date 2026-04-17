#!/usr/bin/env python3
"""
Extract Gradle test targets for Spring Framework modified/added test files.

Output JSON schema:
  {
    "modified": [":module:test --tests ClassName", ...],
    "added":    [":module:test --tests ClassName", ...],
    "source_modules": ["spring-core", ...],
    "all_modules":    ["spring-core", ...]
  }
"""
import argparse
import json
import os
import subprocess

TEST_EXTENSIONS = (".java", ".kt", ".groovy", ".scala")
TEST_SUFFIXES = ("Test.java", "Tests.java", "TestCase.java", "IT.java",
                 "IntegrationTest.java", "Test.kt", "Tests.kt",
                 "Test.groovy", "Tests.groovy")


def _find_gradle_module(repo: str, rel_path: str) -> str:
    """Walk up looking for build.gradle / build.gradle.kts / <dir>.gradle; return :module style path."""
    head = os.path.dirname(rel_path.replace("\\", "/"))
    while head:
        for bf in ("build.gradle", "build.gradle.kts"):
            if os.path.exists(os.path.join(repo, head, bf)):
                return ":" + head.replace("/", ":")
        # Spring Framework uses <module-name>.gradle (e.g. spring-core.gradle)
        module_name = os.path.basename(head)
        if module_name and os.path.exists(os.path.join(repo, head, f"{module_name}.gradle")):
            return ":" + head.replace("/", ":")
        parent = os.path.dirname(head)
        if parent == head:
            break
        head = parent
    for bf in ("build.gradle", "build.gradle.kts"):
        if os.path.exists(os.path.join(repo, bf)):
            return ""
    return ""


def _is_test_file(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/")
    if "/src/test/" not in p:
        return False
    return any(p.endswith(s) for s in TEST_SUFFIXES) or (
        os.path.basename(p).startswith("Test") and p.endswith(".java")
    )


def _is_main_source(rel_path: str) -> bool:
    p = rel_path.replace("\\", "/")
    return "/src/main/" in p and any(p.endswith(e) for e in TEST_EXTENSIONS)


def _extract_class_name(rel_path: str) -> str:
    p = rel_path.replace("\\", "/")
    for marker in ("/src/test/java/", "/src/test/kotlin/",
                   "/src/test/groovy/", "/src/test/scala/"):
        if marker in p:
            class_part = p.split(marker, 1)[1]
            return class_part.replace("/", ".").rsplit(".", 1)[0]
    # fallback
    if "/java/" in p:
        return p.split("/java/", 1)[1].replace("/", ".").rsplit(".", 1)[0]
    return os.path.basename(p).rsplit(".", 1)[0]


def _parse_status_output(output: str) -> list:
    entries = []
    for line in output.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            status, path = parts[0].strip(), parts[1].strip()
            if status and path:
                entries.append((status, path))
    return entries


def main():
    parser = argparse.ArgumentParser(description="Extract Gradle test targets for Spring Framework")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--commit", help="Commit hash to analyze")
    parser.add_argument("--worktree", action="store_true", help="Analyze current worktree diff")
    parser.add_argument("--files-json", help="JSON array of [status, filepath] pairs (skips git)")
    args = parser.parse_args()

    empty = {"modified": [], "added": [], "source_modules": [], "all_modules": []}

    if not args.commit and not args.worktree and not args.files_json:
        print(json.dumps(empty))
        return

    if args.files_json:
        try:
            raw = json.loads(args.files_json)
            entries = []
            for item in raw:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    entries.append((str(item[0]), str(item[1])))
                else:
                    entries.append(("M", str(item)))
        except Exception:
            entries = []
    else:
        if args.worktree:
            cmd = ["git", "diff", "--name-status", "--diff-filter=AMRT"]
        else:
            cmd = ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", args.commit]
        try:
            output = subprocess.check_output(cmd, cwd=args.repo, text=True, timeout=60)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            print(json.dumps(empty))
            return
        entries = _parse_status_output(output)

    modified_tests: set = set()
    added_tests: set = set()
    source_modules: set = set()
    all_modules: set = set()

    for status, f in entries:
        module = _find_gradle_module(args.repo, f)
        module_key = module.lstrip(":").replace(":", "/") if module else ""

        if module_key:
            all_modules.add(module_key)

        if _is_main_source(f) and module_key:
            source_modules.add(module_key)

        if not _is_test_file(f):
            continue

        class_name = _extract_class_name(f)
        # No quotes around class name — Gradle handles it fine without them
        target = f"{module}:test --tests {class_name}" if module else f"test --tests {class_name}"

        if status == "A":
            added_tests.add(target)
        else:
            modified_tests.add(target)

    result = {
        "modified": sorted(modified_tests),
        "added": sorted(added_tests),
        "source_modules": sorted(source_modules),
        "all_modules": sorted(all_modules),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
