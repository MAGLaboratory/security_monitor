#!/usr/bin/env python3

import mpv
import multiprocessing
import threading
import logging
import signal
from Xlib.ext import dpms
from Xlib import display
import paho.mqtt.client as mqtt
from enum import Enum
from dataclasses import dataclass
from dataclasses_json import dataclass_json

def gen_pos(div, pos):
    # assumes that these values were already checked
    if (pos == 0):
        pos_str = "+0"
    elif (pos < div-1):
        pos_str = f"+{100*pos//div}%"
    else:
        pos_str = "-0"

    return pos_str

def gen_geo_str(colDiv, rowDiv, colPos, rowPos):
    # divisions must be greater than 0
    assert colDiv > 0
    assert rowDiv > 0
    # positions must be less than divisions
    assert colPos < colDiv
    assert rowPos < rowDiv

    # column width calculation
    geo_str=f"{100//colDiv}%"
    # row width calculation
    geo_str=f"{geo_str}x{100//rowDiv}%"
    # column position
    geo_str += gen_pos(colDiv, colPos)
    # row position
    geo_str += gen_pos(rowDiv, rowPos)

    return geo_str

class SecurityMonitorX2():
    urls = ["rtsp://maglab:magcat@connor.maglab:8554/Camera1_sub",
            "rtsp://maglab:magcat@connor.maglab:8554/Camera2_sub"]
    
    def __init__(self, quit_event):
        self.event_all = quit_event
    
    def play_thread(self, event_in, event_out, span, pos, url):
        player = mpv.MPV()
        player.border = "no"
        player.keepaspect = "no"
        geo_str = gen_geo_str(2,1,pos,0)
        player.geometry = geo_str
        player.ao = "pulseaudio"
        player.profile = "low-latency"
        player.play(url)
        player.wait_until_playing()
        event_out.set()
        try:
            while not self.event_all.is_set() and not event_in.is_set():
                try:
                    player.wait_for_event(None, timeout=1)
                    logging.debug(f".{pos}")
                except TimeoutError:
                    continue
                except mpv.ShutdownError:
                    self.event_all.set()
                except KeyboardInterrupt:
                    logging.warn("Player caught Keyboard Interrupt.")
                    continue
        finally:
            logging.debug("Stopping player.")
            player.terminate()
            del player
    
    
    def handle_player(self, p_cnt, init_d = True):
        # inital player logic
        if (init_d):
            next_pi = (p_cnt + 2) % 4
        else:
            next_pi = p_cnt
            p_cnt = (p_cnt + 2) % 4
        pos = p_cnt % 2
        url = self.urls[pos]
        logging.info(f"Starting player: {next_pi}")
        self.thr[next_pi] = multiprocessing.Process(target=self.play_thread, args=(
            self.evt[next_pi],
            self.evt[p_cnt],
            0,
            pos,
            url))
        self.thr[next_pi].daemon = True
        self.evt[next_pi].clear()
        self.thr[next_pi].start()
    
    def main(self):
        
        logging.info("Starting 2x security monitor")

        self.evt = [multiprocessing.Event() for _ in range(4)]
        self.thr = [None] * 4
        self.event_w = multiprocessing.Event()
    
        try: 
            self.handle_player(0, False)
            self.handle_player(1, False)
            time_cnt = 0
            p_cnt = 0
            while not self.event_all.is_set():
                logging.debug("Splitter Waiting.")
                self.event_w.wait(10)
                time_cnt += 1
                if (time_cnt >= 30):
                    time_cnt = 0
                    self.handle_player(p_cnt)
                    self.thr[p_cnt].join()
                    p_cnt = (p_cnt + 1) % 4
        finally:
            logging.info("Waiting for player threads...")
            for t in self.thr:
                if not t == None:
                    t.join()

            logging.info("Stopping 2x security monitor.")

class MonitorTop(mqtt.Client):
    class MTState(Enum):
        PLAYING = 0
        STOPPED = 1
        RESTART = 2

    def __init__(self, callbackAPIVersion):
        self.screenOff = threading.Event()
        self.stopPlaying = threading.Event()
        self.monitorExit = threading.Event()
        self.mtstate = self.MTState.PLAYING
        self.last_mtstate = self.MTState.PLAYING

        mqtt.Client.__init__(self, callbackAPIVersion)

    @dataclass_json
    @dataclass
    class config:
        name: str

    def on_connect(self, obj, flag, reason, properties):
        logging.info(f"mqtt connected: {reason}")
        self.subscribe("reporter/checkup_req")
        self.subscribe("secmon00/CMD_DisplayOn")

    def on_message(self, obj, userdata, msg):
        if msg.topic == "reporter/checkup_req":
            logging.info("Checkup requested.")
            # checkup
        elif msg.topic == "secmon00/CMD_DisplayOn":
            decoded = msg.payload.decode('utf-8')
            logging.info("Display Commanded: " + decoded)
            if (decoded.lower() == "false" or decoded == "0"):
                self.screenOff.set()
                self.stopPlaying.set()
            if (decoded.lower() == "true" or decoded == "1"):
                self.screenOff.clear()
                self.stopPlaying.clear()

    def on_log(self, obj, level, string):
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
        self.event_w = multiprocessing.Event()

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

        if self.pm_able:
            # disable screensaver
            self.disp.set_screen_saver(0, 0, True, True)
            self.disp.sync()
            # enable DPMS
            self.disp.dpms_enable()
            self.disp.sync()
            # set DPMS timers to 0
            self.disp.dpms_set_timeouts(0, 0, 0)
            self.disp.sync()

        self.connect("hal.maglab", 1883, 60)
        self.loop_start()

        sm2 = SecurityMonitorX2(self.stopPlaying)
        while not self.monitorExit.is_set():
            logging.debug("Montior Loop")
            # execution
            if self.mtstate == self.MTState.PLAYING:
                self.stopPlaying.clear()
                self.disp.dpms_force_level(dpms.DPMSModeOn)
                self.disp.sync()
                sm2.main()
            # restart or stopped
            if self.mtstate == self.MTState.STOPPED:
                if self.last_mtstate == self.MTState.PLAYING:
                    self.disp.dpms_force_level(dpms.DPMSModeOff)
                    self.disp.sync()
            if self.mtstate != self.MTState.PLAYING:
                self.event_w.wait(10)

            # transitions
            if self.mtstate == self.MTState.PLAYING:
                if self.screenOff.is_set():
                    self.mtstate = self.MTState.STOPPED
            elif self.mtstate == self.MTState.RESTART:
                self.mtstate = self.MTState.PLAYING
            elif self.mtstate == self.MTState.STOPPED:
                if not self.screenOff.is_set():
                    self.mtstate = self.MTState.PLAYING

            self.last_mtstate = self.mtstate

        self.loop_stop()

if __name__ == "__main__":
    monitor = MonitorTop(mqtt.CallbackAPIVersion.VERSION1)
    monitor.main()
