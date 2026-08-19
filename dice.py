from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

DEFAULT_ENEMY_DAMAGE = os.getenv("ENEMY_DAMAGE_DICE", "1d6")
LINK_API_URL = os.getenv("LINK_API_URL", "").rstrip("/")
LINKDICE_API_KEY = os.getenv("LINKDICE_API_KEY", "").strip()
LINK_API_TIMEOUT = float(os.getenv("LINK_API_TIMEOUT", "4"))
PLAYER_ROLE_NAME = os.getenv("PLAYER_ROLE_NAME", "player")

FALLBACK_WEAPON_NAME = "유리 파편"
FALLBACK_ATTACK_POWER = 4
FALLBACK_SHIELD_NAME = "화물 상자 뚜껑"
FALLBACK_DEFENSE_POWER = 1
FALLBACK_HP = 20
FALLBACK_BOMBS = 2
BOMB_DAMAGE_MIN = 10
BOMB_DAMAGE_MAX = 14

ATTACK_PERFECT = 0.85
ATTACK_GOOD = 1.25
DEFENSE_PERFECT = 0.90
DEFENSE_GOOD = 1.30

DICE_RE = re.compile(r"^(?P<count>\d{1,2})d(?P<sides>\d{1,4})(?P<mod>[+-]\d{1,4})?$", re.I)

ATTACK_FLAVOR = [
    "아직인가...",
    "좀만 더 보자...",
    "가만히 있네...",
    "타이밍 잡기 어렵네...",
    "계속 지켜보자...",
]
ATTACK_FAKEOUT = [
    "**💥 앗!!! 멀쩡히 서 있다!!!**",
    "**💥 앗!!! 이쪽을 쳐다본다!!!**",
    "**💥 앗!!! 괜히 한 바퀴 돌았다!!!**",
    "**💥 앗!!! 아무 일도 없었다!!!**",
]
ATTACK_REAL = [
    "**💥 앗!!! 지금이야!!!**",
    "**💥 앗!!! 기회다!!!**",
]
DEFEND_FLAVOR = [
    "아직인가...",
    "언제 치려나...",
    "좀만 더 보자...",
    "가만히 있네...",
    "뭘 하려는 거지...",
    "계속 지켜보자...",
]
DEFEND_FAKEOUT = [
    "**💥 앗!!! 아무 일도 없다!!!**",
    "**💥 앗!!! 그냥 쳐다본다!!!**",
    "**💥 앗!!! 가만히 서 있다!!!**",
    "**💥 앗!!! 괜히 움직였다!!!**",
    "**💥 앗!!! 다시 멈춰 섰다!!!**",
    "**💥 앗!!! 그냥 지나간다!!!**",
]
DEFEND_REAL = [
    "**💥 앗!!! 공격한다!!!**",
    "**💥 앗!!! 지금 막아!!!**",
]


def get_db_path() -> Path:
    railway_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    data_dir = Path(railway_volume) if railway_volume else Path("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "linkdice.db"


DB_PATH = get_db_path()


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with connect_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                attack INTEGER,
                defense INTEGER,
                agility INTEGER,
                intelligence INTEGER,
                luck INTEGER,
                hp INTEGER,
                max_hp INTEGER,
                special_name TEXT,
                special_level INTEGER,
                bombs INTEGER,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS active_enemies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                hp INTEGER NOT NULL,
                max_hp INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_active_enemies_guild
            ON active_enemies (guild_id, active, hp);

            CREATE TABLE IF NOT EXISTS sync_settings (
                guild_id INTEGER PRIMARY KEY,
                hp_sync INTEGER NOT NULL DEFAULT 1,
                bombs_sync INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS trpg_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                turn_number INTEGER NOT NULL DEFAULT 1,
                turn_locked INTEGER NOT NULL DEFAULT 0,
                resolving INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_session
            ON trpg_sessions (guild_id)
            WHERE active = 1;

            CREATE TABLE IF NOT EXISTS trpg_participants (
                session_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (session_id, user_id),
                FOREIGN KEY (session_id) REFERENCES trpg_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS trpg_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                turn_number INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                action_name TEXT NOT NULL,
                grade TEXT,
                elapsed REAL,
                enemy_id INTEGER,
                attack_power INTEGER,
                weapon_name TEXT,
                link_connected INTEGER NOT NULL DEFAULT 0,
                custom_attack INTEGER,
                custom_enemy_id INTEGER,
                custom_attack_power INTEGER,
                bomb_damage INTEGER NOT NULL DEFAULT 0,
                finalized INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finalized_at TEXT,
                UNIQUE (session_id, turn_number, user_id),
                FOREIGN KEY (session_id) REFERENCES trpg_sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_trpg_actions_turn
            ON trpg_actions (session_id, turn_number, resolved, user_id);
            """
        )
        player_columns = {row[1] for row in conn.execute("PRAGMA table_info(player_stats)")}
        if "bombs" not in player_columns:
            conn.execute("ALTER TABLE player_stats ADD COLUMN bombs INTEGER")
        if "defense" not in player_columns:
            conn.execute("ALTER TABLE player_stats ADD COLUMN defense INTEGER")
        conn.commit()


def ensure_player(guild_id: int, user_id: int) -> None:
    with connect_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO player_stats (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        conn.commit()


def get_player(guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM player_stats WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()


def update_player_fields(guild_id: int, user_id: int, **fields: object) -> None:
    if not fields:
        return
    allowed = {"hp", "max_hp", "bombs"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown player fields: {unknown}")
    ensure_player(guild_id, user_id)
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = list(fields.values()) + [guild_id, user_id]
    with connect_db() as conn:
        conn.execute(
            f"UPDATE player_stats SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND user_id = ?",
            values,
        )
        conn.commit()


def get_sync_settings(guild_id: int) -> tuple[bool, bool]:
    with connect_db() as conn:
        conn.execute("INSERT OR IGNORE INTO sync_settings (guild_id) VALUES (?)", (guild_id,))
        row = conn.execute(
            "SELECT hp_sync, bombs_sync FROM sync_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        conn.commit()
    return bool(row["hp_sync"]), bool(row["bombs_sync"])


def set_sync_settings(
    guild_id: int,
    hp_sync: Optional[bool] = None,
    bombs_sync: Optional[bool] = None,
) -> tuple[bool, bool]:
    current_hp, current_bombs = get_sync_settings(guild_id)
    next_hp = current_hp if hp_sync is None else hp_sync
    next_bombs = current_bombs if bombs_sync is None else bombs_sync
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO sync_settings (guild_id, hp_sync, bombs_sync)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                hp_sync = excluded.hp_sync,
                bombs_sync = excluded.bombs_sync
            """,
            (guild_id, int(next_hp), int(next_bombs)),
        )
        conn.commit()
    return next_hp, next_bombs


def get_active_enemies(guild_id: int, include_down: bool = True) -> list[sqlite3.Row]:
    query = "SELECT * FROM active_enemies WHERE guild_id = ? AND active = 1"
    params: list[object] = [guild_id]
    if not include_down:
        query += " AND hp > 0"
    query += " ORDER BY id ASC"
    with connect_db() as conn:
        return conn.execute(query, params).fetchall()


def get_enemy(guild_id: int, enemy_id: int) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM active_enemies WHERE guild_id = ? AND id = ? AND active = 1",
            (guild_id, enemy_id),
        ).fetchone()


def spawn_enemy(guild_id: int, name: str, hp: int) -> int:
    with connect_db() as conn:
        cursor = conn.execute(
            "INSERT INTO active_enemies (guild_id, name, hp, max_hp) VALUES (?, ?, ?, ?)",
            (guild_id, name, hp, hp),
        )
        conn.commit()
        return int(cursor.lastrowid)


def deactivate_enemy(guild_id: int, enemy_id: int) -> bool:
    with connect_db() as conn:
        cursor = conn.execute(
            "UPDATE active_enemies SET active = 0 WHERE guild_id = ? AND id = ? AND active = 1",
            (guild_id, enemy_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_active_session(guild_id: int) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM trpg_sessions WHERE guild_id = ? AND active = 1 ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()


def create_session(guild_id: int, user_ids: list[int]) -> sqlite3.Row:
    with connect_db() as conn:
        old = conn.execute(
            "SELECT id FROM trpg_sessions WHERE guild_id = ? AND active = 1",
            (guild_id,),
        ).fetchone()
        if old is not None:
            raise ValueError("이미 진행 중인 세션이 있습니다.")
        cursor = conn.execute(
            "INSERT INTO trpg_sessions (guild_id, active, turn_number, turn_locked, resolving) VALUES (?, 1, 1, 0, 0)",
            (guild_id,),
        )
        session_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO trpg_participants (session_id, user_id) VALUES (?, ?)",
            [(session_id, user_id) for user_id in user_ids],
        )
        conn.commit()
    session = get_active_session(guild_id)
    if session is None:
        raise RuntimeError("세션 생성에 실패했습니다.")
    return session


def end_session(guild_id: int) -> bool:
    with connect_db() as conn:
        cursor = conn.execute(
            "UPDATE trpg_sessions SET active = 0, turn_locked = 1, resolving = 0 WHERE guild_id = ? AND active = 1",
            (guild_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_participant_ids(session_id: int) -> list[int]:
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT user_id FROM trpg_participants WHERE session_id = ? ORDER BY user_id",
            (session_id,),
        ).fetchall()
    return [int(row["user_id"]) for row in rows]


def is_participant(session_id: int, user_id: int) -> bool:
    with connect_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM trpg_participants WHERE session_id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
    return row is not None


def get_turn_action(session_id: int, turn_number: int, user_id: int) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM trpg_actions WHERE session_id = ? AND turn_number = ? AND user_id = ?",
            (session_id, turn_number, user_id),
        ).fetchone()


def reserve_action(
    session: sqlite3.Row,
    user_id: int,
    kind: str,
    action_name: str,
    enemy_id: Optional[int] = None,
    attack_power: Optional[int] = None,
    weapon_name: Optional[str] = None,
    link_connected: bool = False,
) -> int:
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO trpg_actions (
                session_id, guild_id, turn_number, user_id, kind, action_name,
                enemy_id, attack_power, weapon_name, link_connected, finalized
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                int(session["id"]),
                int(session["guild_id"]),
                int(session["turn_number"]),
                user_id,
                kind,
                action_name,
                enemy_id,
                attack_power,
                weapon_name,
                int(link_connected),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finalize_action(action_id: int, grade: str, elapsed: Optional[float]) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE trpg_actions
            SET grade = ?, elapsed = ?, finalized = 1, finalized_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (grade, elapsed, action_id),
        )
        conn.commit()


def finalize_bomb_action(action_id: int, damage: int) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE trpg_actions
            SET grade = 'BOMB', bomb_damage = ?, finalized = 1, finalized_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (damage, action_id),
        )
        conn.commit()


def delete_action(action_id: int) -> None:
    with connect_db() as conn:
        conn.execute("DELETE FROM trpg_actions WHERE id = ?", (action_id,))
        conn.commit()


def get_turn_actions(session_id: int, turn_number: int) -> list[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            "SELECT * FROM trpg_actions WHERE session_id = ? AND turn_number = ? ORDER BY id",
            (session_id, turn_number),
        ).fetchall()


def get_missing_participants(session: sqlite3.Row) -> list[int]:
    participant_ids = get_participant_ids(int(session["id"]))
    with connect_db() as conn:
        rows = conn.execute(
            """
            SELECT user_id FROM trpg_actions
            WHERE session_id = ? AND turn_number = ? AND finalized = 1
            """,
            (int(session["id"]), int(session["turn_number"])),
        ).fetchall()
    done = {int(row["user_id"]) for row in rows}
    return [user_id for user_id in participant_ids if user_id not in done]


def lock_turn(session_id: int) -> None:
    with connect_db() as conn:
        conn.execute("UPDATE trpg_sessions SET turn_locked = 1 WHERE id = ?", (session_id,))
        conn.commit()


def set_resolving(session_id: int, value: bool) -> None:
    with connect_db() as conn:
        conn.execute(
            "UPDATE trpg_sessions SET resolving = ?, turn_locked = 1 WHERE id = ?",
            (int(value), session_id),
        )
        conn.commit()


def advance_turn(session_id: int) -> int:
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE trpg_sessions
            SET turn_number = turn_number + 1, turn_locked = 0, resolving = 0
            WHERE id = ? AND active = 1
            """,
            (session_id,),
        )
        row = conn.execute("SELECT turn_number FROM trpg_sessions WHERE id = ?", (session_id,)).fetchone()
        conn.commit()
    return int(row["turn_number"])


def set_custom_action_resolution(
    action_id: int,
    is_attack: bool,
    enemy_id: Optional[int] = None,
    attack_power: Optional[int] = None,
) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE trpg_actions
            SET custom_attack = ?, custom_enemy_id = ?, custom_attack_power = ?
            WHERE id = ?
            """,
            (int(is_attack), enemy_id, attack_power, action_id),
        )
        conn.commit()


