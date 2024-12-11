# security_monitor

![pylint](https://img.shields.io/badge/PyLint-10.00-brightgreen?logo=python&logoColor=white)

A bit of software to display a video wall using MPV.  This package is primarily intended for displaying rtsp streams from IP cameras.

## Branch Description
This branch exists for initial development of the security monitor.  The exit of initial development will likely be the the addition of configurable items for:
* automatic control
* UDP ip / port
* URLs
* screen divisions
* screen "on" input

## Description
This software mainly controls a video wall made of MPV instances.  There are three interfaces that control how the MPV instances run:
* MQTT PIR input (haldor compatible)
* MQTT control
* UDP control

There are two modes in which the wall is controlled:
* automatic 
* manual

### Automatic Mode
In automatic mode, a motion event triggers the system to stay on for a period of time.

### Manual Mode
In manual mode, on and off is controlled by messaging through either MQTT or UDP.

### Message Format
This section provides a short overview of how the messages for decoding look.
A tuple-looking message is received: `(<message>,<hmac>)`
The `<message>` is data in JSON and utf-8.  The `<hmac>` is base64.

#### Message Processing
The root element of the JSON message is a dictionary of keys and values.
At the moment, there are three keys in the message that are important:
* restart
* auto
* force

A `restart` hoolean value of true signals to the program that the splitter needs to restart each MPV instance.

An `auto` boolean value controls whether the monitor should automatically control screen on / off and playing from the motion sensor.

A `force` boolean value controls whether the monitor is on / off.  Receiving a valid `force` message will disable automatic mode.

## Dependencies
This is a list of non-default python packages that may need to be downloaded for this software to work:
* python-mpv
* Xlib
* paho-mqtt
* dataclasses-json

## Installation
Modify startup applications in xfce to include this python script either with or without a terminal emulator attached.

## Configuration
An example configuration file will be provided.

## Help
Please leave a Github issue or email the default MAG Laboratory contact at maglaboratory dot org.

## Authors
* @blu006

## Version History 
TODO

## License
Public Domain

## Acknowledgements
Thanks to Andrew Rowson for explaining why this program kept deadlocking: https://www.growse.com/2018/04/23/python-multiprocessing-challenges.html 
