# kanary-template: mqtt
# kind: source
# level: basic
# summary: Subscribe to MQTT topics and evaluate each message as soon as it arrives (MQTTSource; needs paho-mqtt).
import os

import kanary


# No interval or schedule here: MQTTSource runs with interval = 0, so poll() waits
# for messages and the rules run as soon as one arrives. Any other makes init() fail.
@kanary.source(source_id="template.mqtt")
class TemplateMqtt(kanary.MQTTSource):
    description = "TODO: describe the broker and the devices."

    host = "mqtt.example.org"  # TODO
    port = 1883                # usually 8883 with tls = True
    # tls = True
    # Keep the login out of this file, e.g. in environment variables.
    username = os.environ.get("MQTT_USERNAME")
    password = os.environ.get("MQTT_PASSWORD")
    qos = 1

    # Topic filter -> input name. None names the input after the topic ("/" -> "."),
    # as every filter with a wildcard must: "+" matches one level, "#" the rest.
    topics = {
        "plant/room_a/temperature": "room_a.temperature",  # TODO
        "plant/pumps/#": None,  # plant/pumps/p1/flow -> plant.pumps.p1.flow
    }

    # A payload is a number ("21.5"), a state ("ON" / "true" -> 1.0, "OFF" -> 0.0),
    # other text ("RUNNING", kept as it is), or JSON. A JSON object gives one input
    # per key: {"t": 21.5, "rh": 40} on "room_a" -> room_a.t, room_a.rh.
    # "mqtt.connected" is 1 while connected; the client reconnects by itself and
    # subscribes again.
    # Needs the paho-mqtt package in the Python environment that runs KANARY: see
    # docs/deployment.md, "When A Plugin Needs Additional Python Modules".


# Alert when the broker is unreachable, and when values stop coming:
#
#   @kanary.rule(rule_id="template.mqtt.disconnected", inputs="template.mqtt:mqtt.connected",
#                severity=kanary.ERROR)
#   class MqttDisconnected(kanary.ExpectedValueRule):
#       expected = 1
#
#   @kanary.rule(rule_id="template.mqtt.stale", inputs="template.mqtt:room_a.temperature",
#                severity=kanary.WARN, evaluation_interval=30 * kanary.second)
#   class MqttStale(kanary.StaleRule):
#       timeout = 5 * kanary.minute
