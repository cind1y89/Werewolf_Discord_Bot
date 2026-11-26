"""Checks a role's JSON against what the engine can actually run.

  python -m game.validate            check every role and preset
  python -m game.validate my_role    check one role

A role can use any mix of the building blocks the engine understands (see the lists below).
Anything it does not understand is reported here and shown on the confirm screen, instead of
failing silently in the middle of a game."""
from __future__ import annotations

import sys

from . import day, night

REQUIRED_KEYS = (
    "id", "name", "en_name", "initial_state", "skill_description", "actions",
    "resources", "ability_refs", "abilities", "death_trigger", "dynamic_rules", "custom_data",
)
SEER_RESULTS = {"VILLAGER", "WEREWOLF", "DYNAMIC"}

# Abilities that run on their own from the data: nothing else has to be written.
PASSIVE_EFFECTS = {"IMMUNE_NIGHT_DEATH", "IMMUNE_DEATH_CAUSE", "IMMUNE_CHARM", "DELAY_DEATH_UNTIL_NIGHT"}            # trigger PASSIVE
REACTIONS = {"ON_BEING_SEER_CHECKED", "ON_BEING_WITCH_POISONED"}           # need resource reflect_charge
DYNAMIC_RULES = {"ROLE_MODEL_DIED", "LOVERS_MIXED_FACTION", "LAST_WOLF_STANDING"}
SECONDARY_NIGHT = {"use_poison"}  # part of the witch's panel, not a turn of its own


def ability_problems(ab: dict, where: str) -> list[str]:
    ident, trigger = ab.get("id", "?"), ab.get("trigger")
    effects = {e["type"] for e in ab.get("effects", [])}
    if trigger in ("NIGHT", "FIRST_NIGHT"):
        if ident not in night.PANEL_ABILITIES and ident not in SECONDARY_NIGHT:
            return [f"{where}：夜晚技能「{ident}」還沒有對應的程式（night.py / night_flow.py）。"]
    elif trigger == "DAY_SPEECH":
        if ident not in day.SKILL_NAMES:
            return [f"{where}：白天技能「{ident}」還沒有對應的程式（day.py 的 SKILL_NAMES / use_day_skill）。"]
    elif trigger == "DAY_VOTE":
        if "CANCEL_EXILE" not in effects:
            return [f"{where}：投票階段技能「{ident}」只支援「免於放逐」(CANCEL_EXILE) 這類效果。"]
    elif trigger == "DAWN":
        if effects != {"CHECK_NEIGHBORS_FOR_WOLF"}:
            return [f"{where}：天亮技能「{ident}」只支援效果 CHECK_NEIGHBORS_FOR_WOLF（訓熊師）。"]
    elif trigger == "PASSIVE":
        if not effects <= PASSIVE_EFFECTS:
            return [f"{where}：被動技能「{ident}」只支援效果 {sorted(PASSIVE_EFFECTS)}。"]
    elif trigger in REACTIONS:
        if effects != {"REFLECT_ATTACKER"}:
            return [f"{where}：反應技能「{ident}」只支援效果 REFLECT_ATTACKER。"]
    else:
        return [f"{where}：技能「{ident}」的觸發時機 {trigger} 引擎還不支援。"]
    return []