def mark_turn_actions_resolved(session_id: int, turn_number: int) -> None:
    with connect_db() as conn:
        conn.execute(
            "UPDATE trpg_actions SET resolved = 1 WHERE session_id = ? AND turn_number = ?",
            (session_id, turn_number),
        )
        conn.commit()


def fallback_link_profile() -> dict:
    return {
        "weapon": {"name": FALLBACK_WEAPON_NAME, "power": FALLBACK_ATTACK_POWER},
        "ring": None,
        "shield": {"name": FALLBACK_SHIELD_NAME, "power": FALLBACK_DEFENSE_POWER, "hp_bonus": 0},
        "head": None,
        "attack_power": FALLBACK_ATTACK_POWER,
        "defense_power": FALLBACK_DEFENSE_POWER,
        "hp": FALLBACK_HP,
        "max_hp": FALLBACK_HP,
        "bombs": FALLBACK_BOMBS,
    }


async def link_api_request(
    method: str,
    guild_id: int,
    user_id: int,
    payload: Optional[dict] = None,
) -> Optional[dict]:
    if not LINK_API_URL or not LINKDICE_API_KEY:
        return None
    url = f"{LINK_API_URL}/linkdice/player/{guild_id}/{user_id}"
    headers = {"Authorization": f"Bearer {LINKDICE_API_KEY}"}
    timeout = aiohttp.ClientTimeout(total=LINK_API_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, headers=headers, json=payload) as response:
                if response.status >= 400:
                    return None
                return await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def get_effective_profile(guild_id: int, user_id: int) -> tuple[dict, bool]:
    ensure_player(guild_id, user_id)
    local = get_player(guild_id, user_id)
    remote = await link_api_request("GET", guild_id, user_id)
    connected = remote is not None
    profile = remote if remote is not None else fallback_link_profile()
    hp_sync, bombs_sync = get_sync_settings(guild_id)

    if remote is not None and hp_sync:
        update_player_fields(guild_id, user_id, hp=int(remote["hp"]), max_hp=int(remote["max_hp"]))
        local = get_player(guild_id, user_id)
    elif local is None or local["hp"] is None or local["max_hp"] is None:
        update_player_fields(guild_id, user_id, hp=int(profile["hp"]), max_hp=int(profile["max_hp"]))
        local = get_player(guild_id, user_id)

    if remote is not None and bombs_sync:
        update_player_fields(guild_id, user_id, bombs=int(remote["bombs"]))
        local = get_player(guild_id, user_id)
    elif local is None or local["bombs"] is None:
        update_player_fields(guild_id, user_id, bombs=int(profile["bombs"]))
        local = get_player(guild_id, user_id)

    if local is not None:
        if not hp_sync or remote is None:
            profile["hp"] = int(local["hp"])
            profile["max_hp"] = int(local["max_hp"])
        if not bombs_sync or remote is None:
            profile["bombs"] = int(local["bombs"])

    return profile, connected


async def write_link_resources(
    guild_id: int,
    user_id: int,
    hp: Optional[int] = None,
    bombs: Optional[int] = None,
) -> bool:
    payload: dict[str, int] = {}
    if hp is not None:
        payload["hp"] = int(hp)
    if bombs is not None:
        payload["bombs"] = int(bombs)
    if not payload:
        return True
    result = await link_api_request("PATCH", guild_id, user_id, payload)
    if result is None:
        return False
    fields: dict[str, int] = {}
    if "hp" in payload:
        fields["hp"] = int(result["hp"])
        fields["max_hp"] = int(result["max_hp"])
    if "bombs" in payload:
        fields["bombs"] = int(result["bombs"])
    update_player_fields(guild_id, user_id, **fields)
    return True


def parse_and_roll_dice(expression: str) -> tuple[int, list[int], int]:
    normalized = expression.replace(" ", "").lower()
    match = DICE_RE.fullmatch(normalized)
    if not match:
        raise ValueError("주사위식은 1d6, 2d4+1 같은 형식이어야 합니다.")
    count = int(match.group("count"))
    sides = int(match.group("sides"))
    modifier = int(match.group("mod") or 0)
    if count < 1 or count > 20 or sides < 2 or sides > 1000:
        raise ValueError("주사위식 범위를 확인해주세요.")
    rolls = [random.randint(1, sides) for _ in range(count)]
    total = max(0, sum(rolls) + modifier)
    return total, rolls, modifier


def format_dice_roll(expression: str, rolls: list[int], modifier: int, total: int) -> str:
    detail = " + ".join(str(r) for r in rolls)
    if modifier > 0:
        detail += f" + {modifier}"
    elif modifier < 0:
        detail += f" - {abs(modifier)}"
    return f"`{expression}` → {detail} = **{total}**"


def enemy_display_name(enemy: sqlite3.Row) -> str:
    return f"{enemy['name']} #{enemy['id']}"


