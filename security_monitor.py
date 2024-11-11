#!/usr/bin/env python3

"""
This is the main file of the security monitor program written by MAG Laboratory.

The program is written with a goal to provide a video wall for the space.

There are three command inputs for video wall on/off including:
    - PIR
    - UDP app
    - MQTT

This program makes extensive use of the python-mpv library.

Display blanking is accomplished through use of the python Xlib for X11.  Wayland support is not
a current priority but may become one if the base distributions for raspbian / armbian begin
supporting only wayland.
"""

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
from typing import Optional
import os
import re
import json
import base64
import zlib
import hashlib
import hmac
import time

class Utils:
    """
    This class contains utilities for token management and command message validation.
    Both MQTT and UDP should emit the same messages for validation.
    """
    START = "magld_"
    MINCTLEN = 2
    B64CRCLEN = 6

    @staticmethod
    def b64enc(obj):
        """ encode in b64 (and without the padding) """
        return base64.b64encode(obj).decode("utf-8").rstrip('=')

    @staticmethod
    def b64pad(line):
        """ pad for the python b64 library """
        num = (4 - len(line) % 4) % 4
        return f"{line}{'='*num}"

    @staticmethod
    def token_decode(token):
        """ 
        decode and validate token
        return the "central token" as a byte array once validated 
        """
        logging.debug(f"Token decode utility called with: {token}")
        token = token.rstrip()
        # length verification
        assert len(token) >= len(Utils.START) + Utils.MINCTLEN + Utils.B64CRCLEN
        # header verification
        assert token[0:len(Utils.START)].lower() == Utils.START
        # retrieve token in bytes
        # pad token with magical number of pad characters to make base64 happy
        central_token = Utils.b64pad(token[len(Utils.START):-Utils.B64CRCLEN])
        central_token = base64.b64decode(str.encode(central_token))

        end_checksum = token[-Utils.B64CRCLEN:]
        # although the default is big endian for most libraries, we use little
        # endian here to keep consistent with the encoding schemes used by
        # other famous tokens...
        calc_checksum = Utils.b64enc(zlib.crc32(central_token).to_bytes(4, "little"))
        # checksum verification
        assert calc_checksum == end_checksum

        return central_token

    @staticmethod
    def wr_hmac(msg, token):
        """ calculate the HMAC based on a token and the message """
        logging.debug(f"HMAC calculation utility called with: {msg} and {token}")
        obj = hmac.new(token, msg=str.encode(msg), digestmod=hashlib.sha256)
        return Utils.b64enc(obj.digest())

class autoMotionTimer(threading.Thread):
    """ Timer thread for monitor shutdown """
    def __init__(self, autoEvent, inEvent, screenOn, screenOff):
        # "super" init
        threading.Thread.__init__(self)
        # callbacks
        self._onFun = screenOn
        self._offFun = screenOff
        # input
        self._auto = autoEvent
        self._input = inEvent
        # my veriables
        self._event = threading.Event()

    def run(self):
        """
        main run function for the timer thread
        triggers to turn the monitor off at limit
        """
        counter = 0
        limit = 900
        logging.debug("Automatic control start.")
        while not self._event.wait(1):
            # get input status and clear it
            isSet = self._input.is_set()
            if isSet:
                self._input.clear()
            # is automatic mode allowed
            if self._auto.is_set():
                if isSet:
                    counter = 0
                # screen on
                self._onFun()

            # increment counter until limit
            if counter < limit:
                counter += 1

            if self._auto.is_set():
                if counter >= limit:
                    # screen off
                    self._offFun()

        logging.debug("Automatic control stop.")

    # stops the monitor thread
    def stop(self):
        logging.debug("Automatic control stop requested.")
        self._event.set()

