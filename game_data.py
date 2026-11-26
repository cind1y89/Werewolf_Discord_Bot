"""Loads the split game data under data/ into the same structure basic.json had.

Layout (add a file and it is picked up automatically):
  data/meta.json
  data/rules/{rule_set,night_pipeline,win_evaluation,victory_modes}.json
  data/enums/<name>.json
  data/player_state_template.json
  data/abilities/<ability_id>.json
  data/roles/<faction>/<role_id>.json
  data/presets/<preset_id>.json
"""
import json
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_dir(folder: Path, pattern: str = "*.json") -> dict:
    return {p.stem: _read(p) for p in sorted(folder.glob(pattern))}


def load_game_data(data_dir: Path = DATA_DIR) -> dict:
    data = _read(data_dir / "meta.json")
    for name in ("rule_set", "night_pipeline", "win_evaluation", "victory_modes"):
        data[name] = _read(data_dir / "rules" / f"{name}.json")
    data["enums"] = _read_dir(data_dir / "enums")
    data["player_state_template"] = _read(data_dir / "player_state_template.json")
    data["ability_library"] = _read_dir(data_dir / "abilities")
    data["roles"] = _read_dir(data_dir / "roles", "*/*.json")
    data["presets"] = _read_dir(data_dir / "presets")
    return data


if __name__ == "__main__":
    d = load_game_data()
    print(f"{len(d['roles'])} roles, {len(d['ability_library'])} abilities, {len(d['enums'])} enums")
