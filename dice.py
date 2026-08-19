from __future__ import annotations

import os
import random
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands






LEVEL_TO_VALUE = {
    1: 30,
    2: 40,
    3: 50,
    4: 60,
    5: 70,
}


DEFAULT_ENEMY_DAMAGE = os.getenv("ENEMY_DAMAGE_DICE", "1d6")
LINK_API_URL = os.getenv("LINK_API_URL", "").rstrip("/")
LINKDICE_API_KEY = os.getenv("LINKDICE_API_KEY", "").strip()
LINK_API_TIMEOUT = float(os.getenv("LINK_API_TIMEOUT", "4"))
FALLBACK_WEAPON_NAME = "유리 파편"
FALLBACK_ATTACK_POWER = 4
FALLBACK_SHIELD_NAME = "화물 상자 뚜껑"
FALLBACK_DEFENSE_POWER = 1
FALLBACK_HP = 20
FALLBACK_BOMBS = 2
BOMB_DAMAGE_MIN = 10
BOMB_DAMAGE_MAX = 14

PLAYER_ROLE_NAME = os.getenv("PLAYER_ROLE_NAME", "player")



DICE_RE = re.compile(r"^(?P<count>\d{1,2})d(?P<sides>\d{1,4})(?P<mod>[+-]\d{1,4})?$", re.I)







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

            CREATE TABLE IF NOT EXISTS turn_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                skill_name TEXT NOT NULL,
                skill_level INTEGER NOT NULL,
                target_value INTEGER NOT NULL,
                roll INTEGER NOT NULL,
                result TEXT NOT NULL,
                success INTEGER NOT NULL,
                enemy_id INTEGER,
                direct_attack INTEGER NOT NULL DEFAULT 0,
                counts_as_attack INTEGER NOT NULL DEFAULT 0,
                resolved INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (enemy_id) REFERENCES active_enemies(id)
            );

            CREATE INDEX IF NOT EXISTS idx_turn_actions_guild
            ON turn_actions (guild_id, resolved, id);

            CREATE TABLE IF NOT EXISTS sync_settings (
                guild_id INTEGER PRIMARY KEY,
                hp_sync INTEGER NOT NULL DEFAULT 1,
                bombs_sync INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        player_columns = {row[1] for row in conn.execute("PRAGMA table_info(player_stats)")}
        if "defense" not in player_columns:
            conn.execute("ALTER TABLE player_stats ADD COLUMN defense INTEGER")
        if "bombs" not in player_columns:
            conn.execute("ALTER TABLE player_stats ADD COLUMN bombs INTEGER")
        conn.commit()


def get_player(guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT * FROM player_stats
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()


def ensure_player(guild_id: int, user_id: int) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO player_stats (guild_id, user_id)
            VALUES (?, ?)
            """,
            (guild_id, user_id),
        )
        conn.commit()


def update_player_fields(guild_id: int, user_id: int, **fields: object) -> None:
    if not fields:
        return

    allowed = {
        "attack",
        "defense",
        "hp",
        "max_hp",
        "bombs",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown player fields: {unknown}")

    ensure_player(guild_id, user_id)

    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = list(fields.values()) + [guild_id, user_id]

    with connect_db() as conn:
        conn.execute(
            f"""
            UPDATE player_stats
            SET {assignments}, updated_at = CURRENT_TIMESTAMP
            WHERE guild_id = ? AND user_id = ?
            """,
            values,
        )
        conn.commit()


def get_sync_settings(guild_id: int) -> tuple[bool, bool]:
    with connect_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sync_settings (guild_id) VALUES (?)",
            (guild_id,),
        )
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
    query = """
        SELECT * FROM active_enemies
        WHERE guild_id = ? AND active = 1
    """
    params: list[object] = [guild_id]

    if not include_down:
        query += " AND hp > 0"

    query += " ORDER BY id ASC"

    with connect_db() as conn:
        return conn.execute(query, params).fetchall()


def get_enemy(guild_id: int, enemy_id: int) -> Optional[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT * FROM active_enemies
            WHERE guild_id = ? AND id = ? AND active = 1
            """,
            (guild_id, enemy_id),
        ).fetchone()


def spawn_enemy(guild_id: int, name: str, hp: int) -> int:
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO active_enemies (guild_id, name, hp, max_hp)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, name, hp, hp),
        )
        conn.commit()
        return int(cursor.lastrowid)


