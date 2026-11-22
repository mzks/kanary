"""Poll results: kanary.inputs() with a helper's result, and test-poll output fed to test-evaluate."""

from datetime import datetime, timezone
import unittest

import kanary

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def source(source_id, result):
    return type(source_id, (kanary.Source,), {"source_id": source_id, "interval": 60.0, "poll": lambda self: result})


class Sum(kanary.Rule):
    rule_id = "sum"
    inputs = ["a:t", "b:t"]
    tags = ["t"]

    def evaluate(self, ctx):
        total = sum(ctx.values())
        return kanary.error(f"sum {total}") if total > 10 else kanary.ok(f"sum {total}")


def start_engine(case, sources, rules):
    engine = kanary.Engine(
        source_registry=sources,
        rule_registry={rule.rule_id: rule for rule in rules},
        output_registry={},
        now_fn=lambda: T0,
    )
    engine.start()
    case.addCleanup(engine.shutdown)
    return engine


class InputsTest(unittest.TestCase):
    def test_adds_to_the_result_of_a_helper(self):
        base = kanary.inputs(("t", 1.0, T0), ("up", 0, T0), metadata={"rows": 1})
        merged = kanary.inputs(base, ("up", 1), timestamp=T0, metadata={"note": "x"})
        self.assertEqual([(item.name, item.value) for item in merged.inputs], [("t", 1.0), ("up", 0), ("up", 1)])
        self.assertEqual((merged.status, merged.metadata), ("ok", {"rows": 1, "note": "x"}))
        engine = start_engine(self, {"a": source("a", merged)}, [])
        snapshot = engine.test_poll("a")
        self.assertEqual(snapshot["inputs"]["a:up"]["value"], 1)  # the later one replaces it

    def test_no_data_adds_nothing_and_no_new_snapshot_is_returned_as_it_is(self):
        merged = kanary.inputs(kanary.no_data(reason="no rows"), ("up", 1))
        self.assertEqual(([item.name for item in merged.inputs], merged.status), (["up"], "ok"))
        for result in (kanary.no_update(), kanary.skip(reason="filling")):
            self.assertIs(kanary.inputs(result, ("up", 1)), result)

    def test_samples_of_a_buffer_are_inputs(self):
        buffer = kanary.Buffer()
        buffer.add(("p", 1.5, T0))
        result = kanary.inputs(buffer["p"].last)
        self.assertEqual([(item.name, item.value, item.timestamp) for item in result.inputs], [("p", 1.5, T0)])
        with self.assertRaisesRegex(ValueError, r"an input must be a \(name, value, \.\.\.\) tuple"):
            kanary.inputs([1.5])

    def test_the_types_are_not_public(self):
        self.assertFalse(hasattr(kanary, "SourceResult"))
        self.assertFalse(hasattr(kanary, "Measurement"))


class TestPollToTestEvaluateTest(unittest.TestCase):
    def setUp(self):
        self.engine = start_engine(
            self,
            {"a": source("a", kanary.inputs(("t", 4.0, T0), metadata={"rows": 1})), "b": source("b", kanary.no_data(reason="no rows"))},
            [Sum, type("A", (kanary.RangeRule,), {"rule_id": "a.t", "inputs": "a:t", "tags": ["t"], "high": 5.0})],
        )

    def test_prints_source_input_names(self):
        self.assertEqual(self.engine.test_poll("a"), {
            "source_id": "a",
            "status": "ok",
            "metadata": {"rows": 1},
            "inputs": {"a:t": {"value": 4.0, "timestamp": T0, "metadata": {}}},
        })
        self.assertEqual(self.engine.test_poll("b"), {"source_id": "b", "status": "empty", "reason": "no rows", "inputs": {}})

    def test_the_output_of_two_sources_together_tests_a_rule_that_reads_both(self):
        self.engine.sources["b"].poll = lambda: kanary.inputs(("t", 7.0, T0))
        a, b = self.engine.test_poll("a"), self.engine.test_poll("b")
        result = self.engine.test_evaluate("sum", {"inputs": {**a["inputs"], **b["inputs"]}})
        self.assertEqual((result["state"], result["message"]), ("FIRING", "sum 11.0"))

    def test_an_empty_poll_is_an_empty_snapshot_of_its_source(self):
        self.engine.evaluate_source("b", kanary.inputs(("t", 9.0, T0)), now=T0)
        result = self.engine.test_evaluate("sum", {**self.engine.test_poll("b"), "inputs": {"a:t": {"value": 4.0}}})
        self.assertEqual((result["state"], result["message"]), ("OK", "sum 4.0"))  # b:t is gone

    def test_names_without_a_source(self):
        # An alert's payload has the source's own names; fine for a rule that reads one source.
        alert = self.engine.evaluate_source("a", kanary.inputs(("t", 6.0, T0)), now=T0)["a.t"]
        self.assertEqual(sorted(alert.payload["inputs"]), ["t"])
        self.assertEqual(self.engine.test_evaluate("a.t", alert.payload)["state"], "FIRING")
        with self.assertRaisesRegex(ValueError, r"input 't' needs its source: write '<source_id>:t' \(rule 'sum' reads a, b\)"):
            self.engine.test_evaluate("sum", {"inputs": {"t": {"value": 1}}})

    def test_the_channels_of_before_0_8_are_read(self):
        result = self.engine.test_evaluate("a.t", {"channels": {"t": {"value": 6.0}}, "status": "ok"})
        self.assertEqual(result["state"], "FIRING")


if __name__ == "__main__":
    unittest.main()
