# Skilling Trials — Simulation Rules

Reference spec for the Labyrinth / Skilling Trials assignment optimizer. It captures how
member data is read, how per-member stats are derived, and the exact formulas used to
simulate a trial and score an assignment. The web UI (`index.html`) uses a simplified,
uniform-per-trial version of this; the Python optimizer (`optimize_trials.py`) implements
the full per-member version described here.

The 10 skills:
`Milking, Foraging, Woodcutting, Cheesesmithing, Crafting, Tailoring, Cooking, Brewing, Alchemy, Enhancing`

Skill groupings used throughout:

| Group | Skills | Matters for |
| --- | --- | --- |
| **Gathering** | Milking, Foraging, Woodcutting | Double-progress chance |
| **Artisan** | Cheesesmithing, Crafting, Tailoring | +11.2% efficiency (gear) |
| **Cooking/Brewing** | Cooking, Brewing | +11.2% efficiency (gear) |
| **Glove** | Alchemy, Enhancing | +11.2% speed (gear) |
| **Enhancing** | Enhancing | Special case — see below |

---

## 1. Member data (CSV)

We load members from a CSV like `The Arsenal_members_2026-07-25.csv`.

- **1.1** We only care, per member, about the **10 skills**, and for each skill its
  **level**, **tool**, and **house**. Relevant columns per skill `S`:
  `S` (level), `S Tool` (`holy` / `celestial` / blank), `S Tool Enh` (enhancement level,
  integer), `S House` (house level, integer).
- **1.2 Missing data defaults:** missing tool → **holy at +5**; missing house → **level 3**.
  (Applied per field: a blank tool defaults to holy, a blank enhancement defaults to +5, a
  blank house defaults to 3.)
- **1.3 House bonuses** (per house level):
  - **1.3.1 Enhancing house:** `+0.05%` success rate **and** `+1%` speed **per level**.
  - **1.3.2 Other houses:** `+1.5%` efficiency **per level**.
- **1.4 Tool bonuses** — tools give **speed**, except on Enhancing where they give **success rate**:
  - **1.4.1 Enhancing tool (success rate, at +0):** holy `+3.6%`, celestial `+4.2%`.
  - **1.4.2 Other tool (speed, at +0):** holy `+90%`, celestial `+105%`.
  - **1.4.3 Enhancement scaling.** The tool's base bonus is scaled up by its enhancement
    level. Bonus at each level (fraction of base, i.e. total multiplier `= 1 + table[level]`):

    | +N | bonus | +N | bonus | +N | bonus | +N | bonus |
    |----|-------|----|-------|----|-------|----|-------|
    | 1  | 2.0%  | 6  | 15.0% | 11 | 33.4% | 16 | 64.4% |
    | 2  | 4.2%  | 7  | 18.2% | 12 | 38.4% | 17 | 72.4% |
    | 3  | 6.6%  | 8  | 21.6% | 13 | 44.0% | 18 | 81.0% |
    | 4  | 9.2%  | 9  | 25.2% | 14 | 50.2% | 19 | 90.2% |
    | 5  | 12.0% | 10 | 29.0% | 15 | 57.0% | 20 | 100%  |

    `+0` = 0%. **`tool_bonus = base_bonus × (1 + table[enh])`.**
    - e.g. a **+5 holy Milking tool** → `90% × 1.12 = 100.8%` speed.
    - e.g. a **+10 celestial Woodcutting tool** → `105% × 1.29 = 135.45%` speed.

## 2. Trial selection

Pick **4** of the 10 skills; members are assigned across those 4 trials.

## 3. Trial structure (tiers)

Tiers start at level **100**, then **110, 120, …** (step 10). Required progress for a tier:

```
TotalWork = DifficultyLevel × 400 × (1 + Players/100)
```

where `Players` is the roster size of that trial (attendance tax: +1% required work per attendee).

## 4. Progress simulation

Every assigned player makes progress in parallel. Per player, per skill:

### 4.1 Success rate
Base **80%**, adjusted multiplicatively by the level gap to the current room:
`-1%` per level **below** the room, `+0.5%` per level **above**. Floor **5%** (never lower).

