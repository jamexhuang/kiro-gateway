# -*- coding: utf-8 -*-
"""
Integration test: Verify Write tool truncation repair works end-to-end.

This file is intentionally ~3KB to test whether the gateway can handle
a Write tool call of this size without truncation.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TruncationTestCase:
    """A single test case for truncation behavior."""
    name: str
    input_size_bytes: int
    expected_repair: bool
    expected_content_preserved_pct: float
    actual_result: Optional[str] = None
    passed: bool = False


@dataclass
class TruncationTestSuite:
    """Collection of truncation test cases with reporting."""
    cases: List[TruncationTestCase] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def add_case(self, name: str, input_size: int, expect_repair: bool, expect_pct: float):
        self.cases.append(TruncationTestCase(
            name=name,
            input_size_bytes=input_size,
            expected_repair=expect_repair,
            expected_content_preserved_pct=expect_pct,
        ))

    def run_all(self):
        self.started_at = time.time()
        for case in self.cases:
            self._run_single(case)
        self.finished_at = time.time()

    def _run_single(self, case: TruncationTestCase):
        """Run a single test case against the repair function."""
        from kiro.parsers import _repair_truncated_json

        # Generate a realistic Write tool payload
        content = "x" * case.input_size_bytes
        full_json = json.dumps({
            "file_path": "/tmp/test_output.py",
            "content": content
        })

        # Simulate truncation at various points
        for cut_pct in [90, 75, 50, 25]:
            cut_pos = int(len(full_json) * cut_pct / 100)
            truncated = full_json[:cut_pos]
            result = _repair_truncated_json(truncated)

            if result:
                parsed = json.loads(result)
                preserved = len(parsed.get("content", ""))
                actual_pct = preserved / len(content) * 100

                if case.expected_repair and actual_pct >= case.expected_content_preserved_pct * 0.8:
                    case.passed = True
                    case.actual_result = f"Cut@{cut_pct}%: preserved {actual_pct:.1f}%"
                    return

        if not case.expected_repair:
            case.passed = True
            case.actual_result = "Correctly not repaired"

    def report(self) -> str:
        """Generate a human-readable report."""
        lines = [
            f"Truncation Test Suite Report",
            f"{'=' * 50}",
            f"Duration: {self.finished_at - self.started_at:.3f}s",
            f"Cases: {len(self.cases)}",
            f"Passed: {sum(1 for c in self.cases if c.passed)}",
            f"Failed: {sum(1 for c in self.cases if not c.passed)}",
            "",
        ]
        for case in self.cases:
            status = "PASS" if case.passed else "FAIL"
            lines.append(f"  [{status}] {case.name}: {case.actual_result or 'No result'}")

        return "\n".join(lines)


def test_small_write():
    """Test repair of a small Write tool call (~500 bytes)."""
    suite = TruncationTestSuite()
    suite.add_case("Small Write (500B)", 500, True, 40.0)
    suite.run_all()
    report = suite.report()
    print(report)
    assert all(c.passed for c in suite.cases), report


def test_medium_write():
    """Test repair of a medium Write tool call (~5KB)."""
    suite = TruncationTestSuite()
    suite.add_case("Medium Write (5KB)", 5000, True, 40.0)
    suite.run_all()
    report = suite.report()
    print(report)
    assert all(c.passed for c in suite.cases), report


def test_large_write():
    """Test repair of a large Write tool call (~50KB)."""
    suite = TruncationTestSuite()
    suite.add_case("Large Write (50KB)", 50000, True, 40.0)
    suite.run_all()
    report = suite.report()
    print(report)
    assert all(c.passed for c in suite.cases), report


def test_130_byte_scenario():
    """
    Test the 130-byte scenario observed in production.
    At 130 bytes, only file_path is preserved, content is empty.
    This should NOT be marked as 'repaired' for recovery purposes.
    """
    from kiro.parsers import _repair_truncated_json

    # Simulate the exact 130-byte scenario
    # The tool_start event contains: {"file_path": "/some/path", "content": ""}
    # with content truncated to nothing
    truncated_130 = '{"file_path": "/Users/jamexhuang/Documents/github/kiro-gateway/tests/some_file.py", "content": "im'
    result = _repair_truncated_json(truncated_130)

    if result:
        parsed = json.loads(result)
        content = parsed.get("content", "")
        print(f"130-byte repair: file_path={parsed.get('file_path')}, content_len={len(content)}")
        # At 130 bytes, content should be nearly empty
        assert len(content) < 10, f"Expected near-empty content at 130 bytes, got {len(content)} chars"
    else:
        print("130-byte scenario: repair returned None (expected for very small payloads)")


if __name__ == "__main__":
    print("Running truncation repair tests...\n")
    test_small_write()
    print()
    test_medium_write()
    print()
    test_large_write()
    print()
    test_130_byte_scenario()
    print("\nAll tests passed!")
