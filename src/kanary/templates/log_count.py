# kanary-template: log-count
# kind: source
# level: advanced
# summary: Count new log lines matching regular expressions since the last poll (LogCountSource).
# summary-ja: 前回の poll 以降に増えた log の行のうち、正規表現に一致するものを数える (LogCountSource)。
import kanary


@kanary.source(source_id="template.log_count", interval=1 * kanary.minute)
class TemplateLogCount(kanary.LogCountSource):
    description = "TODO: describe the log and what the patterns mean."

    path = "/var/log/daq/run.log"  # TODO
    # Input name -> regular expression (Python re, searched anywhere in the line).
    # Each poll reports how many new lines matched, so `interval` is the counting window.
    patterns = {
        "errors": r"\bERROR\b",
        "timeouts": r"timed? ?out",
    }
    # True: ignore what is already in the file at startup.
    start_at_end = True
    # Rotation (new file, or truncated) restarts from the top of the new file.
    # The last matching line is kept in each input's metadata.


# "More than 10 errors per minute":
#
#   @kanary.rule(rule_id="template.log_count.errors", inputs="template.log_count:errors",
#                severity=kanary.WARN, tags=["daq"])
#   class TooManyErrors(kanary.RangeRule):
#       high = 10
