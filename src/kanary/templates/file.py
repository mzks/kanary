# kanary-template: file
# kind: source
# level: basic
# summary: Watch files or glob patterns: exists, age since last write, size, count, last-line number (FileSource).
import kanary


@kanary.source(source_id="template.file", interval=1 * kanary.minute)
class TemplateFile(kanary.FileSource):
    description = "TODO: describe the files and who writes them."

    # Input name -> path or glob pattern (relative paths are next to this plugin file).
    # A path, such as "backup" below, produces:
    #   backup.exists       1 or 0
    #   backup.age_seconds  seconds since the last modification
    #   backup.size_bytes
    #   backup.value        the last line as a number (only with read_last_line)
    # A pattern such as "runs" ("/data/run_*.root", or "/data/**/*.root" for subfolders)
    # produces the same for the newest matching file, plus runs.count and
    # runs.total_size_bytes.
    paths = {
        "backup": "/var/backups/daily.tar.gz",   # TODO
        "runs": "/data/runs/*.root",
        "level": "/run/sensors/level.txt",
    }
    # A plain list also works; names come from the file names ("daily.tar.gz") or the
    # fixed part of a pattern ("*.root" -> "root"):
    #   paths = ["/var/backups/daily.tar.gz", "/data/runs/*.root"]
    read_last_line = True


# "The backup has not been written for 26 hours", "no new run file for 2 hours":
#
#   @kanary.rule(rule_id="template.file.backup.old", inputs="template.file:backup.age_seconds",
#                severity=kanary.ERROR, tags=["backup"])
#   class BackupTooOld(kanary.RangeRule):
#       high = 26 * kanary.hour
#
#   @kanary.rule(rule_id="template.file.runs.old", inputs="template.file:runs.age_seconds",
#                severity=kanary.WARN, tags=["daq"])
#   class NoNewRun(kanary.RangeRule):
#       high = 2 * kanary.hour
#       missing_fires = True  # no matching file at all fires too
