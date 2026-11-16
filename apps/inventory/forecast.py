"""Dự báo nhu cầu theo tuần & SS/ROP/Q từ dự báo – đặc tả "Sửa mô hình dự báo nhu cầu & SS/ROP" (16/11/2026).

MỌI công thức dự báo nằm ở module này; calc_param(), màn SS/ROP và file Excel đều gọi tới đây (không chép công thức).

Phần A – hàm thuần (nhận danh sách số theo tuần, trả kết quả, KHÔNG đọc DB) -> test bằng SimpleTestCase:
  moving average (MA, < 8 tuần) · Holt mức + xu hướng (8–103 tuần) · Holt-Winters mùa theo tháng (≥ 104 tuần)
  chọn α, β bằng backtest (WAPE nhỏ nhất) · σe = √(Σe² / (H − 1)) · WAPE cách cũ (TB 13 tuần trước)
Phần B – nối dự báo vào SS / ROP / Q (mục 5):
  D_LT = tổng dự báo LT ngày tới (dự báo tuần / 7 cho từng ngày) · σe_ngày = σe_tuần / √7 · F_ngày = D_LT / LT
  FULL:   SS = Z·√(LT·σe_ngày² + F_ngày²·σLT²)      DEMAND: SS = Z·σe_ngày·√LT      MANUAL: SS nhập tay
  ROP = D_LT + SS ;  Q = max(MOQ; tổng dự báo review_days ngày tới) (Q nhập tay ghi đè, làm tròn xe như cũ)
"""
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

ALPHAS = (0.1, 0.2, 0.3, 0.4, 0.5)
BETAS = (0.05, 0.1, 0.2, 0.3)
HORIZON = 13          # chỉ dự báo tối đa 13 tuần tới
MIN_HOLT = 8          # < 8 tuần: trung bình (MA)
MIN_HW = 104          # ≥ 104 tuần (2 năm): Holt-Winters mùa theo tháng
MIN_SEASON_POINTS = 4  # chỉ số mùa có < 4 điểm dữ liệu -> 1
BASELINE_WEEKS = 13   # "cách cũ": trung bình 13 tuần trước

MODEL_LABELS = {"MA": "Chưa đủ dữ liệu để dự báo", "HOLT": "Holt (α, β)", "HW": "Holt-Winters (α, β, mùa theo tháng)"}


# ------------------------------------------------------------------ tiện ích tuần
def monday(d):
    """Tuần bắt đầu thứ Hai."""
    return d - timedelta(days=d.weekday())


def week_month(week_start):
    """Tháng của tuần = tháng của ngày thứ Năm trong tuần (week_start + 3 ngày)."""
    return (week_start + timedelta(days=3)).month


def week_range(first, last):
    """Danh sách thứ Hai từ first tới last (gồm cả 2 đầu)."""
    out, w = [], monday(first)
    while w <= last:
        out.append(w)
        w += timedelta(days=7)
    return out


def fill_weeks(by_week, first, last):
    """Chuỗi tuần liên tục: tuần không có đơn có giá trị 0 (thiếu tuần 0 thì mô hình hiểu sai là nhu cầu liên tục)."""
    weeks = week_range(first, last)
    return weeks, [float(by_week.get(w, 0.0)) for w in weeks]


# ------------------------------------------------------------------ mô hình
def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs):
    """Độ lệch chuẩn MẪU (STDEV.S)."""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def holt(y, alpha, beta):
    """Holt mức + xu hướng. Khởi tạo L₀ = TB 4 tuần đầu, T₀ = (TB tuần 5–8 − TB tuần 1–4) / 4, rồi cập nhật từ tuần 1
    tới tuần n. Trả về (L_n, T_n, one_step) với one_step[t] = max(0, L_{t−1} + T_{t−1}) = dự báo tuần t lập từ dữ liệu
    tới tuần t−1."""
    head = y[:4]
    level = _mean(head)
    trend = (_mean(y[4:8]) - level) / 4 if len(y) >= 8 else 0.0
    one_step = []
    for v in y:
        one_step.append(max(0.0, level + trend))
        prev = level
        level = alpha * v + (1 - alpha) * (level + trend)
        trend = beta * (level - prev) + (1 - beta) * trend
    return level, trend, one_step