# My uwudp listener
class UDPListen(threading.Thread):
    def __init__(self, msgDecode):
        threading.Thread.__init__(self)
        # callbacks
        self._cmdMsgApply = msgDecode
        # internet protocol
        self._ip = "0.0.0.0"
        self._port = 11017
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self._ip, self._port))
        self._inputs = [self._sock]

    # runs the UDP thread
    def run(self):
        logging.info(f"Listening for UDP packets on: {self._ip}:{self._port}")
        # hacky way to end a while loop using python
        while True:
            read, _, _ = select.select(self._inputs, [], [], 1)
            for s in read:
                if self._sock != None and s == self._sock:
                    try:
                        data, addr = self._sock.recvfrom(1024)
                    except socket.error:
                        logging.debug("UDP socket closed.")
                        break # this break will end the for un-naturally
                    logging.info(f"Received packet from {addr[0]}:{addr[1]}")
                    decoded = data.decode()
                    logging.debug(f"Data: {decoded}")
                    # fail false
                    if not self._cmdMsgApply(decoded):
                        response = "OK"

                    self._sock.sendto(response.encode(), addr)
                else:
                    break
            else:
                # skips the break at the end if the for loop was allowed to end naturally
                continue
            # executes if the for loop was also broken
            break

    # stops the UDP thread
    def stop(self):
        logging.debug("UDP stop called.")
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
    # generate position string based on divisions and index
    def _gen_pos(self, div, pos):
        # assumes that these values were already checked
        # position aligned to the left
        if pos == 0:
            pos_str = "+0"
        # position in the center
        elif pos < div-1:
            pos_str = f"+{100*pos//div}%"
        # position aligned to the right
        else:
            pos_str = "-0"

        return pos_str

    # generate geometry string
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

    # calculate number of divisions based on a magic index number
    def _calc_div(self, index):
        assert index >= 0
        # this function expects a screen that is "wide" and not "tall"
        col = 1
        row = 1
        while index != 0:
            index -= 1
            if col <= row:
                col += 1
            else:
                row += 1

        self._div = [col, row]
        self._total = col * row

    # index to position.  position is a tuple.
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
        # wait until the player is playing
        # timeout added here to terminate if the URL is not found
        try:
            player.wait_until_playing(timeout=60)
        # set the output event to terminate the player behind this one
        except Exception as e:
            logging.error(f"Player {name} stopped while waiting to start playing: {str(e)}")
            player.terminate()
        finally:
            event_out.set()
            logging.info(f"Asking bottom player to {name} to end.")

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
            logging.info(f"Player {name} stopping.")
            player.terminate()
            del player

    # helper function to spawn a player
    def _handle_player(self, last_p, running = True):
        # inital player logic
        if running:
            # self._total is the number of players visible.
            # the actual number of players is self._total * 2
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
            # start initial players
            for i in range(self._total):
                self._handle_player(i, False)
            time_cnt = 0
            p_cnt = 0
            while not self._event_all.is_set():
                time_cnt += 1
                # TODO configure this
                if time_cnt >= 300:
                    time_cnt = 0
                    # "handle" with the "started" parameter set to True
                    # starts the replacement player which asks the replaced player to stop
                    self._handle_player(p_cnt)
                    self.thr[p_cnt].join()
                    p_cnt = (p_cnt + 1) % (self._total*2)
                self.event_w.wait(1)
        finally:
            logging.info("Waiting for player threads...")
            for t in self.thr:
                if not t == None:
                    t.join()

            logging.info("Stopping security monitor.")

