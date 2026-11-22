# kanary-template: sql-long
# kind: source
# level: basic
# summary: Read the newest name/value row of every name in a long table from SQL, from settings only (SQLLongSource).
# summary-ja: long 形式の table から、名前ごとに最新の名前/値の行を SQL で読む (SQLLongSource)。
import kanary


@kanary.source(source_id="template.sql_long", interval=30 * kanary.second)
class TemplateSqlLong(kanary.SQLLongSource):
    description = "TODO: describe the table."

    # Connection: see the sql-wide template for postgresql / mysql settings.
    database_type = "sqlite"
    database = "samples.db"  # sqlite: file path (relative = next to this plugin); others: database name
    timeout = 5.0

    # A long table: one row per quantity and time, with a name and a value column (for
    # one column per quantity, use the "sql-wide" template; docs/plugins.md shows both
    # shapes). The newest row of every name is read.
    table = "samples"
    name_column = "metric_name"
    value_column = "metric_value"
    timestamp_column = "observed_at"
    # Zone of timestamps stored without one: an IANA name ("Asia/Tokyo"), an offset
    # ("+09:00", "+9"), or "UTC". The default is this host's local time.
    # timestamp_timezone = "UTC"
    # Only scan rows of the last 10 minutes: keeps the query cheap on a large table.
    lookback = 10 * kanary.minute

    # Upstream name -> input name. When set, other names are ignored; when empty,
    # every name is used as-is. The upstream name is kept in each input's metadata.
    names = {
        "plant.room_a.temperature_c": "room_a.temperature",
        "plant.room_b.temperature_c": "room_b.temperature",
    }

    # To write the SQL yourself, set query (table and the column settings are then
    # not used). Return name, value, and timestamp columns, newest first; the first
    # row per name wins.
    #   query = """
    #       SELECT observed_at, metric_name, metric_value FROM samples
    #       WHERE observed_at > datetime('now', '-10 minutes')
    #       ORDER BY observed_at DESC
    #   """
