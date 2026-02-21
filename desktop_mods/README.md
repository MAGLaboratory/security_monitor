# xfce desktop modifications to run this script nicely

xfce opens a autostart apps and `xfce4-panel` which both interfere with the
normal functioning of this app

## Disable Autostart Apps
copy the autostart apps from the system autostart and into your local autostart
```
cp /etc/xdg/autostart/* ~/.config/autostart
```
disable autostarting apps
```
cd ~/.config/autostart
for f in `ls`; do
    echo "Hidden=true" >> $f;
done
```

## Disable `xfce4-panel`
from `/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml`, delete the
property containing `xfce4-panel`
