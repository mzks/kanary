# kanary-template: prometheus
# kind: source
# level: basic
# summary: Read series from a Prometheus /metrics page, e.g. node_exporter or an application's metrics (PrometheusSource).
import kanary


@kanary.source(source_id="template.prometheus", interval=30 * kanary.second)
class TemplatePrometheus(kanary.PrometheusSource):
    description = "TODO: describe the exporter."

    url = "http://localhost:9100/metrics"  # TODO (9100: node_exporter)
    timeout = 5.0
    # A token, when the page needs one (from an environment variable; import os):
    # headers = {"Authorization": f"Bearer {os.environ.get('METRICS_TOKEN', '')}"}

    # Input name -> series, written as in Prometheus. The labels written must match
    # exactly; the others may be anything. When a series selector matches several series, name
    # the label that varies: {sensor} below becomes each series' sensor label
    # (temp.temp1, temp.temp2, ...).
    metrics = {
        "load1": "node_load1",
        "root_free_bytes": 'node_filesystem_avail_bytes{mountpoint="/"}',
        "temp.{sensor}": 'node_hwmon_temp_celsius{chip="platform_coretemp_0"}',
        "requests_5xx": 'http_requests_total{code="500"}',
    }
    # A series not on the page is listed in the result's metadata["missing"]; NaN
    # has no value. The page itself not answering is a poll failure.


# A counter (..._total) only grows; a RateRule gives its rate per second:
#
#   @kanary.rule(rule_id="template.prometheus.errors", inputs="template.prometheus:requests_5xx",
#                severity=kanary.WARN, tags=["web"])
#   class ServerErrors(kanary.RateRule):
#       high = 5 / kanary.minute
#
# Less than 10 GiB free: a RangeRule with low = 10 * 1024**3 on root_free_bytes.
