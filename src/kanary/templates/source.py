# kanary-template: source
# kind: source
# level: basic
# summary: Poll an external system on an interval or a cron schedule.
import kanary


@kanary.source(
    source_id="template.source",
    interval=1 * kanary.minute,
    # Or a cron-like schedule instead of interval, in the host's local time
    # ("minute hour day month weekday"):
    # schedule="*/5 * * * *",
    # Shortcuts: "@hourly" (minute 0), "@daily" = "@midnight" (00:00),
    #            "@weekly" (Sunday 00:00), "@monthly" (the 1st, 00:00),
    #            "@yearly" = "@annually" (January 1, 00:00).
)
class TemplateSource:
    description = "TODO: describe what this source reads."

    # Retries after a poll() failure, then terminate() + init() attempts.
    max_retry = 1
    max_reinit = 1

    def init(self):
        # Open connections and clients here, not at module top level.
        # Settings can live next to this file:
        #   config = kanary.load_toml("my_source", filename="config.toml")
        self.client = None

    def poll(self):
        # Always use finite timeouts for HTTP, database, and socket I/O:
        # poll() runs in this source's own thread, but a hang stops this source.
        #
        # Each input is (name, value) or (name, value, timestamp, metadata).
        # Rules select them by source and name, here "template.source:temperature".
        return kanary.inputs(
            ("temperature", 21.5),
            ("humidity", 40.0, None, {"unit": "%"}),
            metadata={"host": "TODO"},
        )

        # Other results:
        # return kanary.no_data(reason="query returned no rows")    # empty snapshot; rules run
        # return kanary.no_update(reason="nothing new since last")  # keep last snapshot; rules run
        # return kanary.skip(reason="maintenance window")           # ignore this poll; rules do not run

    def terminate(self):
        # Close what init() opened.
        self.client = None
