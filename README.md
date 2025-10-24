# mqtt-receipt-printer
Enable a receipt printer to be driven via a mqtt broker

## Protocol

We use three topics: "status", "print", and "printed". Payloads for
all these topics are json. (In practice there will be a topic prefix
as well.)

The print server maintains a connection to the broker and publishes
the current printer status to the "status" topic, with the retain flag
set. Status examples: `{"status": "Ready", "ok": true}` `{"status":
"Out of paper": "ok": false}` `{"status": "Offline": "ok": false}`

"status" is set as the Will topic of the RPi connection, with the
retain flag set and the "Offline" status set as the Will payload —
this means that if the print server disconnects for any reason (turned
off, network outage, etc.) the client can immediately tell it is
offline.

The print server subscribes to the "print" topic.

The client does not maintain a connection to the broker; it only
connects when it needs to print. It can fetch the current status at
any time without waiting for a round-trip to the server, simply by
connecting to the broker and subscribing to the retained "status"
topic.

To print, the client subscribes to the "printed" topic and publishes a
print job to the "print" topic. Example print job payload: `{"jobid":
"[random GUID]", "data": "[ESC/POS job to send to the printer, base64
encoded"}`

When the server receives a job on the "print" topic, it sends it to
the printer and publishes the job progress to the "printed"
topic. Sample progress messages: `{"jobid": "[copied from print job]",
"status": "In progress", "finished": false, "success": false}`
`{"jobid": "[copied from print job]", "status": "Printed", "finished":
true, "success": true}` `{"jobid": "[copied from print job]", "status":
"Aborted", "finished": true, "success": false}`

The client looks for messages on the "printed" topic with the
appropriate jobid, and can follow the state of the print job until it
sees a message with "finished" set to true, or there is a timeout. It
can report success or failure to the user. (Or "we don't know whether
this printed" if it sees a timeout, which I don't think is completely
avoidable. Perhaps we can add some kind of "cancel job" message the
client can send if it gives up on a job.)

## Limitations

This has only been tested with an Epson TM-U220 with USB
interface. The method used to fetch the printer status is likely to
work with other Epson ESC/POS printers, but is not generic. (We are
only doing this because the USB interface for the TM-U220 doesn't
support the LPGETSTATUS ioctl of the Linux usblp driver.)

## Sample configuration

The configuration is stored in a TOML file. Example:

```
hostname = "mqtt.haymakers.individualpubs.co.uk"
port = 8883
client_id = "barbarella"
username = "barbarella"
password = "[redacted]"
prefix = "barbarella"
printer = "/dev/epson-tm-u220"
status_check_interval = 5.0
```

## Sample client

This is a [quicktill](https://github.com/sde1000/quicktill) printer
driver that implements this protocol:

```
import paho.mqtt.client as mqtt
import io
import ssl
import time
import json
import uuid
import base64


class MQTTPrinter(quicktill.pdrivers.printer):
    def __init__(self, driver, host, port, username, password, prefix):
        super().__init__(driver, description=f"MQTT on {host}")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.status_topic = f"{prefix}/status"
        self.print_topic = f"{prefix}/print"
        self.printed_topic = f"{prefix}/printed"

    def _create_client(self):
        client = mqtt.Client()
        client.username_pw_set(self.username, self.password)
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        client.connect_async(self.host, self.port)
        return client

    def _run_client(self, client, end_time, done_fn):
        try:
            rc = client.reconnect()
        except ConnectionRefusedError:
            return

        while not done_fn() and time.time() < end_time:
            client.loop(timeout=end_time - time.time())

        client.disconnect()

    def offline(self):
        start = time.time()
        client = self._create_client()
        status = None

        def done():
            return status is not None

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                client.subscribe(self.status_topic, 0)

        def on_status_message(client, userdata, msg):
            nonlocal status
            status = msg.payload

        client.on_connect = on_connect
        client.message_callback_add(
            self.status_topic, on_status_message)

        self._run_client(client, start + 2.0, done)

        if status:
            try:
                sd = json.loads(status)
            except json.JSONDecodeError:
                sd = {"status": "Invalid status", "ok": False}
            if not sd["ok"]:
                return sd["status"]
            return
        return "No response from MQTT broker within time limit"

    def _send_job(self, data):
        # Raise quicktill.pdrivers.PrinterError(self, msg) if print fails
        jobid = str(uuid.uuid4())
        start = time.time()
        client = self._create_client()
        finished = False
        status = None

        def done():
            return finished

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                client.subscribe(self.printed_topic, 0)

        def on_subscribe(client, userdata, mid, granted_qos):
            client.publish(self.print_topic, json.dumps({
                "jobid": jobid,
                "data": base64.b64encode(data).decode('ascii'),
            }))

        def on_printed_message(client, userdata, msg):
            nonlocal finished, status
            _s = json.loads(msg.payload)
            if _s['jobid'] == jobid:
                status = _s
                if status['finished']:
                    finished = True

        client.on_connect = on_connect
        client.on_subscribe = on_subscribe
        client.message_callback_add(
            self.printed_topic, on_printed_message)

        self._run_client(client, start + 10.0, done)

        if not finished:
            raise quicktill.pdrivers.PrinterError(
                self, "No response to print job: we don't know whether "
                "it printed or not.")

        if not status['success']:
            raise quicktill.pdrivers.PrinterError(
                self, status['status'])

    def print_canvas(self, canvas):
        with io.BytesIO() as f:
            self._driver.process_canvas(canvas, f)
            self._send_job(f.getvalue())

    def kickout(self):
        with io.BytesIO() as f:
            self._driver.kickout(f)
            self._send_job(f.getvalue())
```
