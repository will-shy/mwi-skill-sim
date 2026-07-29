#!/usr/bin/env python3
"""
Skilling Trials assignment optimizer.

Reads a guild member CSV (per-skill level / tool / house / enhancement), takes the 4
chosen trial skills, and assigns members across the 4 trials to maximize total tiers
cleared. Implements the full per-member model from SIMULATION_RULES.md (each member has
their own tool/house/enhancement, hence their own speed/interval), which is why the
simulation accumulates expected progress *rates* rather than ticking in lockstep like the
simplified index.html model.

Usage:
    python3 optimize_trials.py MEMBERS.csv --skills Milking Woodcutting Cooking Enhancing
    python3 optimize_trials.py MEMBERS.csv -s Milking Woodcutting Cooking Enhancing \
        --building Milking=1 Cooking=2          # optional guild-building levels
    python3 optimize_trials.py MEMBERS.csv -s ... --max-roster 22 --json out.json

Run with -h for all options.
"""

import argparse
import csv
import json
import math
import sys

# ---------------------------------------------------------------------------
# Skill metadata (rule intro)
# ---------------------------------------------------------------------------
SKILLS = ["Milking", "Foraging", "Woodcutting", "Cheesesmithing", "Crafting",
          "Tailoring", "Cooking", "Brewing", "Alchemy", "Enhancing"]
# The +11.2% efficiency gear bonus (rule 4.3) applies to three skill-specific groups, each
# from its own gear piece (collector's boots / eye watch / red culinary hat). Kept distinct
# so each can be tuned independently. Alchemy gets none (it has gloves speed instead).
GATHERING = {"Milking", "Foraging", "Woodcutting"}          # collector's boots (+11.2% eff) + double chance
ARTISAN = {"Cheesesmithing", "Crafting", "Tailoring"}       # eye watch (+11.2% eff)
COOKING_BREWING = {"Cooking", "Brewing"}                    # red culinary hat (+11.2% eff)
GLOVE = {"Alchemy", "Enhancing"}
ENHANCING = "Enhancing"

# Rule 1.4.3 — enhancement bonus as a fraction of base stat. Index = enhancement level.
ENH_TABLE = {
    0: 0.0, 1: 0.020, 2: 0.042, 3: 0.066, 4: 0.092, 5: 0.120, 6: 0.150, 7: 0.182,
    8: 0.216, 9: 0.252, 10: 0.290, 11: 0.334, 12: 0.384, 13: 0.440, 14: 0.502,
    15: 0.570, 16: 0.644, 17: 0.724, 18: 0.810, 19: 0.902, 20: 1.000,
}


def enh_factor(level):
    """Total tool multiplier (1 + bonus) for a given enhancement level (rule 1.4.3)."""
    if level <= 0:
        return 1.0
    if level in ENH_TABLE:
        return 1.0 + ENH_TABLE[level]
    return 1.0 + ENH_TABLE[20]  # cap beyond +20