def hp_bar(current: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, current / maximum))
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def member_label(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"<@{user_id}>"


def grade_color(grade: str) -> discord.Colour:
    if grade == "PERFECT":
        return discord.Colour.gold()
    if grade == "GOOD":
        return discord.Colour.green()
    return discord.Colour.red()


def grade_damage(power: int, grade: str) -> int:
    if grade == "PERFECT":
        return max(1, round(power * 1.40))
    if grade == "GOOD":
        return max(1, int(power))
    return 0


def defense_damage(raw_damage: int, defense_power: int, grade: str) -> int:
    if raw_damage <= 0:
        return 0
    if grade == "PERFECT":
        return 0
    damage = raw_damage
    if grade == "GOOD":
        damage = max(1, round(damage * 0.45))
    damage = max(0, damage - max(0, defense_power))
    if grade == "MISS":
        damage = max(1, damage)
    return damage


class LinkDiceBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        init_db()
        self.tree.add_command(enemy_group)
        self.tree.add_command(player_group)
        self.tree.add_command(turn_group)
        await self.tree.sync()
        print("Global commands synced")


bot = LinkDiceBot()
active_timing_games: dict[tuple[int, int], "TimingGame"] = {}
active_session_lobbies: dict[int, "SessionSetupView"] = {}


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")
    print(f"Database: {DB_PATH}")


async def enemy_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []
    needle = current.lower().strip()
    choices: list[app_commands.Choice[str]] = []
    for enemy in get_active_enemies(interaction.guild_id, include_down=False):
        label = f"{enemy_display_name(enemy)} — HP {enemy['hp']}/{enemy['max_hp']}"
        if needle and needle not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(enemy["id"])))
        if len(choices) >= 25:
            break
    return choices


async def any_active_enemy_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []
    needle = current.lower().strip()
    choices: list[app_commands.Choice[str]] = []
    for enemy in get_active_enemies(interaction.guild_id, include_down=True):
        state = "DOWN" if int(enemy["hp"]) <= 0 else f"HP {enemy['hp']}/{enemy['max_hp']}"
        label = f"{enemy_display_name(enemy)} — {state}"
        if needle and needle not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(enemy["id"])))
        if len(choices) >= 25:
            break
    return choices


async def player_role_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    guild = interaction.guild
    if guild is None:
        return []
    role = discord.utils.get(guild.roles, name=PLAYER_ROLE_NAME)
    if role is None:
        return []
    needle = current.lower().strip()
    choices: list[app_commands.Choice[str]] = []
    for member in role.members:
        searchable = f"{member.display_name} {member.name} {member.id}".lower()
        if needle and needle not in searchable:
            continue
        choices.append(
            app_commands.Choice(
                name=f"{member.display_name} (@{member.name})"[:100],
                value=str(member.id),
            )
        )
        if len(choices) >= 25:
            break
    return choices


async def require_player_turn(interaction: discord.Interaction) -> Optional[sqlite3.Row]:
    if interaction.guild_id is None:
        await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
        return None
    session = get_active_session(interaction.guild_id)
    if session is None:
        await interaction.response.send_message("진행 중인 LINKDICE 세션이 없습니다. 관리자가 `/세션`을 시작해야 합니다.", ephemeral=True)
        return None
    if not is_participant(int(session["id"]), interaction.user.id):
        await interaction.response.send_message("현재 세션 참가자가 아닙니다.", ephemeral=True)
        return None
    if bool(session["turn_locked"]):
        await interaction.response.send_message("현재 턴은 종료되었습니다. 정산이 끝날 때까지 기다려주세요.", ephemeral=True)
        return None
    existing = get_turn_action(int(session["id"]), int(session["turn_number"]), interaction.user.id)
    if existing is not None:
        await interaction.response.send_message("이번 턴에는 이미 행동했습니다. 필요하면 `/롤백`을 사용해주세요.", ephemeral=True)
        return None
    return session


async def maybe_announce_turn_complete(guild: discord.Guild, channel: discord.abc.Messageable) -> None:
    session = get_active_session(guild.id)
    if session is None or bool(session["turn_locked"]):
        return
    missing = get_missing_participants(session)
    if missing:
        return
    lock_turn(int(session["id"]))
    view = TurnReadyView(guild.id)
    await channel.send("**모든 참가자가 행동했습니다. 턴이 종료되었습니다.**", view=view)


class TimingButton(discord.ui.Button):
    def __init__(self, game: "TimingGame") -> None:
        super().__init__(label="누르기", style=discord.ButtonStyle.primary)
        self.game = game

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.user_id:
            await interaction.response.send_message("이 판정은 행동을 선언한 플레이어만 누를 수 있습니다.", ephemeral=True)
            return
        await self.game.pressed(interaction)


class TimingView(discord.ui.View):
    def __init__(self, game: "TimingGame") -> None:
        super().__init__(timeout=15)
        self.game = game
        self.add_item(TimingButton(game))


class TimingGame:
    def __init__(
        self,
        interaction: discord.Interaction,
        action_id: int,
        kind: str,
        action_name: str,
        enemy_name: Optional[str] = None,
        weapon_name: Optional[str] = None,
        link_connected: bool = True,
    ) -> None:
        self.interaction = interaction
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.user_id = interaction.user.id
        self.action_id = action_id
        self.kind = kind
        self.action_name = action_name
        self.enemy_name = enemy_name
        self.weapon_name = weapon_name
        self.link_connected = link_connected
        self.state = "waiting"
        self.started_at: Optional[float] = None
        self.finished = False
        self.cancelled = False
        self.task: Optional[asyncio.Task] = None
        self.view = TimingView(self)

    def thresholds(self) -> tuple[float, float]:
        if self.kind == "defense":
            return DEFENSE_PERFECT, DEFENSE_GOOD
        return ATTACK_PERFECT, ATTACK_GOOD

    def base_embed(self, text: str) -> discord.Embed:
        title = self.action_name if self.kind == "custom" else ("방어" if self.kind == "defense" else "공격")
        description = f"캐릭터 · `{self.interaction.user.display_name}`\n"
        if self.enemy_name:
            description += f"대상 · `{self.enemy_name}`\n"
        description += f"\n{text}"
        embed = discord.Embed(title=title, description=description, color=discord.Colour.blurple())
        embed.set_footer(text="신호가 나오면 버튼을 누르세요. 너무 빠르거나 늦으면 MISS입니다.")
        return embed

    async def start(self) -> None:
        key = (int(self.interaction.guild_id), self.user_id)
        active_timing_games[key] = self
        await self.interaction.response.send_message(embed=self.base_embed("타이밍을 기다리는 중..."), view=self.view)
        self.task = asyncio.create_task(self.sequence())

    async def sequence(self) -> None:
        flavor = DEFEND_FLAVOR if self.kind == "defense" else ATTACK_FLAVOR
        fakeouts = DEFEND_FAKEOUT if self.kind == "defense" else ATTACK_FAKEOUT
        real_cues = DEFEND_REAL if self.kind == "defense" else ATTACK_REAL
        try:
            for _ in range(random.randint(1, 3)):
                await asyncio.sleep(random.uniform(0.65, 1.35))
                if self.finished or self.cancelled:
                    return
                is_fake = random.random() < 0.42
                self.state = "fake" if is_fake else "waiting"
                line = random.choice(fakeouts if is_fake else flavor)
                await self.interaction.edit_original_response(embed=self.base_embed(line), view=self.view)
            await asyncio.sleep(random.uniform(0.55, 1.15))
            if self.finished or self.cancelled:
                return
            self.state = "real"
            self.started_at = time.monotonic()
            await self.interaction.edit_original_response(embed=self.base_embed(random.choice(real_cues)), view=self.view)
            _, good = self.thresholds()
            await asyncio.sleep(good + 0.45)
            if not self.finished and not self.cancelled and self.state == "real":
                await self.finish("MISS", None, "늦었다.")
        except asyncio.CancelledError:
            return
        except discord.HTTPException:
            if not self.finished:
                await self.finish("MISS", None, "판정 메시지를 갱신하지 못했습니다.")

    async def pressed(self, interaction: discord.Interaction) -> None:
        if self.finished or self.cancelled:
            await interaction.response.defer()
            return
        if self.state != "real" or self.started_at is None:
            await interaction.response.defer()
            await self.finish("MISS", None, "너무 빨랐다.")
            return
        elapsed = time.monotonic() - self.started_at
        perfect, good = self.thresholds()
        if elapsed <= perfect:
            grade = "PERFECT"
        elif elapsed <= good:
            grade = "GOOD"
        else:
            grade = "MISS"
        await interaction.response.defer()
        await self.finish(grade, elapsed, None)

    async def finish(self, grade: str, elapsed: Optional[float], reason: Optional[str]) -> None:
        if self.finished or self.cancelled:
            return
        self.finished = True
        if self.task and self.task is not asyncio.current_task() and not self.task.done():
            self.task.cancel()
        finalize_action(self.action_id, grade, elapsed)
        key = (int(self.interaction.guild_id), self.user_id)
        active_timing_games.pop(key, None)
        for item in self.view.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        title = self.action_name if self.kind == "custom" else ("방어" if self.kind == "defense" else "공격")
        lines = [f"캐릭터 · `{self.interaction.user.display_name}`"]
        if self.enemy_name:
            lines.append(f"대상 · `{self.enemy_name}`")
        lines.append("")
        lines.append(f"결과 · **{grade}**")
        if elapsed is not None:
            lines.append(f"반응 · `{elapsed:.2f}초`")
        elif reason:
            lines.append(f"판정 · {reason}")
        embed = discord.Embed(title=title, description="\n".join(lines), color=grade_color(grade))
        if self.kind == "attack":
            footer = f"{self.weapon_name or FALLBACK_WEAPON_NAME}으로 공격했다!"
            if grade != "MISS":
                footer += " · 데미지는 /턴 종료에서 적용됩니다."
            else:
                footer += " · 공격이 빗나갔습니다."
            if not self.link_connected:
                footer += " · LINK 연결 실패로 기본 장비를 사용했습니다."
            embed.set_footer(text=footer)
        elif self.kind == "defense":
            embed.set_footer(text="이 턴의 방어 판정으로 기록되었습니다.")
        else:
            embed.set_footer(text="이 턴의 행동으로 기록되었습니다.")
        try:
            await self.interaction.edit_original_response(embed=embed, view=self.view)
        except discord.HTTPException:
            pass
        if self.guild is not None and self.channel is not None:
            await maybe_announce_turn_complete(self.guild, self.channel)

    async def cancel(self, reason: str = "이 행동은 `/롤백`으로 취소되었습니다.") -> None:
        if self.finished or self.cancelled:
            return
        self.cancelled = True
        if self.task and not self.task.done():
            self.task.cancel()
        key = (int(self.interaction.guild_id), self.user_id)
        active_timing_games.pop(key, None)
        for item in self.view.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        embed = discord.Embed(title=self.action_name, description=reason, color=discord.Colour.light_grey())
        try:
            await self.interaction.edit_original_response(embed=embed, view=self.view)
        except discord.HTTPException:
            pass


