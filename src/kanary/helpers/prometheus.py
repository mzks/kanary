"""PrometheusSource: read a Prometheus /metrics page (the text exposition format)."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..models import PollResult
from ..source import Source, inputs, no_data

_USER_AGENT = "kanary"
_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_SAMPLE = re.compile(rf"^({_NAME})(?:\{{(.*)\}})?\s+(\S+)(?:\s+(-?\d+))?\s*$")
_LABEL = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(?:,|$)')
_SELECTOR = re.compile(rf"^\s*({_NAME})\s*(?:\{{(.*)\}})?\s*$")
_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class MetricSample:
    """One line of a /metrics page: node_load1 1.5, or name{label="value"} 3 1700000000000."""

    name: str
    labels: dict[str, str] = field(default_factory=dict)
    value: float | None = None  # None for NaN
    timestamp: datetime | None = None  # only when the page gives one


def parse_metrics(text: str) -> list[MetricSample]:
    """The samples of a page in the Prometheus text format; comments are skipped."""
    samples = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.match(line)
        if match is None:
            raise ValueError(f"line {number} is not a metric sample: {line[:120]!r}")
        name, labels, value, stamp = match.groups()
        number_value = float(value)
        samples.append(MetricSample(
            name=name,
            labels=_labels(labels or "", line),
            value=None if math.isnan(number_value) else number_value,
            timestamp=datetime.fromtimestamp(int(stamp) / 1000, tz=timezone.utc) if stamp else None,
        ))
    return samples


class PrometheusSource(Source):
    """GET a Prometheus /metrics page and publish the selected series as inputs.

    ``metrics`` maps an input name to a series, written as in Prometheus:
    ``node_load1`` or ``node_filesystem_avail_bytes{mountpoint="/"}`` (labels
    match exactly; those not written may be anything). When a series
    selector matches several series, put the varying label in the name:
    ``"temp.{sensor}": "node_hwmon_temp_celsius"`` gives temp.temp1, temp.temp2.
    A series that is not on the page is listed in metadata["missing"]; NaN has
    no value. Counters (``..._total``) only grow: a RateRule on one gives its
    rate per second.
    """

    url: str | None = None
    headers: dict[str, str] = {}
    timeout: float = 5.0
    metrics: dict[str, str] = {}

    def fetch_text(self) -> str:
        if not self.url:
            raise ValueError(f"{type(self).__name__}.url is not set")
        request = Request(
            str(self.url),
            headers={"Accept": "text/plain;version=0.0.4", "User-Agent": _USER_AGENT, **self.headers},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    def poll(self) -> PollResult:
        if not self.metrics:
            raise ValueError(f"{type(self).__name__}.metrics is empty: map input names to series")
        samples = parse_metrics(self.fetch_text())
        items: list[tuple[str, Any, Any, dict[str, Any]]] = []
        missing: list[str] = []
        for name, selector in self.metrics.items():
            found = select_series(samples, selector)
            if not found:
                missing.append(name)
                continue
            if len(found) > 1 and not _PLACEHOLDER.search(name):
                raise ValueError(
                    f"{selector!r} matches {len(found)} series; add a label to it, or a {{label}} to the "
                    f"input name {name!r}, e.g. {name + '.{' + _varying_label(found) + '}'!r}"
                )
            for sample in found:
                items.append((_input_name(name, sample), sample.value, sample.timestamp, {"labels": sample.labels}))
        metadata: dict[str, Any] = {"url": urlsplit(str(self.url))._replace(query="", fragment="").geturl()}
        if missing:
            metadata["missing"] = missing
        if not items:
            return no_data(reason="none of the series is on the page", metadata=metadata)
        return inputs(items, metadata=metadata)


def select_series(samples: list[MetricSample], selector: str) -> list[MetricSample]:
    """The samples a selector such as name{label="value"} matches."""
    match = _SELECTOR.match(selector)
    if match is None:
        raise ValueError(f"not a series selector: {selector!r} (write name or name{{label=\"value\"}})")
    name, labels = match.group(1), _labels(match.group(2) or "", selector)
    return [
        sample for sample in samples
        if sample.name == name and all(sample.labels.get(key) == value for key, value in labels.items())
    ]


def _labels(text: str, where: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    position = 0
    text = text.strip()
    while position < len(text):
        match = _LABEL.match(text, position)
        if match is None:
            raise ValueError(f"cannot read the labels in {where[:120]!r}")
        labels[match.group(1)] = re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), match.group(2))
        position = match.end()
    return labels


def _input_name(template: str, sample: MetricSample) -> str:
    def fill(match: re.Match[str]) -> str:
        label = match.group(1)
        if label not in sample.labels:
            raise ValueError(f"the input name {template!r} uses {{{label}}}, which {sample.name} does not have")
        return sample.labels[label]

    return _PLACEHOLDER.sub(fill, template)


def _varying_label(samples: list[MetricSample]) -> str:
    keys = sorted({key for sample in samples for key in sample.labels})
    for key in keys:
        if len({sample.labels.get(key) for sample in samples}) > 1:
            return key
    return keys[0] if keys else "label"
