"""The paper's statistic (Section 6, "Metric and statistics").

For one seed, the statistic over a set of configurations is the mean, over the set, of ln(Froq makespan / SWIPER-alone
makespan); a configuration where Froq waits adds ln 1 = 0.  A comparison's ratio is exp of the mean of that statistic over
the seeds, with a two-sided 95 % t interval; the test is one-sided (ratio below 1) with seeds - 1 degrees of freedom.
"""
import math
import statistics

from scipy.stats import t as student_t


def per_seed(logratio, configs, seeds):
    """logratio: {(config, seed): ln ratio}; a config missing from logratio (waiting) counts 0."""
    return [sum(logratio.get((c, s), 0.0) for c in configs) / len(configs) for s in seeds]


def compare(xs, level=0.95):
    """xs: per-seed statistics.  Returns ratio, interval, t, one-sided p, the one-sided upper bound."""
    k = len(xs)
    m, sd = statistics.fmean(xs), statistics.stdev(xs)
    se = sd / math.sqrt(k)
    h = student_t.ppf(1 - (1 - level) / 2, k - 1) * se
    out = dict(ratio=math.exp(m), lo=math.exp(m - h), hi=math.exp(m + h), mean_log=m, sd=sd, df=k - 1)
    if se > 0:
        out.update(t=m / se, p=float(student_t.cdf(m / se, k - 1)),
                   upper=math.exp(m + student_t.ppf(level, k - 1) * se))
    return out


def holm(ps, alpha=0.05):
    """Holm's step-down over {name: p}; returns {name: (step, rejected)}."""
    out, go = {}, True
    for i, k in enumerate(sorted(ps, key=lambda x: (ps[x], x))):
        go = go and ps[k] <= alpha / (len(ps) - i)
        out[k] = (i + 1, go)
    return out


def cut(r):
    """Reduction of quantum-side time in percent for a ratio r."""
    return 100.0 * (1.0 - r)
