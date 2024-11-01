#!/usr/bin/env python3

import mpv
import multiprocessing
import threading
import logging
import signal
import socket
import select
from Xlib.ext import dpms
from Xlib import display
import paho.mqtt.client as mqtt
from enum import Enum
from dataclasses import dataclass
from dataclasses_json import dataclass_json
import os
import re
import json
import base64
import zlib
import hashlib
import hmac

class utils:
    START = "magld_"
    MINCTLEN = 2
    B64CRCLEN = 6
    def b64enc(obj):
        return base64.b64encode(obj).decode("utf-8").rstrip('=')

    def b64pad(s):
        return s + '=' * ((4 - len(s) % 4) % 4)

    # token is returned as byte array
    def token_decode(token):
        logging.debug(f"Token decode called with: {token}")
        token = token.rstrip()
        # length verification
        assert len(token) >= len(utils.START) + utils.MINCTLEN + utils.B64CRCLEN
        # header verification
        assert token[0:len(utils.START)].lower() == utils.START
        # retrieve token in bytes
        # pad token with magical number of pad characters to make base64 happy
        central_token = utils.b64pad(token[len(utils.START):-utils.B64CRCLEN])
        central_token = base64.b64decode(str.encode(central_token))

        end_checksum = token[-utils.B64CRCLEN:]
        # although the default is big endian for most libraries, we use little 
        # endian here to keep consistent with the encoding schemes used by
        # other famous tokens... 
        calc_checksum = utils.b64enc(zlib.crc32(central_token).to_bytes(4, "little"))
        # checksum verification
        assert calc_checksum == end_checksum

        return central_token

    def wr_hmac(msg, token):
        logging.debug(f"HMAC calculation utility called with: {msg} and {token}")
        obj = hmac.new(token, msg=str.encode(msg), digestmod=hashlib.sha256)
        return utils.b64enc(obj.digest())


# My uwudp listener
class UDPListen(threading.Thread):
    def __init__(self, msgAuth, onCallback, offCallback):
        threading.Thread.__init__(self)
        # callbacks
        self._msgAuth = msgAuth
        self._onCB = onCallback
        self._offCB = offCallback
        # internet protocol
        self._ip = "0.0.0.0"
        self._port = 11017
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self._ip, self._port))
        self._inputs = [self._sock]

    def run(self):
        logging.info(f"Listening for UDP packets on: {self._ip}:{self._port}")
        while True:
            read, _, _ = select.select(self._inputs, [], [], 1)
            for s in read:
                if self._sock != None and s == self._sock:
                    try: 
                        data, addr = self._sock.recvfrom(1024)
                    except socket.error:
                        logging.info("UDP socket closed.")
                        break
                    logging.info(f"Received packet from {addr[0]}:{addr[1]}")
                    decoded = data.decode()
                    logging.debug(f"Data: {decoded}")
                    # the default response is NO.  keep it this way until we have a valid input
                    response = "NO"
                    if (decoded.lower() == "on"):
                        logging.info(f"Received valid ON command from {addr[0]}:{addr[1]}")
                        self._onCB()
                        response = "OK"
                    elif (decoded.lower() == "off"):
                        logging.info(f"Received valid OFF command from {addr[0]}:{addr[1]}")
                        self._offCB()
                        response = "OK"
                    else:
                        """
                        The JSON and HMAC key are contained in a `pair` from Kotlin
                        we run the output formatting of a pair through this particular
                        regex.
                        This output is somewhat equivalent for interpreting Python `tuple`s.

                        And the HMAC output is b64 encoded.
                        """
                        logging.debug("Attempting to match UDP input...")
                        matches = re.fullmatch("\((\{.+\})\, (.+)\)", decoded)
                        if (matches != None):
                            logging.debug(f"The split strings are: {matches[1]} and {matches[2]}")
                            try:
                                data = json.loads(matches[1])
                                force = data["force"]
                                self._msgAuth(matches[1], matches[2])
                                logging.info(f"Received monitor status force: {force}")
                                if (force):
                                    self._onCB()
                                    response = "OK"
                                else:
                                    self._offCB()
                                    response = "OK"

                            except (json.JSONDecodeError, AttributeError, AssertionError) as e:
                                logging.info(e.toString())
                                pass

                    self._sock.sendto(response.encode(), addr)
            else:
                continue
            break

    def stop(self):
        logging.info("UDP stop called.")
        self._sock.close()

