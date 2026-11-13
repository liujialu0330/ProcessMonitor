"""卡片实时趋势的定长聚合量；与分析页共用判定函数，不读取数据库。"""
import math
from collections import deque

from core.analysis.trend import FitInput, judge
from data.mappers import normalize_ms
from utils.metrics import value_class, VALUE_CLASS_LEVEL


class LiveStats:
    RECENT_LIMIT = 256

    def __init__(self):
        self.rows = {}
        self._recent = {}
        self._overflow = {}
        self._since = None
        self.needs_rebuild = False

    def clear(self):
        self.rows.clear()
        self._recent.clear()
        self._overflow.clear()
        self._since = None
        self.needs_rebuild = False

    def invalidate(self, since=None):
        """统计边界变化后，旧判定不可继续显示；新采样仍留在有界缓存中。"""
        self.rows.clear()
        self._since = normalize_ms(since) if since is not None else None
        self.needs_rebuild = True

    def add(self, metric, value, when):
        if when is None or not math.isfinite(value) or value_class(metric) != VALUE_CLASS_LEVEL:
            return
        when = normalize_ms(when)
        recent = self._recent.setdefault(metric, deque())
        if recent and recent[-1][0] == when:
            # 数据库以毫秒时间戳去重；相同时间的值不可重复计入趋势。
            if recent[-1][1] != value:
                self.needs_rebuild = True
            recent[-1] = (when, value)
            return
        recent.append((when, value))
        if len(recent) > self.RECENT_LIMIT:
            old, _ = recent.popleft()
            self._overflow[metric] = max(old, self._overflow.get(metric, old))
        if self.needs_rebuild:
            return
        self._add(metric, value, when)

    def _add(self, metric, value, when):
        if self._since is not None and when < self._since:
            return
        row = self.rows.setdefault(metric, [when, when, 0, 0., 0., 0., 0., 0., value, value, value, value])
        x = (when - row[0]).total_seconds()
        if x < 0:
            return
        row[1], row[2] = when, row[2] + 1
        row[3] += x
        row[4] += value
        row[5] += x * x
        row[6] += value * value
        row[7] += x * value
        row[9], row[10], row[11] = value, min(row[10], value), max(row[11], value)

    def restore(self, aggregates, since=None):
        """按各指标落库水位合并，避免查询中追加或尚未落库的样本重复/遗漏。"""
        since = normalize_ms(since) if since is not None else None
        for metric, agg in aggregates.items():
            lost_until = self._overflow.get(metric)
            if (lost_until is not None and (since is None or lost_until >= since)
                    and (agg.last_ts is None or agg.last_ts < lost_until)):
                # 写库长期受阻且缓存已截断，不能把不完整数据伪装为完整统计。
                return False
            if any(when == agg.last_ts and value != agg.last_value
                   for when, value in self._recent.get(metric, ())):
                # 同一毫秒后来采到的新值尚未落库，旧聚合不能覆盖这个修正。
                return False
        self.rows.clear()
        self._since = since
        for metric, agg in aggregates.items():
            if agg.count:
                self.rows[metric] = [agg.first_ts, agg.last_ts, agg.count,
                    agg.sum_x, agg.sum_y, agg.sum_xx, agg.sum_yy, agg.sum_xy,
                    agg.first_value, agg.last_value, agg.min_value, agg.max_value]
            for when, value in self._recent.get(metric, ()):
                if agg.last_ts is None or when > agg.last_ts:
                    self._add(metric, value, when)
        self.needs_rebuild = False
        return True

    def verdict(self, metric, interval):
        if self.needs_rebuild:
            return 'insufficient'
        row = self.rows.get(metric)
        if row is None:
            return 'insufficient'
        first, last, count, sx, sy, sxx, syy, sxy, fv, lv, lo, hi = row
        fit_input = FitInput(count, sx, sy, sxx, sxy, syy,
                             (last - first).total_seconds(), fv, lv, lo, hi)
        result = judge(fit_input, interval or 1)
        return result.verdict.value
