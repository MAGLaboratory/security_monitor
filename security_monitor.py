#!/usr/bin/env python3

import mpv
import multiprocessing

urls = ["rtsp://maglab:magcat@connor.maglab:8554/Camera1_sub",
        "rtsp://maglab:magcat@connor.maglab:8554/Camera2_sub"]

def play_thread(event_all, event_in, event_out, span, pos, url):
    player = mpv.MPV()
    player.border = "no"
    player.keepaspect = "no"
    geo_str = "50%x100%"
    if (pos == 0):
        geo_str = f"{geo_str}+0+0"
    elif (pos == 1):
        geo_str = f"{geo_str}-0-0"
    player.geometry = geo_str
    player.ao = "pulseaudio"
    player.play(url)
    player.wait_until_playing()
    event_out.set()
    try:
        while not event_all.is_set() and not event_in.is_set():
            try:
                player.wait_for_event(None, timeout=1)
            except TimeoutError:
                continue
            except mpv.ShutdownError:
                event_all.set()
            except KeyboardInterrupt:
                continue
    finally:
        player.terminate()

    del player

def handle_player(p_cnt, event_all, thr, evt, init_d = True):
    # inital player logic
    if (init_d):
        next_pi = (p_cnt + 2) % 4
    else:
        next_pi = p_cnt
        p_cnt = (p_cnt + 2) % 4
    pos = p_cnt % 2
    url = urls[pos]
    print(f"Starting player: {next_pi}")
    thr[next_pi] = multiprocessing.Process(target=play_thread, args=(
        event_all,
        evt[next_pi],
        evt[p_cnt],
        0,
        pos,
        url))
    evt[next_pi].clear()
    thr[next_pi].start()

def main():
    
    print("Start")
    
    event_all = multiprocessing.Event()
    evt = [multiprocessing.Event() for _ in range(4)]
    thr = [None] * 4

    try: 
        handle_player(0, event_all, thr, evt, False)
        handle_player(1, event_all, thr, evt, False)
        time_cnt = 0
        p_cnt = 0
        while not event_all.is_set():
            try:
                event_all.wait(10)
                time_cnt += 1
                if (time_cnt >= 30):
                    time_cnt = 0
                    handle_player(p_cnt, event_all, thr, evt)
                    thr[p_cnt].join()
                    p_cnt = (p_cnt + 1) % 4
            except KeyboardInterrupt:
                event_all.set()
                raise
    except KeyboardInterrupt:
        print("Caught interrupt.")
    
    for t in thr:
        if not t == None:
            t.join()
    print("End")

if __name__ == "__main__":
    main()