# Security Monitor Windowing and Splitting
class SecurityMonitor():
    # TODO use queue for return instead
    urls = ["rtsp://maglab:magcat@connor.maglab:8554/Camera1_sub",
            "rtsp://maglab:magcat@connor.maglab:8554/Camera2_sub"]

    # initialize with an event and division index
    #  sample division indices to divisions:
    #    0 -> 1x1
    #    1 -> 2x1
    #    2 -> 2x2
    #    3 -> 3x2
    #    4 -> 3x3
    def __init__(self, quit_event, div_idx):
        self._event_all = quit_event
        self._calc_div(div_idx)

    # Helper Functions
    def _gen_pos(self, div, pos):
        # assumes that these values were already checked
        # position aligned to the left
        if (pos == 0):
            pos_str = "+0"
        # position in the center
        elif (pos < div-1):
            pos_str = f"+{100*pos//div}%"
        # position aligned to the right
        else:
            pos_str = "-0"
    
        return pos_str
    
    def _gen_geo_str(self, idx):
        # divisions
        # must be greater than 0
        assert self._div[0] > 0
        assert self._div[1] > 0
        # must have columns and rows in division
        assert len(self._div) == 2

        # position
        # calculate column and row
        colDiv = self._div[0]
        rowDiv = self._div[1]
        [colPos, rowPos] = self._idx2pos(idx)
        # positions must be less than divisions
        assert colPos < self._div[0]
        assert rowPos < self._div[1]
    
        # column width calculation
        geo_str=f"{100//self._div[0]}%"
        # row width calculation
        geo_str=f"{geo_str}x{100//self._div[1]}%"
        # column position
        geo_str += self._gen_pos(colDiv, colPos)
        # row position
        geo_str += self._gen_pos(rowDiv, rowPos)
    
        return geo_str
    
    def _calc_div(self, index):
        assert index >= 0 
        # this function expects a screen that is "wide" and not "tall"
        col = 1
        row = 1
        while index != 0:
            index -= 1
            if (col <= row):
                col += 1
            else:
                row += 1
    
        self._div = [col, row]
        self._total = col * row
    
    def _idx2pos(self, idx):
        assert idx < self._total
        return [idx % self._div[0], idx // self._div[0]]
    
    # this thread actually contains the mpv stream player
    def _play_thread(self, event_in, event_out, idx, url, name):
        player = mpv.MPV()
        # a series of configuration options that make the player act like a
        # security monitor
        player.network_timeout = 10
        player.border = "no"
        player.keepaspect = "no"
        player.ao = "pulseaudio"
        player.profile = "low-latency"
        geo_str = self._gen_geo_str(idx)
        player.geometry = geo_str
        # enter the camera URL and wait until it starts to play
        player.play(url)
        player.wait_until_playing()
        # set the output event to terminate the player behind this one
        event_out.set()
        logging.info(f"Signaling player {name} start.")
        try:
            while not self._event_all.is_set() and not event_in.is_set():
                try:
                    player.wait_for_event(None, timeout=1)
                except TimeoutError:
                    # this is normal.  the function should be timing out.
                    continue
                except mpv.ShutdownError:
                    logging.error("Unexpected player shutdown.  Shutting down.")
                    self._event_all.set()
                except KeyboardInterrupt:
                    logging.warn("Player caught Keyboard Interrupt.")
                    continue
        finally:
            logging.info(f"Stopping player {name}.")
            player.terminate()
            del player
    
    # helper function to spawn a player
    def _handle_player(self, last_p, running = True):
        # inital player logic
        if (running):
            pi = (last_p + self._total) % (self._total * 2)
        else:
            # state where the players are initializing
            pi = last_p
            last_p = (last_p + self._total) % (self._total * 2)
        pos = last_p % self._total
        url = self.urls[pos]
        logging.info(f"Starting player: {pi}")
        self.thr[pi] = multiprocessing.Process(target=self._play_thread, args=(
            self.evt[pi],
            self.evt[last_p],
            pos,
            url,
            pi))
        self.thr[pi].daemon = True
        self.evt[pi].clear()
        self.thr[pi].start()
        logging.info(f"Player started: {pi}")
   
    # main / run function within the class
    def main(self):
        logging.info("Starting security monitor")
        assert len(self.urls) >= self._total

        self.evt = [multiprocessing.Event() for _ in range(self._total*2)]
        self.thr = [None] * (self._total*2)
        self.event_w = threading.Event()
    
        try: 
            self._handle_player(0, False)
            self._handle_player(1, False)
            time_cnt = 0
            p_cnt = 0
            while not self._event_all.is_set():
                logging.debug("Splitter Waiting.")
                time_cnt += 1
                if (time_cnt >= 3):
                    time_cnt = 0
                    self._handle_player(p_cnt)
                    self.thr[p_cnt].join()
                    p_cnt = (p_cnt + 1) % (self._total*2)
                self.event_w.wait(10)
        finally:
            logging.info("Waiting for player threads...")
            for t in self.thr:
                if not t == None:
                    t.join()

            logging.info("Stopping security monitor.")

# Top Level Security Monitor Management
class MonitorTop(mqtt.Client):
    class MTState(Enum):
        PLAYING = 0
        STOPPED = 1
        RESTART = 2

    def __init__(self, callbackAPIVersion):
        # turns the screen off
        self.screenOff = threading.Event()
        # stops video
        self.stopPlaying = multiprocessing.Event()
        # exits this program
        self.monitorExit = threading.Event()

        # security monitor state
        self.mtstate = self.MTState.PLAYING
        self.last_mtstate = self.MTState.PLAYING

        mqtt.Client.__init__(self, callbackAPIVersion)

    @dataclass_json
    @dataclass
    class config:
        name: str
        tokens: list[str]

    def on_connect(self, mqttc, obj, flag, reason, properties):
        logging.info(f"MQTT connected: {reason}")
        self.subscribe("reporter/checkup_req")
        self.subscribe("secmon00/CMD_DisplayOn")

    def msgAuth(self, msg, code):
        logging.debug(f"MsgAuth called with: {msg} and {code}")
        match = False
        for token in self._tokens:
            calc = utils.wr_hmac(msg, token)
            logging.debug(f"Calculated hmac as: {calc}")
            if (calc == code):
                match = True
                break

        assert(match)
        # assert(any(util.hmac(msg, token) == code for token in self._tokens))

    def cmdMsgDecode(self, cmd)
        retval = 1
        matches = re.fullmatch("\((\{.+\})\, (.+)\)", cmd)
        if (matches != None):
            logging.debug(f"The split strings are: {matches[1]} and {matches[2]}")
            try:
                data = json.loads(matches[1])
                force = data["force"]
                self._msgAuth(matches[1], matches[2])
                logging.info(f"Received monitor status force: {force}")
                if (force):
                    self.monOn()
                    retval = 0
                else:
                    self.monOff()
                    retval = 0

            except (json.JSONDecodeError, AttributeError, AssertionError) as e:
                logging.info(e.toString())
                pass

        return retval

    def monOn(self):
        self.screenOff.clear()
        self.stopPlaying.clear()

    def monOff(self):
        self.screenOff.set()
        self.stopPlaying.set()

    def on_message(self, mqttc, obj, msg):
        if msg.topic == "reporter/checkup_req":
            logging.info("Checkup requested.")
            # checkup
        elif msg.topic == "secmon00/CMD_DisplayOn":
            # do 
            decoded = msg.payload.decode('utf-8')
            logging.info("Display Commanded: " + decoded)
            if (decoded.lower() == "false" or decoded == "0"):
                self.monOff()
            if (decoded.lower() == "true" or decoded == "1"):
                self.monOn()

    def on_log(self, mqttc, obj, level, string):
        if level == mqtt.MQTT_LOG_DEBUG:
            logging.debug("PAHO MQTT DEBUG: " + string)
        elif level == mqtt.MQTT_LOG_INFO:
            logging.info("PAHO MQTT INFO: " + string)
        elif level == mqtt.MQTT_LOG_NOTICE:
            logging.info("PAHO MQTT NOTICE: " + string)
        else:
            logging.error("PAHO MQTT ERROR: " + string)

    def signal_handler(self, signum, frame):
        logging.warning(f"Caught a deadly signal: {signum}!")
        self.stopPlaying.set()
        self.monitorExit.set()

    def main(self):
        logging.basicConfig(level="DEBUG")
        logging.info("Starting Security Monitor Program")

        logging.info("Decoding tokens")
        self._tokens = []
        for token in ["magld_BK9puFlNlsY9d6y39b+1UOJtqaB7kLvSzEXXJg2N196Q"]:
            try:
                self._tokens.append(utils.token_decode(token))
            except:
                pass
        #self._tokens = list(filter(utils.token_decode, ["magld_BK9puFlNlsY9d6y39b+1UOJtqaB7kLvSzEXXJg2N196Q"]))
        logging.debug(f"Tokens decoded: {self._tokens}")

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        # X11
        self.disp = display.Display()
        try:
            self.pm_able = self.disp.dpms_capable()
        except ValueError:
            self.capable = False
        if not self.pm_able:
            logging.warn("Display is not DPMS capable.")
        logging.debug(f"DPMS capable: {self.pm_able}")

        if self.pm_able:
            logging.debug("Configuring DPMS.")
            # disable screensaver
            #  timeout (setting timeout to 0 disables the screensaver)
            #  interval
            #  prefer blanking
            #  allow exposures
            self.disp.set_screen_saver(0, 0, True, True)
            self.disp.sync()
            # enable DPMS
            self.disp.dpms_enable()
            self.disp.sync()
            # set DPMS timers to 0
            #  standy  (setting to 0 disables)
            #  suspend (setting to 0 disables)
            #  off     (setting to 0 disables)
            self.disp.dpms_set_timeouts(0, 0, 0)
            self.disp.sync()

        logging.info("Starting MQTT.")
        # connect MQTT
        #  host hal.maglab
        #  port 1883 (default)
        #  timeout 60
        self.connect("hal.maglab", 1883, 60)
        self.loop_start()

        logging.info("Starting UDP.")
        self.udp = UDPListen(self.msgAuth, self.monOn, self.monOff)
        self.udp.start()

        #  security monitor splitter / windower initialize
        sm2 = None
        while not self.monitorExit.is_set():
            logging.debug(f"Montior Loop State: {self.mtstate}")
            # execution
            if self.mtstate == self.MTState.PLAYING:
                self.stopPlaying.clear()
                sm2 = SecurityMonitor(self.stopPlaying, 1)
                if self.pm_able:
                    logging.info("Turning Screen ON.")
                    self.disp.dpms_force_level(dpms.DPMSModeOn)
                    self.disp.sync()
                logging.info("Calling Splitter.")
                sm2.main()
            #  restart or stopped
            if self.mtstate == self.MTState.STOPPED:
                if self.last_mtstate == self.MTState.PLAYING:
                    if self.pm_able:
                        logging.info("Turning Screen Off.")
                        self.disp.dpms_force_level(dpms.DPMSModeOff)
                        self.disp.sync()
            if self.mtstate != self.MTState.PLAYING:
                self.monitorExit.wait(1)

            # save the last mtstate before computing state transitions
            self.last_mtstate = self.mtstate

            # transitions
            if self.mtstate == self.MTState.PLAYING:
                if self.screenOff.is_set():
                    self.mtstate = self.MTState.STOPPED
            elif self.mtstate == self.MTState.RESTART:
                self.mtstate = self.MTState.PLAYING
            elif self.mtstate == self.MTState.STOPPED:
                if not self.screenOff.is_set():
                    self.mtstate = self.MTState.PLAYING

        logging.info("Stopping MQTT.")
        self.loop_stop()

        logging.info("Stopping UDP.")
        self.udp.stop()

if __name__ == "__main__":
    # there is an explanation for why this is calling "monitorTop."
    # the function "SecurityMontior" was actually developed before a monitor
    # top was envisioned to encapsulate it.
    monitor = MonitorTop(mqtt.CallbackAPIVersion.VERSION2)
    monitor.main()