def deactivate_enemy(guild_id: int, enemy_id: int) -> bool:
    with connect_db() as conn:
        cursor = conn.execute(
            """
            UPDATE active_enemies
            SET active = 0
            WHERE guild_id = ? AND id = ? AND active = 1
            """,
            (guild_id, enemy_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def add_turn_action(
    guild_id: int,
    user_id: int,
    skill_name: str,
    skill_level: int,
    target_value: int,
    roll: int,
    result: str,
    success: bool,
    enemy_id: Optional[int] = None,
    direct_attack: bool = False,
) -> int:
    with connect_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO turn_actions (
                guild_id, user_id, skill_name, skill_level,
                target_value, roll, result, success,
                enemy_id, direct_attack, counts_as_attack
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                skill_name,
                skill_level,
                target_value,
                roll,
                result,
                int(success),
                enemy_id,
                int(direct_attack),
                int(direct_attack),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def get_pending_actions(guild_id: int) -> list[sqlite3.Row]:
    with connect_db() as conn:
        return conn.execute(
            """
            SELECT * FROM turn_actions
            WHERE guild_id = ? AND resolved = 0
            ORDER BY id ASC
            """,
            (guild_id,),
        ).fetchall()



def resolve_all_pending_actions(guild_id: int) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE turn_actions
            SET resolved = 1
            WHERE guild_id = ? AND resolved = 0
            """,
            (guild_id,),
        )
        conn.commit()







def roll_d100() -> int:
    return random.randint(1, 100)


def judge_roll(roll: int, target: int) -> tuple[str, bool]:
    if roll == 1:
        return "대성공", True

    if (target < 50 and roll >= 96) or (target >= 50 and roll == 100):
        return "대실패", False

    if roll <= target // 5:
        return "극단적 성공", True

    if roll <= target // 2:
        return "어려운 성공", True

    if roll <= target:
        return "성공", True

    return "실패", False


def parse_and_roll_dice(expression: str) -> tuple[int, list[int], int]:
    normalized = expression.replace(" ", "").lower()
    match = DICE_RE.fullmatch(normalized)
    if not match:
        raise ValueError("주사위식은 1d6, 2d4+1 같은 형식이어야 합니다.")

    count = int(match.group("count"))
    sides = int(match.group("sides"))
    modifier = int(match.group("mod") or 0)

    if count < 1 or count > 20:
        raise ValueError("주사위 개수는 1~20개만 사용할 수 있습니다.")
    if sides < 2 or sides > 1000:
        raise ValueError("주사위 면수는 2~1000만 사용할 수 있습니다.")

    rolls = [random.randint(1, sides) for _ in range(count)]
    total = max(0, sum(rolls) + modifier)
    return total, rolls, modifier


def damage_expression_for_attack_power(attack_power: int) -> str:
    return f"1d{max(1, attack_power)}"


def format_dice_roll(expression: str, rolls: list[int], modifier: int, total: int) -> str:
    detail = " + ".join(str(r) for r in rolls)
    if modifier > 0:
        detail += f" + {modifier}"
    elif modifier < 0:
        detail += f" - {abs(modifier)}"
    return f"`{expression}` → {detail} = **{total}**"


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
        update_player_fields(
            guild_id,
            user_id,
            hp=int(remote["hp"]),
            max_hp=int(remote["max_hp"]),
        )
        local = get_player(guild_id, user_id)
    elif local is None or local["hp"] is None or local["max_hp"] is None:
        update_player_fields(
            guild_id,
            user_id,
            hp=int(profile["hp"]),
            max_hp=int(profile["max_hp"]),
        )
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
    payload = {}
    if hp is not None:
        payload["hp"] = int(hp)
    if bombs is not None:
        payload["bombs"] = int(bombs)
    if not payload:
        return True
    result = await link_api_request("PATCH", guild_id, user_id, payload)
    if result is None:
        return False
    fields = {}
    if "hp" in payload:
        fields["hp"] = int(result["hp"])
        fields["max_hp"] = int(result["max_hp"])
    if "bombs" in payload:
        fields["bombs"] = int(result["bombs"])
    update_player_fields(guild_id, user_id, **fields)
    return True







def enemy_display_name(enemy: sqlite3.Row) -> str:
    return f"{enemy['name']} #{enemy['id']}"


def hp_bar(current: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return "░" * width
    ratio = max(0.0, min(1.0, current / maximum))
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


def stat_text(level: Optional[int]) -> str:
    if level is None:
        return "미설정"
    return f"Lv.{level} ({LEVEL_TO_VALUE[level]})"


def member_label(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"<@{user_id}>"


def result_color(result: str) -> discord.Colour:
    if result == "대성공":
        return discord.Colour.gold()
    if result in ("극단적 성공", "어려운 성공", "성공"):
        return discord.Colour.green()
    if result == "대실패":
        return discord.Colour.red()
    return discord.Colour.light_grey()


def make_roll_embed(
    title: str,
    actor: str,
    skill_name: str,
    level: int,
    target: int,
    roll: int,
    result: str,
    footer: Optional[str] = None,
    target_name: Optional[str] = None,
) -> discord.Embed:
    lines = [f"캐릭터 · `{actor}`"]
    if target_name:
        lines.append(f"대상 · `{target_name}`")
    lines.extend(
        [
            f"판정 · `{skill_name}`",
            f"수치 · `Lv.{level}` → **{target}**",
            f"다이스 · `1d100` → **{roll}**",
            f"결과 · **{result}**",
        ]
    )
    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=result_color(result),
    )
    if footer:
        embed.set_footer(text=footer)
    return embed







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

        test_guild_id = os.getenv("TEST_GUILD_ID")
        if test_guild_id:
            guild = discord.Object(id=int(test_guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print(f"Commands synced to test guild {test_guild_id}")
        else:
            await self.tree.sync()
            print("Global commands synced")


bot = LinkDiceBot()


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

    current_lower = current.lower().strip()
    enemies = get_active_enemies(interaction.guild_id, include_down=False)

    choices: list[app_commands.Choice[str]] = []
    for enemy in enemies:
        label = (
            f"{enemy_display_name(enemy)} — "
            f"HP {enemy['hp']}/{enemy['max_hp']}"
        )
        if current_lower and current_lower not in label.lower():
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

    current_lower = current.lower().strip()
    enemies = get_active_enemies(interaction.guild_id, include_down=True)
    choices: list[app_commands.Choice[str]] = []

    for enemy in enemies:
        state = "DOWN" if enemy["hp"] <= 0 else f"HP {enemy['hp']}/{enemy['max_hp']}"
        label = f"{enemy_display_name(enemy)} — {state}"
        if current_lower and current_lower not in label.lower():
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







async def perform_skill_roll(
    interaction: discord.Interaction,
    column: str,
    display_name: str,
) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True
        )
        return

    player = get_player(interaction.guild_id, interaction.user.id)
    if player is None or player[column] is None:
        await interaction.response.send_message(
            f"{interaction.user.mention}님의 **{display_name}** 수치가 설정되어 있지 않습니다.\n"
            "관리자에게 `/세팅`을 요청해주세요.",
            ephemeral=True,
        )
        return

    level = int(player[column])
    target = LEVEL_TO_VALUE[level]
    roll = roll_d100()
    result, _ = judge_roll(roll, target)

    embed = make_roll_embed(
        title=display_name,
        actor=interaction.user.display_name,
        skill_name=display_name,
        level=level,
        target=target,
        roll=roll,
        result=result,
        footer="LINKDICE 판정 결과입니다.",
    )
    await interaction.response.send_message(embed=embed)






@bot.tree.command(name="공격", description="에너미를 대상으로 공격 판정을 합니다.")
@app_commands.guild_only()
@app_commands.rename(enemy="에너미")
@app_commands.describe(enemy="공격할 에너미")
@app_commands.autocomplete(enemy=enemy_autocomplete)
async def attack(interaction: discord.Interaction, enemy: str) -> None:
    if interaction.guild_id is None:
        return

    try:
        enemy_id = int(enemy)
    except ValueError:
        await interaction.response.send_message(
            "현재 등장 중인 에너미를 목록에서 선택해주세요.", ephemeral=True
        )
        return

    target_enemy = get_enemy(interaction.guild_id, enemy_id)
    if target_enemy is None or target_enemy["hp"] <= 0:
        await interaction.response.send_message(
            "그 에너미는 현재 공격할 수 없습니다.", ephemeral=True
        )
        return

    player = get_player(interaction.guild_id, interaction.user.id)
    if player is None or player["attack"] is None:
        await interaction.response.send_message(
            f"{interaction.user.mention}님의 **공격** 수치가 설정되어 있지 않습니다.\n"
            "관리자에게 `/세팅`을 요청해주세요.",
            ephemeral=True,
        )
        return

    level = int(player["attack"])
    target = LEVEL_TO_VALUE[level]
    roll = roll_d100()
    result, success = judge_roll(roll, target)

    add_turn_action(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        skill_name="공격",
        skill_level=level,
        target_value=target,
        roll=roll,
        result=result,
        success=success,
        enemy_id=enemy_id,
        direct_attack=True,
    )

    suffix = (
        "데미지는 /턴 종료에서 확정됩니다."
        if success
        else "공격에 실패하여 데미지가 발생하지 않습니다."
    )
    embed = make_roll_embed(
        title="공격",
        actor=interaction.user.display_name,
        target_name=enemy_display_name(target_enemy),
        skill_name="공격",
        level=level,
        target=target,
        roll=roll,
        result=result,
        footer=suffix,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="방어", description="방어 판정을 합니다.")
@app_commands.guild_only()
async def defense(interaction: discord.Interaction) -> None:
    await perform_skill_roll(interaction, "defense", "방어")






@bot.tree.command(name="세팅", description="플레이어의 LINKDICE 판정 수치를 설정합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(
    member="유저",
    attack_value="공격",
    defense_value="방어",
)
@app_commands.describe(
    member="설정할 유저",
    attack_value="공격 레벨 (1~5)",
    defense_value="방어 레벨 (1~5)",
)
async def setup_stats(
    interaction: discord.Interaction,
    member: discord.Member,
    attack_value: Optional[int] = None,
    defense_value: Optional[int] = None,
) -> None:
    levels = [attack_value, defense_value]
    if any(value is not None and value not in LEVEL_TO_VALUE for value in levels):
        await interaction.response.send_message(
            "능력치 레벨은 **1~5** 사이여야 합니다.", ephemeral=True
        )
        return
    if all(value is None for value in levels):
        await interaction.response.send_message(
            "변경할 값을 하나 이상 입력해주세요.", ephemeral=True
        )
        return

    fields: dict[str, object] = {}
    changes: list[str] = []
    for column, value, label in (
        ("attack", attack_value, "공격"),
        ("defense", defense_value, "방어"),
    ):
        if value is not None:
            fields[column] = value
            changes.append(f"{label}: Lv.{value} ({LEVEL_TO_VALUE[value]})")

    update_player_fields(interaction.guild_id, member.id, **fields)
    await interaction.response.send_message(
        f"**{member.display_name}** 설정 완료\n" + "\n".join(f"- {c}" for c in changes),
        ephemeral=True,
    )


@bot.tree.command(name="스탯", description="LINKDICE 능력치와 LINK 장비를 확인합니다.")
@app_commands.guild_only()
@app_commands.rename(member="유저")
@app_commands.describe(member="생략하면 자신의 스탯을 확인합니다.")
async def stats(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
) -> None:
    if interaction.guild_id is None:
        return
    target_member = member or interaction.user
    ensure_player(interaction.guild_id, target_member.id)
    player = get_player(interaction.guild_id, target_member.id)
    profile, connected = await get_effective_profile(interaction.guild_id, target_member.id)
    ring = profile.get("ring")
    head = profile.get("head")
    hp_sync, bombs_sync = get_sync_settings(interaction.guild_id)
    embed = discord.Embed(title=f"{target_member.display_name} · LINKDICE")
    embed.add_field(
        name="판정",
        value=(
            f"공격 · {stat_text(player['attack'])}\n"
            f"방어 · {stat_text(player['defense'])}"
        ),
        inline=False,
    )
    embed.add_field(
        name="LINK",
        value=(
            f"체력 · **{profile['hp']}/{profile['max_hp']}**\n"
            f"공격력 · **{profile['attack_power']}**\n"
            f"방어력 · **{profile['defense_power']}**\n"
            f"폭탄 · **{profile['bombs']}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="장비",
        value=(
            f"무기 · `{profile['weapon']['name']}` ({profile['weapon']['power']})\n"
            f"반지 · `{ring['name']}` (+{ring['power']})\n" if ring else f"무기 · `{profile['weapon']['name']}` ({profile['weapon']['power']})\n반지 · `없음`\n"
        ) + (
            f"방패 · `{profile['shield']['name']}` ({profile['shield']['power']})\n"
            f"투구 · `{head['name']}` ({head['power']})" if head else f"방패 · `{profile['shield']['name']}` ({profile['shield']['power']})\n투구 · `없음`"
        ),
        inline=False,
    )
    source = "LINK 실시간 연결" if connected else "LINK 연결 실패 · 기본 장비/마지막 전투값 사용"
    embed.set_footer(
        text=f"{source} · 체력 양방향 {'ON' if hp_sync else 'OFF'} · 폭탄 양방향 {'ON' if bombs_sync else 'OFF'}"
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="동기화", description="LINK의 체력과 폭탄을 LINKDICE로 다시 불러옵니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(member="유저")
async def sync_from_link(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if interaction.guild_id is None:
        return
    remote = await link_api_request("GET", interaction.guild_id, member.id)
    if remote is None:
        await interaction.response.send_message(
            "LINK와 연결할 수 없습니다. LINK_API_URL과 LINKDICE_API_KEY를 확인해주세요.",
            ephemeral=True,
        )
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
@app_commands.rename(
    member="유저",
    hp="체력",
    bombs="폭탄",
    hp_sync="체력연동",
    bombs_sync="폭탄연동",
)
async def debug_sync(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
    hp: Optional[int] = None,
    bombs: Optional[int] = None,
    hp_sync: Optional[bool] = None,
    bombs_sync: Optional[bool] = None,
) -> None:
    if interaction.guild_id is None:
        return
    current_hp_sync, current_bombs_sync = set_sync_settings(
        interaction.guild_id, hp_sync, bombs_sync
    )
    target = member or interaction.user
    profile, connected = await get_effective_profile(interaction.guild_id, target.id)
    if hp is not None:
        hp = max(0, min(hp, int(profile["max_hp"])))
        update_player_fields(
            interaction.guild_id, target.id, hp=hp, max_hp=int(profile["max_hp"])
        )
        profile["hp"] = hp
        if current_hp_sync:
            connected = await write_link_resources(interaction.guild_id, target.id, hp=hp) and connected
    if bombs is not None:
        bombs = max(0, bombs)
        update_player_fields(interaction.guild_id, target.id, bombs=bombs)
        profile["bombs"] = bombs
        if current_bombs_sync:
            connected = await write_link_resources(interaction.guild_id, target.id, bombs=bombs) and connected
    await interaction.response.send_message(
        f"**{target.display_name}** · HP {profile['hp']}/{profile['max_hp']} · 폭탄 {profile['bombs']}\n"
        f"체력 양방향: **{'ON' if current_hp_sync else 'OFF'}** · "
        f"폭탄 양방향: **{'ON' if current_bombs_sync else 'OFF'}**\n"
        f"LINK 연결: **{'정상' if connected else '확인 필요'}**",
        ephemeral=True,
    )







enemy_group = app_commands.Group(name="에너미", description="에너미를 관리합니다.")


@bot.tree.command(name="등장", description="새 에너미를 현재 장면에 등장시킵니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(name="이름", hp="체력")
@app_commands.describe(name="에너미 이름", hp="에너미 체력")
async def enemy_spawn(
    interaction: discord.Interaction,
    name: str,
    hp: int,
) -> None:
    if interaction.guild_id is None:
        return

    clean_name = name.strip()
    if not clean_name:
        await interaction.response.send_message("이름을 입력해주세요.", ephemeral=True)
        return
    if hp < 1:
        await interaction.response.send_message("체력은 1 이상이어야 합니다.", ephemeral=True)
        return

    enemy_id = spawn_enemy(interaction.guild_id, clean_name, hp)
    embed = discord.Embed(
        title=f"{clean_name} #{enemy_id}",
        description=f"체력 · {hp_bar(hp, hp)} **{hp}/{hp}**",
        color=discord.Colour.light_grey(),
    )
    embed.set_footer(text="현재 장면에 등장했습니다.")
    await interaction.response.send_message(embed=embed)


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
        if enemy["hp"] <= 0:
            value = "`DOWN`"
        else:
            value = f"{hp_bar(enemy['hp'], enemy['max_hp'])} **{enemy['hp']}/{enemy['max_hp']}**"
        embed.add_field(name=enemy_display_name(enemy), value=value, inline=False)
    embed.set_footer(text="현재 등장한 에너미입니다.")
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
        await interaction.response.send_message(
            "목록에서 에너미를 선택해주세요.", ephemeral=True
        )
        return

    target = get_enemy(interaction.guild_id, enemy_id)
    if target is None:
        await interaction.response.send_message(
            "해당 에너미를 찾을 수 없습니다.", ephemeral=True
        )
        return

    deactivate_enemy(interaction.guild_id, enemy_id)
    await interaction.response.send_message(
        f"**{enemy_display_name(target)}** 퇴장",
    )







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
        await interaction.response.send_message(
            f"@{PLAYER_ROLE_NAME} 역할의 플레이어를 목록에서 선택해주세요.",
            ephemeral=True,
        )
        return
    member = guild.get_member(user_id)
    if member is None:
        await interaction.response.send_message(
            "해당 플레이어를 서버에서 찾을 수 없습니다.", ephemeral=True
        )
        return
    role = discord.utils.get(guild.roles, name=PLAYER_ROLE_NAME)
    if role is None:
        await interaction.response.send_message(
            f"서버에 `@{PLAYER_ROLE_NAME}` 역할이 없습니다.", ephemeral=True
        )
        return
    if role not in member.roles:
        await interaction.response.send_message(
            f"{member.mention}님은 `@{PLAYER_ROLE_NAME}` 역할을 가지고 있지 않습니다.",
            ephemeral=True,
        )
        return
    profile, connected = await get_effective_profile(guild.id, member.id)
    old_hp = int(profile["hp"])
    max_hp = int(profile["max_hp"])
    if old_hp <= 0:
        await interaction.response.send_message(
            f"{member.mention}님은 이미 **DOWN** 상태입니다.", ephemeral=True
        )
        return
    try:
        raw_damage, rolls, modifier = parse_and_roll_dice(DEFAULT_ENEMY_DAMAGE)
    except ValueError as exc:
        await interaction.response.send_message(
            f"ENEMY_DAMAGE_DICE 설정 오류: {exc}", ephemeral=True
        )
        return
    defense_power = max(0, int(profile["defense_power"]))
    defense_roll = random.randint(1, defense_power) if defense_power > 0 else 0
    damage = max(0, raw_damage - defense_roll)
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
            f"공격 · {format_dice_roll(DEFAULT_ENEMY_DAMAGE, rolls, modifier, raw_damage)}\n"
            f"방어 · `1d{defense_power}` → **{defense_roll}**\n" if defense_power > 0 else
            f"대상 · `{member.display_name}`\n"
            f"공격 · {format_dice_roll(DEFAULT_ENEMY_DAMAGE, rolls, modifier, raw_damage)}\n"
            f"방어 · **0**\n"
        ) + (
            f"피해 · **{damage}**\n"
            f"체력 · {hp_bar(new_hp, max_hp)} **{old_hp} → {new_hp}/{max_hp}**"
            + ("\n상태 · **DOWN**" if new_hp <= 0 else "")
        ),
        color=discord.Colour.red() if damage > 0 else discord.Colour.green(),
    )
    footer = "에너미의 공격은 즉시 적용됩니다."
    if hp_sync and (not connected or not synced):
        footer += " · LINK 체력 동기화 실패"
    embed.set_footer(text=footer)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="폭탄", description="LINK의 폭탄을 사용해 에너미에게 즉시 피해를 줍니다.")
@app_commands.guild_only()
@app_commands.rename(enemy="에너미")
@app_commands.autocomplete(enemy=enemy_autocomplete)
async def bomb_attack(interaction: discord.Interaction, enemy: str) -> None:
    if interaction.guild_id is None:
        return
    try:
        enemy_id = int(enemy)
    except ValueError:
        await interaction.response.send_message(
            "현재 등장 중인 에너미를 목록에서 선택해주세요.", ephemeral=True
        )
        return
    target_enemy = get_enemy(interaction.guild_id, enemy_id)
    if target_enemy is None or target_enemy["hp"] <= 0:
        await interaction.response.send_message(
            "그 에너미는 현재 공격할 수 없습니다.", ephemeral=True
        )
        return
    profile, connected = await get_effective_profile(interaction.guild_id, interaction.user.id)
    bombs = int(profile["bombs"])
    if bombs <= 0:
        await interaction.response.send_message("폭탄이 없습니다.", ephemeral=True)
        return
    damage = random.randint(BOMB_DAMAGE_MIN, BOMB_DAMAGE_MAX)
    next_bombs = bombs - 1
    old_hp = int(target_enemy["hp"])
    max_hp = int(target_enemy["max_hp"])
    new_hp = max(0, old_hp - damage)
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
        synced = await write_link_resources(
            interaction.guild_id, interaction.user.id, bombs=next_bombs
        )
    embed = discord.Embed(
        title="폭탄",
        description=(
            f"캐릭터 · `{interaction.user.display_name}`\n"
            f"대상 · `{enemy_display_name(target_enemy)}`\n"
            f"피해 · **{damage}**\n"
            f"에너미 체력 · {hp_bar(new_hp, max_hp)} **{old_hp} → {new_hp}/{max_hp}**\n"
            f"남은 폭탄 · **{next_bombs}**"
            + ("\n상태 · **DOWN**" if new_hp <= 0 else "")
        ),
        color=discord.Colour.orange(),
    )
    footer = "폭탄 피해는 즉시 적용됩니다."
    if bombs_sync and (not connected or not synced):
        footer += " · LINK 폭탄 동기화 실패"
    embed.set_footer(text=footer)
    await interaction.response.send_message(embed=embed)







async def resolve_turn(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
) -> str:
    actions = get_pending_actions(guild.id)
    if not actions:
        return "이번 턴에 처리할 판정이 없습니다."

    damage_entries: list[dict[str, object]] = []
    enemy_totals: defaultdict[int, int] = defaultdict(int)
    skipped: list[str] = []

    for action in actions:
        if not action["success"] or not action["counts_as_attack"]:
            continue
        if action["enemy_id"] is None:
            skipped.append(
                f"{member_label(guild, action['user_id'])} — {action['skill_name']}: 대상 없음"
            )
            continue

        enemy = get_enemy(guild.id, int(action["enemy_id"]))
        if enemy is None:
            skipped.append(
                f"{member_label(guild, action['user_id'])} — {action['skill_name']}: 에너미 없음"
            )
            continue

        profile, connected = await get_effective_profile(guild.id, int(action["user_id"]))
        attack_power_value = max(0, int(profile["attack_power"]))
        expression = damage_expression_for_attack_power(attack_power_value)
        try:
            damage, rolls, modifier = parse_and_roll_dice(expression)
        except ValueError as exc:
            skipped.append(
                f"{member_label(guild, action['user_id'])} — 데미지식 오류: {exc}"
            )
            continue

        enemy_id = int(enemy["id"])
        enemy_totals[enemy_id] += damage
        damage_entries.append(
            {
                "user_id": int(action["user_id"]),
                "skill_name": str(action["skill_name"]),
                "enemy_id": enemy_id,
                "enemy_name": enemy_display_name(enemy),
                "expression": expression,
                "rolls": rolls,
                "modifier": modifier,
                "damage": damage,
                "attack_power": attack_power_value,
                "weapon_name": profile["weapon"]["name"],
            }
        )

    enemy_summaries: list[str] = []
    with connect_db() as conn:
        for enemy_id, total_damage in enemy_totals.items():
            enemy = conn.execute(
                """
                SELECT * FROM active_enemies
                WHERE guild_id = ? AND id = ? AND active = 1
                """,
                (guild.id, enemy_id),
            ).fetchone()
            if enemy is None:
                continue

            old_hp = int(enemy["hp"])
            max_hp = int(enemy["max_hp"])
            new_hp = max(0, old_hp - total_damage)
            conn.execute(
                "UPDATE active_enemies SET hp = ? WHERE id = ? AND guild_id = ?",
                (new_hp, enemy_id, guild.id),
            )

            state = " — **DOWN**" if new_hp <= 0 else ""
            enemy_summaries.append(
                f"**{enemy_display_name(enemy)}**\n"
                f"HP {hp_bar(new_hp, max_hp)} **{old_hp} → {new_hp}/{max_hp}** "
                f"(총 -{total_damage}){state}"
            )

        conn.execute(
            """
            UPDATE turn_actions
            SET resolved = 1
            WHERE guild_id = ? AND resolved = 0
            """,
            (guild.id,),
        )
        conn.commit()

    embed = discord.Embed(title="턴 결과", color=discord.Colour.blurple())
    if damage_entries:
        for entry in damage_entries:
            embed.add_field(
                name=f"{member_label(guild, entry['user_id'])} → {entry['enemy_name']}",
                value=(
                    f"판정 · `{entry['skill_name']}`\n"
                    f"무기 · `{entry['weapon_name']}` · 공격력 **{entry['attack_power']}**\n"
                    f"데미지 · {format_dice_roll(entry['expression'], entry['rolls'], entry['modifier'], entry['damage'])}"
                ),
                inline=False,
            )
    else:
        embed.description = "이번 턴에 적용되는 데미지가 없습니다."
    for summary in enemy_summaries:
        embed.add_field(name="\u200b", value=summary, inline=False)
    footer_parts = []
    if skipped:
        footer_parts.append("처리 제외: " + " / ".join(skipped))
    footer_parts.append("턴의 공격 데미지가 확정되었습니다.")
    embed.set_footer(text=" · ".join(footer_parts)[:2048])
    await channel.send(embed=embed)
    return "턴 종료 처리 완료."


turn_group = app_commands.Group(name="턴", description="턴을 관리합니다.")


@turn_group.command(name="종료", description="현재 턴의 공격 데미지를 확정하고 적용합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def turn_end(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if guild is None:
        return

    actions = get_pending_actions(guild.id)
    if not actions:
        await interaction.response.send_message(
            "이번 턴에 처리할 공격 판정이 없습니다.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    await resolve_turn(guild, interaction.channel)
    await interaction.followup.send("턴 종료 처리 완료.", ephemeral=True)






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
