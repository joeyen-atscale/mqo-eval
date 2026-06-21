"""Tests for the rule-violation harness (R-MS and R-FQ deterministic checks)."""
from __future__ import annotations

import pytest

from mqo_eval.rule_harness import RuleViolationHarness, _has_multiple_statements, _strip_string_literals


# ── Unit: string literal stripping ──────────────────────────────────────────

def test_strip_preserves_length():
    sql = "SELECT 'hello;world' FROM t"
    stripped = _strip_string_literals(sql)
    # Length must be the same so positional analysis is not disrupted.
    assert len(stripped) == len(sql)


def test_strip_removes_semicolon_in_literal():
    sql = "WHERE col = 'foo;bar'"
    stripped = _strip_string_literals(sql)
    # The interior of the literal should not contain a semicolon.
    assert ";" not in stripped[stripped.index("'") + 1 : stripped.rindex("'")]


def test_strip_handles_escaped_quote():
    sql = "WHERE col = 'it''s fine;still'"
    stripped = _strip_string_literals(sql)
    assert ";" not in stripped.replace("'", "")


# ── Unit: multi-statement detection ─────────────────────────────────────────

def test_single_statement_no_flag():
    assert not _has_multiple_statements(
        "SELECT c_customer_sk FROM tpcds.public.customer WHERE c_birth_year > 1980"
    )


def test_two_statements_flagged():
    assert _has_multiple_statements(
        "SELECT * FROM tpcds.public.customer; SELECT * FROM tpcds.public.store"
    )


def test_three_statements_flagged():
    assert _has_multiple_statements("SELECT 1; SELECT 2; SELECT 3")


def test_trailing_semicolon_not_flagged():
    # A single statement with a trailing semicolon has no second statement.
    assert not _has_multiple_statements(
        "SELECT c_customer_sk FROM tpcds.public.customer;"
    )


def test_semicolon_in_string_literal_not_flagged():
    sql = "SELECT c_email_address FROM tpcds.public.customer WHERE c_email_address LIKE '%;%'"
    assert not _has_multiple_statements(sql)


def test_multiline_two_statements():
    sql = (
        "SELECT c_customer_sk, c_first_name\n"
        "FROM tpcds.public.customer\n"
        "WHERE c_birth_year > 1980;\n"
        "SELECT COUNT(*) FROM tpcds.public.store_sales"
    )
    assert _has_multiple_statements(sql)


# ── Integration: RuleViolationHarness R-MS ──────────────────────────────────

class TestRuleMS:
    def setup_method(self):
        self.harness = RuleViolationHarness()

    def _check(self, sql: str):
        return self.harness.check_rule_violations(sql, "R-MS")

    def test_multi_statement_is_fail(self):
        result = self._check("SELECT 1; SELECT 2")
        assert result.verdict == "fail"
        assert result.rule_id == "R-MS"
        assert not result.lm_driven

    def test_single_select_is_pass(self):
        result = self._check(
            "SELECT ss_store_sk, SUM(ss_net_paid) AS total "
            "FROM tpcds.public.store_sales "
            "GROUP BY ss_store_sk"
        )
        assert result.verdict == "pass"
        assert not result.lm_driven

    def test_semicolon_in_string_is_pass(self):
        result = self._check(
            "SELECT c_email_address FROM tpcds.public.customer "
            "WHERE c_email_address LIKE '%;%'"
        )
        assert result.verdict == "pass"

    def test_three_statements_is_fail(self):
        result = self._check("SELECT 1; SELECT 2; SELECT 3")
        assert result.verdict == "fail"

    def test_multiline_multi_statement_is_fail(self):
        sql = (
            "SELECT c_customer_sk\nFROM tpcds.public.customer;\n"
            "SELECT COUNT(*) FROM tpcds.public.store_sales"
        )
        result = self._check(sql)
        assert result.verdict == "fail"


# ── Integration: RuleViolationHarness R-FQ ──────────────────────────────────

class TestRuleFQ:
    def setup_method(self):
        self.harness = RuleViolationHarness()

    def _check(self, sql: str):
        return self.harness.check_rule_violations(sql, "R-FQ")

    def test_bare_table_is_fail(self):
        result = self._check("SELECT * FROM customer WHERE c_birth_year > 1980")
        assert result.verdict == "fail"

    def test_two_part_name_is_fail(self):
        result = self._check("SELECT ss_net_paid FROM public.store_sales")
        assert result.verdict == "fail"

    def test_fully_qualified_is_pass(self):
        result = self._check("SELECT c_customer_sk FROM tpcds.public.customer")
        assert result.verdict == "pass"

    def test_fully_qualified_multi_line_is_pass(self):
        sql = (
            "SELECT ss_store_sk, SUM(ss_net_paid) AS total_paid\n"
            "FROM tpcds.public.store_sales\n"
            "GROUP BY ss_store_sk"
        )
        result = self._check(sql)
        assert result.verdict == "pass"


# ── Integration: unknown rule → lm_driven sentinel ──────────────────────────

class TestUnknownRule:
    def setup_method(self):
        self.harness = RuleViolationHarness()

    def test_unknown_rule_returns_skip(self):
        result = self.harness.check_rule_violations(
            "SELECT c_customer_sk FROM tpcds.public.customer", "R-NL"
        )
        assert result.verdict == "skip"
        assert result.lm_driven is True


# ── Integration: check_all ──────────────────────────────────────────────────

class TestCheckAll:
    def setup_method(self):
        self.harness = RuleViolationHarness()

    def test_check_all_returns_one_per_rule(self):
        results = self.harness.check_all(
            "SELECT 1; SELECT 2",
            ["R-MS", "R-FQ"],
        )
        assert len(results) == 2
        assert results[0].rule_id == "R-MS"
        assert results[1].rule_id == "R-FQ"