def role_problems(data: dict, role_id: str) -> list[str]:
    """What stops this role from working fully. An empty list means it is ready to play."""
    role = data["roles"].get(role_id)
    if role is None:
        return [f"找不到角色「{role_id}」。"]
    problems = []
    missing = [k for k in REQUIRED_KEYS if k not in role]
    if missing:
        return [f"缺少欄位：{', '.join(missing)}"]  # nothing else can be checked reliably

    enums = data["enums"]
    if role["id"] != role_id:
        problems.append(f"檔名（{role_id}）與 id（{role['id']}）必須相同。")
    init = role["initial_state"]
    for key, enum in (("faction", "factions"), ("category", "categories"), ("win_condition", "win_conditions")):
        if init.get(key) not in enums[enum]:
            problems.append(f"initial_state.{key}={init.get(key)!r} 不在 enums/{enum}.json 裡。")
    if init.get("seer_result") not in SEER_RESULTS:
        problems.append(f"initial_state.seer_result 必須是 {sorted(SEER_RESULTS)} 其中之一。")

    library = data["ability_library"]
    for ref in role["ability_refs"]:
        if ref not in library:
            problems.append(f"ability_refs「{ref}」不在 data/abilities/ 裡。")
        else:
            problems += ability_problems(library[ref], f"共用技能 {ref}")
    for ab in role["abilities"]:
        problems += ability_problems(ab, f"技能 {ab.get('id', '?')}")
        for res in ([ab["resource"]] if ab.get("resource") else []):
            if res not in role["resources"]:
                problems.append(f"技能 {ab['id']} 使用資源「{res}」，但 resources 裡沒有定義。")
        if ab["trigger"] in REACTIONS and "reflect_charge" not in role["resources"]:
            problems.append(f"反應技能 {ab['id']} 需要 resources.reflect_charge。")

    trig = role["death_trigger"]
    if trig:
        if trig["effect"]["type"] != "KILL_TARGET":
            if trig["effect"]["type"] not in ("KILL_LINKED_CHARMED_TARGET", "KILL_VOTERS"):
                problems.append(f"death_trigger.effect.type={trig['effect']['type']} 引擎只支援 KILL_TARGET。")
        elif "bullet" not in role["resources"]:
            problems.append("death_trigger 開槍需要 resources.bullet（initial / max）。")
        for key in ("allowed_causes", "forbidden_causes"):
            bad = [c for c in trig[key] if c not in enums["death_causes"]]
            if bad:
                problems.append(f"death_trigger.{key} 有未知死因：{bad}")
    for rule in role["dynamic_rules"]:
        if rule["trigger"] not in DYNAMIC_RULES:
            problems.append(f"dynamic_rules 觸發 {rule['trigger']} 引擎還不支援。")
    return problems


def check_all(data: dict) -> dict[str, list[str]]:
    result = {rid: role_problems(data, rid) for rid in data["roles"]}
    for pid, preset in data["presets"].items():
        for rid in preset["roles"] + [preset["fill_role"]]:
            if rid not in data["roles"]:
                result.setdefault(f"preset:{pid}", []).append(f"預設配置用了不存在的角色「{rid}」。")
        if "players" in preset and not preset.get("note"):  # a board with a note may be off on purpose
            need = preset["players"] + (2 if "thief" in preset["roles"] else 0)
            if len(preset["roles"]) != need:
                result.setdefault(f"preset:{pid}", []).append(
                    f"{preset['players']} 人的板型應有 {need} 張牌，但設定了 {len(preset['roles'])} 張。")
        bad_rules = [k for k in preset.get("rules", {}) if k not in data["rule_set"]["settings"]]
        if bad_rules:
            result.setdefault(f"preset:{pid}", []).append(f"rules 裡有未知的規則設定：{bad_rules}")
    return {k: v for k, v in result.items() if v}


def doc_notes(data: dict, role_ids: list[str] | None = None) -> list[str]:
    """Non-blocking reminders: the card's 技能判定 section comes from `skill_rules`."""
    return [f"{rid}：沒有 skill_rules（玩家卡片上的「技能判定」會是空的）。"
            for rid in (role_ids or data["roles"]) if rid in data["roles"] and not data["roles"][rid].get("skill_rules")]


def main(argv: list[str]) -> int:
    from game_data import load_game_data
    data = load_game_data()
    for note in doc_notes(data, argv or None):
        print("⚠️ ", note)
    if argv:
        found = {rid: role_problems(data, rid) for rid in argv}
        found = {k: v for k, v in found.items() if v}
        checked = len(argv)
    else:
        found, checked = check_all(data), len(data["roles"]) + len(data["presets"])
    if not found:
        print(f"✅ 全部通過（檢查了 {checked} 項）。")
        return 0
    for name, problems in found.items():
        print(f"❌ {name}")
        for p in problems:
            print("   -", p)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
