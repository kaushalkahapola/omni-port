import pytest
from src.core.state import (
    PatchClassification,
    PatchType,
    LocalizationResult,
    PatchRetryContext,
)


def test_patch_classification():
    classification = PatchClassification(
        patch_type=PatchType.TYPE_III,
        confidence=0.85,
        token_budget_estimate=15000,
        reasoning="Method rename detected",
        is_auto_generated=False,
    )
    assert classification.patch_type == "TYPE_III"
    assert classification.confidence == 0.85
    assert classification.token_budget_estimate == 15000
    assert not classification.is_auto_generated


def test_localization_result():
    result = LocalizationResult(
        method_used="javaparser",
        confidence=0.9,
        context_snapshot="public void test() {}",
        symbol_mappings={"Old.class": "New.class"},
        file_path="src/main/Test.java",
        start_line=10,
        end_line=15,
    )
    assert result.method_used == "javaparser"
    assert result.symbol_mappings["Old.class"] == "New.class"
    assert result.start_line == 10


def test_patch_retry_context():
    context = PatchRetryContext(
        error_type="compile_error_missing_import",
        error_message="cannot find symbol import X",
        attempt_count=2,
        suggested_action="use NamespaceAdapter",
    )
    assert context.error_type == "compile_error_missing_import"
    assert context.attempt_count == 2
