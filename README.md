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
