# kanary-template: push
# kind: source
# level: advanced
# summary: Evaluate values as soon as your own listener receives them (a device callback, a socket): kanary.Inbox.
import socket
import threading

import kanary


@kanary.source(
    source_id="template.push",
    # 0: call poll() again as soon as it returns. Only for a poll() that waits,
    # like the one below; a poll() that returns at once would spin the CPU.
    interval=0,
)
class TemplatePush(kanary.Source):
    description = "TODO: describe who sends values to this source."

    # For HTTP or MQTT, the "http-push" and "mqtt" templates need only settings.
    # This one is for any other listener; here, UDP datagrams such as
    #   echo "temperature 21.5" | nc -u -w1 127.0.0.1 8912
    listen_host = "127.0.0.1"
    listen_port = 8912

    def init(self):
        # Values arrive on the listener's thread and wait in the inbox until poll()
        # takes them, so none is lost while rules are being evaluated.
        self.inbox = kanary.Inbox()
        self.stopping = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((self.listen_host, self.listen_port))
        self.sock.settimeout(0.5)  # so that listen() sees stopping
        self.thread = threading.Thread(target=self.listen, daemon=True)
        self.thread.start()

    def listen(self):
        # Your listener, on its own thread. Anything that calls self.inbox.put() works,
        # e.g. the callback of a device library. put() takes the same items as
        # kanary.inputs(), and a value without a time gets the time it arrived.
        while not self.stopping.is_set():
            try:
                data, _sender = self.sock.recvfrom(4096)
                name, value = data.decode().split()
                self.inbox.put((name, float(value)))
            except socket.timeout:
                continue
            except ValueError:
                continue  # not "name number": ignore it

    def poll(self):
        # Waits up to 1 second for something to arrive, then returns every input with
        # its newest value; a burst is evaluated once. Nothing: skip() (rules do not
        # run). KANARY can stop or reload this source only between polls.
        return self.inbox.wait(timeout=1.0)
        # To see every value of a burst (a short spike), take them all instead:
        #   self.buffer.add(*self.inbox.take(timeout=1.0))   # see the "buffered" template

    def terminate(self):
        self.stopping.set()
        self.thread.join(timeout=2.0)
        self.sock.close()


# Nothing arriving is not an error for this source; a stale rule catches it:
#
#   @kanary.rule(rule_id="template.push.stale", inputs="template.push:*",
#                severity=kanary.WARN, evaluation_interval=30 * kanary.second)
#   class PushStale(kanary.StaleRule):
#       timeout = 5 * kanary.minute
