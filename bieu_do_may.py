"""
Lịch mây từng năm cho quan sát thiên văn — mỗi năm 1 ảnh, số liệu thật từng giờ.

Trục ngang: 365 ngày. Trục dọc: 12h trưa -> nửa đêm -> 12h trưa hôm sau (đêm nằm giữa hình).
4 khung: tổng lượng mây, mây tầng thấp (<3 km), giữa (3–8 km), cao (>8 km).
Đè lên: hoàng hôn / bình minh và chạng vạng thiên văn (mặt trời -18°).
Đường đỏ: ngày giữa của mỗi chuỗi >= 3 đêm quang (mây trung bình lúc tối hẳn < 20 %).

Dữ liệu: Open-Meteo Historical Weather API (tái phân tích ERA5, ô lưới ~25 km), lưu đệm trong data-cache/.

Ví dụ:
    python bieu_do_may.py                          # Hà Nội, 2025
    python bieu_do_may.py --tu 2016 --den 2025     # mỗi năm 1 ảnh
    python bieu_do_may.py --lat 21.08 --lon 105.37 --ten "Ba Vì" --tu 2024
"""
import argparse
import datetime as dt
import os
import time
import unicodedata

import numpy as np
import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "data-cache")   # dữ liệu tải về (không đưa lên git)
OUT_DIR = os.path.join(HERE, "images")
API = "https://archive-api.open-meteo.com/v1/archive"
TZ_NAME, TZ_OFFSET = "Asia/Ho_Chi_Minh", 7
VARS = ["cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"]
CLEAR_THRESHOLD = 20      # tổng mây < 20 % thì tính là "quang"

# Màu (bảng màu đã kiểm tra cho người mù màu): tối = trời quang, trắng = mây
INK, INK_2, MUTED, GRID = "#0b0b0b", "#52514e", "#898781", "#e1e0d9"
SURFACE = "#fcfcfb"
RAMP = ["#ffffff", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5",
        "#256abf", "#184f95", "#104281", "#0d366b"]
SUN_COLOR = "#eb6834"
BEST_COLOR = "#b42318"    # đường đỏ đánh dấu tâm vùng đêm quang
MIN_RUN, MAX_GAP = 3, 1   # vùng = ít nhất 3 đêm quang, cho phép xen 1 đêm xấu


