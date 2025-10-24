from typing import Dict, List, Tuple, Any, Optional, NoReturn
import sys
import os
import paho.mqtt.client as mqtt
import ssl
import argparse
import tomllib
import json
import time
import logging
import sdnotify  # type: ignore
import base64
log = logging.getLogger(__name__)


def status_message(text: str, ok: bool) -> Dict[str, Any]:
    return {"status": text, "ok": ok}


class ReceiptPrinter:
    status_offline = status_message("Offline", False)
    status_cover_open = status_message("Cover is open", False)
    status_paper_being_fed = status_message(
        "Paper is being fed by the paper feed button", False)
    status_oop = status_message("Out of paper", False)
    status_error = status_message("Error light is on", False)
    status_no_response = status_message("No response from printer", False)
    status_not_connected = status_message("Printer not connected", False)
    status_ready = status_message("Ready", True)

    def __init__(self, config: Dict[str, Any],
                 notifier: sdnotify.SystemdNotifier) -> None:
        self.printer = config["printer"]
        self.notifier = notifier
        prefix = config.get("prefix", "")
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"
        self.status_topic = f"{prefix}status"
        self.print_topic = f"{prefix}print"
        self.printed_topic = f"{prefix}printed"
        self.mqttc = mqtt.Client(client_id=config["client_id"])
        self.mqttc.will_set(self.status_topic, json.dumps(self.status_offline),
                            retain=True)
        self.mqttc.username_pw_set(config["username"], config["password"])
        self.mqttc.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        self.connected = False
        self.current_status = self.status_offline
        # Print queue is a list of (request-id, data) tuples
        self.print_queue: List[Tuple[str, bytes]] = []
        self.status_check_interval = config.get("status_check_interval", 5.0)
        self.mqttc.connect_async(config["hostname"], port=config["port"])
        self.mqttc.on_connect = self.on_connect
        self.mqttc.message_callback_add(
            self.print_topic, self.on_print_message)

    @staticmethod
    def _nocreat_opener(path: str, flags: int) -> int:
        return os.open(path, flags & ~os.O_CREAT)

    @staticmethod
    def _find_dle_eot_response(r: bytes) -> Optional[int]:
        for b in r:
            if (b & 0x93) == 0x12:
                return b
        return None

    def fetch_status(self) -> Dict[str, Any]:
        # Unfortunately this appears to be very sensitive to
        # timing. Also, we appear to need to close the fd after
        # sending a command in order to be able to pick up the reply —
        # using f.flush() doesn't appear to do anything. Frustrating!
        try:
            with open(self.printer, 'a+b', opener=self._nocreat_opener) as f:
                log.debug("Draining input buffer")
                r = f.read(32)
                log.debug(f"Drained bytes {r=}")

            with open(self.printer, 'a+b', opener=self._nocreat_opener) as f:
                # DLE EOT 1: "transmit printer status"
                # response: 0fw1od10
                # - f = 1 if paper feed button is pressed
                # - w = 1 if waiting for online recovery
                # - o = 1 if offline
                # - d = state of drawer connector pin 3
                log.debug("Sending DLE EOT 1")
                f.write(bytes((0x10, 0x04, 0x01)))  # DLE EOT 1
            time.sleep(0.1)
            with open(self.printer, 'a+b', opener=self._nocreat_opener) as f:
                log.debug("Reading...")
                r = f.read(32)
                log.debug(f"{r=}")
                n1 = self._find_dle_eot_response(r)
                if n1 is None:
                    log.debug("No valid response to DLE EOT 1")
                    return self.status_no_response
                log.debug(f"Read n1={n1:02x}")
                if not (n1 & 0x28):
                    return self.status_ready
            with open(self.printer, 'a+b', opener=self._nocreat_opener) as f:
                # DLE EOT 2: "transmit offline cause status"
                # response: 0ep1fc10
                # - e = 1 if error occurred
                # - p = 1 if paper out
                # - f = 1 if paper being fed by paper feed button
                # - c = 1 if cover is open
                log.debug("Sending DLE EOT 2")
                f.write(bytes((0x10, 0x04, 0x02)))  # DLE EOT 2
            time.sleep(0.1)
            with open(self.printer, 'a+b', opener=self._nocreat_opener) as f:
                log.debug("Reading...")
                r = f.read(32)
                log.debug(f"{r=}")
                n2 = self._find_dle_eot_response(r)
                if n2 is None:
                    log.debug("No valid response to DLE EOT 2")
                    return self.status_error
                log.debug(f"Read n2={n2:02x}")
                if n2 & 0x04:
                    return self.status_cover_open
                if n2 & 0x08:
                    return self.status_paper_being_fed
                if n2 & 0x20:
                    return self.status_oop
                if n2 & 0x40:
                    return self.status_error
        except IOError as e:
            log.debug(f"printer not connected {e=}")
            return self.status_not_connected
        return status_message(
            f"Unrecognised printer status: {n1=} {n2=}", False)

    def check_printer_status(self) -> None:
        new_status = self.fetch_status()
        if new_status != self.current_status:
            self.current_status = new_status
            self.mqttc.publish(
                self.status_topic, json.dumps(self.current_status),
                retain=True)

    def on_connect(self, client: mqtt.Client, userdata: Any,
                   flags: Dict[str, int], rc: int) -> None:
        log.debug(f"on_connect {rc=}")
        if rc == 5:
            log.fatal("mqtt: Not authorised")
            sys.exit(1)

        # XXX deal with all other known rc values here!

        if rc == 0:
            log.debug("Connected")
            self.connected = True
            client.subscribe(self.print_topic, 0)

    def on_print_message(self, client: mqtt.Client, userdata: Any,
                         msg: mqtt.MQTTMessage) -> None:
        try:
            req = json.loads(msg.payload)
            log.debug(f"{req=}")
        except json.JSONDecodeError:
            log.info(f"Ignoring non-JSON print request {msg.payload=}")
            return
        if "jobid" not in req:
            log.info(f"Ignoring print request missing jobid: {req=}")
            return
        jobid = str(req["jobid"])
        if "data" not in req:
            log.info(f"Responding to print request missing data: {req=}")
            self.send_print_status(jobid, "Aborted: missing data")
            return
        try:
            data = base64.b64decode(req["data"])
        except Exception as e:
            log.info(f"Error decoding print data: {req=}")
            self.send_print_status(jobid, f"Error decoding data: {e=}")
            return
        self.print_queue.append((jobid, data))

    def send_print_status(
            self, jobid: str, message: str,
            finished: bool = True, success: bool = False) -> None:
        self.mqttc.publish(self.printed_topic, json.dumps({
            "jobid": jobid,
            "status": message,
            "finished": finished,
            "success": success,
        }))

    def run(self) -> NoReturn:
        status_check_deadline = 0.0
        while True:
            self.notifier.notify("WATCHDOG=1")
            if self.print_queue:
                jobid, data = self.print_queue.pop(0)
                status = self.fetch_status()
                if status['ok']:
                    try:
                        with open(self.printer, 'ab',
                                  opener=self._nocreat_opener) as f:
                            f.write(data)
                            self.send_print_status(
                                jobid, "Printed", success=True)
                    except Exception as e:
                        self.send_print_status(
                            jobid, f"Print failed: {e=}", success=False)
                else:
                    self.send_print_status(
                        jobid, status['status'], success=False)

            while not self.connected:
                try:
                    self.mqttc.reconnect()
                    self.connected = True
                except ConnectionRefusedError:
                    time.sleep(1)

            if self.connected:
                now = time.time()
                if status_check_deadline <= now:
                    self.check_printer_status()
                    status_check_deadline = now + self.status_check_interval
                timeout = max(status_check_deadline - now, 0.0)
                rc = self.mqttc.loop(timeout=timeout)
                if rc == mqtt.MQTT_ERR_CONN_LOST:
                    self.connected = False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config_file", type=argparse.FileType('rb'),
                        help="TOML config file")
    parser.add_argument("--debug", "-d", action="store_true")
    args = parser.parse_args()
    config = tomllib.load(args.config_file)
    notifier = sdnotify.SystemdNotifier()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    rp = ReceiptPrinter(config, notifier)
    notifier.notify("READY=1")
    rp.run()
