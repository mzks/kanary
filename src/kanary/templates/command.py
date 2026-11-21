# kanary-template: command
# kind: source
# level: basic
# summary: Run a local command and read numbers from its output (CommandSource).
# summary-ja: 手元の command を実行し、出力から数値を読む (CommandSource)。
import kanary


@kanary.source(source_id="template.command", interval=1 * kanary.minute)
class TemplateCommand(kanary.CommandSource):
    description = "TODO: describe what the command measures."

    # A list is run as-is; a string is split like a shell would, but no shell is used
    # (no pipes or globs). For a pipeline, run ["sh", "-c", "... | ..."] explicitly.
    command = ["df", "-P", "/"]  # TODO
    timeout = 10.0  # seconds; a timeout counts as a poll failure (retry, then reinit)

    # How stdout becomes inputs (all values are converted to numbers):
    #   "number"   "42"                  -> value=42.0            (name: value_name)
    #   "keyvalue" "load=0.8 users=3"    -> load=0.8, users=3.0
    #   "json"     '{"a": {"b": 1}}'     -> a.b=1.0
    #   "auto"     picks one of the above from the output's shape
    # "85%" becomes 85.0, true/false become 1.0/0.0. Anything else that is not a number
    # is kept as text in the result metadata rather than as an input.
    output_format = "auto"
    value_name = "value"

    # The exit code is reported as the input "exit_code"; alert on it with a RangeRule
    # (high = 0). Set False to leave it out.
    report_exit_code = True

    # env = {"LC_ALL": "C"}  # added to the inherited environment
    # cwd = "/tmp"

    def parse(self, stdout):
        # Override when the output needs custom parsing; return (numbers, texts).
        # Here: `df -P /` prints a header, then "fs blocks used avail 42% /".
        lines = stdout.strip().splitlines()
        fields = lines[-1].split() if lines else []
        if len(fields) < 5:
            raise ValueError(f"unexpected df output: {stdout!r}")  # a poll failure when exit_code == 0
        return {"used_percent": kanary.to_number(fields[4])}, {}
