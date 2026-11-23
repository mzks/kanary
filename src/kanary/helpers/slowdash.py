"""SlowDashSource: read the newest point of SlowDash channels (GET /api/data)."""

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import math
import re
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from ..models import PollResult
from ..source import Source, inputs
from ..units import minute
from ..values import to_number

_USER_AGENT = "kanary"
UP_INPUT = "slowdash.up"
MISSING_INPUT = "slowdash.missing"


class SlowDashSource(Source):
    """Read the newest point of SlowDash channels as inputs, each at its own time.

    ``url`` is the SlowDash address as opened in the browser
    ("http://localhost:18881"; a page such as .../slowplot.html?config=... works
    too). ``channels`` maps an input name to a channel, or lists channels, each
    named after itself. A channel pattern ("oil.*", from /api/channels) takes
    every matching channel: listed, each is named after itself; in an input
    name, ``*`` is what ``*`` matched ({"hv.*": "pmt.hv.*"}: pmt.hv.17 is hv.17).

    The input ``slowdash.up`` is 1 when SlowDash answers /api/ping, else 0 with
    the reason in its metadata["error"] and no channel inputs: no answer is a
    value for a rule, not a failure of this source.

    Each poll asks for the last ``lookback`` seconds of every channel in one
    request and takes each channel's newest point; the input's timestamp is that
    point's time, so a StaleRule fires when a channel stops updating. A channel
    with no point in that time gets its last point before from SlowDash
    (``prior_data=1``: the SQL data sources of SlowDash 2026-02 and later);
    otherwise it has no input. The input ``slowdash.missing`` counts the entries
    of ``channels`` without a point (a typo, a channel gone, or no point in
    ``lookback`` there) and a pattern that matches nothing; their names are in
    its metadata["missing"] and in the result's. A rule on ``*`` cannot see an
    input that is not there, so check slowdash.missing for them.
    Numbers and states such as "ON" are converted as kanary.to_number()
    reads them, other text is kept as it is, and objects (histograms, tables,
    trees) are listed in metadata["objects"].
    """

    url: str | None = None
    headers: dict[str, str] = {}
    timeout: float = 5.0
    channels: dict[str, str] | list[str] = {}
    lookback: float = 10 * minute

    def api_url(self) -> str:
        """The address the API is under: url without a page, query, or a trailing /api."""
        if not self.url:
            raise ValueError(f"{type(self).__name__}.url is not set")
        parts = urlsplit(str(self.url).strip())
        if not parts.scheme or not parts.netloc:
            raise ValueError(f"{type(self).__name__}.url must be like 'http://host:18881', not {self.url!r}")
        segments = [segment for segment in parts.path.split("/") if segment]
        if segments and segments[-1].lower().endswith((".html", ".htm")):
            segments.pop()
        if segments and segments[-1] == "api":
            segments.pop()
        return f"{parts.scheme}://{parts.netloc}" + "".join("/" + segment for segment in segments)

    def channel_map(self) -> dict[str, str]:
        """{input name: channel or pattern} from ``channels``: a mapping, a list, or "ch0,ch1"."""
        channels: Any = self.channels
        if isinstance(channels, str):
            channels = channels.split(",")
        if isinstance(channels, Mapping):
            mapping = {str(name).strip(): channel for name, channel in channels.items()}
        else:
            mapping = {str(channel).strip(): channel for channel in channels if str(channel).strip()}
        if not mapping:
            raise ValueError(f"{type(self).__name__}.channels is empty: list channels or map input names to channels")
        for name, channel in mapping.items():
            if not isinstance(channel, str) or not channel.strip() or "," in channel:
                raise ValueError(f"{type(self).__name__}.channels[{name!r}] must be one channel name, not {channel!r}")
            mapping[name] = channel.strip()
        return mapping

    def ping(self) -> str | None:
        """None when SlowDash answers GET /api/ping, else why it does not."""
        request = Request(f"{self.api_url()}/api/ping", headers={"User-Agent": _USER_AGENT, **self.headers})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read(200).decode("utf-8", errors="replace")
        except Exception as exc:  # refused, timed out, an HTTP error from a proxy, ...
            return f"{type(exc).__name__}: {exc}"
        if "pong" not in body:
            return f"/api/ping answered {body.strip()[:80]!r}, not 'pong'"
        return None

    def fetch_channels(self) -> list[str]:
        """Every channel SlowDash lists (GET /api/channels), sorted."""
        document = self._get_json("/api/channels")
        if not isinstance(document, list):
            raise ValueError(f"SlowDash /api/channels answered {type(document).__name__}, not a list")
        names = (entry.get("name") if isinstance(entry, Mapping) else entry for entry in document)
        return sorted({name for name in names if isinstance(name, str) and name})

    def resolve_channels(self, mapping: dict[str, str]) -> tuple[dict[str, str], list[str]]:
        """{input name: channel} with the patterns expanded, and the patterns that match nothing."""
        available = self.fetch_channels() if any(_is_pattern(channel) for channel in mapping.values()) else []
        resolved: dict[str, str] = {}
        unmatched: list[str] = []
        for name, channel in mapping.items():
            found = _expand(name, channel, available) if _is_pattern(channel) else [(name, channel)]
            if not found:
                unmatched.append(name)
            for input_name, matched in found:
                if input_name in (UP_INPUT, MISSING_INPUT):
                    raise ValueError(f"{input_name!r} is an input of SlowDashSource itself; name {matched!r} otherwise")
                if resolved.setdefault(input_name, matched) != matched:
                    raise ValueError(f"the input {input_name!r} would be both {resolved[input_name]!r} and {matched!r}")
        return resolved, unmatched

    def fetch_data(self, channels: list[str]) -> dict[str, Any]:
        """The /api/data answer for these channels: {channel: {"start", "t", "x"}}."""
        if not isinstance(self.lookback, (int, float)) or self.lookback <= 0:
            raise ValueError(f"{type(self).__name__}.lookback must be a positive number of seconds")
        path = ",".join(quote(channel, safe="/:@") for channel in channels)
        query = urlencode({"length": f"{self.lookback:g}", "to": "0", "prior_data": "1"})
        document = self._get_json(f"/api/data/{path}?{query}")
        if not isinstance(document, dict):
            raise ValueError(f"SlowDash /api/data answered {type(document).__name__}, not channel data")
        return document

    def poll(self) -> PollResult:
        mapping = self.channel_map()
        metadata: dict[str, Any] = {"url": self.api_url()}
        error = self.ping()
        if error is not None:
            return inputs([(UP_INPUT, 0, None, {"error": error})], metadata=metadata)
        resolved, missing = self.resolve_channels(mapping)
        document = self.fetch_data(list(dict.fromkeys(resolved.values()))) if resolved else {}
        items: list[tuple[Any, ...]] = [(UP_INPUT, 1)]
        objects: list[str] = []
        for name, channel in resolved.items():
            point = newest_point(document.get(channel))
            if point is None:
                missing.append(name)
                continue
            value, timestamp = point
            if isinstance(value, (dict, list)):
                objects.append(name)
                continue
            items.append((name, _scalar(value), timestamp, {"channel": channel}))
        items.insert(1, (MISSING_INPUT, len(missing), None, {"missing": missing}))
        if missing:
            metadata["missing"] = missing
        if objects:
            metadata["objects"] = objects
        return inputs(items, metadata=metadata)

    def _get_json(self, path: str) -> Any:
        request = Request(
            self.api_url() + path,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT, **self.headers},
        )
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)


