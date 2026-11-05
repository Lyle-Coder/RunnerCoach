# -*- coding: utf-8 -*-
"""临时：按纵向偏移截取卡片局部，便于目视校验排版。用法 py tools/_shot.py <offset> <h> <scale> <out>"""
import sys, os, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "output", "plan-105k-card.html")
CROP = os.path.join(ROOT, "output", "_crop.html")
CHROME = r"C:/Program Files/Google/Chrome/Application/chrome.exe"

off, h, scale, out = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
s = open(SRC, encoding="utf-8").read()
s = s.replace(".poster{width:1080px;height:1920px;",
              ".poster{width:1080px;height:1920px;margin-top:-%spx;" % off)
s = s.replace("body{background:#fff;overflow:hidden;", "body{background:#fff;overflow:hidden;")
open(CROP, "w", encoding="utf-8").write(s)
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                "--force-device-scale-factor=%s" % scale,
                "--window-size=1080,%s" % h,
                "--screenshot=" + os.path.join(ROOT, "output", out),
                "file:///" + CROP.replace("\\", "/")], check=False)
print("shot", out)