# ---------------------------------------------------------------- dữ liệu
def fetch_year(lat, lon, year):
    """Tải 1 năm + ngày 1/1 năm sau (để có nửa sau đêm 31/12). Có lưu đệm."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{lat:.3f}_{lon:.3f}_{year}.npz")
    if os.path.exists(path):
        return dict(np.load(path))
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": f"{year}-01-01", "end_date": f"{year + 1}-01-01",
        "hourly": ",".join(VARS), "timezone": TZ_NAME,
    }
    for attempt in range(4):
        r = requests.get(API, params=params, timeout=120)
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        r.raise_for_status()
        break
    h = r.json()["hourly"]
    data = {v: np.array([np.nan if x is None else x for x in h[v]], dtype=float) for v in VARS}
    data["time"] = np.array(h["time"])
    np.savez_compressed(path, **data)
    time.sleep(1)
    return data


def night_grid(series, year):
    """Chuỗi giờ (từ 1/1 năm đó tới hết 1/1 năm sau) -> lưới 365 ngày x 24 giờ, giờ tính từ 12h trưa."""
    n_days = (dt.date(year + 1, 1, 1) - dt.date(year, 1, 1)).days
    full = series[: (n_days + 1) * 24]
    grid = np.full((n_days, 24), np.nan)
    for d in range(n_days):
        grid[d] = full[d * 24 + 12: d * 24 + 36]
    if n_days == 366:                         # bỏ 29/2 cho gọn
        grid = np.delete(grid, 59, axis=0)
    return grid


# ---------------------------------------------------------------- mặt trời
def sun_events(lat, lon, alt_deg):
    """Giờ (tính từ 12h trưa, đơn vị giờ) mặt trời lặn xuống / mọc lên qua độ cao alt_deg, cho 365 ngày."""
    n = np.arange(1, 366)
    g = 2 * np.pi / 365 * (n - 1)
    decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
            + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    eot = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                    - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))  # phút
    noon = 12 - (4 * lon - 60 * TZ_OFFSET + eot) / 60                       # giờ địa phương
    phi = np.radians(lat)
    cos_h = (np.sin(np.radians(alt_deg)) - np.sin(phi) * np.sin(decl)) / (np.cos(phi) * np.cos(decl))
    h = np.degrees(np.arccos(np.clip(cos_h, -1, 1))) / 15
    sunset = noon + h - 12           # tính từ 12h trưa
    sunrise = noon - h + 12          # sáng hôm sau, tính từ 12h trưa hôm trước
    return sunset, sunrise


# ---------------------------------------------------------------- vùng đêm quang
def night_mean(grid, dusk, dawn):
    """Trung bình mỗi ngày trong khoảng tối hẳn (giữa chạng vạng tối và chạng vạng sáng)."""
    k = np.arange(24) + 0.5
    out = np.full(grid.shape[0], np.nan)
    for d in range(grid.shape[0]):
        m = (k >= dusk[d]) & (k <= dawn[d])
        out[d] = np.nanmean(grid[d, m])
    return out


def best_centers(good):
    """Tìm các chuỗi đêm tốt (>= MIN_RUN, cho phép xen MAX_GAP đêm xấu), trả về ngày ở giữa mỗi chuỗi."""
    centers, start, last, gap = [], None, None, 0
    for d, ok in enumerate(list(good) + [False] * (MAX_GAP + 1)):
        if ok:
            if start is None:
                start = d
            last, gap = d, 0
        elif start is not None:
            gap += 1
            if gap > MAX_GAP:
                n_good = int(np.sum(good[start:last + 1]))
                if n_good >= MIN_RUN:
                    centers.append((start + last) // 2)
                start, gap = None, 0
    return centers


def draw_best(ax, centers, label):
    for c in centers:
        ax.axvline(c + 0.5, color=BEST_COLOR, lw=1.4, zorder=5)
        if label:
            d = dt.date(2025, 1, 1) + dt.timedelta(days=int(c))
            ax.text(c + 0.5, -0.25, f"{d.day}/{d.month}", color=BEST_COLOR, fontsize=7.5,
                    fontweight="semibold", ha="center", va="bottom", clip_on=False)


# ---------------------------------------------------------------- vẽ
def draw_panel(ax, grid, cmap, vmin, vmax, sun, title):
    im = ax.imshow(grid.T, aspect="auto", cmap=cmap, vmin=vmin, vmax=vmax,
                   extent=[0, 365, 24, 0], interpolation="nearest")
    days = np.arange(365) + 0.5
    (ss, sr), (ts, tr) = sun
    for y in (ss, sr):
        ax.plot(days, y, color=SUN_COLOR, lw=1.6)
    for y in (ts, tr):
        ax.plot(days, y, color=SUN_COLOR, lw=1.3, ls=(0, (4, 3)))

    # Vạch: ngày 1 (ghi tên tháng, đậm) và ngày 10, 20 (ghi số, nhạt)
    pos, lab = [], []
    for m in range(1, 13):
        start = (dt.date(2025, m, 1) - dt.date(2025, 1, 1)).days
        pos += [start, start + 9, start + 19]
        lab += [f"T{m}", "10", "20"]
    ax.set_xticks(pos)
    ax.set_xticklabels(lab, ha="left")
    for t, l in zip(ax.get_xticklabels(), lab):
        if l.startswith("T"):
            t.set_fontweight("semibold"); t.set_color(INK)
        else:
            t.set_fontsize(7); t.set_color(MUTED)
    for x in pos[::3][1:]:
        ax.axvline(x, color=MUTED, lw=0.6, alpha=0.45)
    ax.set_yticks(range(0, 25, 3))
    ax.set_yticklabels([f"{(12 + h) % 24}h" for h in range(0, 25, 3)])
    ax.set_xlim(0, 365)
    ax.set_ylim(24, 0)
    ax.set_title(title, loc="left", fontsize=11, color=INK, fontweight="semibold", pad=6)
    ax.tick_params(colors=MUTED, labelcolor=INK_2, labelsize=8.5, length=3)
    for s in ax.spines.values():
        s.set_visible(False)
    return im


def label_sun_lines(ax, sun):
    """Ghi nhãn trực tiếp lên các đường mặt trời (ở khung chính)."""
    (ss, sr), (ts, tr) = sun
    x = 182  # giữa năm
    kw = dict(color=SUN_COLOR, fontsize=8.5, fontweight="semibold", ha="center",
              bbox=dict(boxstyle="round,pad=0.15", fc=SURFACE, ec="none", alpha=0.85))
    ax.text(x, ss[x] - 0.35, "hoàng hôn", va="bottom", **kw)
    ax.text(x, ts[x] + 0.35, "tối hẳn (chạng vạng thiên văn)", va="top", **kw)
    ax.text(x, tr[x] - 0.35, "bắt đầu sáng", va="bottom", **kw)
    ax.text(x, sr[x] + 0.35, "bình minh", va="top", **kw)


def draw_year(lat, lon, ten, year, out_dir):
    d = fetch_year(lat, lon, year)
    grids = {v: night_grid(d[v], year) for v in VARS}

    sun = (sun_events(lat, lon, -0.833), sun_events(lat, lon, -18))
    dusk, dawn = sun[1]
    good = night_mean(grids["cloud_cover"], dusk, dawn) < CLEAR_THRESHOLD
    centers = best_centers(good)
    print(f"  {year}: tâm các vùng đêm quang:", ", ".join(
        f"{(dt.date(2025, 1, 1) + dt.timedelta(days=int(c))):%d/%m}" for c in centers) or "không có")

    cmap_cloud = LinearSegmentedColormap.from_list("cloud", RAMP[::-1])     # trắng = nhiều mây
    fig = plt.figure(figsize=(19, 15))
    gs = fig.add_gridspec(4, 2, height_ratios=[2.2, 1, 1, 1], width_ratios=[60, 1],
                          hspace=0.32, wspace=0.02, left=0.06, right=0.95, top=0.905, bottom=0.04)
    fig.suptitle(f"Lịch mây cho quan sát thiên văn — {ten} ({lat:.2f}° B, {lon:.2f}° Đ), {year}",
                 x=0.06, y=0.975, ha="left", fontsize=15, fontweight="semibold", color=INK)
    fig.text(0.06, 0.945,
             "Màu tối = trời quang, trắng = nhiều mây. Trục dọc: 12h trưa → nửa đêm → 12h trưa hôm sau. "
             f"Đường đỏ = ngày giữa của mỗi chuỗi ≥ {MIN_RUN} đêm quang (mây đêm < {CLEAR_THRESHOLD} %). "
             "Số liệu thật từng giờ. Nguồn: Open-Meteo / ERA5 (ô lưới ~25 km).",
             fontsize=9.5, color=INK_2)

    names = {"cloud_cover": "Tổng lượng mây từng giờ",
             "cloud_cover_low": "Mây tầng thấp (dưới 3 km) — che kín, sương mù, mưa phùn",
             "cloud_cover_mid": "Mây tầng giữa (3–8 km)",
             "cloud_cover_high": "Mây tầng cao (trên 8 km) — mây ti, làm trời mờ nhưng vẫn thấy sao sáng"}
    ax0 = None
    for i, v in enumerate(VARS):
        ax = fig.add_subplot(gs[i, 0], sharex=ax0)
        im = draw_panel(ax, grids[v], cmap_cloud, 0, 100, sun, names[v])
        draw_best(ax, centers, label=(i == 0))
        if i == 0:
            ax0 = ax
            label_sun_lines(ax, sun)
            ax.set_title(names[v], loc="left", fontsize=11, color=INK, fontweight="semibold", pad=18)
        cax = fig.add_subplot(gs[i, 1])
        cb = fig.colorbar(im, cax=cax)
        cb.set_label("% che phủ", color=INK_2, fontsize=9)
        cax.tick_params(colors=MUTED, labelcolor=INK_2, labelsize=8)
        for sp in cax.spines.values():
            sp.set_visible(False)

    out = os.path.join(out_dir, f"lich-may-{slugify(ten)}-{year}.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def slugify(text):
    t = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return "".join(c for c in t if not unicodedata.combining(c)).replace(" ", "-")


def main():
    p = argparse.ArgumentParser(description="Lịch mây từng năm cho quan sát thiên văn (mỗi năm 1 ảnh)")
    p.add_argument("--lat", type=float, default=21.03)
    p.add_argument("--lon", type=float, default=105.85)
    p.add_argument("--ten", default="Hà Nội")
    p.add_argument("--tu", type=int, default=2025, help="năm đầu")
    p.add_argument("--den", type=int, default=None, help="năm cuối (mặc định = năm đầu)")
    p.add_argument("--thu-muc", default=OUT_DIR, help="nơi lưu ảnh")
    a = p.parse_args()
    den = a.den or a.tu

    out_dir = os.path.join(a.thu_muc, slugify(a.ten))
    os.makedirs(out_dir, exist_ok=True)
    plt.rcParams.update({"font.family": "Segoe UI", "figure.facecolor": SURFACE,
                         "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE})
    for year in range(a.tu, den + 1):
        print(f"Năm {year}...", flush=True)
        draw_year(a.lat, a.lon, a.ten, year, out_dir)
    print("Đã lưu ảnh vào:", out_dir)


if __name__ == "__main__":
    main()
