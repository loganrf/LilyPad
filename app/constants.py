"""Fixed vocabularies used by forms, storage and reports.

Keys are what gets stored in the database; values are display labels.
Keep keys stable -- changing one orphans existing rows.
"""

FEED_METHODS = {
    "breast": "Breast",
    "bottle": "Bottle",
    "solids": "Solids",
}

FEED_SIDES = {
    "left": "Left",
    "right": "Right",
    "both": "Both",
}

BOTTLE_CONTENTS = {
    "breast_milk": "Breast milk",
    "formula": "Formula",
    "mixed": "Mixed",
}

FEED_QUALITY = {
    1: "Poor",
    2: "Fair",
    3: "Good",
    4: "Great",
}

FEED_ISSUES = {
    "spit_up": "Spit-up",
    "vomit": "Vomited",
    "fussy": "Fussy",
    "sleepy": "Too sleepy",
    "poor_latch": "Poor latch",
    "gagging": "Gagging / choking",
    "gassy": "Gassy",
    "hiccups": "Hiccups",
}

DIAPER_KINDS = {
    "wet": "Wet",
    "dirty": "Dirty",
    "mixed": "Wet + dirty",
    "dry": "Dry",
}

# Kinds that count as a bowel movement / as a wet diaper.
DIRTY_KINDS = ("dirty", "mixed")
WET_KINDS = ("wet", "mixed")

STOOL_COLORS = {
    "meconium": "Black / tarry (meconium)",
    "green": "Green",
    "yellow": "Yellow / mustard",
    "orange": "Orange",
    "brown": "Brown",
    "red": "Red / bloody",
    "black": "Black (after first days)",
    "pale": "White / pale / clay",
}

# Colors that commonly warrant a call to the pediatrician. Shown as a gentle
# prompt in the UI and highlighted in reports. Not medical advice.
STOOL_COLOR_FLAGS = {
    "red": "Red or bloody stool is worth calling your pediatrician about.",
    "black": "Black stool after the first few days (once meconium has passed) "
    "is worth calling your pediatrician about.",
    "pale": "White, pale or clay-colored stool can signal a liver problem. "
    "Contact your pediatrician promptly.",
}

STOOL_CONSISTENCY = {
    "watery": "Watery",
    "loose": "Loose / seedy",
    "soft": "Soft / pasty",
    "formed": "Formed",
    "hard": "Hard / pellets",
    "mucousy": "Mucousy",
}

AMOUNTS = {
    "small": "Small",
    "medium": "Medium",
    "large": "Large",
}

REMINDER_KINDS = {
    "feeding": "Feeding",
    "diaper": "Any diaper change",
    "diaper_wet": "Wet diaper",
    "diaper_dirty": "Bowel movement",
    "vitals": "Vitals check",
    "custom": "Custom routine",
}

REMINDER_STAGE_LABELS = {
    "ok": "On track",
    "window": "Window open",
    "due": "Due now",
    "overdue": "Overdue",
    "in_progress": "In progress",
    "snoozed": "Snoozed",
    "inactive": "Outside active hours",
}

# ntfy priority scale (1 = min, 5 = urgent).
NTFY_PRIORITIES = {
    1: "Min (silent)",
    2: "Low",
    3: "Default",
    4: "High",
    5: "Urgent",
}

# "When did it happen?" quick chips, in minutes ago.
WHEN_CHIPS = [0, 5, 15, 30, 60]
DURATION_CHIPS = [5, 10, 15, 20, 30, 45]
AMOUNT_CHIPS_ML = [30, 60, 90, 120, 150]
AMOUNT_CHIPS_OZ = [1, 2, 3, 4, 5]

ML_PER_OZ = 29.5735

# A feed timer left running longer than this is treated as finished for the
# purpose of reminders, so a forgotten "Stop" doesn't silence alerts forever.
STALE_FEED_MINUTES = 120
