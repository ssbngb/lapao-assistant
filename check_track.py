"""轨迹预演：不连模拟器，按标准 400m 跑道几何算出轨迹并画图。

用途：开跑前确认轨迹形状是「体育场形」（两直道+两半圆）且落在真实跑道范围内。

用法：
    .venv\\Scripts\\python.exe check_track.py            # 默认 2.05km
    .venv\\Scripts\\python.exe check_track.py 1700       # 指定米数
"""
import sys, math, random
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from PIL import Image, ImageDraw

TARGET_M = float(sys.argv[1]) if len(sys.argv) > 1 else 2050.0

# 与 campus_run.py 保持一致的标准 400m 跑道几何
LANE_STRAIGHT = 84.39
LANE_RADIUS = 36.5
LANE_LEN = 2 * LANE_STRAIGHT + 2 * math.pi * LANE_RADIUS
STEP = 4.0
JITTER = 0.5


def lane_pos(s):
    """返回 (north_m, east_m)，标准跑道几何，逆时针。"""
    A = LANE_STRAIGHT / 2.0
    R = LANE_RADIUS
    P1 = LANE_STRAIGHT
    P2 = P1 + math.pi * R
    P3 = 2 * P1 + math.pi * R
    P4 = 2 * P1 + 2 * math.pi * R
    s = s % P4
    if s < P1:
        return (-A + s, R)
    elif s < P2:
        th = (s - P1) / R
        return (A + R * math.sin(th), R * math.cos(th))
    elif s < P3:
        return (A - (s - P2), -R)
    else:
        th = math.pi + (s - P3) / R
        return (-A + R * math.sin(th), R * math.cos(th))


# --- 生成轨迹 ---
lane_s = 0.0
pts = []
done = 0.0
while done < TARGET_M:
    st = min(STEP, TARGET_M - done)
    lane_s = (lane_s + st) % LANE_LEN
    dn, de = lane_pos(lane_s)
    dn += random.uniform(-JITTER, JITTER)
    de += random.uniform(-JITTER, JITTER)
    pts.append((de, dn))        # (x=东, y=北)
    done += st

# --- 统计 ---
xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
w_m, h_m = maxx - minx, maxy - miny

# 累计转角
total_turn = 0.0
for i in range(2, len(pts)):
    v1 = (pts[i-1][0] - pts[i-2][0], pts[i-1][1] - pts[i-2][1])
    v2 = (pts[i][0] - pts[i-1][0], pts[i][1] - pts[i-1][1])
    if abs(v1[0]) + abs(v1[1]) < 1e-9 or abs(v2[0]) + abs(v2[1]) < 1e-9:
        continue
    a1 = math.atan2(v1[1], v1[0]); a2 = math.atan2(v2[1], v2[0])
    total_turn += abs((a2 - a1 + math.pi) % (2 * math.pi) - math.pi)
turn_deg = math.degrees(total_turn)
laps = done / LANE_LEN

# --- 画图（含标准跑道参考线）---
W, H, M = 820, 700, 60
pad = 30
sx = (W - 2 * M) / max(w_m + 2 * pad, 1e-6)
sy = (H - 2 * M) / max(h_m + 2 * pad, 1e-6)
s = min(sx, sy)


def T(x, y):
    return (M + (x - minx + pad) * s, H - M - (y - miny + pad) * s)


img = Image.new("RGB", (W, H), (248, 248, 244))
d = ImageDraw.Draw(img)

# 标准跑道理想轮廓（灰色虚线参考）
ideal = [lane_pos(t * LANE_LEN / 360.0) for t in range(361)]
ideal_pts = [T(de, dn) for dn, de in ideal]
d.line(ideal_pts, fill=(205, 205, 200), width=2)

# 实际轨迹
path = [T(x, y) for x, y in pts]
if len(path) > 1:
    d.line(path, fill=(22, 160, 100), width=3)
d.ellipse([path[0][0]-6, path[0][1]-6, path[0][0]+6, path[0][1]+6], fill=(30, 90, 220))
d.ellipse([path[-1][0]-6, path[-1][1]-6, path[-1][0]+6, path[-1][1]+6], fill=(220, 60, 60))

d.text((M, 16), f"track preview  {TARGET_M/1000:.2f} km   laps={laps:.1f}   pts={len(pts)}", fill=(40, 40, 40))
d.text((M, 32), f"bbox = {w_m:.0f} x {h_m:.0f} m   (400m lane outer ≈ 73 x 157 m)", fill=(40, 40, 40))
d.text((M, 48), f"total turn = {turn_deg:.0f} deg", fill=(40, 40, 40))
d.text((M, H - 26), "green=actual track   gray=ideal 400m lane   blue=start  red=end", fill=(100, 100, 100))
img.save("track_preview.png")

print(f"目标里程      {TARGET_M/1000:.2f} km   ({laps:.1f} 圈)")
print(f"采样点        {len(pts)}")
print(f"包围盒        {w_m:.0f} m × {h_m:.0f} m")
print(f"累计转角      {turn_deg:.0f}°")
print()
# 标准 400m 场地外廓：直道 84.39 + 2R = 157.4 (南北) ; 2R = 73 (东西)
exp_w, exp_h = 2 * LANE_RADIUS, LANE_STRAIGHT + 2 * LANE_RADIUS
ok_shape = abs(h_m - exp_h) < 25 and abs(w_m - exp_w) < 25
ok_turn = turn_deg >= 360 * max(laps - 0.5, 0.5)
if ok_shape and ok_turn:
    print(f"判定：跑道形状正确（应约 {exp_w:.0f}×{exp_h:.0f} m），转角符合跑圈 → 可以开跑")
elif turn_deg < 120:
    print("判定：几乎是直线 → 反作弊会终止")
else:
    print(f"判定：形状偏差较大（实测 {w_m:.0f}×{h_m:.0f}，应约 {exp_w:.0f}×{exp_h:.0f}）→ 检查跑道参数")
print("轨迹图已保存: track_preview.png")