def seasonal_indices(weeks, y):
    """12 chỉ số mùa theo tháng. Bỏ xu hướng bằng hồi quy tuyến tính y = a + b·t (bình phương tối thiểu),
    r_t = y_t / (a + b·t) (bỏ tuần có a + b·t ≤ 0); S_m = TB các r_t của tháng m (< 4 điểm -> 1); chuẩn hóa TB = 1."""
    n = len(y)
    if n < 2:
        return [1.0] * 12
    ts = list(range(n))
    tm, ym = _mean(ts), _mean(y)
    sxx = sum((t - tm) ** 2 for t in ts)
    b = sum((t - tm) * (v - ym) for t, v in zip(ts, y)) / sxx if sxx else 0.0
    a = ym - b * tm
    buckets = {m: [] for m in range(1, 13)}
    for t, w, v in zip(ts, weeks, y):
        base = a + b * t
        if base > 0:
            buckets[week_month(w)].append(v / base)
    raw = [(_mean(buckets[m]) if len(buckets[m]) >= MIN_SEASON_POINTS else 1.0) for m in range(1, 13)]
    avg = _mean(raw)
    return [s / avg for s in raw] if avg > 0 else [1.0] * 12


def _hw_one_step(weeks, y, alpha, beta, season):
    """Holt trên chuỗi đã khử mùa y′ = y / S, dự báo 1 tuần trước F_t = (L + T)·S_{m(t)}."""
    s = [season[week_month(w) - 1] or 1.0 for w in weeks]
    level, trend, one = holt([v / f for v, f in zip(y, s)], alpha, beta)
    return level, trend, [f1 * f for f1, f in zip(one, s)]


def wape(actual, fc):
    """WAPE = Σ|e| / Σy (thay MAPE vì có tuần bằng 0). Σy = 0: 0 nếu không sai, ngược lại vô cùng."""
    num = sum(abs(a - f) for a, f in zip(actual, fc))
    den = sum(actual)
    if den <= 0:
        return 0.0 if num == 0 else math.inf
    return num / den


def sigma_e(actual, fc):
    """σe tuần = √(Σe² / (H − 1))."""
    h = len(actual)
    if h < 2:
        return 0.0
    return math.sqrt(sum((a - f) ** 2 for a, f in zip(actual, fc)) / (h - 1))


@dataclass
class ForecastResult:
    model: str                      # MA / HOLT / HW
    n_weeks: int
    alpha: float = None
    beta: float = None
    level: float = 0.0
    trend: float = 0.0
    seasonal: list = None           # 12 chỉ số (HW) hoặc None
    forecast_weeks: list = field(default_factory=list)   # 13 tuần tới, tuần đầu = tuần ngay sau tuần đủ cuối cùng
    first_forecast_week: date = None
    sigma_e_week: float = 0.0
    wape: float = None
    wape_baseline: float = None
    backtest: list = field(default_factory=list)         # [(tuần, thực tế, dự báo 1 tuần trước)]
    history: list = field(default_factory=list)          # [(tuần, thực tế)] – toàn bộ chuỗi huấn luyện

    @property
    def label(self):
        if self.model == "MA":
            return MODEL_LABELS["MA"]
        if self.model == "HOLT":
            return f"Holt (α = {self.alpha:g}, β = {self.beta:g})"
        return f"Holt-Winters (α = {self.alpha:g}, β = {self.beta:g}, mùa theo tháng)"

    @property
    def improvement(self):
        """Mô hình mới giảm sai số bao nhiêu % so với cách cũ (TB 13 tuần)."""
        if self.wape is None or not self.wape_baseline or math.isinf(self.wape_baseline):
            return None
        return (1 - self.wape / self.wape_baseline) * 100

    def week_forecast(self, week_start):
        """Dự báo của 1 tuần tương lai (ngoài 13 tuần thì giữ giá trị tuần cuối)."""
        if not self.forecast_weeks:
            return 0.0
        if self.first_forecast_week is None:
            return self.forecast_weeks[0]
        k = (monday(week_start) - self.first_forecast_week).days // 7
        return self.forecast_weeks[min(max(k, 0), len(self.forecast_weeks) - 1)]

    def demand_over(self, start, days):
        """Tổng dự báo của `days` ngày (có thể lẻ) tính từ ngày start – mỗi ngày = dự báo tuần / 7."""
        total, i, left = 0.0, 0, float(days)
        while left > 1e-9:
            part = min(1.0, left)
            d = start + timedelta(days=i)
            total += self.week_forecast(d) / 7 * part
            left -= part
            i += 1
        return total