# ---------------------------------------------------------------------------
# Config (rule defaults; community buffs already include the 1.2x from rule 4.9)
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        # Success rate (rule 4.1)
        self.base_success = 0.80
        self.bonus_above = 0.005          # per level above room
        self.penalty_below = 0.01         # per level below room
        self.min_success = 0.05
        # Enhancing tool success bonuses at +0 (rule 1.4.1)
        self.enh_tool_holy = 0.036
        self.enh_tool_celestial = 0.042
        self.enh_house_success = 0.0005   # per house level (rule 1.3.1)
        self.enh_house_speed = 0.01       # per house level (rule 1.3.1)
        # Other tool speed at +0 (rule 1.4.2)
        self.tool_holy_speed = 0.90
        self.tool_celestial_speed = 1.05
        # Double progress, gathering only (rule 4.2, community x1.2 -> 24)
        self.double_achievement = 0.02
        self.double_community = 0.24
        self.double_gear = 0.04
        # Efficiency (rule 4.3, community x1.2 -> 16.8)
        self.house_eff_per_level = 0.015
        self.eff_achievement = 0.02
        self.eff_community = 0.168
        self.eff_gathering_gear = 0.112        # collector's boots: Milking, Foraging, Woodcutting
        self.eff_artisan_gear = 0.112          # eye watch: Cheesesmithing, Crafting, Tailoring
        self.eff_cooking_brewing_gear = 0.112  # red culinary hat: Cooking, Brewing
        # Outfits (rule 1.5): per-member top/bottom pieces, +10% each at +0, scaled by the
        # enhancement table. Efficiency on all skills except Enhancing, where it's speed.
        self.outfit_base = 0.10
        # Speed (rule 4.5)
        self.cape_speed = 0.05
        self.glove_speed = 0.112
        self.neck_speed = 0.052
        self.enh_community_speed = 0.24   # 20% x 1.2, Enhancing only
        # Trial structure (rule 3, 4.6, 4.8)
        self.start_level = 100
        self.level_step = 10
        self.progress_mult = 400
        self.attendance_pct = 0.01        # +1% required work per attendee
        self.trial_duration = 3600.0
        self.base_interval_enh = 8.0
        self.base_interval_other = 10.0
        self.max_roster = 22
        # Robustness: prefer each trial's last cleared tier to have >= this buffer into the
        # next tier (so it's not a razor-edge clear that RNG could flip). Secondary preference
        # ONLY — it must never sacrifice a real tier. The optimizer objective per trial is
        #   optScore = tiers + frac_weight*frac + robust_weight*min(frac, robust_margin)
        # where frac is progress into the current (uncleared) tier. Clearing a tier is worth
        # +1 and always wins iff  frac_weight + robust_weight*robust_margin < 1, which is why
        # frac_weight is 0.5 (not 1): 0.5 + 4*0.1 = 0.9 < 1. Below the margin each unit of
        # buffer is worth 4.5x; above it, 0.5x — so slack is pulled to fragile trials first.
        self.robust_margin = 0.10
        self.robust_weight = 4.0
        self.frac_weight = 0.5
        # Missing-data defaults (rule 1.2)
        self.default_tool = "holy"
        self.default_enh = 5
        self.default_house = 3


def clamp01(v):
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


# ---------------------------------------------------------------------------
# CSV loading (rule 1)
# ---------------------------------------------------------------------------
def _to_int(val, default):
    val = (val or "").strip()
    if val == "":
        return default
    try:
        return int(float(val))
    except ValueError:
        return default


def _to_outfit(val):
    """Outfit column (rule 1.5): empty or < 0 -> no piece (None); else enhancement level."""
    val = (val or "").strip()
    if val == "":
        return None
    try:
        n = int(float(val))
    except ValueError:
        return None
    return None if n < 0 else n


def _outfit_cell(row, skill, piece):
    """Read '<Skill> Top/Bottom', tolerating the 'Forging' header typo for Foraging."""
    v = row.get(f"{skill} {piece}")
    if v is None and skill == "Foraging":
        v = row.get(f"Forging {piece}")
    return _to_outfit(v)


