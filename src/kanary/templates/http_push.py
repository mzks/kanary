# kanary-template: http-push
# kind: source
# level: basic
# summary: Receive values over HTTP POST and evaluate them as soon as they arrive (HTTPPushSource).
import os

import kanary


# No interval or schedule here: HTTPPushSource runs with interval = 0, so poll() waits
# for values and the rules run as soon as one arrives. Any other makes init() fail.
@kanary.source(source_id="template.http_push")
class TemplateHttpPush(kanary.HTTPPushSource):
    description = "TODO: describe who sends values here."

    host = "127.0.0.1"  # "0.0.0.0" accepts from other hosts
    port = 8911
    path = "/"
    # With a token, senders need "Authorization: Bearer <token>". Keep it out of this
    # file, e.g. in an environment variable; None accepts everyone who can connect.
    token = os.environ.get("KANARY_PUSH_TOKEN")

    # Senders POST a JSON object of input name -> value:
    #   curl -X POST http://127.0.0.1:8911/ -H "Authorization: Bearer $KANARY_PUSH_TOKEN" \
    #        -d '{"temperature": 21.5, "humidity": 40}'
    # A value may carry its own time (unix time or ISO 8601):
    #   {"temperature": {"value": 21.5, "timestamp": "2026-11-20T10:00:00+09:00"}}
    # Values posted together are evaluated together; a burst is evaluated once, with
    # the newest value of each input. Inputs not in a request keep their last value.
    # The server is http.server: fine on a trusted network; from the internet, put a
    # reverse proxy with TLS in front.


# Nothing arriving is not an error for this source; a stale rule catches it:
#
#   @kanary.rule(rule_id="template.http_push.stale", inputs="template.http_push:*",
#                severity=kanary.WARN, evaluation_interval=30 * kanary.second)
#   class PushStale(kanary.StaleRule):
#       timeout = 5 * kanary.minute