@bot.tree.command(name="공격", description="에너미를 대상으로 순발력 공격 판정을 합니다.")
@app_commands.guild_only()
@app_commands.rename(enemy="에너미")
@app_commands.describe(enemy="공격할 에너미")
@app_commands.autocomplete(enemy=enemy_autocomplete)
async def attack(interaction: discord.Interaction, enemy: str) -> None:
    session = await require_player_turn(interaction)
    if session is None or interaction.guild_id is None:
        return
    try:
        enemy_id = int(enemy)
    except ValueError:
        await interaction.response.send_message("현재 등장 중인 에너미를 목록에서 선택해주세요.", ephemeral=True)
        return
    target = get_enemy(interaction.guild_id, enemy_id)
    if target is None or int(target["hp"]) <= 0:
        await interaction.response.send_message("그 에너미는 현재 공격할 수 없습니다.", ephemeral=True)
        return
    profile, connected = await get_effective_profile(interaction.guild_id, interaction.user.id)
    weapon = profile.get("weapon") or {"name": FALLBACK_WEAPON_NAME, "power": FALLBACK_ATTACK_POWER}
    weapon_name = str(weapon.get("name", FALLBACK_WEAPON_NAME))
    attack_power = max(1, int(profile.get("attack_power", FALLBACK_ATTACK_POWER)))
    try:
        action_id = reserve_action(
            session,
            interaction.user.id,
            "attack",
            "공격",
            enemy_id=enemy_id,
            attack_power=attack_power,
            weapon_name=weapon_name,
            link_connected=connected,
        )
    except sqlite3.IntegrityError:
        await interaction.response.send_message("이번 턴에는 이미 행동했습니다.", ephemeral=True)
        return
    game = TimingGame(
        interaction,
        action_id,
        "attack",
        "공격",
        enemy_name=enemy_display_name(target),
        weapon_name=weapon_name,
        link_connected=connected,
    )
    await game.start()


@bot.tree.command(name="방어", description="순발력 방어 판정을 합니다.")
@app_commands.guild_only()
async def defense(interaction: discord.Interaction) -> None:
    session = await require_player_turn(interaction)
    if session is None:
        return
    try:
        action_id = reserve_action(session, interaction.user.id, "defense", "방어")
    except sqlite3.IntegrityError:
        await interaction.response.send_message("이번 턴에는 이미 행동했습니다.", ephemeral=True)
        return
    game = TimingGame(interaction, action_id, "defense", "방어")
    await game.start()


@bot.tree.command(name="행동", description="임의의 행동으로 순발력 판정을 합니다.")
@app_commands.guild_only()
@app_commands.rename(action_name="행동이름")
@app_commands.describe(action_name="시도할 행동의 이름 또는 설명")
async def custom_action(interaction: discord.Interaction, action_name: str) -> None:
    session = await require_player_turn(interaction)
    if session is None:
        return
    clean_name = action_name.strip()
    if not clean_name:
        await interaction.response.send_message("행동 이름을 입력해주세요.", ephemeral=True)
        return
    clean_name = clean_name[:100]
    try:
        action_id = reserve_action(session, interaction.user.id, "custom", clean_name)
    except sqlite3.IntegrityError:
        await interaction.response.send_message("이번 턴에는 이미 행동했습니다.", ephemeral=True)
        return
    game = TimingGame(interaction, action_id, "custom", clean_name)
    await game.start()


@bot.tree.command(name="폭탄", description="LINK의 폭탄을 사용해 에너미에게 즉시 피해를 줍니다.")
@app_commands.guild_only()
@app_commands.rename(enemy="에너미")
@app_commands.autocomplete(enemy=enemy_autocomplete)
async def bomb_attack(interaction: discord.Interaction, enemy: str) -> None:
    session = await require_player_turn(interaction)
    if session is None or interaction.guild_id is None:
        return
    try:
        enemy_id = int(enemy)
    except ValueError:
        await interaction.response.send_message("현재 등장 중인 에너미를 목록에서 선택해주세요.", ephemeral=True)
        return
    target = get_enemy(interaction.guild_id, enemy_id)
    if target is None or int(target["hp"]) <= 0:
        await interaction.response.send_message("그 에너미는 현재 공격할 수 없습니다.", ephemeral=True)
        return
    profile, connected = await get_effective_profile(interaction.guild_id, interaction.user.id)
    bombs = int(profile["bombs"])
    if bombs <= 0:
        await interaction.response.send_message("폭탄이 없습니다.", ephemeral=True)
        return
    try:
        action_id = reserve_action(
            session,
            interaction.user.id,
            "bomb",
            "폭탄",
            enemy_id=enemy_id,
            link_connected=connected,
        )
    except sqlite3.IntegrityError:
        await interaction.response.send_message("이번 턴에는 이미 행동했습니다.", ephemeral=True)
        return
    damage = random.randint(BOMB_DAMAGE_MIN, BOMB_DAMAGE_MAX)
    old_hp = int(target["hp"])
    max_hp = int(target["max_hp"])
    new_hp = max(0, old_hp - damage)
    next_bombs = bombs - 1
    with connect_db() as conn:
        conn.execute(
            "UPDATE active_enemies SET hp = ? WHERE guild_id = ? AND id = ?",
            (new_hp, interaction.guild_id, enemy_id),
        )
        conn.commit()
    update_player_fields(interaction.guild_id, interaction.user.id, bombs=next_bombs)
    _, bombs_sync = get_sync_settings(interaction.guild_id)
    synced = True
    if bombs_sync:
        synced = await write_link_resources(interaction.guild_id, interaction.user.id, bombs=next_bombs)
    finalize_bomb_action(action_id, damage)
    embed = discord.Embed(
        title="폭탄",
        description=(
            f"캐릭터 · `{interaction.user.display_name}`\n"
            f"대상 · `{enemy_display_name(target)}`\n\n"
            f"피해 · **{damage}**\n"
            f"에너미 체력 · {hp_bar(new_hp, max_hp)} **{old_hp} → {new_hp}/{max_hp}**\n"
            f"남은 폭탄 · **{next_bombs}**"
            + ("\n상태 · **DOWN**" if new_hp <= 0 else "")
        ),
        color=discord.Colour.orange(),
    )
    footer = "폭탄은 즉시 적용되며 이번 턴의 행동으로 처리됩니다."
    if bombs_sync and (not connected or not synced):
        footer += " · LINK 폭탄 동기화 실패"
    embed.set_footer(text=footer)
    await interaction.response.send_message(embed=embed)
    if interaction.guild is not None and interaction.channel is not None:
        await maybe_announce_turn_complete(interaction.guild, interaction.channel)