# Top Level Security Monitor Management
# there is an explanation for why this is calling "monitorTop."
# the function "SecurityMontior" was actually developed before a monitor
# top was envisioned to encapsulate it.
class MonitorTop(mqtt.Client):
    class MTState(Enum):
        PLAYING = 0
        STOPPED = 1
        RESTART = 2

    # initialization function
    def __init__(self):
        # automatic mode
        self.auto = threading.Event()
        self.auto.set()
        self.motion = threading.Event()
        # turns the screen off
        self.screenOff = threading.Event()
        # stops video
        self.stopPlaying = multiprocessing.Event()
        # exits this program
        self.monitorExit = threading.Event()

        # security monitor state
        self.mtstate = self.MTState.PLAYING
        self.last_mtstate = self.MTState.PLAYING

        mqtt.Client.__init__(self, mqtt.CallbackAPIVersion.VERSION2)

    @dataclass_json
    @dataclass
    # configuration dataclass
    class config:
        name: str
        urls: list[str]
        tokens: list[str]
        mqtt_broker: str
        mqtt_port: Optional[int] = 1883
        mqtt_timeout: Optional[int] = 60
        splitter_refresh_rate: Optional[int] = 300

    # overloaded MQTT on_connect function
    def on_connect(self, mqttc, obj, flag, reason, properties):
        logging.info(f"MQTT connected: {reason}")
        self.subscribe("reporter/checkup_req")
        self.subscribe("secmon00/cmd")
        self.subscribe("daisy/event")
        self.subscribe("daisy/checkup")

    # message authentication function
    def msgAuth(self, msg, code):
        logging.debug(f"MsgAuth called with: {msg} and {code}")
        match = False
        for token in self._tokens:
            calc = Utils.wr_hmac(msg, token)
            logging.debug(f"Calculated hmac as: {calc}")
            if calc == code:
                match = True
                break
        # throws an assertion if there are no matches
        assert match

    """
    The JSON and HMAC key are contained in a `pair` from Kotlin
    we run the output formatting of a pair through this particular
    regex.
    This output is somewhat equivalent for interpreting Python `tuple`s.

    And the HMAC output is b64 encoded.
    """
    def cmdMsgApply(self, cmd):
        retval = 1
        matches = re.fullmatch(r"\((\{.+\})\, (.+)\)", cmd)
        if matches != None:
            logging.debug(f"The split strings are: {matches[1]} and {matches[2]}")
            try:
                data = json.loads(matches[1])
                # validate the message time
                current_time = time.time()
                sent_time = data["time"]
                diff_time = current_time - sent_time
                logging.debug(f"Current time: {current_time}, Sent time: {sent_time}, \
                        Time Diff: {diff_time}")
                assert abs(diff_time) <= 7200

                self.msgAuth(matches[1], matches[2])
                # handle restarting
                if "restart" in data:
                    refresh = data["restart"]
                    logging.info(f"Received monitor restart: {restart}")
                    if refresh:
                        self.monRestart()
                        retval = 0
                # handle automatic mode
                elif "auto" in data and data["auto"] == True:
                    self.auto.set()
                elif "force" in data:
                    self.auto.clear()
                    force = data["force"]
                    logging.info(f"Received monitor status force: {force}")
                    if force:
                        self.monOn()
                        retval = 0
                    else:
                        self.monOff()
                        retval = 0

            except (json.JSONDecodeError, AttributeError, AssertionError) as e:
                logging.info(str(e)) # apparently not .toString
                pass

        return retval

    # turns the monitor on
    def monOn(self):
        if self.screenOff.is_set():
            self.screenOff.clear()
            self.stopPlaying.clear()

    # turns the monitor off
    def monOff(self):
        if not self.screenOff.is_set():
            self.screenOff.set()
            self.stopPlaying.set()

    # restarts the internal video wall class
    def monRestart(self):
        self.stopPlaying.set()
        self.mtstate = self.MTState.RESTART

    # overloaded MQTT on_message function
    def on_message(self, mqttc, obj, msg):
        if msg.topic == "reporter/checkup_req":
            logging.info("Checkup requested.")
            # TODO checkup
        elif msg.topic == "secmon00/cmd":
            # do
            decoded = msg.payload.decode('utf-8')
            logging.info("Display Commanded: " + decoded)
            self.cmdMsgApply(decoded)
        elif msg.topic.startswith("daisy"):
            decoded = msg.payload.decode('utf-8')
            logging.debug(f"Daisy message received: {decoded}")
            data = json.loads(decoded)
            if "ConfRm Motion" in data and data["ConfRm Motion"] == 1:
                logging.info("Received motion.")
                self.motion.set()

    # overloaded MQTT on_log function
    def on_log(self, mqttc, obj, level, string):
        if level == mqtt.MQTT_LOG_DEBUG:
            logging.debug("PAHO MQTT DEBUG: " + string)
        elif level == mqtt.MQTT_LOG_INFO:
            logging.info("PAHO MQTT INFO: " + string)
        elif level == mqtt.MQTT_LOG_NOTICE:
            logging.info("PAHO MQTT NOTICE: " + string)
        else:
            logging.error("PAHO MQTT ERROR: " + string)

    # signal handling helper function
    def signal_handler(self, signum, frame):
        logging.warning(f"Caught a deadly signal: {signum}!")
        self.stopPlaying.set()
        self.monitorExit.set()

    # main function
    def main(self):
        logging.basicConfig(level="DEBUG")
        logging.info("Starting Security Monitor Program")
        self._client_id = str.encode(self.config.name)

        # check if the configuration exists
        assert len(self.config.name)

        logging.info("Decoding tokens")
        self._tokens = []
        for token in self.config.tokens:
            try:
                self._tokens.append(Utils.token_decode(token))
            except:
                logging.error("Token not accepted")
                pass
        if not len(self._tokens):
            logging.critical("No tokens accepted.")
        else:
            logging.debug(f"Tokens decoded")

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
        self.connect(self.config.mqtt_broker, self.config.mqtt_port, self.config.mqtt_timeout)
        self.loop_start()

        logging.info("Starting UDP.")
        self.udp = UDPListen(self.cmdMsgApply)
        self.udp.start()

        logging.info("Starting automatic control.")
        self.amt = autoMotionTimer(self.auto, self.motion, self.monOn, self.monOff)
        self.amt.start()

        logging.info("Turning screen on.")
        self.monOn()

        #  security monitor splitter / windower initialize
        sm2 = None
        while not self.monitorExit.is_set():
            if self.mtstate != self.last_mtstate:
                logging.debug(f"Montior Loop State: {self.mtstate}")
            # execution
            if self.mtstate == self.MTState.PLAYING:
                self.stopPlaying.clear()
                sm2 = SecurityMonitor(self.stopPlaying, 1)
                sm2.urls = self.config.urls
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

        logging.info("Turning screen on.")
        self.monOn()

        logging.info("Stopping automatic control.")
        self.amt.stop()

        logging.info("Stopping UDP.")
        self.udp.stop()

        logging.info("Stopping MQTT.")
        self.loop_stop()

# main function for the entire program
if __name__ == "__main__":
    monitor = MonitorTop()
    pgm_path = os.path.dirname(os.path.abspath(__file__))
    with open(pgm_path + "/mon_config.json", "r") as config_file:
        monitor.config = MonitorTop.config.from_json(config_file.read())
    monitor.main()
