#!/usr/bin/env python3

import mpv
import multiprocessing
import logging
from Xlib.ext import dpms
from Xlib import display
import paho.mqtt.client as mqtt

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
    
    # MPV
    event_all = None
    evt = [None] * 4
    thr = [None] * 4
    
    # X11
    disp = None

    def __init__(self, quit_event):
        self.event_all = quit_event

    def __init__(self):
        self.event_all = multiprocessing.Event()
    
    def play_thread(self, event_in, event_out, span, pos, url):
        player = mpv.MPV()
        player.border = "no"
        player.keepaspect = "no"
        geo_str = gen_geo_str(2,1,pos,0)
        print("Geometry: " + geo_str)
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
                except TimeoutError:
                    continue
                except mpv.ShutdownError:
                    self.event_all.set()
                except KeyboardInterrupt:
                    continue
        finally:
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
        print(f"Starting player: {next_pi}")
        self.thr[next_pi] = multiprocessing.Process(target=self.play_thread, args=(
            self.evt[next_pi],
            self.evt[p_cnt],
            0,
            pos,
            url))
        self.evt[next_pi].clear()
        self.thr[next_pi].start()
    
    def main(self):
        
        print("Starting 2x security monitor")

        self.evt = [multiprocessing.Event() for _ in range(4)]
        self.thr = [None] * 4
    
        try: 
            self.handle_player(0, False)
            self.handle_player(1, False)
            time_cnt = 0
            p_cnt = 0
            while not self.event_all.is_set():
                try:
                    self.event_all.wait(10)
                    time_cnt += 1
                    if (time_cnt >= 30):
                        time_cnt = 0
                        self.handle_player(p_cnt)
                        self.thr[p_cnt].join()
                        p_cnt = (p_cnt + 1) % 4
                except KeyboardInterrupt:
                    self.event_all.set()
                    raise
        except KeyboardInterrupt:
            print("Caught interrupt.")
        
        for t in self.thr:
            if not t == None:
                t.join()

        print("Stopping 2x security monitor.")

if __name__ == "__main__":
    sm2 = SecurityMonitorX2()
    sm2.main()
