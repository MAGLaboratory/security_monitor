#!/usr/bin/env python3

# pylint: disable=logging-fstring-interpolation

"""
This is the main file of the security monitor program written by MAG Laboratory.

The program is written with a goal to provide a video wall for the space.

There are three command inputs for video wall on/off including:
    - PIR
    - UDP (app)
    - MQTT

This program makes extensive use of the python-mpv library.

Display blanking is accomplished through use of the python Xlib for X11.  Wayland support is not
a current priority but may become one if the base distributions for raspbian / armbian begin
supporting only wayland.
"""

import multiprocessing
import threading
import logging
import signal
import socket
import select
import os
import re
import json
import base64
import zlib
import hashlib
import hmac
import time
import copy
import queue
from enum import IntEnum
from typing import Optional
from dataclasses import dataclass
from dataclasses_json import dataclass_json
import mpv
from Xlib import display
from Xlib.ext import dpms
import paho.mqtt.client as mqtt

class Utils:
    """
    This class contains utilities for token management and command message validation.
    Both MQTT and UDP should provide the same messages for validation.

    Also, one function for removing items from a queue is provided.
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

    @staticmethod
    def clear_queue(quu):
        """ This helper clears a queue"""
        while not quu.empty():
            logging.debug("Utility clearing queue")
            try:
                _ = quu.get_nowait()
            except queue.Empty:
                logging.error("Utility queue clear on a supposedly non-empty returned empty.")

class AutoMotionTimer(threading.Thread):
    """ Timer thread for monitor shutdown """
    def __init__(self, bools, indices, functions, timeout):
        # "super" init
        threading.Thread.__init__(self)
        # callbacks
        # note that the order goes On/Off
        # in the digital electronics world, this could be considered active-low
        self._on_fun = functions[0]
        self._off_fun = functions[1]
        # input
        # since it is not possible to create pointer references in python, let's use a worse way:
        # reference by integer position!
        self._bools = bools
        self._auto_idx = indices[0]
        self._in_idx = indices[1]
        # timeout
        # self explanatory
        self._timeout = timeout
        # class exit variable
        self._event = threading.Event()

    def run(self):
        """
        main run function for the timer thread
        triggers to turn the monitor off at timeout
        """
        counter = 0
        last_auto = self._bools[self._auto_idx]
        logging.debug("Automatic control start.")
        while not self._event.wait(1):
            # get input status and clear it
            trig = self._bools[self._in_idx]
            if trig:
                self._bools[self._in_idx] = False

            # increment counter until limit
            if counter < self._timeout:
                counter += 1

            if self._bools[self._auto_idx]:
                # if triggered, turn on and reset counter
                if trig:
                    counter = 0
                    # screen on
                    self._on_fun()
                # turn off the screen
                # or turn back to a known-on state if we are resuming automatic control
                if counter >= self._timeout:
                    # screen off
                    logging.info("Automatic motion timer turning screen off")
                    self._off_fun()
                elif last_auto is False:
                    # screen on
                    logging.info("Automatic motion timer turning screen on")
                    self._on_fun()

            last_auto = self._bools[self._auto_idx]

        logging.debug("Automatic control stop.")

    def stop(self):
        """ stops the monitor thread """
        logging.debug("Automatic control stop requested.")
        self._event.set()

class UDPListen(threading.Thread):
    """
    My uwudp listener
    The maximum packet size is 1024 and there are no provisions for lengthening this.
    """
    def __init__(self, msgDecode):
        threading.Thread.__init__(self)
        # callbacks
        self._cmd_msg_apply = msgDecode
        # internet protocol
        self._ip = "0.0.0.0"
        self._port = 11017
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((self._ip, self._port))
        self._inputs = [self._sock]

    def run(self):
        """ runs the UDP thread """
        logging.info(f"Listening for UDP packets on: {self._ip}:{self._port}")
        # hacky way to end a while loop using python
        while True:
            # select does not return automatically when the socket is closed, so a timeout must be
            # specified so that it can return with the closed loop in `read`
            read, _, _ = select.select(self._inputs, [], [], 1)
            for sel_fd in read:
                if self._sock is not None and sel_fd == self._sock:
                    # guilty until proven innocent
                    response = "NO"
                    try:
                        data, addr = self._sock.recvfrom(1024)
                    except socket.error:
                        logging.debug("UDP socket closed.")
                        break # this break will end the for un-naturally
                    logging.info(f"Received packet from {addr[0]}:{addr[1]}")
                    decoded = data.decode()
                    logging.debug(f"Data: {decoded}")
                    # fail false
                    if not self._cmd_msg_apply(decoded):
                        response = "OK"

                    self._sock.sendto(response.encode(), addr)
                else:
                    break
            else:
                # skips the break at the end if the for loop was allowed to end naturally
                continue
            # executes if the for loop was also broken
            break

    def stop(self):
        """ stops the UDP thread """
        logging.debug("UDP stop called.")
        self._sock.close()

class SecurityMonitor():
    """ Security Monitor Windowing and Splitting """
    urls = ["rtsp://maglab:magcat@connor.maglab:8554/Camera1_sub",
            "rtsp://maglab:magcat@connor.maglab:8554/Camera2_sub"]

    # initialize with an event and division index
    #  sample division indices to divisions:
    #    0 -> 1x1
    #    1 -> 2x1
    #    2 -> 2x2
    #    3 -> 3x2
    #    4 -> 3x3
    def __init__(self, quit_queue, splitter_refresh_rate, div_idx):
        self.refresh_rate = splitter_refresh_rate
        self._queue_all = quit_queue
        self._div = self.calc_div(div_idx)

        self.que = [multiprocessing.Queue() for _ in range(self._div[2]*2)]
        self.proc = [None] * (self._div[2]*2)
        self.url_idx = list(range(self._div[2]))

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
        assert len(self._div) == 3

        # position
        # calculate column and row
        col_div = self._div[0]
        row_div = self._div[1]
        [col_pos, row_pos] = self._idx2pos(idx)
        # positions must be less than divisions
        assert col_pos < self._div[0]
        assert row_pos < self._div[1]

        # column width calculation
        geo_str=f"{100//self._div[0]}%"
        # row width calculation
        geo_str=f"{geo_str}x{100//self._div[1]}%"
        # column position
        geo_str += self._gen_pos(col_div, col_pos)
        # row position
        geo_str += self._gen_pos(row_div, row_pos)

        return geo_str

    @staticmethod
    def calc_div(index):
        """ 
        calculate number of divisions based on a magic index number
        returns : a three-element list consisting of columns, rows, and total players
        """
        assert index >= 0
        # this function expects a screen that is "wide" and not "tall"
        col = 1
        row = 1
        while index != 0:
            index -= 1
            # column number priority
            if col <= row:
                col += 1
            else:
                row += 1

        # final element is the total number of players visible
        return [col, row, col * row]

    # index to position.  position is a tuple.
    def _idx2pos(self, idx):
        assert idx < self._div[2]
        return [idx % self._div[0], idx // self._div[0]]

    # this process actually contains the mpv stream player
    def _play_process(self, queue_in, queue_out, name):
        idx = self.url_idx[name % self._div[2]]
        geo_str = self._gen_geo_str(idx)
        player = mpv.MPV()
        # a series of configuration options that make the player act like a
        # security monitor
        # empirically determined on the maglab internal network
        player.network_timeout = 10
        player.border = "no"
        player.keepaspect = "no"
        player.ao = "pulseaudio"
        player.profile = "low-latency"
        player.geometry = geo_str
        player.loop_playlist = "inf"
        # enter the camera URL and wait until it starts to play
        player.play(self.urls[idx])
        # wait until the player is playing
        # timeout added here to terminate if the URL is not found
        try:
            logging.debug(f"Waiting for player {name} to start...")
            player.wait_until_playing(timeout=15)
        # set the output event to terminate the player behind this one
        # pylint: disable-next=broad-exception-caught
        except Exception as exc:
            logging.error(f"Player {name} stopped while waiting to start playing: {str(exc)}")
            player.terminate()
        finally:
            logging.debug(f"Asking player below {name} to end.")
            queue_out.put(True)

        while True:
            try:
                _ = queue_in.get(timeout=1)
                # if the queue returns actual data, shut down this thread
                break
            except queue.Empty:
                # normal exception.  the queue should return empty most of the time
                # we use this as an opportunity to run the "finally" block and check the player
                continue
            finally:
                if player.core_shutdown:
                    # shut everything down if the player shuts down unexpectedly
                    logging.critical("Unexpected player shutdown.  Shutting down.")
                    self._queue_all.put(True)
                    # pylint: disable-next=lost-exception
                    break

        logging.info(f"Player {name} stopping.")
        player.terminate()
        del player

    # helper function to spawn a player
    def _handle_player(self, last_p, running = True):
        # inital player logic
        if running:
            # self._div[2] is the number of players visible.
            # the actual number of players is self._div[2] * 2
            i_play = (last_p + self._div[2]) % (self._div[2] * 2)
        else:
            # state where the players are initializing
            i_play = last_p
            last_p = (last_p + self._div[2]) % (self._div[2] * 2)
        logging.debug(f"Starting player: {i_play}")
        self.proc[i_play] = multiprocessing.Process(target=self._play_process, args=(
            self.que[i_play],
            self.que[last_p],
            i_play))
        self.proc[i_play].daemon = True
        # clear the queue
        while not self.que[i_play].empty():
            logging.debug(f"Cleaning queue for player {i_play}")
            # the original get() caused a deadlock
            try:
                _ = self.que[i_play].get_nowait()
            except queue.Empty:
                logging.error(f"Attempt to read non-empty queue for player {i_play} returned empty")
        self.proc[i_play].start()
        logging.info(f"Player process started: {i_play}")

    def main(self):
        """ main / run function within the class """
        logging.info("Starting security monitor")
        assert len(self.urls) >= self._div[2]

        try:
            # start initial players
            for i in range(self._div[2]):
                self._handle_player(i, False)
            time_cnt = 0
            p_cnt = 0
            while True:
                time_cnt += 1
                if time_cnt >= self.refresh_rate:
                    time_cnt = 0
                    # "handle" with the "started" parameter set to True
                    # starts the replacement player which asks the replaced player to stop
                    self._handle_player(p_cnt)
                    self.proc[p_cnt].join(15)
                    # the process is not terminated
                    if self.proc[p_cnt].exitcode is None:
                        logging.error(f"Killing stuck player {p_cnt}")
                        self.proc[p_cnt].kill()
                    else:
                        logging.debug(f"Successfully joined {p_cnt}")
                    p_cnt = (p_cnt + 1) % (self._div[2]*2)
                try:
                    _ = self._queue_all.get(timeout=1)
                    # if the queue returns data, shut everything down
                    break
                except queue.Empty:
                    # normal exception.  the queue should return empty unless we are exiting.
                    continue

        finally:
            logging.info("Asking player processes to exit...")
            for cur_q in self.que:
                cur_q.put(True)

            logging.info("Waiting for player processes...")
            for curr_proc, _ in enumerate(self.proc):
                if self.proc[curr_proc] is not None:
                    self.proc[curr_proc].join(15)
                    # the process is not terminated
                    if self.proc[curr_proc].exitcode is None:
                        logging.error(f"Killing stuck player {curr_proc}")
                        self.proc[curr_proc].kill()

            logging.info("Security Monitor Splitter stopped.")

    def set_url(self, urls):
        """Sets the URLs used by the player based on the function argument"""
        self.urls = copy.deepcopy(urls)

# pylint: disable-next=too-many-instance-attributes
class MonitorTop(mqtt.Client):
    """
    Top Level Security Monitor Management
    there is an explanation for why this is calling "monitorTop."
    the function "SecurityMontior" was actually developed before a monitor
    top was envisioned to encapsulate it.
    """
    class MTState(IntEnum):
        """ This enumerates the player state machine """
        PLAYING = 0
        STOPPED = 1
        RESTART = 2

    class BLIndex(IntEnum):
        """ Enumerates the index of booleans in the list (BL) """
        PM_ABLE = 0
        AUTO = 1
        MOTION = 2
        SCREEN_OFF = 3

    # initialization function
    def __init__(self):
        pgm_path = os.path.dirname(os.path.abspath(__file__))
        with open(f"{pgm_path}/mon_config.json", "r", encoding="utf-8") as config_file:
            # pylint: disable-next=no-member
            self.config = MonitorTop.Config.from_json(config_file.read())

        # tokens
        self._tokens = []

        # X11
        self.disp = display.Display()

        # boolean flags
        # PM_ABLE
        # AUTO
        # MOTION
        # SCREEN_OFF
        self.bools = [False, True, False, False]

        # stops video
        self.stop_playing = multiprocessing.Queue()
        # exits this program
        self.monitor_exit = threading.Event()

        # security monitor state
        self.mtstate = self.MTState.PLAYING
        self.last_mtstate = self.MTState.PLAYING

        # UDP
        self.udp = UDPListen(self.cmd_msg_apply)

        # automatic control
        self.amt = AutoMotionTimer(self.bools, [self.BLIndex.AUTO, self.BLIndex.MOTION],
                [self.mon_on, self.mon_off], self.config.auto_timeout)

        mqtt.Client.__init__(self, mqtt.CallbackAPIVersion.VERSION2)

    @dataclass_json
    @dataclass
    class Config:
        # pylint: disable=too-many-instance-attributes
        """ configuration dataclass """
        name: str
        urls: list[str]
        tokens: list[str]
        event_host: str
        event: str
        mqtt_broker: str
        mqtt_port: Optional[int] = 1883
        mqtt_timeout: Optional[int] = 60
        splitter_refresh_rate: Optional[int] = 300
        splitter_div_mode: Optional[int] = 1
        loglevel: Optional[str] = None
        max_cmd_delta: Optional[int] = 7200
        auto_timeout: Optional[int] = 500

    # overloaded MQTT on_connect function
    def on_connect(self, _, __, ___, reason, ____):
        # pylint: disable=invalid-overridden-method, arguments-differ
        logging.info(f"MQTT connected: {reason}")
        self.subscribe("reporter/checkup_req")
        self.subscribe(f"{self.config.name}/cmd")
        self.subscribe(f"{self.config.event_host}/+")

    def msg_auth(self, msg, code):
        """ message authentication function """
        logging.debug(f"msg_auth called with: {msg} and {code}")
        match = False
        for token in self._tokens:
            calc = Utils.wr_hmac(msg, token)
            logging.debug(f"Calculated hmac as: {calc}")
            if calc == code:
                match = True
                break
        # throws an assertion if there are no matches
        assert match

    def cmd_msg_apply(self, cmd):
        """
        The JSON and HMAC key are contained in a `pair` from Kotlin
        we run the output formatting of a pair through this particular
        regex.
        This output is somewhat equivalent for interpreting Python `tuple`s.
    
        And the HMAC output is b64 encoded.
        """
        retval = 1
        logging.debug(f"Received in command channel: {cmd}")
        matches = re.fullmatch(r"\((\{.+\})\, (.+)\)", cmd)
        if matches is not None:
            logging.debug(f"The split strings are: {matches[1]} and {matches[2]}")
            try:
                data = json.loads(matches[1])
                # validate the message time
                current_time = time.time()
                sent_time = data["time"]
                diff_time = current_time - sent_time
                logging.debug(f"Current time: {current_time}, Sent time: {sent_time}, "\
                        f"Time Diff: {diff_time}")
                assert abs(diff_time) <= self.config.max_cmd_delta

                self.msg_auth(matches[1], matches[2])
                # handle restarting
                if "restart" in data:
                    refresh = data["restart"]
                    logging.info(f"Received monitor restart: {refresh}")
                    if refresh:
                        self.mon_restart()
                        retval = 0
                # handle automatic mode
                elif "auto" in data and data["auto"] is True:
                    logging.info("Received automatic mode enable.")
                    self.bools[self.BLIndex.AUTO] = True
                    retval = 0
                elif "force" in data:
                    self.bools[self.BLIndex.AUTO] = False
                    force = data["force"]
                    logging.info(f"Received monitor status force: {force}")
                    if force:
                        self.mon_on()
                        retval = 0
                    else:
                        self.mon_off()
                        retval = 0

            except (json.JSONDecodeError, AttributeError, AssertionError) as exc:
                logging.info(str(exc)) # apparently not .toString

        return retval

    def mon_on(self):
        """ turns the monitor on. this function changes flags that control the state machine """
        if self.bools[self.BLIndex.SCREEN_OFF]:
            self.bools[self.BLIndex.SCREEN_OFF] = False
            Utils.clear_queue(self.stop_playing)

    def mon_off(self):
        """ turns the monitor off. this function changes flags that control the state machine """
        if self.bools[self.BLIndex.SCREEN_OFF] is False:
            self.bools[self.BLIndex.SCREEN_OFF] = True
            self.stop_playing.put(True)

    def mon_restart(self):
        """ restarts the internal video wall class """
        self.stop_playing.put(True)
        self.mtstate = self.MTState.RESTART

    def on_message(self, _, __, msg):
        # pylint: disable=invalid-overridden-method, arguments-differ
        """ overloaded MQTT on_message function """
        if msg.topic == "reporter/checkup_req":
            logging.info("Checkup requested.")
            dict_msg = {}
            dict_msg[f"{self.config.name} On"] = int(not self.bools[self.BLIndex.SCREEN_OFF])
            logging.debug(f"Checkup message: {dict_msg}")
            self.publish(f"{self.config.name}/checkup", json.dumps(dict_msg))
        elif msg.topic == f"{self.config.name}/cmd":
            # do
            decoded = msg.payload.decode('utf-8')
            logging.info(f"Display Commanded: {decoded}")
            self.cmd_msg_apply(decoded)
        elif msg.topic.startswith(self.config.event_host):
            decoded = msg.payload.decode('utf-8')
            logging.debug(f"Motion message received: {decoded}")
            try:
                data = json.loads(decoded)
                if self.config.event in data and data[self.config.event] != 0:
                    logging.info("Received motion.")
                    self.bools[self.BLIndex.MOTION] = True
            except ValueError:
                logging.info("JSON decode failed.")

    def on_log(self, _, __, level, string):
        # pylint: disable=invalid-overridden-method, arguments-differ
        """ overloaded MQTT on_log function """
        if level == mqtt.MQTT_LOG_DEBUG:
            logging.debug(f"PAHO MQTT DEBUG: {string}")
        elif level == mqtt.MQTT_LOG_INFO:
            logging.info(f"PAHO MQTT INFO: {string}")
        elif level == mqtt.MQTT_LOG_NOTICE:
            logging.info(f"PAHO MQTT NOTICE: {string}")
        else:
            logging.error(f"PAHO MQTT ERROR: {string}")

    def signal_handler(self, signum, _):
        """ signal handling helper function """
        logging.warning(f"Caught a deadly signal: {signum}!")
        self.stop_playing.put(True)
        self.monitor_exit.set()

    def _mt_loop(self):
        """
        main function helper loop, also the state machine
        note that this is where the screen state is controlled.
        the functions mon_on and mon_off just set flags that this function follows
        """
        if self.mtstate != self.last_mtstate:
            logging.debug(f"Montior Loop State: {self.mtstate}")
        # execution
        if self.mtstate == self.MTState.PLAYING:
            Utils.clear_queue(self.stop_playing)
            sm2 = SecurityMonitor(self.stop_playing, self.config.splitter_refresh_rate,
                    self.config.splitter_div_mode)
            sm2.urls = self.config.urls
            if self.bools[self.BLIndex.PM_ABLE]:
                logging.info("Turning Screen ON.")
                self.disp.dpms_force_level(dpms.DPMSModeOn)
                self.disp.sync()
            logging.info("Calling Splitter.")
            # blocking while executing.  unblocked with the queue.
            sm2.main()
        #  restart or stopped
        if self.mtstate == self.MTState.STOPPED:
            if self.last_mtstate == self.MTState.PLAYING:
                if self.bools[self.BLIndex.PM_ABLE]:
                    logging.info("Turning Screen Off.")
                    self.disp.dpms_force_level(dpms.DPMSModeOff)
                    self.disp.sync()
        if self.mtstate != self.MTState.PLAYING:
            self.monitor_exit.wait(1)

        # save the last mtstate before computing state transitions
        self.last_mtstate = self.mtstate

        # transitions
        if self.mtstate == self.MTState.PLAYING:
            if self.bools[self.BLIndex.SCREEN_OFF]:
                self.mtstate = self.MTState.STOPPED
        elif self.mtstate == self.MTState.RESTART:
            self.mtstate = self.MTState.PLAYING
        elif self.mtstate == self.MTState.STOPPED:
            if self.bools[self.BLIndex.SCREEN_OFF] is False:
                self.mtstate = self.MTState.PLAYING


    def main(self):
        # pylint: disable=too-many-statements
        """ main function """
        try:
            if isinstance(logging.getLevelName(self.config.loglevel.upper()), int):
                logging.basicConfig(level=self.config.loglevel.upper())
            else:
                logging.warning("Log level not configured. Defaulting to WARNING.")
        except (KeyError, AttributeError) as err:
            logging.warning(f"Log level not configured. Defaulting to WARNING. Caught: {err}")
        logging.info("Starting Security Monitor Program")
        self._client_id = str.encode(self.config.name)

        # check if the configuration exists
        assert len(self.config.name)

        logging.info("Decoding tokens")
        for token in self.config.tokens:
            try:
                self._tokens.append(Utils.token_decode(token))
            except AssertionError:
                logging.error("Token not accepted")
        if self._tokens:
            logging.debug("Tokens decoded")
        else:
            logging.critical("No tokens accepted.")

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        # X11
        try:
            self.bools[self.BLIndex.PM_ABLE] = self.disp.dpms_capable()
        except ValueError:
            pass
        if not self.bools[self.BLIndex.PM_ABLE]:
            logging.warning("Display is not DPMS capable.")
        logging.debug(f"DPMS capable: {self.bools[self.BLIndex.PM_ABLE]}")

        if self.bools[self.BLIndex.PM_ABLE]:
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
        self.connect(self.config.mqtt_broker, self.config.mqtt_port, self.config.mqtt_timeout)
        self.loop_start()

        logging.info("Starting UDP.")
        self.udp.start()

        logging.info("Starting automatic control.")
        self.amt.start()

        #  security monitor splitter / windower initialize
        while not self.monitor_exit.is_set():
            self._mt_loop()

        logging.info("Monitor top state machine loop exited.")

        logging.info("Stopping automatic control.")
        self.amt.stop()

        if self.bools[self.BLIndex.PM_ABLE]:
            logging.info("Turning Screen ON.")
            self.disp.dpms_force_level(dpms.DPMSModeOn)
            self.disp.sync()

        logging.info("Stopping UDP.")
        self.udp.stop()

        logging.info("Stopping MQTT.")
        self.loop_stop()

# main function for the entire program
if __name__ == "__main__":
    monitor = MonitorTop()
    monitor.main()
