"""Tunable game constants. Change timings here."""

# Defaults for each game. The host can change the first two in the setup panel.
# The wolves have their own night time, since they have to talk it over.  
SOLO_ACTION_SECONDS = 45   # default night time: how long each role has to act
WOLF_ACTION_SECONDS = 90   # default night time for the wolf pack
SPEAKER_SECONDS = 60       # day: speaking time per player
NIGHT_SECONDS_CHOICES = (20, 30, 45, 60, 90, 120)   # night time: how long each role has to act
NIGHT_WOLF_SECONDS_CHOICES = (30, 45, 60, 90, 120, 180, 240)  # the wolves only
SPEAK_SECONDS_CHOICES = (30, 45, 60, 90, 120, 180, 0)  # 0 = unlimited: the speaker ends with 跳過
PANEL_TIMEOUT = 600        # how long a private panel stays clickable (a safety limit only)
DAY_VOTE_SECONDS = 60
HUNTER_SHOT_SECONDS = 45

DEAD_ROLE_DELAY = (6, 12)   # seconds to wait when a night role's player is dead (hides who is dead)
FAKE_ACT_DELAY = (1.5, 3.0)  # seconds before a test-mode bot player acts

# Sheriff election (警長競選)
SHERIFF_SIGNUP_SECONDS = 30      # time to choose 上警 / 不上警
SHERIFF_WITHDRAW_SECONDS = 20    # time for the candidates to 退水 after the campaign speeches
SHERIFF_DIRECTION_SECONDS = 20   # time for the sheriff to choose the speaking direction
SHERIFF_BADGE_SECONDS = 45       # time for a dead sheriff to hand over (or tear) the badge
