from __future__ import annotations

import os
import random
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


# ============================================================
# CONFIG
# ============================================================

LEVEL_TO_VALUE = {
    1: 30,
    2: 40,
    3: 50,
    4: 60,
    5: 70,
}

STAT_COLUMNS = {
    "공격": "attack",
    "민첩": "agility",
    "지능": "intelligence",
    "행운": "luck",
}

# CoC-inspired placeholders for the current weapon boolean system.
# Change these later if LINKDICE gets real weapon types.
UNARMED_DAMAGE = os.getenv("UNARMED_DAMAGE", "1d3")
WEAPON_DAMAGE = os.getenv("WEAPON_DAMAGE", "1d6")
DEFAULT_ENEMY_DAMAGE = os.getenv("ENEMY_DAMAGE_DICE", "1d6")

PLAYER_ROLE_NAME = os.getenv("PLAYER_ROLE_NAME", "player")

# Discord's checkbox group supports up to 10 options in a modal.
MAX_TURN_CHECKBOX_ACTIONS = 10

DICE_RE = re.compile(r"^(?P<count>\d{1,2})d(?P<sides>\d{1,4})(?P<mod>[+-]\d{1,4})?$", re.I)


# ============================================================
# DATABASE
# ============================================================


def get_db_path() -> Path:
    """Use the Railway volume when attached, otherwise ./data locally."""
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
                agility INTEGER,
                intelligence INTEGER,
                luck INTEGER,
                hp INTEGER,
                max_hp INTEGER,
                special_name TEXT,
                special_level INTEGER,
                has_weapon INTEGER NOT NULL DEFAULT 0,
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
            """
        )
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
        "agility",
        "intelligence",
        "luck",
        "hp",
        "max_hp",
        "special_name",
        "special_level",
        "has_weapon",
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


def mark_action_as_attack(guild_id: int, action_id: int, enemy_id: int) -> None:
    with connect_db() as conn:
        conn.execute(
            """
            UPDATE turn_actions
            SET counts_as_attack = 1, enemy_id = ?
            WHERE guild_id = ? AND id = ? AND resolved = 0
            """,
            (enemy_id, guild_id, action_id),
        )
        conn.commit()


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


# ============================================================
# DICE + RESULT RULES
# ============================================================


def roll_d100() -> int:
    return random.randint(1, 100)


def judge_roll(roll: int, target: int) -> tuple[str, bool]:
    """CoC-style percentile grading used as the current LINKDICE placeholder."""
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
    """Roll a restricted dice expression such as 1d6, 2d4+1, or 1d8-1."""
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


def damage_expression_for_player(player: sqlite3.Row) -> str:
    return WEAPON_DAMAGE if bool(player["has_weapon"]) else UNARMED_DAMAGE


def format_dice_roll(expression: str, rolls: list[int], modifier: int, total: int) -> str:
    detail = " + ".join(str(r) for r in rolls)
    if modifier > 0:
        detail += f" + {modifier}"
    elif modifier < 0:
        detail += f" - {abs(modifier)}"
    return f"`{expression}` → {detail} = **{total}**"


# ============================================================
# DISPLAY HELPERS
# ============================================================


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


# ============================================================
# BOT
# ============================================================


class LinkDiceBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # Needed so the /플레이어 공격 autocomplete can reliably inspect @player role members.
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        init_db()

        # Groups are added before sync.
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


# ============================================================
# AUTOCOMPLETE
# ============================================================


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


# ============================================================
# COMMON SKILL ROLL
# ============================================================


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
    result, success = judge_roll(roll, target)

    add_turn_action(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        skill_name=display_name,
        skill_level=level,
        target_value=target,
        roll=roll,
        result=result,
        success=success,
    )

    await interaction.response.send_message(
        f"**{interaction.user.display_name} — {display_name}**\n"
        f"수치: **Lv.{level} → {target}**\n"
        f"1d100: **{roll}**\n"
        f"결과: **{result}**\n"
        "*이번 턴 판정으로 기록되었습니다.*"
    )


# ============================================================
# BASIC PLAYER COMMANDS
# ============================================================


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
        "**데미지는 `/턴 종료`에서 확정됩니다.**"
        if success
        else "공격에 실패하여 데미지가 발생하지 않습니다."
    )

    await interaction.response.send_message(
        f"**{interaction.user.display_name} → {enemy_display_name(target_enemy)}**\n"
        f"공격 Lv.{level} → **{target}**\n"
        f"1d100: **{roll}**\n"
        f"결과: **{result}**\n"
        f"{suffix}"
    )


@bot.tree.command(name="민첩", description="민첩 판정을 합니다.")
@app_commands.guild_only()
async def agility(interaction: discord.Interaction) -> None:
    await perform_skill_roll(interaction, "agility", "민첩")


@bot.tree.command(name="지능", description="지능 판정을 합니다.")
@app_commands.guild_only()
async def intelligence(interaction: discord.Interaction) -> None:
    await perform_skill_roll(interaction, "intelligence", "지능")


@bot.tree.command(name="행운", description="행운 판정을 합니다.")
@app_commands.guild_only()
async def luck(interaction: discord.Interaction) -> None:
    await perform_skill_roll(interaction, "luck", "행운")


@bot.tree.command(name="특수", description="자신의 특수 능력 판정을 합니다.")
@app_commands.guild_only()
async def special(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        return

    player = get_player(interaction.guild_id, interaction.user.id)
    if (
        player is None
        or player["special_name"] is None
        or player["special_level"] is None
    ):
        await interaction.response.send_message(
            f"{interaction.user.mention}님의 **특수**가 설정되어 있지 않습니다.\n"
            "관리자에게 `/세팅`을 요청해주세요.",
            ephemeral=True,
        )
        return

    level = int(player["special_level"])
    target = LEVEL_TO_VALUE[level]
    roll = roll_d100()
    result, success = judge_roll(roll, target)
    special_name = str(player["special_name"])

    add_turn_action(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        skill_name=f"특수: {special_name}",
        skill_level=level,
        target_value=target,
        roll=roll,
        result=result,
        success=success,
    )

    await interaction.response.send_message(
        f"**{interaction.user.display_name} — 특수: {special_name}**\n"
        f"수치: **Lv.{level} → {target}**\n"
        f"1d100: **{roll}**\n"
        f"결과: **{result}**\n"
        "*이번 턴 판정으로 기록되었습니다.*"
    )


# ============================================================
# ADMIN: PLAYER SETUP
# ============================================================


@bot.tree.command(name="세팅", description="플레이어의 LINKDICE 능력치를 설정합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(
    member="유저",
    attack_value="공격",
    agility_value="민첩",
    intelligence_value="지능",
    luck_value="행운",
    hp_value="체력",
    special_name="특수",
    special_value="특수값",
)
@app_commands.describe(
    member="설정할 유저",
    attack_value="공격 레벨 (1~5)",
    agility_value="민첩 레벨 (1~5)",
    intelligence_value="지능 레벨 (1~5)",
    luck_value="행운 레벨 (1~5)",
    hp_value="최대 체력. 입력하면 현재 체력도 이 값으로 회복됩니다.",
    special_name="특수 능력 이름 (예: 재봉)",
    special_value="특수 능력 레벨 (1~5)",
)
async def setup_stats(
    interaction: discord.Interaction,
    member: discord.Member,
    attack_value: Optional[int] = None,
    agility_value: Optional[int] = None,
    intelligence_value: Optional[int] = None,
    luck_value: Optional[int] = None,
    hp_value: Optional[int] = None,
    special_name: Optional[str] = None,
    special_value: Optional[int] = None,
) -> None:
    levels = [attack_value, agility_value, intelligence_value, luck_value, special_value]
    if any(value is not None and value not in LEVEL_TO_VALUE for value in levels):
        await interaction.response.send_message(
            "능력치 레벨은 **1~5** 사이여야 합니다.", ephemeral=True
        )
        return

    if hp_value is not None and hp_value < 1:
        await interaction.response.send_message(
            "체력은 1 이상이어야 합니다.", ephemeral=True
        )
        return

    if (special_name is None) != (special_value is None):
        await interaction.response.send_message(
            "특수를 설정하려면 **특수 이름**과 **특수값**을 함께 입력해주세요.",
            ephemeral=True,
        )
        return

    if all(value is None for value in levels) and hp_value is None and special_name is None:
        await interaction.response.send_message(
            "변경할 값을 하나 이상 입력해주세요.", ephemeral=True
        )
        return

    fields: dict[str, object] = {}
    changes: list[str] = []

    for column, value, label in (
        ("attack", attack_value, "공격"),
        ("agility", agility_value, "민첩"),
        ("intelligence", intelligence_value, "지능"),
        ("luck", luck_value, "행운"),
    ):
        if value is not None:
            fields[column] = value
            changes.append(f"{label}: Lv.{value} ({LEVEL_TO_VALUE[value]})")

    if hp_value is not None:
        fields["hp"] = hp_value
        fields["max_hp"] = hp_value
        changes.append(f"체력: {hp_value}/{hp_value}")

    if special_name is not None and special_value is not None:
        cleaned_name = special_name.strip()
        if not cleaned_name:
            await interaction.response.send_message(
                "특수 이름을 입력해주세요.", ephemeral=True
            )
            return
        fields["special_name"] = cleaned_name
        fields["special_level"] = special_value
        changes.append(
            f"특수: {cleaned_name} Lv.{special_value} ({LEVEL_TO_VALUE[special_value]})"
        )

    update_player_fields(interaction.guild_id, member.id, **fields)

    await interaction.response.send_message(
        f"**{member.display_name}** 설정 완료\n" + "\n".join(f"- {c}" for c in changes),
        ephemeral=True,
    )


@bot.tree.command(name="무기", description="플레이어의 무기 보유 상태를 설정합니다.")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.rename(member="유저", equipped="보유")
@app_commands.describe(member="설정할 유저", equipped="무기를 들고 있으면 True")
async def weapon(
    interaction: discord.Interaction,
    member: discord.Member,
    equipped: bool,
) -> None:
    update_player_fields(
        interaction.guild_id,
        member.id,
        has_weapon=int(equipped),
    )
    expression = WEAPON_DAMAGE if equipped else UNARMED_DAMAGE
    await interaction.response.send_message(
        f"**{member.display_name}** 무기: **{'보유' if equipped else '미보유'}**\n"
        f"현재 데미지식: `{expression}`",
        ephemeral=True,
    )


@bot.tree.command(name="스탯", description="LINKDICE 능력치를 확인합니다.")
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
    player = get_player(interaction.guild_id, target_member.id)
    if player is None:
        await interaction.response.send_message(
            f"{target_member.mention}님의 능력치가 아직 설정되지 않았습니다.",
            ephemeral=True,
        )
        return

    hp_text = (
        f"{player['hp']}/{player['max_hp']}"
        if player["hp"] is not None and player["max_hp"] is not None
        else "미설정"
    )
    special_text = (
        f"{player['special_name']} — {stat_text(player['special_level'])}"
        if player["special_name"] is not None and player["special_level"] is not None
        else "미설정"
    )
    weapon_text = "보유" if player["has_weapon"] else "미보유"

    await interaction.response.send_message(
        f"**{target_member.display_name} — LINKDICE 스탯**\n"
        f"공격: {stat_text(player['attack'])}\n"
        f"민첩: {stat_text(player['agility'])}\n"
        f"지능: {stat_text(player['intelligence'])}\n"
        f"행운: {stat_text(player['luck'])}\n"
        f"특수: {special_text}\n"
        f"체력: **{hp_text}**\n"
        f"무기: **{weapon_text}**"
    )


# ============================================================
# /에너미 ...
# ============================================================


enemy_group = app_commands.Group(name="에너미", description="에너미를 관리합니다.")


@enemy_group.command(name="등장", description="새 에너미를 현재 장면에 등장시킵니다.")
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
    await interaction.response.send_message(
        f"**{clean_name} #{enemy_id}** 등장\n"
        f"HP {hp_bar(hp, hp)} **{hp}/{hp}**"
    )


@enemy_group.command(name="목록", description="현재 장면의 에너미를 확인합니다.")
@app_commands.guild_only()
async def enemy_list(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        return

    enemies = get_active_enemies(interaction.guild_id, include_down=True)
    if not enemies:
        await interaction.response.send_message("현재 등장한 에너미가 없습니다.")
        return

    lines = ["**현재 에너미**"]
    for enemy in enemies:
        if enemy["hp"] <= 0:
            lines.append(f"- **{enemy_display_name(enemy)}** — `DOWN`")
        else:
            lines.append(
                f"- **{enemy_display_name(enemy)}** — "
                f"{hp_bar(enemy['hp'], enemy['max_hp'])} "
                f"{enemy['hp']}/{enemy['max_hp']}"
            )
    await interaction.response.send_message("\n".join(lines))


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


# ============================================================
# /플레이어 공격
# ============================================================


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

    stats_row = get_player(guild.id, member.id)
    if stats_row is None or stats_row["hp"] is None or stats_row["max_hp"] is None:
        await interaction.response.send_message(
            f"{member.mention}님의 체력이 설정되어 있지 않습니다. `/세팅`에서 체력을 설정해주세요.",
            ephemeral=True,
        )
        return

    old_hp = int(stats_row["hp"])
    max_hp = int(stats_row["max_hp"])
    if old_hp <= 0:
        await interaction.response.send_message(
            f"{member.mention}님은 이미 **DOWN** 상태입니다.", ephemeral=True
        )
        return

    try:
        damage, rolls, modifier = parse_and_roll_dice(DEFAULT_ENEMY_DAMAGE)
    except ValueError as exc:
        await interaction.response.send_message(
            f"ENEMY_DAMAGE_DICE 설정 오류: {exc}", ephemeral=True
        )
        return

    new_hp = max(0, old_hp - damage)
    update_player_fields(guild.id, member.id, hp=new_hp)

    down = "\n**DOWN**" if new_hp <= 0 else ""
    await interaction.response.send_message(
        f"**에너미 → {member.display_name}**\n"
        f"데미지: {format_dice_roll(DEFAULT_ENEMY_DAMAGE, rolls, modifier, damage)}\n"
        f"HP {hp_bar(new_hp, max_hp)} **{old_hp} → {new_hp}/{max_hp}**"
        f"{down}"
    )


# ============================================================
# TURN END RESOLUTION
# ============================================================


async def resolve_turn(
    guild: discord.Guild,
    channel: discord.abc.Messageable,
) -> str:
    """Roll damage for all successful attack-marked actions, then apply totals."""
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

        player = get_player(guild.id, int(action["user_id"]))
        if player is None:
            skipped.append(
                f"{member_label(guild, action['user_id'])} — 플레이어 데이터 없음"
            )
            continue

        expression = damage_expression_for_player(player)
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

    lines = ["## 턴 결과"]

    if damage_entries:
        for entry in damage_entries:
            lines.append(
                f"**{member_label(guild, entry['user_id'])} → {entry['enemy_name']}** "
                f"({entry['skill_name']})\n"
                f"{format_dice_roll(entry['expression'], entry['rolls'], entry['modifier'], entry['damage'])} DAMAGE"
            )
    else:
        lines.append("이번 턴에 적용되는 데미지가 없습니다.")

    if enemy_summaries:
        lines.append("### 에너미 상태")
        lines.extend(enemy_summaries)

    if skipped:
        lines.append("### 처리 제외")
        lines.extend(f"- {item}" for item in skipped)

    result = "\n\n".join(lines)
    await channel.send(result)
    return result


class TargetSelect(discord.ui.Select):
    def __init__(self, parent_view: "TargetAssignmentView") -> None:
        self.parent_view = parent_view
        enemies = get_active_enemies(parent_view.guild.id, include_down=False)
        options = [
            discord.SelectOption(
                label=enemy_display_name(enemy)[:100],
                value=str(enemy["id"]),
                description=f"HP {enemy['hp']}/{enemy['max_hp']}"[:100],
            )
            for enemy in enemies[:25]
        ]
        super().__init__(
            placeholder="이 판정이 공격한 에너미를 선택하세요.",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.admin_id:
            await interaction.response.send_message(
                "`/턴 종료`를 실행한 관리자만 조작할 수 있습니다.", ephemeral=True
            )
            return

        enemy_id = int(self.values[0])
        action = self.parent_view.current_action
        mark_action_as_attack(self.parent_view.guild.id, int(action["id"]), enemy_id)
        self.parent_view.index += 1

        if self.parent_view.index >= len(self.parent_view.actions):
            self.parent_view.stop()
            await interaction.response.edit_message(
                content="대상 지정 완료. 데미지를 계산합니다.",
                view=None,
            )
            await resolve_turn(self.parent_view.guild, interaction.channel)
            return

        self.parent_view.rebuild()
        await interaction.response.edit_message(
            content=self.parent_view.prompt_text(),
            view=self.parent_view,
        )


class TargetAssignmentView(discord.ui.View):
    def __init__(
        self,
        guild: discord.Guild,
        admin_id: int,
        actions: list[sqlite3.Row],
    ) -> None:
        super().__init__(timeout=300)
        self.guild = guild
        self.admin_id = admin_id
        self.actions = actions
        self.index = 0
        self.rebuild()

    @property
    def current_action(self) -> sqlite3.Row:
        return self.actions[self.index]

    def prompt_text(self) -> str:
        action = self.current_action
        return (
            f"**공격 대상 지정 {self.index + 1}/{len(self.actions)}**\n"
            f"{member_label(self.guild, action['user_id'])} — "
            f"{action['skill_name']} {action['result']} ({action['roll']}/{action['target_value']})"
        )

    def rebuild(self) -> None:
        self.clear_items()
        enemies = get_active_enemies(self.guild.id, include_down=False)
        if enemies:
            self.add_item(TargetSelect(self))


class TurnAttackSelectionModal(discord.ui.Modal):
    def __init__(
        self,
        guild: discord.Guild,
        admin_id: int,
        actions: list[sqlite3.Row],
    ) -> None:
        super().__init__(title="턴 종료 — 공격 판정 선택")
        self.guild = guild
        self.admin_id = admin_id
        self.actions = actions

        checkbox_group = discord.ui.CheckboxGroup(
            required=False,
            min_values=0,
            max_values=len(actions),
        )
        for action in actions:
            checkbox_group.add_option(
                label=(
                    f"{member_label(guild, action['user_id'])} — {action['skill_name']}"
                )[:100],
                value=str(action["id"]),
                description=(
                    f"{action['result']} · {action['roll']}/{action['target_value']}"
                )[:100],
            )

        self.attack_choices = checkbox_group
        self.add_item(
            discord.ui.Label(
                text="공격으로 처리할 판정",
                description="체크한 판정만 데미지를 발생시킵니다.",
                component=checkbox_group,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        selected_ids = {int(value) for value in self.attack_choices.values}
        selected_actions = [
            action for action in self.actions if int(action["id"]) in selected_ids
        ]

        if not selected_actions:
            await interaction.response.send_message(
                "추가 공격 판정이 없습니다. 데미지를 계산합니다.",
                ephemeral=True,
            )
            await resolve_turn(self.guild, interaction.channel)
            return

        enemies = get_active_enemies(self.guild.id, include_down=False)
        if not enemies:
            await interaction.response.send_message(
                "공격 대상으로 지정할 수 있는 에너미가 없습니다. "
                "추가 판정은 공격으로 처리하지 않고 턴을 종료합니다.",
                ephemeral=True,
            )
            await resolve_turn(self.guild, interaction.channel)
            return

        view = TargetAssignmentView(
            guild=self.guild,
            admin_id=self.admin_id,
            actions=selected_actions,
        )
        await interaction.response.send_message(
            view.prompt_text(),
            view=view,
            ephemeral=True,
        )


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
            "이번 턴에 처리할 판정이 없습니다.", ephemeral=True
        )
        return

    # Only successful non-/공격 checks need the admin checkbox.
    optional_attack_actions = [
        action
        for action in actions
        if action["success"] and not action["direct_attack"]
    ]

    if not optional_attack_actions:
        await interaction.response.defer(ephemeral=True)
        await resolve_turn(guild, interaction.channel)
        await interaction.followup.send("턴 종료 처리 완료.", ephemeral=True)
        return

    if len(optional_attack_actions) > MAX_TURN_CHECKBOX_ACTIONS:
        await interaction.response.send_message(
            f"이번 턴의 공격 여부 확인 대상이 {len(optional_attack_actions)}개입니다. "
            f"현재 체크박스 UI는 한 번에 {MAX_TURN_CHECKBOX_ACTIONS}개까지 지원합니다. "
            "턴 판정 수를 줄인 뒤 다시 시도해주세요.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        TurnAttackSelectionModal(
            guild=guild,
            admin_id=interaction.user.id,
            actions=optional_attack_actions,
        )
    )


# ============================================================
# ERROR HANDLER
# ============================================================


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


# ============================================================
# START
# ============================================================


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")

bot.run(TOKEN)