def newest_point(data: Any) -> tuple[Any, datetime | None] | None:
    """(value, time) of the newest point of one channel's data, or None when it has none.

    SlowDash gives {"start": unix time, "t": [seconds from start, ...], "x": [value, ...]},
    or a single "t" and "x" for a reduced or object value.
    """
    if not isinstance(data, Mapping) or "x" not in data:
        return None
    t, x = data.get("t"), data["x"]
    if isinstance(x, list):
        if not x:
            return None
        if isinstance(t, list) and len(t) == len(x) and all(_is_number(offset) for offset in t):
            index = max(range(len(x)), key=lambda k: (t[k], k))
            return x[index], _time(data.get("start"), t[index])
        return x[-1], None
    return x, _time(data.get("start"), t)


def _expand(name: str, pattern: str, channels: list[str]) -> list[tuple[str, str]]:
    """(input name, channel) for each channel the pattern matches."""
    regex = _glob_regex(pattern)
    matched = [match for channel in channels if (match := regex.fullmatch(channel))]
    if name == pattern:  # listed: each channel is named after itself
        return [(match.string, match.string) for match in matched]
    stars = name.count("*")
    if stars == 0:
        if len(matched) > 1:
            shown = ", ".join(match.string for match in matched[:3]) + (", ..." if len(matched) > 3 else "")
            raise ValueError(
                f"{pattern!r} matches {len(matched)} channels ({shown}); write * in the input name {name!r} "
                f"for what * matched, or list the pattern to name each channel after itself"
            )
        return [(name, match.string) for match in matched]
    if stars != regex.groups:
        raise ValueError(f"the input name {name!r} has {stars} *, and the channel pattern {pattern!r} {regex.groups}")
    found = []
    for match in matched:
        parts = iter(match.groups())
        found.append((re.sub(r"\*", lambda _: next(parts), name), match.string))
    return found


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """A pattern as fnmatch reads it (*, ?, [...]), with a group for each *."""
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        end = pattern.find("]", index + 2) if char == "[" else -1
        if char == "*":
            out.append("(.*)")
        elif char == "?":
            out.append(".")
        elif end != -1:
            body = pattern[index + 1:end]
            body = "^" + body[1:] if body.startswith("!") else body
            out.append("[" + body.replace("\\", "\\\\") + "]")
            index = end
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out), re.DOTALL)


def _is_pattern(channel: str) -> bool:
    return any(mark in channel for mark in "*?[")


def _time(start: Any, offset: Any) -> datetime | None:
    if not _is_number(start) or not _is_number(offset):
        return None
    return datetime.fromtimestamp(start + offset, tz=timezone.utc)


def _scalar(value: Any) -> Any:
    if value is None:
        return None
    number = to_number(value)
    if number is None:
        return value  # text such as "RUN", for an ExpectedValueRule
    return None if math.isnan(number) else number


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