```
delta   = effectiveLevel − roomLevel
bonus   = delta × 0.005            if delta ≥ 0
          delta × 0.01             if delta < 0     (delta negative → penalty)
sr      = 0.80 × (1 + bonus)
# Enhancing only: add flat tool + house success bonuses (rule 1.3.1, 1.4.1)
sr     += enhToolSuccess + enhHouseSuccess          (Enhancing only)
sr      = clamp(sr, 0.05, 1.0)
```

### 4.2 Double-progress chance (gathering only)
Chance to double the progress on a success — **Milking, Foraging, Woodcutting only**:
`2%` achievement + `20%` community + `4%` gear (2% earring + 2% ring) = **26% baseline**.
With the community multiplier (rule 4.9) the community part becomes `24%`, so **30% used**.
Non-gathering skills: `0`.

### 4.3 Efficiency (not used by Enhancing)
`house×1.5%` (rule 1.3.2) + `2%` achievement + `14%` community + `11.2%` gear. The gear
bonus comes from two separate (independently tunable) groups: **Artisan** (Cheesesmithing,
Crafting, Tailoring) and **Cooking/Brewing** (Cooking, Brewing). Woodcutting is in neither.
With the community multiplier the community part becomes `16.8%`.

```
efficiency = houseLevel × 0.015 + 0.02 + 0.168 + (artisan ? 0.112 : 0)
```

### 4.4 Progress per successful action
- **4.4.1 Enhancing:** `progress = effectiveLevel` (no efficiency).
- **4.4.2 Other skills:** `progress = floor(effectiveLevel × (1 + efficiency))`.

### 4.5 Speed
```
speed = 0.05 (cape)
      + toolSpeed            (rule 1.4.2 × 1.4.3; 0 for Enhancing — its tool gives success, not speed)
      + 0.112 (glove)        (Alchemy and Enhancing only)
      + 0.052 (neck +3)
      + 0.24  (community)     (Enhancing only; 20% baseline × 1.2)
      + houseSpeed           (Enhancing only: houseLevel × 0.01, rule 1.3.1)
```

### 4.6 Interval
`interval = baseInterval / (1 + speed)`, where `baseInterval = 8s` for Enhancing, `10s` otherwise.

### 4.7 / 4.8 Accumulation
Each interval a player rolls for success; a success adds progress (and may double, if gathering).
All players progress simultaneously. When total accumulated progress reaches a tier's
`TotalWork`, the trial advances to the next tier. The trial ends when accumulated time
(across all tiers so far) reaches the **3600 s** threshold.

> **Modeling note.** Because per-member tools/houses/enhancements differ, players have
> **different intervals**, so we don't tick in lockstep. Instead each player contributes a
> constant *expected progress rate* while the room level is fixed:
> `rate = successRate × (1 + doubleChance) × progressPerAction / interval`.
> A tier's time is `TotalWork / Σ rate`; success rate (hence rate) is recomputed at each
> tier because the room level changes.

### 4.9 Community multiplier
All community buffs are `1.2×` their baseline when running the optimizer
(e.g. gathering double-progress community `20% → 24%`; efficiency community `14% → 16.8%`;
Enhancing speed community `20% → 24%`).

### 4.10 Guild buildings
A guild building raises the effective skill level of every attendee: **+2 effective levels
per building level**, applied on top of the member's own CSV level (affects both success
rate and progress).

## 5. Optimizer (see `optimize_trials.py`)

Input: member CSV + the 4 chosen trial skills (+ optional guild-building levels).
Output, per trial: **max tiers cleared**, the **roster with effective levels and
contribution %**, and the **expected progress/second on the last cleared tier**.

The assignment search mirrors `index.html`: greedy marginal-gain fill, then local search
(**prune** weak members, **swap** across trials, refill) repeated until no improvement.
The score optimized is `tiersCleared + fractional progress into the current tier` (a
continuous objective so the search has a gradient between integer tier boundaries), with an
optional roster cap per trial (default 22).
