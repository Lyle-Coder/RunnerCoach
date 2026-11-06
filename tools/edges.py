"""扫描看板 PNG：找出左右两列卡片的底部 y，用于排版对齐。"""
import subprocess, sys

PNG = sys.argv[1] if len(sys.argv) > 1 else "output/card-YYYY-MM-DD.png"

def col_profile(x0, x1):
    out = subprocess.run(
        ["tools/ffmpeg.exe", "-v", "error", "-i", PNG,
         "-vf", f"crop={x1-x0}:ih:{x0}:0", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True).stdout
    w = x1 - x0
    # 每行取平均亮度
    rows = []
    for i in range(0, len(out), w):
        seg = out[i:i+w]
        rows.append(sum(seg) / len(seg))
    return rows

def last_card_row(rows, thresh=27.0):
    last = 0
    for y, v in enumerate(rows):
        if v > thresh:
            last = y
    return last

for name, (x0, x1) in (("左列", (60, 500)), ("右列", (560, 1020))):
    rows = col_profile(x0, x1)
    print(f"{name}: 高度 {len(rows)}px, 卡片底 y≈{last_card_row(rows)}")
