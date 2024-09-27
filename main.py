#!/usr/bin/env python3

import mpv
import threading

def p0(event):
    player = mpv.MPV()
    player.play("rtsp://maglab:magcat@connor.maglab:8554/Camera1_sub")
    while not event.is_set():
        try:
            player.wait_for_playback(timeout=1)
        except TimeoutError:
            continue

def p1(event):
    player2 = mpv.MPV()
    player2.play("rtsp://maglab:magcat@connor.maglab:8554/Camera2_sub")
    while not event.is_set():
        try:
            player2.wait_for_playback(timeout=1)
        except TimeoutError:
            continue


def main():
    
    print("start")
    
    event = threading.Event()
    
    t0 = threading.Thread(target=p0, args=(event,))
    t0.start()
    
    t1 = threading.Thread(target=p1, args=(event,))
    t1.start()

    while not event.is_set():
        try:
            print("Main thread.")
            event.wait(10)
        except KeyboardInterrupt:
            event.set()
            break
    
    print("end")

if __name__ == "__main__":
    main()
