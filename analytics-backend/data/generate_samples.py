"""
Generate realistic product analytics sample datasets.
Uses only stdlib: random, csv, datetime, pathlib
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

# ── Seed for reproducibility ───────────────────────────────────────────────
random.seed(42)

# ── Output paths ──────────────────────────────────────────────────────────
BASE = Path("A:/Kapi_PM/backend/data/samples")
BASE.mkdir(parents=True, exist_ok=True)

EVENTS_PATH        = BASE / "events.csv"
USERS_PATH         = BASE / "users.csv"
FEATURE_USAGE_PATH = BASE / "feature_usage.csv"

# ── Constants ─────────────────────────────────────────────────────────────
END_DATE   = datetime(2026, 3, 29, 23, 59, 59)
START_DATE = END_DATE - timedelta(days=89)   # 90-day window

PLATFORMS  = ["web", "mobile_ios", "mobile_android"]
COUNTRIES  = ["US", "UK", "CA", "DE", "FR", "AU", "IN"]
PLANS      = ["free", "starter", "pro", "enterprise"]
REFERRALS  = ["organic", "paid_search", "social", "referral", "direct", "email_campaign"]

EVENTS_LIST = [
    "page_view", "sign_up", "feature_discover", "onboarding_start",
    "onboarding_complete", "dashboard_view", "report_created", "invite_sent",
    "export_clicked", "subscription_upgraded", "subscription_cancelled",
    "feature_a_used", "feature_b_used", "feature_c_used", "search_performed",
]

FEATURES = [
    "Dashboard", "AI Analyst", "Funnel Builder", "Retention Chart",
    "Segment Explorer", "Report Export", "Team Collaboration",
    "API Access", "Custom Alerts", "Data Import",
]

# Plan weights influence event volumes & feature availability
PLAN_EVENT_MULTIPLIER = {"free": 1, "starter": 2, "pro": 4, "enterprise": 8}
PLAN_WEIGHTS          = [0.50, 0.25, 0.17, 0.08]   # free → enterprise

COUNTRY_WEIGHTS = [0.35, 0.15, 0.10, 0.10, 0.08, 0.07, 0.15]  # US heavy

PLATFORM_WEIGHTS = {
    "web": 0.55,
    "mobile_ios": 0.25,
    "mobile_android": 0.20,
}

# Features available per plan (higher plans unlock more)
PLAN_FEATURES = {
    "free":       ["Dashboard", "Funnel Builder"],
    "starter":    ["Dashboard", "Funnel Builder", "Retention Chart", "Segment Explorer", "Report Export"],
    "pro":        ["Dashboard", "AI Analyst", "Funnel Builder", "Retention Chart",
                   "Segment Explorer", "Report Export", "Team Collaboration", "Custom Alerts", "Data Import"],
    "enterprise": FEATURES,   # all
}

# ── Helpers ───────────────────────────────────────────────────────────────

def weighted_choice(population, weights):
    return random.choices(population, weights=weights, k=1)[0]


def rand_ts(start: datetime, end: datetime) -> datetime:
    """Return a random datetime between start and end."""
    delta = int((end - start).total_seconds())
    return start + timedelta(seconds=random.randint(0, delta))


def apply_weekday_bias(ts: datetime) -> datetime:
    """
    Nudge timestamps: weekdays (Mon-Fri) are ~2× more likely than weekends.
    We achieve this by occasionally shifting weekend events to nearby weekdays.
    """
    if ts.weekday() >= 5 and random.random() < 0.55:   # Sat or Sun
        shift = random.choice([-1, 1, 2])
        ts = ts + timedelta(days=shift)
        # clamp to window
        ts = max(min(ts, END_DATE), START_DATE)
    return ts


def apply_business_hours_bias(ts: datetime) -> datetime:
    """Bias events toward 08:00–22:00 local (approximate)."""
    hour = ts.hour
    if hour < 8 or hour >= 22:
        if random.random() < 0.70:
            new_hour = random.randint(8, 21)
            ts = ts.replace(hour=new_hour, minute=random.randint(0, 59),
                            second=random.randint(0, 59))
    return ts


def realistic_ts(start: datetime, end: datetime) -> datetime:
    ts = rand_ts(start, end)
    ts = apply_weekday_bias(ts)
    ts = apply_business_hours_bias(ts)
    return ts


def fmt(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def power_law_user_activity(n_users: int) -> list:
    """
    Return a list of per-user event counts following a power law
    (a small number of users drive most activity).
    """
    counts = []
    for i in range(n_users):
        rank = i + 1
        # Zipf-like: base_events / rank^0.7
        base = 120 / (rank ** 0.55)
        counts.append(max(1, int(base + random.gauss(0, base * 0.15))))
    random.shuffle(counts)
    return counts


# ── Step 1 – build user table ─────────────────────────────────────────────

N_USERS = 500

print("Generating users …")

users = []
user_ids = [f"u_{i:05d}" for i in range(1, N_USERS + 1)]

# Assign plans with realistic distribution
plans_assigned = random.choices(PLANS, weights=PLAN_WEIGHTS, k=N_USERS)

# Power-law event counts per user (shuffled so high-activity ≠ always user #1)
base_event_counts = power_law_user_activity(N_USERS)

# Apply plan multiplier on top of the power-law base
event_counts = []
for i, uid in enumerate(user_ids):
    plan = plans_assigned[i]
    mult = PLAN_EVENT_MULTIPLIER[plan]
    raw = base_event_counts[i]
    # enterprise gets a further random boost
    if plan == "enterprise":
        raw = int(raw * random.uniform(1.5, 3.0))
    elif plan == "pro":
        raw = int(raw * random.uniform(1.0, 1.8))
    event_counts.append(max(1, raw))

for i, uid in enumerate(user_ids):
    plan    = plans_assigned[i]
    country = weighted_choice(COUNTRIES, COUNTRY_WEIGHTS)
    referral= weighted_choice(REFERRALS, [0.30, 0.20, 0.15, 0.15, 0.12, 0.08])

    # created_at: enterprise / pro tend to be older accounts
    if plan == "enterprise":
        created_at = rand_ts(START_DATE - timedelta(days=365), START_DATE - timedelta(days=90))
    elif plan == "pro":
        created_at = rand_ts(START_DATE - timedelta(days=180), START_DATE)
    elif plan == "starter":
        created_at = rand_ts(START_DATE - timedelta(days=90), END_DATE - timedelta(days=14))
    else:
        created_at = rand_ts(START_DATE - timedelta(days=30), END_DATE - timedelta(days=1))

    # last_active: retention drops – most users churn early
    # Exponential-ish decay: ~50 % active within last 7 days
    decay = random.expovariate(1 / 20)   # mean ~20 days ago
    last_active = END_DATE - timedelta(days=min(int(decay), 89))
    last_active = max(last_active, created_at)

    ec = event_counts[i]
    sc = max(1, ec // random.randint(3, 8))   # sessions ~ events / 3-8

    email = f"user{i+1}@example.com"

    users.append({
        "user_id":        uid,
        "email":          email,
        "plan":           plan,
        "country":        country,
        "created_at":     fmt(created_at),
        "last_active":    fmt(last_active),
        "event_count":    ec,
        "session_count":  sc,
        "referral_source": referral,
    })

# Build lookup maps
user_plan    = {u["user_id"]: u["plan"]    for u in users}
user_country = {u["user_id"]: u["country"] for u in users}
user_created = {u["user_id"]: datetime.strptime(u["created_at"], "%Y-%m-%d %H:%M:%S")
                for u in users}
user_last    = {u["user_id"]: datetime.strptime(u["last_active"], "%Y-%m-%d %H:%M:%S")
                for u in users}

# ── Step 2 – build event log ───────────────────────────────────────────────

print("Generating events …")

# We want ~2000 rows total, distributed across users by their event_count weight
total_events_target = 2000
total_weight        = sum(event_counts)

# Normalize to hit the target
events_per_user = [
    max(1, round(ec / total_weight * total_events_target))
    for ec in event_counts
]

# Event probability weights by plan
EVENT_WEIGHTS_BY_PLAN = {
    "free": [
        0.30,  # page_view
        0.08,  # sign_up
        0.07,  # feature_discover
        0.06,  # onboarding_start
        0.04,  # onboarding_complete
        0.15,  # dashboard_view
        0.03,  # report_created
        0.01,  # invite_sent
        0.02,  # export_clicked
        0.01,  # subscription_upgraded
        0.01,  # subscription_cancelled
        0.08,  # feature_a_used
        0.06,  # feature_b_used
        0.04,  # feature_c_used
        0.04,  # search_performed
    ],
    "starter": [
        0.22, 0.03, 0.05, 0.03, 0.03, 0.18, 0.06, 0.03,
        0.04, 0.02, 0.01, 0.10, 0.09, 0.06, 0.05,
    ],
    "pro": [
        0.18, 0.02, 0.04, 0.02, 0.03, 0.18, 0.10, 0.05,
        0.06, 0.03, 0.01, 0.10, 0.09, 0.05, 0.04,
    ],
    "enterprise": [
        0.15, 0.01, 0.03, 0.01, 0.02, 0.17, 0.12, 0.08,
        0.08, 0.04, 0.01, 0.10, 0.10, 0.06, 0.02,
    ],
}

all_events = []
session_pool = {}   # uid → list of session_ids

for i, uid in enumerate(user_ids):
    plan     = user_plan[uid]
    country  = user_country[uid]
    created  = user_created[uid]
    last     = user_last[uid]
    n_events = events_per_user[i]

    # Determine platform preference for this user
    platform_prefs = {
        "web":            random.uniform(0.3, 0.8),
        "mobile_ios":     random.uniform(0.1, 0.4),
        "mobile_android": random.uniform(0.1, 0.4),
    }
    pf_total = sum(platform_prefs.values())
    pf_weights = [platform_prefs[p] / pf_total for p in PLATFORMS]

    # Sessions for this user
    n_sessions = max(1, n_events // random.randint(3, 8))
    sessions   = [f"s_{uid}_{j:04d}" for j in range(n_sessions)]
    session_pool[uid] = sessions

    ew = EVENT_WEIGHTS_BY_PLAN[plan]

    # Retention decay: events cluster earlier in the window for churned users
    active_window_end = min(last, END_DATE)
    active_window_start = max(created, START_DATE)
    if active_window_end <= active_window_start:
        active_window_start = START_DATE

    for _ in range(n_events):
        event   = weighted_choice(EVENTS_LIST, ew)
        ts      = realistic_ts(active_window_start, active_window_end)
        session = random.choice(sessions)
        platform= weighted_choice(PLATFORMS, pf_weights)

        all_events.append({
            "user_id":    uid,
            "event_name": event,
            "timestamp":  fmt(ts),
            "session_id": session,
            "platform":   platform,
            "country":    country,
        })

# Sort by timestamp
all_events.sort(key=lambda x: x["timestamp"])

print(f"  Total events: {len(all_events)}")

# ── Step 3 – build feature usage ──────────────────────────────────────────

print("Generating feature usage …")

total_fu_target = 3000

# Weight each user by their plan (more active plans → more feature usage)
fu_weights = [PLAN_EVENT_MULTIPLIER[user_plan[uid]] * event_counts[i]
              for i, uid in enumerate(user_ids)]
fu_total = sum(fu_weights)

fu_per_user = [
    max(0, round(w / fu_total * total_fu_target))
    for w in fu_weights
]

# Feature duration ranges (seconds) per feature
FEATURE_DURATION = {
    "Dashboard":           (10,  300),
    "AI Analyst":          (30,  600),
    "Funnel Builder":      (60,  900),
    "Retention Chart":     (30,  480),
    "Segment Explorer":    (45,  720),
    "Report Export":       (5,   120),
    "Team Collaboration":  (20,  400),
    "API Access":          (5,   180),
    "Custom Alerts":       (15,  300),
    "Data Import":         (30,  1200),
}

all_fu = []

for i, uid in enumerate(user_ids):
    plan    = user_plan[uid]
    created = user_created[uid]
    last    = user_last[uid]
    n_fu    = fu_per_user[i]
    if n_fu == 0:
        continue

    sessions = session_pool.get(uid, [f"s_{uid}_0000"])
    available_features = PLAN_FEATURES[plan]

    # Higher-tier plans have more varied feature usage
    if plan == "enterprise":
        feature_weights = [1.5 if f in ["AI Analyst", "Team Collaboration", "API Access",
                                         "Custom Alerts", "Data Import"] else 1.0
                           for f in available_features]
    elif plan == "pro":
        feature_weights = [1.3 if f in ["AI Analyst", "Funnel Builder", "Retention Chart"]
                           else 1.0 for f in available_features]
    else:
        feature_weights = [1.0] * len(available_features)

    active_window_end   = min(last, END_DATE)
    active_window_start = max(created, START_DATE)
    if active_window_end <= active_window_start:
        active_window_start = START_DATE

    for _ in range(n_fu):
        feature = weighted_choice(available_features, feature_weights)
        ts      = realistic_ts(active_window_start, active_window_end)
        session = random.choice(sessions)
        lo, hi  = FEATURE_DURATION[feature]
        duration = int(random.triangular(lo, hi, (lo + hi) // 2))

        all_fu.append({
            "user_id":          uid,
            "feature_name":     feature,
            "used_at":          fmt(ts),
            "session_id":       session,
            "duration_seconds": duration,
        })

all_fu.sort(key=lambda x: x["used_at"])

print(f"  Total feature usage rows: {len(all_fu)}")

# ── Write CSVs ────────────────────────────────────────────────────────────

def write_csv(path: Path, fieldnames: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Wrote {len(rows):,} rows → {path}")


print("\nWriting CSVs …")

write_csv(
    EVENTS_PATH,
    ["user_id", "event_name", "timestamp", "session_id", "platform", "country"],
    all_events,
)

write_csv(
    USERS_PATH,
    ["user_id", "email", "plan", "country", "created_at", "last_active",
     "event_count", "session_count", "referral_source"],
    users,
)

write_csv(
    FEATURE_USAGE_PATH,
    ["user_id", "feature_name", "used_at", "session_id", "duration_seconds"],
    all_fu,
)

print("\nDone.")
