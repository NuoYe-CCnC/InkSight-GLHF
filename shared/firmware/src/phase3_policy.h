#ifndef INKSIGHT_PHASE3_POLICY_H
#define INKSIGHT_PHASE3_POLICY_H

#include <stdint.h>

// Pure, host-testable phase-3 policy.  All times are Asia/Shanghai wall-clock
// seconds supplied by the caller; this file performs no I/O and owns no state.
enum class P3Mode : uint8_t { Active = 0, Light = 1, Night = 2 };

inline const char *p3ModeName(P3Mode mode) {
    switch (mode) {
        case P3Mode::Active: return "active";
        case P3Mode::Light: return "light";
        default: return "night";
    }
}

inline P3Mode p3ModeForMinute(bool workday, int minuteOfDay) {
    if (workday) {
        if (minuteOfDay >= 8 * 60 + 30 && minuteOfDay < 18 * 60 + 30)
            return P3Mode::Active;
        if (minuteOfDay >= 18 * 60 + 30 && minuteOfDay < 22 * 60)
            return P3Mode::Light;
        return P3Mode::Night;
    }
    if (minuteOfDay >= 8 * 60 && minuteOfDay < 20 * 60)
        return P3Mode::Light;
    return P3Mode::Night;
}

inline int p3AdaptiveInterval(P3Mode mode, bool baselineKnown,
                              bool observationKnown, int64_t ageSinceChange,
                              int64_t observeRemaining) {
    if (mode == P3Mode::Active) return 60;
    if (!baselineKnown || !observationKnown)
        return mode == P3Mode::Night ? 300 : 60;
    if (observeRemaining > 0) return 60;
    if (ageSinceChange < 0) ageSinceChange = 0;
    if (mode == P3Mode::Light) {
        if (ageSinceChange < 5 * 60) return 60;
        if (ageSinceChange < 15 * 60) return 180;
        return 300;
    }
    if (ageSinceChange < 5 * 60) return 60;
    if (ageSinceChange < 15 * 60) return 300;
    if (ageSinceChange < 30 * 60) return 600;
    if (ageSinceChange < 60 * 60) return 900;
    return 1800;
}

inline int p3IdleSwitchSeconds(P3Mode mode) {
    if (mode == P3Mode::Active) return 10 * 60;
    if (mode == P3Mode::Light) return 5 * 60;
    return 0;
}

inline int p3DesiredPage(P3Mode mode, int currentPage, bool newsAvailable,
                         bool baselineKnown, bool activityChanged,
                         int64_t nowSec, int64_t lastChangeAt,
                         int64_t aiHoldUntil) {
    if (activityChanged || !baselineKnown) return 0;
    if (currentPage == 1 && !newsAvailable) return 0;
    if (currentPage == 1 || mode == P3Mode::Night) return currentPage;
    int idle = p3IdleSwitchSeconds(mode);
    if (idle > 0 && newsAvailable && nowSec >= aiHoldUntil &&
        lastChangeAt > 0 && nowSec - lastChangeAt >= idle)
        return 1;
    return 0;
}

inline int p3SecondsToModeBoundary(bool todayWorkday, bool tomorrowWorkday,
                                   int minuteOfDay, int second) {
    const int work[] = {8 * 60 + 30, 18 * 60 + 30, 22 * 60};
    const int rest[] = {8 * 60, 20 * 60};
    const int *bounds = todayWorkday ? work : rest;
    int count = todayWorkday ? 3 : 2;
    int nowOfDay = minuteOfDay * 60 + second;
    for (int i = 0; i < count; ++i) {
        int event = bounds[i] * 60;
        if (event > nowOfDay) return event - nowOfDay;
    }
    int tomorrowFirst = (tomorrowWorkday ? 8 * 60 + 30 : 8 * 60) * 60;
    return 24 * 60 * 60 - nowOfDay + tomorrowFirst;
}

inline int p3MinPositive(int current, int candidate) {
    if (candidate <= 0) return current;
    return current <= 0 || candidate < current ? candidate : current;
}

inline int p3NextWakeSeconds(bool networkFailed, int normalDue, int retryDue,
                             int modeDue, int pageDue, int localDue = 0) {
    int next = 0;
    next = p3MinPositive(next, networkFailed ? retryDue : normalDue);
    next = p3MinPositive(next, modeDue);
    next = p3MinPositive(next, pageDue);
    next = p3MinPositive(next, localDue);
    return next > 0 ? next : 1;
}

// Preserve the cadence anchored at check start.  If work overruns one or more
// slots, skip those slots instead of creating an immediate catch-up wake.
inline int64_t p3AlignedNext(int64_t startedAt, int64_t finishedAt, int interval) {
    if (interval < 1) interval = 1;
    int64_t next = startedAt + interval;
    if (next <= finishedAt) {
        int64_t missed = (finishedAt - startedAt) / interval + 1;
        next = startedAt + missed * interval;
    }
    return next;
}

#endif  // INKSIGHT_PHASE3_POLICY_H
