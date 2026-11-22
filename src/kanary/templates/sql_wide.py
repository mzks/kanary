# kanary-template: sql-wide
# kind: source
# level: basic
# summary: Read the newest row of a wide table (one column per quantity) from SQL, from settings only (SQLWideSource).
# summary-ja: wide 形式（量ごとに column）の table から最新の行を SQL で読む (SQLWideSource)。
import kanary


@kanary.source(source_id="template.sql_wide", interval=30 * kanary.second)
class TemplateSqlWide(kanary.SQLWideSource):
    description = "TODO: describe the table."

    # Connection. "sqlite" and "postgresql" (psycopg comes with KANARY) need nothing
    # extra; "mysql" (also MariaDB) needs the pymysql package in the Python environment
    # that runs KANARY (docs/deployment.md, "When A Plugin Needs Additional Python Modules").
    database_type = "sqlite"
    database = "environment.db"  # sqlite: file path (relative = next to this plugin); others: database name
    # host = "db.example.org"
    # port = 5432                  # default 5432 (postgresql) / 3306 (mysql)
    # user = "kanary"
    # The password is a secret: keep it out of this file, for example in an
    # environment variable (import os), or in secrets.json next to this file
    # (chmod 600, not in git) as {"db_password": "..."}:
    # password = os.environ.get("DB_PASSWORD")
    # password = kanary.load_json("db_password", filename="secrets.json")
    timeout = 5.0                  # connect and statement timeout, seconds

    # A wide table: one row per time, one column per quantity (for name/value rows, use
    # the "sql-long" template; docs/plugins.md shows both shapes). The newest row is read:
    #   SELECT observed_at, temperature, humidity, pump_state FROM environment
    #   ORDER BY observed_at DESC LIMIT 1
    table = "environment"
    timestamp_column = "observed_at"  # each input gets this row's time
    # Zone of timestamps stored without one: an IANA name ("Asia/Tokyo"), an offset
    # ("+09:00", "+9"), or "UTC". The default is this host's local time.
    # timestamp_timezone = "UTC"

    # Input name -> column. Leave empty to use every column of the table (except the
    # timestamp) under its own name. Non-numeric values go to metadata["text"].
    columns = {
        "temperature": "temperature",
        "humidity": "humidity",
        "pump_on": "pump_state",
    }

    # To read anything else (a join, a view, a WHERE clause), write the SQL yourself.
    # When query is set, table and columns are not used. Use the driver's parameter
    # style for params (sqlite: ?, postgresql / mysql: %s), and put the newest row first.
    #   query = """
    #       SELECT observed_at, temperature, humidity, pump_state
    #       FROM environment WHERE station = ?
    #       ORDER BY observed_at DESC LIMIT 1
    #   """
    #   params = ("north",)
    #
    # For another driver or connection option, override connect() and return any
    # DB-API connection (with timeouts and autocommit). A failing poll is retried,
    # then terminate() + init() reconnect.