@bot.tree.command(name="롤백", description="현재 턴에 기록된 행동을 취소합니다.")
@app_commands.guild_only()
@app_commands.rename(member="유저")
@app_commands.describe(member="관리자는 다른 참가자를 선택할 수 있습니다. 비우면 본인입니다.")
async def rollback(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
    guild = interaction.guild
    if guild is None:
        return
    session = get_active_session(guild.id)
    if session is None:
        await interaction.response.send_message("진행 중인 세션이 없습니다.", ephemeral=True)
        return
    if bool(session["turn_locked"]):
        await interaction.response.send_message("이미 종료된 턴은 롤백할 수 없습니다.", ephemeral=True)
        return
    is_admin = bool(getattr(interaction.user.guild_permissions, "administrator", False))
    target = member or interaction.user
    if target.id != interaction.user.id and not is_admin:
        await interaction.response.send_message("다른 사람의 행동은 관리자만 롤백할 수 있습니다.", ephemeral=True)
        return
    action = get_turn_action(int(session["id"]), int(session["turn_number"]), target.id)
    if action is None:
        await interaction.response.send_message(f"{target.mention}님의 이번 턴 행동이 없습니다.", ephemeral=True)
        return
    game = active_timing_games.get((guild.id, target.id))
    if game is not None:
        await game.cancel()
    rollback_note = ""
    if action["kind"] == "bomb" and int(action["finalized"]) and int(action["bomb_damage"]) > 0:
        enemy_id = int(action["enemy_id"])
        enemy = get_enemy(guild.id, enemy_id)
        if enemy is not None:
            restored_hp = min(int(enemy["max_hp"]), int(enemy["hp"]) + int(action["bomb_damage"]))
            with connect_db() as conn:
                conn.execute(
                    "UPDATE active_enemies SET hp = ? WHERE guild_id = ? AND id = ?",
                    (restored_hp, guild.id, enemy_id),
                )
                conn.commit()
        profile, connected = await get_effective_profile(guild.id, target.id)
        restored_bombs = int(profile["bombs"]) + 1
        update_player_fields(guild.id, target.id, bombs=restored_bombs)
        _, bombs_sync = get_sync_settings(guild.id)
        synced = True
        if bombs_sync:
            synced = await write_link_resources(guild.id, target.id, bombs=restored_bombs)
        rollback_note = f" 폭탄 1개와 피해 {int(action['bomb_damage'])}도 되돌렸습니다."
        if bombs_sync and (not connected or not synced):
            rollback_note += " LINK 폭탄 동기화는 실패했습니다."
    delete_action(int(action["id"]))
    await interaction.response.send_message(
        f"**{target.display_name}**님의 행동을 취소했습니다.{rollback_note}\n이번 턴에 다시 행동할 수 있습니다.",
        ephemeral=True,
    )


class SessionSetupView(discord.ui.View):
    def __init__(self, guild: discord.Guild, admin_id: int, explorers: list[discord.Member]) -> None:
        super().__init__(timeout=600)
        self.guild = guild
        self.admin_id = admin_id
        self.explorers = explorers[:25]
        self.explorer_ids = {member.id for member in self.explorers}
        self.checked_ids: set[int] = set()
        self.message: Optional[discord.Message] = None
        self.refresh_buttons()

    def is_ready(self) -> bool:
        return bool(self.explorer_ids) and self.explorer_ids.issubset(self.checked_ids)

    def participant_ids(self) -> list[int]:
        selected = set(self.explorer_ids)
        if self.admin_id in self.checked_ids:
            selected.add(self.admin_id)
        return sorted(selected)

    def build_embed(self) -> discord.Embed:
        lines: list[str] = []
        for member in self.explorers:
            checked = member.id in self.checked_ids
            mark = "☑" if checked else "☐"
            suffix = " · 관리자" if member.id == self.admin_id else ""
            lines.append(f"{mark} <@{member.id}>{suffix}")
        if self.admin_id not in self.explorer_ids:
            admin_checked = self.admin_id in self.checked_ids
            mark = "☑" if admin_checked else "☐"
            lines.append(f"{mark} <@{self.admin_id}> · 관리자 선택 참가")
        ready_mark = "☑" if self.is_ready() else "☐"
        embed = discord.Embed(
            title="LINKDICE SESSION · 참가 확인",
            description="\n".join(lines),
            color=discord.Colour.green() if self.is_ready() else discord.Colour.blurple(),
        )
        if self.is_ready():
            embed.add_field(name="출발", value=f"{ready_mark} 탐사자 전원의 참가 확인이 완료되었습니다.", inline=False)
            embed.set_footer(text="관리자가 출발 버튼을 누르면 1턴이 시작됩니다.")
        else:
            embed.add_field(name="출발", value=f"{ready_mark} 아직 참가 확인을 기다리는 중입니다.", inline=False)
            embed.set_footer(text="각 탐사자는 참가 체크 버튼을 눌러주세요. 관리자는 선택적으로 참가할 수 있습니다.")
        return embed

    def refresh_buttons(self) -> None:
        ready = self.is_ready()
        for item in self.children:
            if isinstance(item, discord.ui.Button) and item.custom_id == "linkdice_session_depart":
                item.label = "출발 ☑" if ready else "출발 ☐"
                item.style = discord.ButtonStyle.success if ready else discord.ButtonStyle.secondary
                item.disabled = not ready

    async def refresh(self, interaction: discord.Interaction) -> None:
        self.refresh_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="참가 체크", style=discord.ButtonStyle.primary, custom_id="linkdice_session_check")
    async def check_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        user_id = interaction.user.id
        allowed = user_id in self.explorer_ids or user_id == self.admin_id
        if not allowed:
            await interaction.response.send_message("이번 세션의 참가 확인 대상이 아닙니다.", ephemeral=True)
            return
        if user_id in self.checked_ids:
            self.checked_ids.remove(user_id)
        else:
            self.checked_ids.add(user_id)
        await self.refresh(interaction)

    @discord.ui.button(label="출발 ☐", style=discord.ButtonStyle.secondary, disabled=True, custom_id="linkdice_session_depart")
    async def depart_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("세션을 연 관리자만 출발할 수 있습니다.", ephemeral=True)
            return
        if not self.is_ready():
            await interaction.response.send_message("아직 참가 확인을 완료하지 않은 탐사자가 있습니다.", ephemeral=True)
            return
        try:
            session = create_session(self.guild.id, self.participant_ids())
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.stop()
        active_session_lobbies.pop(self.guild.id, None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        await interaction.response.edit_message(content="**출발했습니다.**", embed=self.build_embed(), view=self)
        mentions = [f"<@{user_id}>" for user_id in get_participant_ids(int(session["id"]))]
        embed = discord.Embed(
            title="LINKDICE SESSION · 1턴",
            description="\n".join(f"{mention} · **대기**" for mention in mentions),
            color=discord.Colour.blurple(),
        )
        embed.set_footer(text="1턴이 시작되었습니다. 각 참가자는 한 턴에 한 번만 행동할 수 있습니다.")
        if interaction.channel is not None:
            await interaction.channel.send(embed=embed)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.danger, custom_id="linkdice_session_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("세션을 연 관리자만 취소할 수 있습니다.", ephemeral=True)
            return
        self.stop()
        active_session_lobbies.pop(self.guild.id, None)
        await interaction.response.edit_message(content="세션 준비를 취소했습니다.", embed=None, view=None)

    async def on_timeout(self) -> None:
        active_session_lobbies.pop(self.guild.id, None)
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(content="세션 참가 확인 시간이 만료되었습니다.", view=self)
            except discord.HTTPException:
                pass


class SessionEndConfirmView(discord.ui.View):
    def __init__(self, guild_id: int, admin_id: int) -> None:
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.admin_id = admin_id

    @discord.ui.button(label="세션 종료", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("이 확인은 명령어를 실행한 관리자만 할 수 있습니다.", ephemeral=True)
            return
        ended = end_session(self.guild_id)
        for key, game in list(active_timing_games.items()):
            if key[0] == self.guild_id:
                await game.cancel("세션 종료로 이 행동이 취소되었습니다.")
        self.stop()
        if ended:
            await interaction.response.edit_message(content="LINKDICE 세션을 종료했습니다.", view=None)
        else:
            await interaction.response.edit_message(content="진행 중인 세션이 없습니다.", view=None)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("이 확인은 명령어를 실행한 관리자만 할 수 있습니다.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="세션 종료를 취소했습니다.", view=None)


class ActiveSessionView(discord.ui.View):
    def __init__(self, guild_id: int, admin_id: int) -> None:
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.admin_id = admin_id

    @discord.ui.button(label="세션 종료", style=discord.ButtonStyle.danger)
    async def end_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not bool(getattr(interaction.user.guild_permissions, "administrator", False)):
            await interaction.response.send_message("이 버튼은 관리자만 사용할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.send_message(
            "현재 LINKDICE 세션을 종료하시겠습니까?",
            view=SessionEndConfirmView(self.guild_id, interaction.user.id),
            ephemeral=True,
        )


@bot.tree.command(name="세션", description="TRPG 세션 참가 확인을 열거나 현재 세션 상태를 확인합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def session_command(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    active = get_active_session(guild.id)
    if active is not None:
        participant_ids = get_participant_ids(int(active["id"]))
        actions = {int(row["user_id"]): row for row in get_turn_actions(int(active["id"]), int(active["turn_number"]))}
        lines = []
        for user_id in participant_ids:
            action = actions.get(user_id)
            if action is None:
                state = "대기"
            elif not int(action["finalized"]):
                state = "진행 중"
            else:
                grade = str(action["grade"] or "완료")
                state = f"{action['action_name']} · {grade}"
            lines.append(f"<@{user_id}> · **{state}**")
        embed = discord.Embed(
            title=f"LINKDICE SESSION · {int(active['turn_number'])}턴",
            description="\n".join(lines),
            color=discord.Colour.blurple(),
        )
        if bool(active["turn_locked"]):
            embed.set_footer(text="현재 턴은 종료되어 정산 대기 중입니다.")
        else:
            embed.set_footer(text="각 참가자는 한 턴에 한 번만 행동할 수 있습니다.")
        await interaction.response.send_message(
            embed=embed,
            view=ActiveSessionView(guild.id, interaction.user.id),
            ephemeral=True,
        )
        return
    lobby = active_session_lobbies.get(guild.id)
    if lobby is not None:
        await interaction.response.send_message("이미 참가 확인이 진행 중입니다. 기존 `/세션` 메시지를 사용해주세요.", ephemeral=True)
        return
    role = discord.utils.get(guild.roles, name=PLAYER_ROLE_NAME)
    if role is None:
        await interaction.response.send_message(f"서버에 `@{PLAYER_ROLE_NAME}` 역할이 없습니다.", ephemeral=True)
        return
    members = [member for member in role.members if not member.bot]
    if not members:
        await interaction.response.send_message(f"`@{PLAYER_ROLE_NAME}` 역할을 가진 참가자가 없습니다.", ephemeral=True)
        return
    if len(members) > 25:
        await interaction.response.send_message("현재 참가 확인 UI는 한 세션에 최대 25명의 탐사자를 지원합니다.", ephemeral=True)
        return
    view = SessionSetupView(guild, interaction.user.id, members)
    active_session_lobbies[guild.id] = view
    await interaction.response.send_message(embed=view.build_embed(), view=view)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        pass


@bot.tree.command(name="세션종료", description="현재 LINKDICE 세션 또는 참가 확인을 종료합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def session_end_command(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    lobby = active_session_lobbies.get(guild.id)
    active = get_active_session(guild.id)
    if active is None and lobby is None:
        await interaction.response.send_message("진행 중인 세션이나 참가 확인이 없습니다.", ephemeral=True)
        return
    if active is None and lobby is not None:
        lobby.stop()
        active_session_lobbies.pop(guild.id, None)
        for item in lobby.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if lobby.message is not None:
            try:
                await lobby.message.edit(content="관리자가 세션 준비를 종료했습니다.", embed=None, view=None)
            except discord.HTTPException:
                pass
        await interaction.response.send_message("세션 참가 확인을 종료했습니다.", ephemeral=True)
        return
    await interaction.response.send_message(
        "현재 LINKDICE 세션을 종료하시겠습니까? 진행 중인 순발력 판정도 취소됩니다.",
        view=SessionEndConfirmView(guild.id, interaction.user.id),
        ephemeral=True,
    )


@bot.tree.command(name="동기화", description="LINK의 체력과 폭탄을 LINKDICE로 다시 불러옵니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(member="유저")
async def sync_from_link(interaction: discord.Interaction, member: discord.Member) -> None:
    if interaction.guild_id is None:
        return
    remote = await link_api_request("GET", interaction.guild_id, member.id)
    if remote is None:
        await interaction.response.send_message("LINK에 연결하지 못했습니다.", ephemeral=True)
        return
    update_player_fields(
        interaction.guild_id,
        member.id,
        hp=int(remote["hp"]),
        max_hp=int(remote["max_hp"]),
        bombs=int(remote["bombs"]),
    )
    await interaction.response.send_message(
        f"**{member.display_name}** 동기화 완료 · HP {remote['hp']}/{remote['max_hp']} · 폭탄 {remote['bombs']}",
        ephemeral=True,
    )


@bot.tree.command(name="디버그", description="LINK 연동과 전투 자원을 조정합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(member="유저", hp_sync="체력연동", bombs_sync="폭탄연동", hp_value="체력", bombs_value="폭탄")
async def debug_sync(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    hp_sync: Optional[bool] = None,
    bombs_sync: Optional[bool] = None,
    hp_value: Optional[int] = None,
    bombs_value: Optional[int] = None,
) -> None:
    if interaction.guild_id is None:
        return
    target = member or interaction.user
    next_hp_sync, next_bombs_sync = set_sync_settings(interaction.guild_id, hp_sync, bombs_sync)
    profile, connected = await get_effective_profile(interaction.guild_id, target.id)
    if hp_value is not None:
        max_hp = max(int(profile["max_hp"]), 1)
        hp_value = max(0, min(hp_value, max_hp))
        update_player_fields(interaction.guild_id, target.id, hp=hp_value, max_hp=max_hp)
        profile["hp"] = hp_value
        if next_hp_sync:
            await write_link_resources(interaction.guild_id, target.id, hp=hp_value)
    if bombs_value is not None:
        bombs_value = max(0, bombs_value)
        update_player_fields(interaction.guild_id, target.id, bombs=bombs_value)
        profile["bombs"] = bombs_value
        if next_bombs_sync:
            await write_link_resources(interaction.guild_id, target.id, bombs=bombs_value)
    status = "연결됨" if connected else "연결 실패"
    await interaction.response.send_message(
        f"**{target.display_name} · 디버그**\n"
        f"LINK · **{status}**\n"
        f"체력 · **{profile['hp']}/{profile['max_hp']}** · 양방향 {'ON' if next_hp_sync else 'OFF'}\n"
        f"폭탄 · **{profile['bombs']}** · 양방향 {'ON' if next_bombs_sync else 'OFF'}",
        ephemeral=True,
    )


@bot.tree.command(name="등장", description="새 에너미를 현재 장면에 등장시킵니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(name="이름", hp="체력")
async def enemy_spawn(interaction: discord.Interaction, name: str, hp: int) -> None:
    if interaction.guild_id is None:
        return
    clean_name = name.strip()
    if not clean_name or hp < 1:
        await interaction.response.send_message("이름과 1 이상의 체력을 입력해주세요.", ephemeral=True)
        return
    enemy_id = spawn_enemy(interaction.guild_id, clean_name, hp)
    embed = discord.Embed(
        title=f"{clean_name} #{enemy_id}",
        description=f"체력 · {hp_bar(hp, hp)} **{hp}/{hp}**",
        color=discord.Colour.light_grey(),
    )
    embed.set_footer(text="현재 장면에 등장했습니다.")
    await interaction.response.send_message(embed=embed)


enemy_group = app_commands.Group(name="에너미", description="에너미를 관리합니다.")


@enemy_group.command(name="목록", description="현재 장면의 에너미를 확인합니다.")
@app_commands.guild_only()
async def enemy_list(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        return
    enemies = get_active_enemies(interaction.guild_id, include_down=True)
    if not enemies:
        await interaction.response.send_message("현재 등장한 에너미가 없습니다.")
        return
    embed = discord.Embed(title="현재 장면", color=discord.Colour.light_grey())
    for enemy in enemies:
        if int(enemy["hp"]) <= 0:
            value = "`DOWN`"
        else:
            value = f"{hp_bar(int(enemy['hp']), int(enemy['max_hp']))} **{enemy['hp']}/{enemy['max_hp']}**"
        embed.add_field(name=enemy_display_name(enemy), value=value, inline=False)
    await interaction.response.send_message(embed=embed)


@enemy_group.command(name="퇴장", description="에너미를 현재 장면에서 제거합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(enemy="에너미")
@app_commands.autocomplete(enemy=any_active_enemy_autocomplete)
async def enemy_remove(interaction: discord.Interaction, enemy: str) -> None:
    if interaction.guild_id is None:
        return
    try:
        enemy_id = int(enemy)
    except ValueError:
        await interaction.response.send_message("목록에서 에너미를 선택해주세요.", ephemeral=True)
        return
    target = get_enemy(interaction.guild_id, enemy_id)
    if target is None:
        await interaction.response.send_message("해당 에너미를 찾을 수 없습니다.", ephemeral=True)
        return
    deactivate_enemy(interaction.guild_id, enemy_id)
    await interaction.response.send_message(f"**{enemy_display_name(target)}** 퇴장")


player_group = app_commands.Group(name="플레이어", description="플레이어 대상 GM 명령입니다.")


@player_group.command(name="공격", description="에너미의 공격으로 플레이어에게 즉시 데미지를 줍니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(player="플레이어")
@app_commands.describe(player=f"@{PLAYER_ROLE_NAME} 역할의 플레이어")
@app_commands.autocomplete(player=player_role_autocomplete)
async def player_attack(interaction: discord.Interaction, player: str) -> None:
    guild = interaction.guild
    if guild is None:
        return
    try:
        user_id = int(player)
    except ValueError:
        await interaction.response.send_message(f"@{PLAYER_ROLE_NAME} 역할의 플레이어를 목록에서 선택해주세요.", ephemeral=True)
        return
    member = guild.get_member(user_id)
    if member is None:
        await interaction.response.send_message("해당 플레이어를 서버에서 찾을 수 없습니다.", ephemeral=True)
        return
    role = discord.utils.get(guild.roles, name=PLAYER_ROLE_NAME)
    if role is None or role not in member.roles:
        await interaction.response.send_message(f"{member.mention}님은 `@{PLAYER_ROLE_NAME}` 역할을 가지고 있지 않습니다.", ephemeral=True)
        return
    profile, connected = await get_effective_profile(guild.id, member.id)
    old_hp = int(profile["hp"])
    max_hp = int(profile["max_hp"])
    if old_hp <= 0:
        await interaction.response.send_message(f"{member.mention}님은 이미 **DOWN** 상태입니다.", ephemeral=True)
        return
    try:
        raw_damage, rolls, modifier = parse_and_roll_dice(DEFAULT_ENEMY_DAMAGE)
    except ValueError as exc:
        await interaction.response.send_message(f"ENEMY_DAMAGE_DICE 설정 오류: {exc}", ephemeral=True)
        return
    defense_power = max(0, int(profile.get("defense_power", FALLBACK_DEFENSE_POWER)))
    grade = "MISS"
    session = get_active_session(guild.id)
    if session is not None:
        action = get_turn_action(int(session["id"]), int(session["turn_number"]), member.id)
        if action is not None and int(action["finalized"]) and action["kind"] == "defense":
            grade = str(action["grade"] or "MISS")
    damage = defense_damage(raw_damage, defense_power, grade)
    new_hp = max(0, old_hp - damage)
    update_player_fields(guild.id, member.id, hp=new_hp, max_hp=max_hp)
    hp_sync, _ = get_sync_settings(guild.id)
    synced = True
    if hp_sync:
        synced = await write_link_resources(guild.id, member.id, hp=new_hp)
    embed = discord.Embed(
        title="플레이어 공격",
        description=(
            f"대상 · `{member.display_name}`\n"
            f"에너미 공격 · {format_dice_roll(DEFAULT_ENEMY_DAMAGE, rolls, modifier, raw_damage)}\n"
            f"방어 판정 · **{grade}**\n"
            f"LINK 방어력 · **{defense_power}**\n\n"
            f"피해 · **{damage}**\n"
            f"체력 · {hp_bar(new_hp, max_hp)} **{old_hp} → {new_hp}/{max_hp}**"
            + ("\n상태 · **DOWN**" if new_hp <= 0 else "")
        ),
        color=discord.Colour.red() if damage > 0 else discord.Colour.green(),
    )
    footer = "방어 행동이 없으면 MISS 방어로 처리됩니다."
    if hp_sync and (not connected or not synced):
        footer += " · LINK 체력 동기화 실패"
    embed.set_footer(text=footer)
    await interaction.response.send_message(embed=embed)


class TurnReadyView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.guild_id = guild_id

    @discord.ui.button(label="턴 정산", style=discord.ButtonStyle.success)
    async def resolve_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not getattr(interaction.user.guild_permissions, "administrator", False):
            await interaction.response.send_message("관리자만 턴을 정산할 수 있습니다.", ephemeral=True)
            return
        await begin_turn_resolution(interaction)


class MissingTurnConfirmView(discord.ui.View):
    def __init__(self, admin_id: int) -> None:
        super().__init__(timeout=180)
        self.admin_id = admin_id

    @discord.ui.button(label="진행", style=discord.ButtonStyle.danger)
    async def proceed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("`/턴 종료`를 실행한 관리자만 선택할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.edit_message(content="미행동자를 제외하고 턴을 정산합니다.", view=None)
        await begin_turn_resolution(interaction, response_already_used=True)

    @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("`/턴 종료`를 실행한 관리자만 선택할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.edit_message(content="턴 종료를 취소했습니다.", view=None)


class CustomResolutionFlow:
    def __init__(self, guild: discord.Guild, admin_id: int, session_id: int, turn_number: int, actions: list[sqlite3.Row]) -> None:
        self.guild = guild
        self.admin_id = admin_id
        self.session_id = session_id
        self.turn_number = turn_number
        self.actions = actions
        self.index = 0

    @property
    def current(self) -> sqlite3.Row:
        return self.actions[self.index]

    def prompt(self) -> str:
        action = self.current
        return (
            f"**행동 공격 여부 {self.index + 1}/{len(self.actions)}**\n"
            f"{member_label(self.guild, int(action['user_id']))} · `{action['action_name']}` · **{action['grade']}**\n"
            "이 행동을 공격으로 처리합니까?"
        )

    async def send_current(self, interaction: discord.Interaction, edit: bool = False) -> None:
        view = CustomAttackDecisionView(self, int(self.current["id"]))
        if edit:
            await interaction.response.edit_message(content=self.prompt(), view=view)
        elif interaction.response.is_done():
            await interaction.followup.send(self.prompt(), view=view, ephemeral=True)
        else:
            await interaction.response.send_message(self.prompt(), view=view, ephemeral=True)

    async def next(self, interaction: discord.Interaction, edit: bool = False) -> None:
        self.index += 1
        if self.index >= len(self.actions):
            if edit:
                await interaction.response.edit_message(content="행동 판정 입력 완료. 데미지를 정산합니다.", view=None)
            elif interaction.response.is_done():
                await interaction.followup.send("행동 판정 입력 완료. 데미지를 정산합니다.", ephemeral=True)
            else:
                await interaction.response.send_message("행동 판정 입력 완료. 데미지를 정산합니다.", ephemeral=True)
            if interaction.channel is not None:
                await resolve_and_advance_turn(self.guild, interaction.channel, self.session_id, self.turn_number)
            return
        await self.send_current(interaction, edit=edit)


class CustomAttackDecisionView(discord.ui.View):
    def __init__(self, flow: CustomResolutionFlow, action_id: int) -> None:
        super().__init__(timeout=300)
        self.flow = flow
        self.action_id = action_id

    def valid(self) -> bool:
        return self.flow.index < len(self.flow.actions) and int(self.flow.current["id"]) == self.action_id

    @discord.ui.button(label="공격으로 처리", style=discord.ButtonStyle.danger)
    async def as_attack(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.flow.admin_id:
            await interaction.response.send_message("턴을 정산하는 관리자만 선택할 수 있습니다.", ephemeral=True)
            return
        if not self.valid():
            await interaction.response.send_message("이미 처리된 행동입니다.", ephemeral=True)
            return
        enemies = get_active_enemies(self.flow.guild.id, include_down=False)
        if not enemies:
            await interaction.response.send_message("공격할 수 있는 에너미가 없습니다.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=(
                f"**{member_label(self.flow.guild, int(self.flow.current['user_id']))} · {self.flow.current['action_name']}**\n"
                "공격 대상을 선택한 뒤 공격 수치를 입력하세요."
            ),
            view=CustomAttackTargetView(self.flow, self.action_id, enemies),
        )

    @discord.ui.button(label="공격 아님", style=discord.ButtonStyle.secondary)
    async def not_attack(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.flow.admin_id:
            await interaction.response.send_message("턴을 정산하는 관리자만 선택할 수 있습니다.", ephemeral=True)
            return
        if not self.valid():
            await interaction.response.send_message("이미 처리된 행동입니다.", ephemeral=True)
            return
        set_custom_action_resolution(self.action_id, False)
        await self.flow.next(interaction, edit=True)


class CustomEnemySelect(discord.ui.Select):
    def __init__(self, parent_view: "CustomAttackTargetView", enemies: list[sqlite3.Row]) -> None:
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=enemy_display_name(enemy)[:100],
                value=str(enemy["id"]),
                description=f"HP {enemy['hp']}/{enemy['max_hp']}"[:100],
            )
            for enemy in enemies[:25]
        ]
        super().__init__(placeholder="공격 대상", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.flow.admin_id:
            await interaction.response.send_message("턴을 정산하는 관리자만 선택할 수 있습니다.", ephemeral=True)
            return
        self.parent_view.enemy_id = int(self.values[0])
        await interaction.response.defer()


class CustomAttackTargetView(discord.ui.View):
    def __init__(self, flow: CustomResolutionFlow, action_id: int, enemies: list[sqlite3.Row]) -> None:
        super().__init__(timeout=300)
        self.flow = flow
        self.action_id = action_id
        self.enemy_id: Optional[int] = None
        self.add_item(CustomEnemySelect(self, enemies))

    @discord.ui.button(label="공격 수치 입력", style=discord.ButtonStyle.success)
    async def power_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.flow.admin_id:
            await interaction.response.send_message("턴을 정산하는 관리자만 선택할 수 있습니다.", ephemeral=True)
            return
        if self.enemy_id is None:
            await interaction.response.send_message("먼저 에너미를 선택해주세요.", ephemeral=True)
            return
        await interaction.response.send_modal(CustomAttackPowerModal(self.flow, self.action_id, self.enemy_id))

    @discord.ui.button(label="뒤로", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.flow.admin_id:
            await interaction.response.send_message("턴을 정산하는 관리자만 선택할 수 있습니다.", ephemeral=True)
            return
        await interaction.response.edit_message(content=self.flow.prompt(), view=CustomAttackDecisionView(self.flow, self.action_id))


class CustomAttackPowerModal(discord.ui.Modal):
    def __init__(self, flow: CustomResolutionFlow, action_id: int, enemy_id: int) -> None:
        super().__init__(title="공격 수치 입력")
        self.flow = flow
        self.action_id = action_id
        self.enemy_id = enemy_id
        self.power = discord.ui.TextInput(label="공격 수치", placeholder="예: 8", required=True, max_length=4)
        self.add_item(self.power)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.flow.admin_id:
            await interaction.response.send_message("턴을 정산하는 관리자만 입력할 수 있습니다.", ephemeral=True)
            return
        try:
            value = int(str(self.power.value).strip())
        except ValueError:
            await interaction.response.send_message("공격 수치는 정수로 입력해주세요.", ephemeral=True)
            return
        if value < 1 or value > 9999:
            await interaction.response.send_message("공격 수치는 1~9999 사이로 입력해주세요.", ephemeral=True)
            return
        set_custom_action_resolution(self.action_id, True, self.enemy_id, value)
        await interaction.response.send_message("공격 수치를 기록했습니다.", ephemeral=True)
        await self.flow.next(interaction, edit=False)


async def begin_turn_resolution(interaction: discord.Interaction, response_already_used: bool = False) -> None:
    guild = interaction.guild
    if guild is None:
        return
    session = get_active_session(guild.id)
    if session is None:
        if not interaction.response.is_done():
            await interaction.response.send_message("진행 중인 세션이 없습니다.", ephemeral=True)
        else:
            await interaction.followup.send("진행 중인 세션이 없습니다.", ephemeral=True)
        return
    if bool(session["resolving"]):
        if not interaction.response.is_done():
            await interaction.response.send_message("이미 턴 정산이 진행 중입니다.", ephemeral=True)
        else:
            await interaction.followup.send("이미 턴 정산이 진행 중입니다.", ephemeral=True)
        return
    set_resolving(int(session["id"]), True)
    actions = get_turn_actions(int(session["id"]), int(session["turn_number"]))
    optional = [
        action
        for action in actions
        if action["kind"] == "custom"
        and int(action["finalized"])
        and str(action["grade"]) in ("PERFECT", "GOOD")
        and action["custom_attack"] is None
    ]
    if optional:
        flow = CustomResolutionFlow(guild, interaction.user.id, int(session["id"]), int(session["turn_number"]), optional)
        await flow.send_current(interaction, edit=False)
        return
    if not interaction.response.is_done():
        await interaction.response.send_message("턴을 정산합니다.", ephemeral=True)
    elif not response_already_used:
        await interaction.followup.send("턴을 정산합니다.", ephemeral=True)
    if interaction.channel is not None:
        await resolve_and_advance_turn(guild, interaction.channel, int(session["id"]), int(session["turn_number"]))


async def resolve_and_advance_turn(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
    session_id: int,
    turn_number: int,
) -> None:
    actions = get_turn_actions(session_id, turn_number)
    damage_entries: list[dict[str, object]] = []
    enemy_totals: dict[int, int] = {}
    skipped: list[str] = []

    for action in actions:
        if not int(action["finalized"]):
            continue
        kind = str(action["kind"])
        grade = str(action["grade"] or "MISS")
        if kind == "attack":
            if grade == "MISS":
                continue
            if action["enemy_id"] is None or action["attack_power"] is None:
                skipped.append(f"{member_label(guild, int(action['user_id']))} · 공격 대상/공격력 없음")
                continue
            enemy_id = int(action["enemy_id"])
            enemy = get_enemy(guild.id, enemy_id)
            if enemy is None:
                skipped.append(f"{member_label(guild, int(action['user_id']))} · 대상 에너미 없음")
                continue
            power = int(action["attack_power"])
            damage = grade_damage(power, grade)
            enemy_totals[enemy_id] = enemy_totals.get(enemy_id, 0) + damage
            damage_entries.append(
                {
                    "user_id": int(action["user_id"]),
                    "label": str(action["weapon_name"] or FALLBACK_WEAPON_NAME),
                    "grade": grade,
                    "power": power,
                    "damage": damage,
                    "enemy_id": enemy_id,
                    "enemy_name": enemy_display_name(enemy),
                }
            )
        elif kind == "custom" and int(action["custom_attack"] or 0):
            if grade == "MISS" or action["custom_enemy_id"] is None or action["custom_attack_power"] is None:
                continue
            enemy_id = int(action["custom_enemy_id"])
            enemy = get_enemy(guild.id, enemy_id)
            if enemy is None:
                skipped.append(f"{member_label(guild, int(action['user_id']))} · `{action['action_name']}` 대상 에너미 없음")
                continue
            power = int(action["custom_attack_power"])
            damage = grade_damage(power, grade)
            enemy_totals[enemy_id] = enemy_totals.get(enemy_id, 0) + damage
            damage_entries.append(
                {
                    "user_id": int(action["user_id"]),
                    "label": str(action["action_name"]),
                    "grade": grade,
                    "power": power,
                    "damage": damage,
                    "enemy_id": enemy_id,
                    "enemy_name": enemy_display_name(enemy),
                }
            )

    enemy_summaries: list[str] = []
    with connect_db() as conn:
        for enemy_id, total_damage in enemy_totals.items():
            enemy = conn.execute(
                "SELECT * FROM active_enemies WHERE guild_id = ? AND id = ? AND active = 1",
                (guild.id, enemy_id),
            ).fetchone()
            if enemy is None:
                continue
            old_hp = int(enemy["hp"])
            max_hp = int(enemy["max_hp"])
            new_hp = max(0, old_hp - total_damage)
            conn.execute(
                "UPDATE active_enemies SET hp = ? WHERE guild_id = ? AND id = ?",
                (new_hp, guild.id, enemy_id),
            )
            enemy_summaries.append(
                f"**{enemy_display_name(enemy)}**\n"
                f"{hp_bar(new_hp, max_hp)} **{old_hp} → {new_hp}/{max_hp}** · 총 **-{total_damage}**"
                + (" · **DOWN**" if new_hp <= 0 else "")
            )
        conn.commit()

    mark_turn_actions_resolved(session_id, turn_number)
    next_turn = advance_turn(session_id)

    embed = discord.Embed(title=f"{turn_number}턴 결과", color=discord.Colour.blurple())
    if damage_entries:
        for entry in damage_entries:
            multiplier = "× 1.4" if entry["grade"] == "PERFECT" else "× 1.0"
            embed.add_field(
                name=f"{member_label(guild, int(entry['user_id']))} → {entry['enemy_name']}",
                value=(
                    f"`{entry['label']}` · **{entry['grade']}**\n"
                    f"공격 {entry['power']} {multiplier} → **{entry['damage']} DAMAGE**"
                ),
                inline=False,
            )
    else:
        embed.description = "이번 턴에 정산할 공격 데미지가 없습니다."
    for summary in enemy_summaries:
        embed.add_field(name="\u200b", value=summary, inline=False)
    if skipped:
        embed.add_field(name="처리 제외", value="\n".join(f"• {item}" for item in skipped)[:1024], inline=False)
    embed.set_footer(text=f"{next_turn}턴이 시작되었습니다.")
    await channel.send(embed=embed)


turn_group = app_commands.Group(name="턴", description="턴을 관리합니다.")


@turn_group.command(name="종료", description="현재 턴을 종료하고 공격 데미지를 정산합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def turn_end(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return
    session = get_active_session(guild.id)
    if session is None:
        await interaction.response.send_message("진행 중인 세션이 없습니다.", ephemeral=True)
        return
    if bool(session["resolving"]):
        await interaction.response.send_message("이미 턴 정산이 진행 중입니다.", ephemeral=True)
        return
    missing = get_missing_participants(session)
    if missing:
        mentions = ", ".join(f"<@{user_id}>" for user_id in missing)
        await interaction.response.send_message(
            f"{mentions} 님이 아직 행동하지 않았습니다.\n**이대로 진행하시겠습니까?**",
            view=MissingTurnConfirmView(interaction.user.id),
            ephemeral=True,
        )
        return
    await begin_turn_resolution(interaction)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "이 명령어는 관리자만 사용할 수 있습니다."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "이 명령어는 서버에서만 사용할 수 있습니다."
    else:
        print(f"Command error: {error!r}")
        message = "명령어 처리 중 오류가 발생했습니다. 콘솔 로그를 확인해주세요."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

bot.run(TOKEN)
