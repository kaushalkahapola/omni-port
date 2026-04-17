#!/usr/bin/env python3
"""
Project-aware test result collector.

Reads XML test reports (and optionally console output) and emits normalized JSON:
- test_cases: {"Class#method": "passed|failed|error|skipped"}
- classes: {"Class": "passed|failed|skipped"}
- summary: counts
"""

import argparse
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

PROJECT_CONFIG = {
    "elasticsearch": {"report_pattern": "**/build/test-results/**/*.xml"},
    "hadoop": {"report_pattern": "build/all-test-results/*.xml"},
    "druid": {"report_pattern": "**/target/surefire-reports/*.xml"},
    "graylog2-server": {"report_pattern": "**/target/surefire-reports/*.xml"},
    "hbase": {"report_pattern": "**/target/surefire-reports/*.xml"},
    "spring-framework": {"report_pattern": "**/build/test-results/**/*.xml"},
    "logstash": {"report_pattern": "**/build/test-results/**/*.xml"},
    "sql": {"report_pattern": "**/build/test-results/**/*.xml"},
    "hibernate-orm": {"report_pattern": "**/target/test-results/**/*.xml"},
    "grpc-java": {"report_pattern": "**/build/test-results/**/*.xml"},
    "crate": {"report_pattern": "**/target/surefire-reports/*.xml"},
    "jdk11u-dev": {"report_pattern": "**/JTwork/**/*.xml"},
    "jdk17u-dev": {"report_pattern": "**/JTwork/**/*.xml"},
    "jdk21u-dev": {"report_pattern": "**/JTwork/**/*.xml"},
    "jdk25u-dev": {"report_pattern": "**/JTwork/**/*.xml"},
}


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text or "")


def discover_xml_files(repo: str, project: str) -> list[str]:
    cfg = PROJECT_CONFIG.get(project, {})
    patterns = []
    if "report_pattern" in cfg:
        patterns.append(cfg["report_pattern"])
    
    # Common fallback patterns
    patterns.extend([
        "**/build/test-results/**/*.xml",
        "**/target/surefire-reports/*.xml",
        "**/target/failsafe-reports/*.xml",
        "build/all-test-results/*.xml",
    ])

    paths: set[str] = set()
    for pattern in patterns:
        full_pattern = os.path.join(repo, pattern)
        paths.update(glob.glob(full_pattern, recursive=True))

    return sorted(p for p in paths if p.endswith(".xml"))


def parse_xml(xml_paths: list[str], target_classes: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    test_cases: dict[str, str] = {}
    class_acc: dict[str, list[str]] = {}

    for xml_path in xml_paths:
        try:
            root = ET.parse(xml_path).getroot()
        except Exception:
            continue

        for case in root.findall(".//testcase"):
            classname = (case.attrib.get("classname") or "").strip()
            name = (case.attrib.get("name") or "").strip()
            if not classname or not name:
                continue
            if target_classes:
                matched = False
                for target in target_classes:
                    if target.endswith(".*"):
                        # Package wildcard: match any class whose fully-qualified
                        # name starts with the prefix (e.g. "org.foo.bar.*" matches
                        # "org.foo.bar.BazTest" and "org.foo.bar.sub.QuxTest").
                        prefix = target[:-2]  # strip trailing ".*"
                        if classname == prefix or classname.startswith(prefix + "."):
                            matched = True
                            break
                    elif (classname == target
                          or classname.endswith("." + target)
                          or classname.startswith(target + "$")):
                        # Also match nested/parameterized classes like
                        # "org.foo.BarTest$InnerCase" when target is "org.foo.BarTest"
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

            test_cases[f"{classname}#{name}"] = status
            class_acc.setdefault(classname, []).append(status)

    classes: dict[str, str] = {}
    for cls, statuses in class_acc.items():
        if any(s in {"failed", "error"} for s in statuses):
            classes[cls] = "failed"
        elif all(s == "skipped" for s in statuses):
            classes[cls] = "skipped"
        else:
            classes[cls] = "passed"

    return test_cases, classes


def parse_console(console_text: str, target_classes: set[str]) -> dict[str, str]:
    # Fallback parser if XML is missing.
    out: dict[str, str] = {}
    clean = strip_ansi(console_text)

    for m in re.finditer(r"^\[INFO\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)[^\n]*-- in ([\w.$-]+)", clean, re.MULTILINE):
        _, failures, errors, skipped, cls = m.groups()
        if target_classes:
            matched = False
            for target in target_classes:
                if target.endswith(".*"):
                    prefix = target[:-2]
                    if cls == prefix or cls.startswith(prefix + "."):
                        matched = True
                        break
                elif cls == target or cls.endswith("." + target):
                    matched = True
                    break
            if not matched:
                continue
        fail = int(failures) + int(errors)
        skip = int(skipped)
        if fail > 0:
            out[cls] = "failed"
        elif skip > 0:
            out[cls] = "skipped"
        else:
            out[cls] = "passed"

    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--target-classes", default="")
    parser.add_argument("--console-file", default="")
    args = parser.parse_args()

    target_classes = {c.strip() for c in (args.target_classes or "").split(",") if c.strip()}
    xml_paths = discover_xml_files(args.repo, args.project)

    test_cases, classes = parse_xml(xml_paths, target_classes)

    if not test_cases and args.console_file and os.path.exists(args.console_file):
        try:
            with open(args.console_file, "r", encoding="utf-8") as f:
                ctext = f.read()
            classes = parse_console(ctext, target_classes)
        except Exception:
            pass

    summary_total = len(test_cases) if test_cases else len(classes)
    if test_cases:
        summary_passed = sum(1 for s in test_cases.values() if s == "passed")
        summary_failed = sum(1 for s in test_cases.values() if s in {"failed", "error"})
        summary_skipped = sum(1 for s in test_cases.values() if s == "skipped")
    else:
        summary_passed = sum(1 for s in classes.values() if s == "passed")
        summary_failed = sum(1 for s in classes.values() if s == "failed")
        summary_skipped = sum(1 for s in classes.values() if s == "skipped")

    payload = {
        "xml_reports": xml_paths,
        "target_classes": sorted(target_classes),
        "test_cases": test_cases,
        "classes": classes,
        "summary": {
            "passed": summary_passed,
            "failed": summary_failed,
            "skipped": summary_skipped,
            "total": summary_total,
        },
    }

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
