# kanary-template: expected
# kind: rule
# level: basic
# summary: Fire when a value is not the expected one, e.g. a status code, a 0/1 flag, or a state (ExpectedValueRule).
import kanary


@kanary.rule(
    rule_id="template.expected",
    # With a glob such as "example.source:*.status", each input is checked independently.
    inputs="example.source:pump_on",  # TODO
    severity=kanary.WARN,
    tags=["TODO"],
    # owner="detector-team",
)
class TemplateExpected(kanary.ExpectedValueRule):
    description = "TODO: describe the expected value."

    # One value or a list of allowed values. Numbers compare by value (1 == 1.0 == True),
    # and text values such as "RUNNING" work too.
    expected = 1

    # "different" (default): any other value fires.
    # "equal":               the listed values fire instead (e.g. error codes).
    fire_when = "different"

    # True: an input without a value (e.g. a probe that got no answer) fires too.
    missing_fires = False