def forecast(weeks, y):
    """Dự báo 1 sản phẩm từ chuỗi tuần ĐỦ (đã điền tuần 0, không gồm tuần hiện tại). Hàm thuần."""
    n = len(y)
    nxt = (weeks[-1] + timedelta(days=7)) if weeks else None
    history = list(zip(weeks, y))
    if n < MIN_HOLT:
        f = _mean(y)
        return ForecastResult(model="MA", n_weeks=n, level=f, forecast_weeks=[f] * HORIZON, first_forecast_week=nxt,
                              sigma_e_week=_stdev(y), history=history)
    h = min(26, n // 4)
    ev = range(n - h, n)
    actual = [y[t] for t in ev]
    use_hw = n >= MIN_HW
    # mùa tính từ phần dữ liệu TRƯỚC cửa sổ đánh giá khi backtest (không "nhìn trộm" tương lai)
    bt_season = seasonal_indices(weeks[:n - h], y[:n - h]) if use_hw else None
    best = None
    for a in ALPHAS:
        for b in BETAS:
            if use_hw:
                _, _, one = _hw_one_step(weeks, y, a, b, bt_season)
            else:
                _, _, one = holt(y, a, b)
            fc = [one[t] for t in ev]
            score = wape(actual, fc)
            if best is None or score < best[0] - 1e-12:  # bằng nhau: giữ α (rồi β) nhỏ hơn – đã duyệt tăng dần
                best = (score, a, b, fc)
    score, a, b, fc = best
    baseline = [_mean(y[max(0, t - BASELINE_WEEKS):t]) for t in ev]
    # dự báo thật: tính lại mùa trên TOÀN BỘ chuỗi
    season = seasonal_indices(weeks, y) if use_hw else None
    if use_hw:
        level, trend, _ = _hw_one_step(weeks, y, a, b, season)
        fut = [max(0.0, (level + k * trend) * season[week_month(nxt + timedelta(days=7 * (k - 1))) - 1])
               for k in range(1, HORIZON + 1)]
    else:
        level, trend, _ = holt(y, a, b)
        fut = [max(0.0, level + k * trend) for k in range(1, HORIZON + 1)]
    return ForecastResult(model="HW" if use_hw else "HOLT", n_weeks=n, alpha=a, beta=b, level=level, trend=trend,
                          seasonal=season, forecast_weeks=fut, first_forecast_week=nxt,
                          sigma_e_week=sigma_e(actual, fc), wape=score, wape_baseline=wape(actual, baseline),
                          backtest=[(weeks[t], y[t], f) for t, f in zip(ev, fc)], history=history)


# ------------------------------------------------------------------ Phần B: SS / ROP / Q từ dự báo (mục 5)
def ss_rop_q(fr, *, method, z, lt, lt_std, today, review_days, moq, manual_ss=None, manual_q=None):
    """Một chỗ duy nhất tính SS, ROP, Q từ kết quả dự báo – calc_param, màn SS/ROP và Excel cùng gọi hàm này."""
    lt = max(float(lt), 0.0)
    d_lt = fr.demand_over(today, lt) if lt else 0.0
    f_day = d_lt / lt if lt else fr.week_forecast(today) / 7
    se_day = fr.sigma_e_week / math.sqrt(7)
    z = float(z)
    if method == "FULL":
        ss = z * math.sqrt(lt * se_day ** 2 + f_day ** 2 * float(lt_std) ** 2)
    elif method == "DEMAND":
        ss = z * se_day * math.sqrt(lt)
    else:
        ss = float(manual_ss or 0)
    rop = d_lt + ss
    d_review = fr.demand_over(today, review_days)
    q = float(manual_q) if manual_q is not None else max(float(moq), math.ceil(round(d_review, 9)))
    return {"d_lt": d_lt, "f_day": f_day, "sigma_e_day": se_day, "ss": ss, "rop": rop, "d_review": d_review, "q": q,
            "lt": lt, "lt_std": float(lt_std), "z": z}
