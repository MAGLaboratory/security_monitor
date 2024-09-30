#!/usr/bin/env python3

import mpv
import threading

def p0(event):
    player = mpv.MPV()
    player.border = "no"
    player.geometry = "50%x100%+0+0"
    player.keepaspect = "no"
    player.play("rtsp://maglab:magcat@connor.maglab:8554/Camera1_sub")
    while not event.is_set():
        try:
            player.wait_for_shutdown(timeout=1)
        except TimeoutError:
            continue
        except:
            event.set()
    player.terminate()

def p1(event):
    player2 = mpv.MPV()
    player2.border = "no"
    player2.geometry = "50%x100%-0-0"
    player2.keepaspect = "no"
    player2.play("rtsp://maglab:magcat@connor.maglab:8554/Camera2_sub")
    while not event.is_set():
        try:
            player2.wait_for_shutdown(timeout=1)
        except TimeoutError:
            continue
        except: 
            event.set()
    player2.terminate()


def main():
    
    print("start")
    
    event = threading.Event()
    

    try: 
        while True:
            event.clear()
            counter = 0
            t0 = threading.Thread(target=p0, args=(event,))
            t0.start()
    
            t1 = threading.Thread(target=p1, args=(event,))
            t1.start()
            while not event.is_set():
                try:
                    print("Main thread.")
                    event.wait(10)
                    counter += 1
                    if (counter == 30):
                        counter = 0
                        event.set()
                except KeyboardInterrupt:
                    event.set()
                    raise
            t0.join()
            t1.join()
    except KeyboardInterrupt:
        print("caught interrupt.")
    
    print("end")

if __name__ == "__main__":
    main()
