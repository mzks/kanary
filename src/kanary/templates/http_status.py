# kanary-template: http-status
# kind: source
# level: basic
# summary: Request URLs and report the HTTP status code and response time, e.g. API health checks (HTTPStatusSource).
# summary-ja: URL に request し、HTTP status code と応答時間を出す。例: API の health check (HTTPStatusSource)。
import kanary


@kanary.source(source_id="template.http_status", interval=1 * kanary.minute)
class TemplateHttpStatus(kanary.HTTPStatusSource):
    description = "TODO: describe the web services or APIs."

    # Input name -> URL. "web" below produces "web.status" (the HTTP status code, e.g.
    # 200 or 503) and "web.response_ms". When nothing answers at all (DNS failure,
    # refused, timeout), both have no value and the error text is in the metadata.
    # To read values out of a JSON response, use the "http-json" template instead.
    urls = {
        "web": "https://example.org/",  # TODO
        "api": "https://api.example.org/health",
    }
    # Extra request headers; a token from an environment variable, as in the
    # "http-json" template:
    #   headers = {"Authorization": f"Bearer {os.environ.get('API_TOKEN', '')}"}
    timeout = 5.0  # seconds per URL; URLs are requested in parallel


# Alert unless the status is 200, including when nothing answers:
#
#   @kanary.rule(rule_id="template.http_status.not_ok", inputs="template.http_status:*.status",
#                severity=kanary.ERROR, tags=["web"])
#   class NotOk(kanary.ExpectedValueRule):
#       expected = 200        # or [200, 204]
#       missing_fires = True  # no answer fires too
