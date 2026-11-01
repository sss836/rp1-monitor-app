"""Bounded smooth trajectories for simulation; no robot command executor."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SmoothMove:
    """Seventh-order move with zero velocity, acceleration and jerk at both ends."""

    start_rad: float
    end_rad: float
    duration_s: float

    @classmethod
    def bounded(cls, start_rad, end_rad, *, speed_rad_s, acceleration_rad_s2, jerk_rad_s3,
                maximum_stroke_rad):
        values = (start_rad, end_rad, speed_rad_s, acceleration_rad_s2,
                  jerk_rad_s3, maximum_stroke_rad)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in values):
            raise ValueError("轨迹参数必须是有限数值")
        if min(speed_rad_s, acceleration_rad_s2, jerk_rad_s3, maximum_stroke_rad) <= 0:
            raise ValueError("速度、加速度、jerk和行程上限必须为正")
        distance = abs(end_rad - start_rad)
        if distance > maximum_stroke_rad:
            raise ValueError("超出单段行程，必须重新安排工装/预定位；不自动扩大范围")
        # Conservative analytic bounds for s(u)=35u^4-84u^5+70u^6-20u^7.
        duration = max(
            0.25, 2.1875 * distance / speed_rad_s,
            math.sqrt(8.0 * distance / acceleration_rad_s2),
            (60.0 * distance / jerk_rad_s3) ** (1 / 3),
        )
        return cls(float(start_rad), float(end_rad), duration)

    def sample(self, time_s):
        if not isinstance(time_s, (int, float)) or isinstance(time_s, bool) or not math.isfinite(time_s):
            raise ValueError("轨迹时间必须有限")
        if time_s <= 0:
            return self.start_rad, 0.0, 0.0, 0.0
        if time_s >= self.duration_s:
            return self.end_rad, 0.0, 0.0, 0.0
        u = time_s / self.duration_s
        delta, duration = self.end_rad - self.start_rad, self.duration_s
        s = 35 * u**4 - 84 * u**5 + 70 * u**6 - 20 * u**7
        velocity = 140 * u**3 - 420 * u**4 + 420 * u**5 - 140 * u**6
        acceleration = 420 * u**2 - 1680 * u**3 + 2100 * u**4 - 840 * u**5
        jerk = 840 * u - 5040 * u**2 + 8400 * u**3 - 4200 * u**4
        return (self.start_rad + delta * s, delta * velocity / duration,
                delta * acceleration / duration**2, delta * jerk / duration**3)


def local_jog(center_rad, amplitude_rad, lower_rad, upper_rad, **bounds):
    """One joint: center -> positive -> center -> negative -> center, with no sweep."""
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
               for v in (center_rad, amplitude_rad, lower_rad, upper_rad)):
        raise ValueError("关节范围和幅度必须有限")
    if amplitude_rad <= 0 or not lower_rad < center_rad - amplitude_rad < center_rad + amplitude_rad < upper_rad:
        raise ValueError("小幅动作须完全位于关节范围内；不能用局部检查代替两端限位")
    points = (center_rad, center_rad + amplitude_rad, center_rad,
              center_rad - amplitude_rad, center_rad)
    return [SmoothMove.bounded(a, b, **bounds) for a, b in zip(points, points[1:])]
