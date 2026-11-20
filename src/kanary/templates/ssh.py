# kanary-template: ssh
# kind: source
# level: advanced
# summary: Run a command on a remote host over ssh, e.g. disk, load, or GPU status (SSHCommandSource).
import kanary


@kanary.source(source_id="template.ssh", interval=5 * kanary.minute)
class TemplateSsh(kanary.SSHCommandSource):
    description = "TODO: describe the remote host and what is measured."

    host = "daq01.example.org"  # TODO
    # user = "monitor"
    # port = 22
    # identity_file = "~/.ssh/kanary_monitor"   # a key without a passphrase, or an ssh-agent
    connect_timeout = 5.0
    # ssh_options = ["-o", "StrictHostKeyChecking=yes"]
    #
    # ssh runs with BatchMode=yes, so it never prompts. Exit code 255 (ssh itself
    # failed: unreachable, auth) is a poll failure, not the command's exit code.

    # Run on the remote host. A string goes to the remote shell as-is, so pipes work.
    # Print "name=value" pairs and they become inputs.
    command = (
        "printf 'load1=%s ' $(cut -d' ' -f1 /proc/loadavg); "
        "df -P / | awk 'NR==2 {print \"root_used_percent=\" $5}'"
    )
    timeout = 20.0  # covers connecting plus running
    output_format = "keyvalue"