def load_members(path, cfg):
    """Return list of member dicts: {name, role, skills:{skill:{level,tool,enh,house}}}."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames or []
        missing = [s for s in SKILLS if s not in cols]
        if missing:
            raise SystemExit(f"CSV missing skill columns: {', '.join(missing)}")
        members = []
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            skills = {}
            for s in SKILLS:
                tool = (row.get(f"{s} Tool") or "").strip().lower()
                if tool not in ("holy", "celestial"):
                    tool = cfg.default_tool           # rule 1.2
                enh = _to_int(row.get(f"{s} Tool Enh"), cfg.default_enh)
                house = _to_int(row.get(f"{s} House"), cfg.default_house)
                skills[s] = {
                    "level": _to_int(row.get(s), 0),
                    "tool": tool,
                    "enh": enh,
                    "house": house,
                    "top": _outfit_cell(row, s, "Top"),            # rule 1.5
                    "bottom": _outfit_cell(row, s, "Bottom"),
                }
            members.append({
                "name": name,
                "role": (row.get("Role") or "").strip(),
                "skills": skills,
            })
    if not members:
        raise SystemExit("No member rows found in CSV.")
    return members


# ---------------------------------------------------------------------------
# Per-member per-skill derived stats (rules 1.3, 1.4, 4.2-4.6)
# ---------------------------------------------------------------------------
def player_skill_stats(member, skill, cfg, building_level=0):
    """
    Precompute everything that does NOT depend on room level. Returns dict with:
      eff_level          effective skill level (CSV + guild building)
      interval           seconds per action
      prog_per_action    progress produced by one successful action
      double_mult        1 + double-progress chance
      enh_success_bonus  flat success-rate add (Enhancing only, else 0)
      rate_factor        double_mult * prog_per_action / interval
                         -> expected progress/sec = rate_factor * successRate(roomLevel)
    """
    sk = member["skills"][skill]
    eff_level = sk["level"] + 2 * building_level            # rule 4.10
    is_enh = skill == ENHANCING
    ef = enh_factor(sk["enh"])                              # rule 1.4.3

    # ---- Outfits (rule 1.5): per-piece 10% x enh table; None = no piece ----
    outfit = sum(cfg.outfit_base * enh_factor(lvl)
                 for lvl in (sk.get("top"), sk.get("bottom")) if lvl is not None)

    # ---- Efficiency (rule 4.3; unused by Enhancing) ----
    if is_enh:
        efficiency = 0.0
    else:
        if skill in GATHERING:
            gear_eff = cfg.eff_gathering_gear
        elif skill in ARTISAN:
            gear_eff = cfg.eff_artisan_gear
        elif skill in COOKING_BREWING:
            gear_eff = cfg.eff_cooking_brewing_gear
        else:
            gear_eff = 0.0                     # Alchemy: no efficiency gear
        efficiency = (sk["house"] * cfg.house_eff_per_level
                      + cfg.eff_achievement
                      + cfg.eff_community
                      + gear_eff
                      + outfit)                # rule 1.5 (efficiency on non-Enhancing)

    # ---- Progress per action (rule 4.4) ----
    if is_enh:
        prog_per_action = float(eff_level)
    else:
        prog_per_action = float(math.floor(eff_level * (1 + efficiency)))
    prog_per_action = max(0.0, prog_per_action)

    # ---- Double progress (rule 4.2) ----
    if skill in GATHERING:
        double_chance = clamp01(cfg.double_achievement + cfg.double_community + cfg.double_gear)
    else:
        double_chance = 0.0
    double_mult = 1 + double_chance

    # ---- Speed / interval (rule 4.5, 4.6) ----
    if is_enh:
        base_tool = cfg.enh_tool_holy if sk["tool"] == "holy" else cfg.enh_tool_celestial
        enh_success_bonus = base_tool * ef + sk["house"] * cfg.enh_house_success  # rule 1.4.1, 1.3.1
        tool_speed = 0.0                                    # Enhancing tool gives success, not speed
        speed = (cfg.cape_speed + cfg.glove_speed + cfg.neck_speed
                 + cfg.enh_community_speed + sk["house"] * cfg.enh_house_speed
                 + outfit)                     # rule 1.5 (speed on Enhancing)
        base_interval = cfg.base_interval_enh
    else:
        base_speed = cfg.tool_holy_speed if sk["tool"] == "holy" else cfg.tool_celestial_speed
        tool_speed = base_speed * ef                        # rule 1.4.2 x 1.4.3
        enh_success_bonus = 0.0
        speed = (cfg.cape_speed + tool_speed + cfg.neck_speed
                 + (cfg.glove_speed if skill in GLOVE else 0.0))
        base_interval = cfg.base_interval_other

    interval = base_interval / (1 + speed)
    rate_factor = double_mult * prog_per_action / interval

    return {
        "eff_level": eff_level,
        "efficiency": efficiency,
        "interval": interval,
        "speed": speed,
        "prog_per_action": prog_per_action,
        "double_chance": double_chance,
        "double_mult": double_mult,
        "enh_success_bonus": enh_success_bonus,
        "rate_factor": rate_factor,
    }


def success_rate(stats, room_level, cfg, is_enh):
    delta = stats["eff_level"] - room_level
    bonus = delta * cfg.bonus_above if delta >= 0 else delta * cfg.penalty_below
    sr = cfg.base_success * (1 + bonus)
    if is_enh:
        sr += stats["enh_success_bonus"]                    # rule 4.1
    return max(cfg.min_success, clamp01(sr))


# ---------------------------------------------------------------------------
# Trial simulation (rules 3, 4.7-4.8) — expected-value rate accumulation
# ---------------------------------------------------------------------------
def simulate_trial(roster_stats, skill, cfg):
    """
    roster_stats: list of per-player stats dicts (from player_skill_stats).
    Returns dict with tiers_cleared, room_level, progress, required, continuous_score,
    last_cleared_rate (progress/s on the last fully cleared tier), tier_log, stalled.
    """
    n = len(roster_stats)
    is_enh = skill == ENHANCING
    if n == 0:
        req0 = cfg.progress_mult * cfg.start_level
        return {"tiers_cleared": 0, "room_level": cfg.start_level, "progress": 0.0,
                "required": req0, "continuous_score": 0.0, "last_cleared_rate": 0.0,
                "tier_log": [], "stalled": False}

    roster_mult = 1 + cfg.attendance_pct * n                # rule 3
    room = cfg.start_level
    time_used = 0.0
    tiers = 0
    last_cleared_rate = 0.0
    tier_log = []
    stalled = False

    while time_used < cfg.trial_duration - 1e-9:
        required = cfg.progress_mult * room * roster_mult
        total_rate = 0.0
        for st in roster_stats:
            sr = success_rate(st, room, cfg, is_enh)
            total_rate += st["rate_factor"] * sr            # expected progress/sec
        if total_rate <= 1e-12:
            stalled = True
            tier_log.append({"level": room, "hp": required, "rate": 0.0,
                             "time": 0.0, "cleared": False, "progress": 0.0})
            break

        time_for_tier = required / total_rate
        if time_used + time_for_tier <= cfg.trial_duration + 1e-9:
            time_used += time_for_tier
            tiers += 1
            last_cleared_rate = total_rate
            tier_log.append({"level": room, "hp": required, "rate": total_rate,
                             "time": time_for_tier, "cleared": True, "progress": required})
            room += cfg.level_step
        else:
            remaining_time = cfg.trial_duration - time_used
            progress = total_rate * remaining_time
            time_used = cfg.trial_duration
            tier_log.append({"level": room, "hp": required, "rate": total_rate,
                             "time": remaining_time, "cleared": False, "progress": progress})
            frac = clamp01(progress / required) if required > 0 else 0.0
            return {"tiers_cleared": tiers, "room_level": room, "progress": progress,
                    "required": required, "continuous_score": tiers + frac,
                    "last_cleared_rate": last_cleared_rate, "tier_log": tier_log,
                    "stalled": stalled}

    # Ran out only by clearing exactly (or stalled): current tier has 0 progress.
    required = cfg.progress_mult * room * roster_mult
    return {"tiers_cleared": tiers, "room_level": room, "progress": 0.0,
            "required": required, "continuous_score": float(tiers),
            "last_cleared_rate": last_cleared_rate, "tier_log": tier_log,
            "stalled": stalled}


# ---------------------------------------------------------------------------
# Optimizer: greedy marginal-gain fill + prune + swap (mirrors index.html)
# ---------------------------------------------------------------------------
def optimize(members, skills, cfg, building_levels):
    T = len(skills)
    # Precompute per-member per-trial stats once.
    stats_by_trial = []
    for skill in skills:
        bl = building_levels.get(skill, 0)
        stats_by_trial.append([player_skill_stats(m, skill, cfg, bl) for m in members])

    rosters = [[] for _ in range(T)]
    unassigned = set(range(len(members)))
    cur_score = [0.0] * T

    def score_of(t, roster):
        # Optimizer objective = true continuous score (tiers + fraction into current tier)
        # PLUS a robustness bonus that rewards lifting the last tier's buffer up to the
        # margin. tiers dominate (weight*margin < 1), so this never trades away a real tier.
        res = simulate_trial([stats_by_trial[t][i] for i in roster], skills[t], cfg)
        frac = res["continuous_score"] - res["tiers_cleared"]
        return res["tiers_cleared"] + cfg.frac_weight * frac + cfg.robust_weight * min(frac, cfg.robust_margin)

    def greedy_fill():
        added = False
        while True:
            open_trials = [t for t in range(T) if len(rosters[t]) < cfg.max_roster]
            if not open_trials or not unassigned:
                break
            best_gain, best_m, best_t = 1e-9, -1, -1
            for t in open_trials:
                base = cur_score[t]
                for m in unassigned:
                    gain = score_of(t, rosters[t] + [m]) - base
                    if gain > best_gain:
                        best_gain, best_m, best_t = gain, m, t
            if best_m == -1:
                break
            rosters[best_t].append(best_m)
            unassigned.discard(best_m)
            cur_score[best_t] = score_of(best_t, rosters[best_t])
            added = True
        return added

    def prune():
        removed = False
        for t in range(T):
            changed = True
            while changed:
                changed = False
                for i in range(len(rosters[t])):
                    without = rosters[t][:i] + rosters[t][i + 1:]
                    s = score_of(t, without)
                    if s > cur_score[t] + 1e-9:
                        unassigned.add(rosters[t][i])
                        rosters[t] = without
                        cur_score[t] = s
                        changed = removed = True
                        break
        return removed

    def swap():
        improved = False
        for t1 in range(T):
            for t2 in range(t1 + 1, T):
                for i in range(len(rosters[t1])):
                    for j in range(len(rosters[t2])):
                        a, b = rosters[t1][i], rosters[t2][j]
                        if a == b:
                            continue
                        n1 = rosters[t1][:]; n1[i] = b
                        n2 = rosters[t2][:]; n2[j] = a
                        s1, s2 = score_of(t1, n1), score_of(t2, n2)
                        if s1 + s2 > cur_score[t1] + cur_score[t2] + 1e-9:
                            rosters[t1], rosters[t2] = n1, n2
                            cur_score[t1], cur_score[t2] = s1, s2
                            improved = True
        return improved

    greedy_fill()
    for _ in range(4):
        p = prune()
        s = swap()
        r = greedy_fill()
        if not (p or s or r):
            break

    # Build detailed results.
    results = []
    for t, skill in enumerate(skills):
        roster_stats = [stats_by_trial[t][i] for i in rosters[t]]
        res = simulate_trial(roster_stats, skill, cfg)
        # Per-player contribution on the last cleared tier's room level (or start if none).
        room = cfg.start_level + max(0, res["tiers_cleared"] - 1) * cfg.level_step
        is_enh = skill == ENHANCING
        contribs = []
        for idx, st in zip(rosters[t], roster_stats):
            sr = success_rate(st, room, cfg, is_enh)
            rate = st["rate_factor"] * sr
            contribs.append({"name": members[idx]["name"], "eff_level": st["eff_level"],
                             "csv_level": members[idx]["skills"][skill]["level"],
                             "rate": rate, "interval": st["interval"],
                             "prog_per_action": st["prog_per_action"], "success": sr})
        total_rate = sum(c["rate"] for c in contribs) or 1.0
        for c in contribs:
            c["pct"] = 100.0 * c["rate"] / total_rate
        contribs.sort(key=lambda c: c["rate"], reverse=True)
        results.append({"skill": skill, "roster_idx": rosters[t], "res": res,
                        "contribs": contribs})

    return {"results": results, "unassigned": sorted(unassigned)}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_report(opt, members, cfg):
    total_tiers = sum(r["res"]["tiers_cleared"] for r in opt["results"])
    print("=" * 78)
    print(f"OPTIMIZED ASSIGNMENT  —  {total_tiers} total tiers cleared across "
          f"{len(opt['results'])} trials  ({len(members)} members loaded, "
          f"{len(opt['unassigned'])} unassigned)")
    print("=" * 78)
    for r in opt["results"]:
        res = r["res"]
        last_room = cfg.start_level + max(0, res["tiers_cleared"] - 1) * cfg.level_step
        frac_pct = 100.0 * (res["progress"] / res["required"]) if res["required"] else 0.0
        print()
        print(f"### {r['skill']}  —  {res['tiers_cleared']} tiers cleared "
              f"(L{cfg.start_level} -> L{res['room_level']}), roster {len(r['contribs'])}/"
              f"{cfg.max_roster}"
              + ("  [STALLED]" if res["stalled"] else ""))
        if res["tiers_cleared"] > 0:
            print(f"    Expected progress on last cleared tier (L{last_room}): "
                  f"{res['last_cleared_rate']:,.1f} progress/s")
        # Progress on the last (failed / partial) tier — the one time ran out on.
        partial = res["tier_log"][-1] if res["tier_log"] and not res["tier_log"][-1]["cleared"] else None
        partial_rate = partial["rate"] if partial else res["last_cleared_rate"]
        margin_pct = cfg.robust_margin * 100
        robust = "ROBUST" if frac_pct >= margin_pct - 1e-9 else "FRAGILE"
        print(f"    Failed tier L{res['room_level']}: made {res['progress']:,.0f} / "
              f"{res['required']:,.0f} progress ({frac_pct:.1f}% of the way) "
              f"at {partial_rate:,.1f} progress/s  [{robust}: buffer {frac_pct:.1f}% vs {margin_pct:.0f}% target]")
        print(f"    {'Member':<22}{'CSV':>5}{'Eff':>5}{'Interval':>10}"
              f"{'Success':>9}{'Prog/act':>10}{'Prog/s':>10}{'Share':>8}")
        for c in r["contribs"]:
            print(f"    {c['name']:<22}{c['csv_level']:>5}{c['eff_level']:>5}"
                  f"{c['interval']:>9.2f}s{c['success']*100:>8.0f}%"
                  f"{c['prog_per_action']:>10.0f}{c['rate']:>10.1f}{c['pct']:>7.1f}%")
    if opt["unassigned"]:
        print()
        print("Unassigned (would add more required work than they'd contribute, or roster full):")
        print("    " + ", ".join(members[i]["name"] for i in opt["unassigned"]))
    print()


def build_json(opt, members, cfg):
    out = {"total_tiers": sum(r["res"]["tiers_cleared"] for r in opt["results"]),
           "trials": [], "unassigned": [members[i]["name"] for i in opt["unassigned"]]}
    for r in opt["results"]:
        res = r["res"]
        last_room = cfg.start_level + max(0, res["tiers_cleared"] - 1) * cfg.level_step
        partial = res["tier_log"][-1] if res["tier_log"] and not res["tier_log"][-1]["cleared"] else None
        frac = (res["progress"] / res["required"]) if res["required"] else 0.0
        out["trials"].append({
            "skill": r["skill"],
            "tiers_cleared": res["tiers_cleared"],
            "final_room_level": res["room_level"],
            "last_cleared_room_level": last_room,
            "last_cleared_progress_per_sec": round(res["last_cleared_rate"], 2),
            "failed_tier": {
                "room_level": res["room_level"],
                "progress_made": round(res["progress"], 1),
                "required": round(res["required"], 1),
                "fraction": round(frac, 4),
                "progress_per_sec": round(partial["rate"] if partial else res["last_cleared_rate"], 2),
                "robust": frac >= cfg.robust_margin - 1e-9,
                "robust_margin": cfg.robust_margin,
            },
            "current_tier_progress": round(res["progress"], 1),
            "current_tier_required": round(res["required"], 1),
            "roster": [{"name": c["name"], "csv_level": c["csv_level"],
                        "effective_level": c["eff_level"], "progress_per_sec": round(c["rate"], 2),
                        "share_pct": round(c["pct"], 2)} for c in r["contribs"]],
            "tier_log": [{"level": tl["level"], "required": round(tl["hp"], 1),
                          "progress_per_sec": round(tl["rate"], 2), "time_s": round(tl["time"], 1),
                          "cleared": tl["cleared"]} for tl in res["tier_log"]],
        })
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_buildings(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--building expects Skill=Level, got: {it}")
        k, v = it.split("=", 1)
        k = k.strip()
        if k not in SKILLS:
            raise SystemExit(f"Unknown skill in --building: {k}")
        out[k] = int(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Skilling Trials assignment optimizer.")
    ap.add_argument("csv", help="member CSV path")
    ap.add_argument("-s", "--skills", nargs="+", required=True,
                    help="the 4 trial skills (e.g. Milking Woodcutting Cooking Enhancing)")
    ap.add_argument("--building", nargs="*", default=[],
                    help="guild building levels, e.g. Milking=1 Cooking=2 (+2 eff level each)")
    ap.add_argument("--max-roster", type=int, default=None, help="max members per trial (default 22)")
    ap.add_argument("--duration", type=float, default=None, help="trial duration seconds (default 3600)")
    ap.add_argument("--margin", type=float, default=None,
                    help="robustness buffer target as a percent; prioritize each cleared tier "
                         "having this much progress into the next tier (default 10; 0 disables)")
    ap.add_argument("--json", metavar="PATH", help="also write results as JSON to PATH")
    args = ap.parse_args(argv)

    # Normalize skill names (accept 'Enhancement' as alias for 'Enhancing').
    alias = {"enhancement": "Enhancing"}
    skills = []
    for s in args.skills:
        canon = alias.get(s.lower(), s)
        match = next((k for k in SKILLS if k.lower() == canon.lower()), None)
        if not match:
            raise SystemExit(f"Unknown skill: {s}. Choose from {', '.join(SKILLS)}")
        skills.append(match)
    if len(skills) != 4:
        raise SystemExit(f"Expected exactly 4 skills, got {len(skills)}: {skills}")
    if len(set(skills)) != 4:
        raise SystemExit("The 4 skills must be distinct.")

    cfg = Config()
    if args.max_roster is not None:
        cfg.max_roster = args.max_roster
    if args.duration is not None:
        cfg.trial_duration = args.duration
    if args.margin is not None:
        cfg.robust_margin = args.margin / 100.0
    buildings = parse_buildings(args.building)

    members = load_members(args.csv, cfg)
    opt = optimize(members, skills, cfg, buildings)
    print_report(opt, members, cfg)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(build_json(opt, members, cfg), f, indent=2)
        print(f"Wrote JSON results to {args.json}")


if __name__ == "__main__":
    main()
