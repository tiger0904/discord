from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


# ----------------------------
# 基本設定
# ----------------------------

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
TIMEZONE = os.getenv("TIMEZONE", "Asia/Taipei")

DB_PATH = Path(os.getenv("DB_PATH", "team_roles.db"))
MANAGER_ROLE_NAME = os.getenv("MANAGER_ROLE_NAME", "森蘭丸管理").strip()
TEAM_ROLE_PREFIXES = [x.strip() for x in os.getenv("TEAM_ROLE_PREFIXES", "丸子").split(",") if x.strip()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("team-role-bot")


# ----------------------------
# DB
# ----------------------------

def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db_connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                actor_id INTEGER,
                created_at TEXT NOT NULL,
                total_rows INTEGER NOT NULL,
                success_count INTEGER NOT NULL,
                not_found_count INTEGER NOT NULL,
                duplicate_count INTEGER NOT NULL,
                role_missing_count INTEGER NOT NULL,
                malformed_count INTEGER NOT NULL
            )
            """
        )


def remember_role(guild_id: int, role: discord.Role) -> None:
    with db_connect() as con:
        con.execute(
            """
            INSERT INTO managed_roles(guild_id, role_id, role_name, created_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET role_name=excluded.role_name
            """,
            (guild_id, role.id, role.name, datetime.now(ZoneInfo(TIMEZONE)).isoformat()),
        )


def managed_role_ids(guild_id: int) -> set[int]:
    with db_connect() as con:
        rows = con.execute(
            "SELECT role_id FROM managed_roles WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return {int(r["role_id"]) for r in rows}


# ----------------------------
# 名單解析 / 暱稱比對
# ----------------------------

@dataclass(slots=True)
class RosterRow:
    raw: str
    group_name: str
    discord_name: str
    game_id: str = ""


def normalize_name(value: str) -> str:
    # NFKC：把常見全形/半形差異正規化
    # casefold：英文大小寫不敏感
    # 空白統一成單一空白
    value = unicodedata.normalize("NFKC", value)
    value = " ".join(value.strip().split())
    return value.casefold()


def parse_roster(text: str) -> tuple[list[RosterRow], list[str]]:
    """
    格式：
    丸子1團
    DC名稱
    DC名稱

    丸子2團
    DC名稱

    同一 Discord 使用者可出現在不同團；(團別, DC名稱) 才是唯一關係。
    同一人在同一團重複出現時會自動去重。
    """
    rows: list[RosterRow] = []
    errors: list[str] = []
    current_role: str | None = None
    seen_pairs: set[tuple[str, str]] = set()

    prefixes = tuple(normalize_name(x) for x in TEAM_ROLE_PREFIXES)

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue

        normalized = normalize_name(line)

        if prefixes and any(normalized.startswith(p) for p in prefixes) and normalized.endswith(normalize_name("團")):
            current_role = line
            continue

        if current_role is None:
            errors.append(f"第 {line_no} 行 `{line}` 前面沒有團名，例如 `丸子1團`。")
            continue

        # 排團資料中沒有 Discord 名稱的項目不做匹配。
        if normalized in {normalize_name("未提供"), normalize_name("空缺"), normalize_name("空缺職業")}:
            continue

        pair = (normalize_name(current_role), normalized)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)

        rows.append(
            RosterRow(
                raw=line,
                group_name=current_role,
                discord_name=line,
                game_id="",
            )
        )

    return rows, errors

@dataclass(slots=True)
class MemberMatch:
    member: discord.Member | None
    status: str
    detail: str = ""


def _normalize_exact(value: str) -> str:
    """NFKC + 空白正規化，但保留英文大小寫。"""
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.strip().split())


def _build_member_indexes(
    members: Iterable[discord.Member],
) -> tuple[
    dict[str, list[discord.Member]],
    dict[str, list[discord.Member]],
    dict[str, list[discord.Member]],
    dict[str, list[discord.Member]],
    dict[int, discord.Member],
]:
    """
    五層索引：display name / username（精確與忽略大小寫）+ Discord ID。
    """
    display_exact: dict[str, list[discord.Member]] = {}
    username_exact: dict[str, list[discord.Member]] = {}
    display_folded: dict[str, list[discord.Member]] = {}
    username_folded: dict[str, list[discord.Member]] = {}
    member_by_id: dict[int, discord.Member] = {}

    for member in members:
        d_exact = _normalize_exact(member.display_name)
        u_exact = _normalize_exact(member.name)

        display_exact.setdefault(d_exact, []).append(member)
        username_exact.setdefault(u_exact, []).append(member)
        display_folded.setdefault(d_exact.casefold(), []).append(member)
        username_folded.setdefault(u_exact.casefold(), []).append(member)
        member_by_id[member.id] = member

    return display_exact, username_exact, display_folded, username_folded, member_by_id


def _unique_members(values: Iterable[discord.Member]) -> list[discord.Member]:
    by_id: dict[int, discord.Member] = {}
    for member in values:
        by_id[member.id] = member
    return list(by_id.values())


def match_member(
    query: str,
    indexes: tuple[
        dict[str, list[discord.Member]],
        dict[str, list[discord.Member]],
        dict[str, list[discord.Member]],
        dict[str, list[discord.Member]],
        dict[int, discord.Member],
    ],
) -> MemberMatch:
    """
    依優先順序比對：
    0. Discord ID（純數字或 <@ID>/<@!ID>）
    1. display_name 完全一致（NFKC/空白正規化，大小寫保留）
    2. username 完全一致（可寫 @username）
    3. display_name 大小寫不敏感
    4. username 大小寫不敏感

    每一層若唯一命中就立即採用；同層命中多個不同 Discord ID 才視為衝突。
    因此 Discord 顯示名稱 `chi` 與 `Chi` 可被分開。
    """
    display_exact, username_exact, display_folded, username_folded, member_by_id = indexes
    exact = _normalize_exact(query)

    # 支援 Discord mention：<@123> / <@!123>，以及直接貼 Discord ID。
    id_text = exact
    if id_text.startswith("<@") and id_text.endswith(">"):
        id_text = id_text[2:-1]
        if id_text.startswith("!"):
            id_text = id_text[1:]
    if id_text.isdigit():
        member = member_by_id.get(int(id_text))
        if member is not None:
            return MemberMatch(member, "ok", "Discord ID")
        return MemberMatch(None, "not_found", "Discord ID 不在目前伺服器成員中")

    # @username 僅把開頭 @ 當作輸入語法；display name 仍可正常包含其他 @ 字元。
    username_query = exact[1:] if exact.startswith("@") and len(exact) > 1 else exact
    folded = exact.casefold()
    username_folded_query = username_query.casefold()

    stages = (
        ("顯示名稱", display_exact.get(exact, [])),
        ("使用者名稱", username_exact.get(username_query, [])),
        ("顯示名稱（忽略大小寫）", display_folded.get(folded, [])),
        ("使用者名稱（忽略大小寫）", username_folded.get(username_folded_query, [])),
    )

    for label, matches in stages:
        unique = _unique_members(matches)
        if len(unique) == 1:
            return MemberMatch(unique[0], "ok", label)
        if len(unique) > 1:
            detail = "、".join(
                f"{m.display_name} (@{m.name}, {m.id})" for m in unique[:5]
            )
            return MemberMatch(
                None,
                "duplicate",
                f"{label}命中 {len(unique)} 位：{detail}",
            )

    return MemberMatch(None, "not_found", "")


def member_name_index(members: Iterable[discord.Member]) -> dict[str, list[discord.Member]]:
    """
    保留舊 helper 供其他程式碼相容使用。
    新的 /同步團員 使用 match_member() 多層比對。
    """
    index: dict[str, list[discord.Member]] = {}
    for member in members:
        key = normalize_name(member.display_name)
        index.setdefault(key, []).append(member)
    return index

def find_role_exact(guild: discord.Guild, role_name: str) -> discord.Role | None:
    wanted = normalize_name(role_name)
    matches = [r for r in guild.roles if normalize_name(r.name) == wanted]
    if len(matches) == 1:
        return matches[0]
    return None


async def load_all_members(
    guild: discord.Guild,
    timeout_seconds: float = 10.0,
) -> list[discord.Member]:
    """
    盡量取得完整伺服器成員清單，但 guild.chunk() 最多等待 10 秒。
    需要 Developer Portal 開啟 SERVER MEMBERS INTENT。
    逾時或 Gateway/API 失敗時，改用目前 member cache，避免 slash command 永久卡住。
    """
    try:
        await asyncio.wait_for(
            guild.chunk(cache=True),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        log.warning(
            "guild.chunk timed out for %s after %.1fs; using cached members (%d)",
            guild.id,
            timeout_seconds,
            len(guild.members),
        )
    except (discord.HTTPException, discord.ClientException) as exc:
        log.warning("guild.chunk failed for %s: %s", guild.id, exc)
    except Exception:
        log.exception("Unexpected guild.chunk failure for %s", guild.id)

    return list(guild.members)


# ----------------------------
# Bot 操作權限
# ----------------------------

def is_bot_manager(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not isinstance(interaction.user, discord.Member):
        return False

    # 伺服器擁有者永遠可以操作，避免管理 Role 設錯後被鎖在外面。
    if interaction.guild.owner_id == interaction.user.id:
        return True

    wanted = normalize_name(MANAGER_ROLE_NAME)
    return any(normalize_name(role.name) == wanted for role in interaction.user.roles)


def manager_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_bot_manager(interaction):
            return True
        raise app_commands.CheckFailure(
            f"需要身分組 {MANAGER_ROLE_NAME} 才能使用此指令"
        )

    return app_commands.check(predicate)


# ----------------------------
# Discord UI
# ----------------------------

class RosterModal(discord.ui.Modal, title="同步本週團員"):
    roster = discord.ui.TextInput(
        label="貼上分團名單",
        style=discord.TextStyle.paragraph,
        placeholder="丸子1團\nLeo Yan\n成員B\n\n丸子2團\n成員C",
        required=True,
        max_length=4000,
    )

    def __init__(self, bot: "TeamRoleBot"):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("這個功能只能在伺服器內使用。", ephemeral=True)
            return

        report = await self.bot.sync_roster(
            guild=interaction.guild,
            roster_text=str(self.roster),
            actor_id=interaction.user.id,
        )
        await interaction.followup.send(report[:2000], ephemeral=True)


class TeamRoleBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.tz = ZoneInfo(TIMEZONE)

    async def setup_hook(self) -> None:
        init_db()

        if GUILD_ID:
            guild_obj = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            log.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Global slash commands synced")


    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) | TeamRoleBot v9", self.user, self.user.id if self.user else "?")

    async def clear_managed_roles(self, guild: discord.Guild) -> tuple[int, int, list[str]]:
        ids = managed_role_ids(guild.id)
        if not ids:
            return 0, 0, []

        roles = [r for r in guild.roles if r.id in ids]
        removed = 0
        failed = 0
        errors: list[str] = []

        for role in roles:
            # Bot 無法修改高於自己最高 Role 的身分組。
            if guild.me is None or role >= guild.me.top_role:
                failed += 1
                errors.append(f"{role.name}：Bot 身分組排序不夠高")
                continue

            # copy list，避免 role.members 在操作時變動
            for member in list(role.members):
                try:
                    await member.remove_roles(
                        role,
                        reason="管理員手動清除團隊身分組",
                    )
                    removed += 1
                    await asyncio.sleep(0.05)
                except (discord.Forbidden, discord.HTTPException) as exc:
                    failed += 1
                    errors.append(f"{member.display_name} / {role.name}：{type(exc).__name__}")

        return removed, failed, errors

    async def sync_roster(
        self,
        guild: discord.Guild,
        roster_text: str,
        actor_id: int | None,
    ) -> str:
        rows, malformed = parse_roster(roster_text)
        members = await load_all_members(guild)
        indexes = _build_member_indexes(members)

        successes: list[str] = []
        not_found: list[str] = []
        duplicates: list[str] = []
        role_missing: list[str] = []
        permission_errors: list[str] = []

        for row in rows:
            match = match_member(row.discord_name, indexes)

            if match.status == "not_found" or match.member is None and match.status != "duplicate":
                not_found.append(f"{row.discord_name}（{row.game_id}）")
                continue

            if match.status == "duplicate":
                duplicates.append(
                    f"{row.discord_name}：{match.detail}"
                )
                continue

            member = match.member
            assert member is not None
            role = find_role_exact(guild, row.group_name)

            if role is None:
                role_missing.append(row.group_name)
                continue

            if guild.me is None or role >= guild.me.top_role:
                permission_errors.append(
                    f"{row.group_name}：Bot 最高身分組必須排在此 Role 上方"
                )
                continue

            try:
                if role not in member.roles:
                    await member.add_roles(
                        role,
                        reason=f"團員名單同步，由 {actor_id or 'scheduler'} 執行",
                    )
                remember_role(guild.id, role)
                successes.append(
                    f"{row.discord_name} → {row.group_name}"
                )
                log.info(
                    "Roster match: %r -> %s (@%s, %s) via %s -> %s",
                    row.discord_name,
                    member.display_name,
                    member.name,
                    member.id,
                    match.detail,
                    row.group_name,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                permission_errors.append(
                    f"{row.discord_name} → {row.group_name}：{type(exc).__name__}"
                )

        with db_connect() as con:
            con.execute(
                """
                INSERT INTO sync_log(
                    guild_id, actor_id, created_at, total_rows,
                    success_count, not_found_count, duplicate_count,
                    role_missing_count, malformed_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild.id,
                    actor_id,
                    datetime.now(self.tz).isoformat(),
                    len(rows) + len(malformed),
                    len(successes),
                    len(not_found),
                    len(duplicates),
                    len(role_missing),
                    len(malformed),
                ),
            )

        def section(title: str, values: list[str], limit: int = 12) -> list[str]:
            if not values:
                return []
            unique = list(dict.fromkeys(values))
            shown = unique[:limit]
            out = [f"\n**{title} ({len(unique)})**"]
            out += [f"• {v}" for v in shown]
            if len(unique) > limit:
                out.append(f"• …另有 {len(unique) - limit} 筆")
            return out

        out = [
            "## 團員身分組同步完成",
            f"✅ 成功關係：**{len(successes)}**",
            f"🔎 找不到暱稱：**{len(not_found)} 筆 / {len(dict.fromkeys(not_found))} 個唯一項目**",
            f"👥 同名衝突：**{len(duplicates)} 筆 / {len(dict.fromkeys(duplicates))} 個唯一項目**",
            f"🏷️ 找不到身分組：**{len(set(role_missing))}**",
            f"🧱 權限/排序錯誤：**{len(permission_errors)}**",
            f"📝 格式錯誤：**{len(malformed)}**",
        ]
        out += section("找不到暱稱", not_found)
        out += section("同名衝突", duplicates)
        out += section("找不到身分組", role_missing)
        out += section("權限問題", permission_errors)
        out += section("格式錯誤", malformed)

        return "\n".join(out)



bot = TeamRoleBot()



@bot.tree.command(name="同步團員", description="貼上本週名單，自動依伺服器暱稱分配團隊身分組")
@manager_only()
@app_commands.guild_only()
async def sync_members(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(RosterModal(bot))


@bot.tree.command(name="清空團隊身分組", description="立刻清除 Bot 曾管理過的團隊身分組成員")
@manager_only()
@app_commands.guild_only()
async def clear_team_roles(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("只能在伺服器內使用。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    removed, failed, errors = await bot.clear_managed_roles(interaction.guild)

    msg = [
        "## 團隊身分組清理完成",
        f"✅ 已移除：**{removed}** 個身分組關聯",
        f"⚠️ 失敗：**{failed}**",
    ]
    if errors:
        msg.append("\n" + "\n".join(f"• {x}" for x in errors[:20]))

    await interaction.followup.send("\n".join(msg)[:2000], ephemeral=True)


@bot.tree.command(name="團隊身分組狀態", description="查看 Bot 目前記住哪些團隊身分組")
@manager_only()
@app_commands.guild_only()
async def role_status(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("只能在伺服器內使用。", ephemeral=True)
        return

    ids = managed_role_ids(interaction.guild.id)
    roles = [r for r in interaction.guild.roles if r.id in ids]

    if not roles:
        await interaction.response.send_message(
            "目前還沒有已管理的團隊身分組。先執行一次 `/同步團員`。",
            ephemeral=True,
        )
        return

    text = "\n".join(f"• {r.name} — {len(r.members)} 人" for r in roles)
    await interaction.response.send_message(
        "## Bot 管理中的團隊身分組\n" + text,
        ephemeral=True,
    )


@bot.tree.command(name="取得成員身分組", description="取得伺服器所有成員目前擁有的身分組，匯出 CSV")
@manager_only()
@app_commands.guild_only()
async def export_member_roles(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("只能在伺服器內使用。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    members = await load_all_members(interaction.guild)

    # UTF-8-SIG 讓 Windows Excel 直接開啟中文 CSV 時不易亂碼。
    text_buffer = io.StringIO(newline="")
    writer = csv.writer(text_buffer)
    writer.writerow([
        "Discord顯示名稱",
        "Discord使用者名稱",
        "Discord ID",
        "是否Bot",
        "身分組數量",
        "所有身分組",
    ])

    member_count = 0
    human_count = 0
    bot_count = 0

    # 依顯示名稱排序，輸出時比較好找。
    for member in sorted(members, key=lambda m: normalize_name(m.display_name)):
        member_count += 1
        if member.bot:
            bot_count += 1
        else:
            human_count += 1

        # 排除 @everyone；其餘 Role 依 Discord 角色階層由高到低列出。
        roles = [role for role in reversed(member.roles) if not role.is_default()]
        role_names = [role.name for role in roles]

        writer.writerow([
            member.display_name,
            member.name,
            str(member.id),
            "是" if member.bot else "否",
            len(role_names),
            " | ".join(role_names),
        ])

    csv_bytes = ("\ufeff" + text_buffer.getvalue()).encode("utf-8")
    file = discord.File(
        io.BytesIO(csv_bytes),
        filename=f"member_roles_{interaction.guild.id}.csv",
    )

    await interaction.followup.send(
        (
            "## 成員身分組取得完成\n"
            f"👥 全部成員：**{member_count}**\n"
            f"👤 真人帳號：**{human_count}**\n"
            f"🤖 Bot 帳號：**{bot_count}**\n\n"
            "CSV 已包含：顯示名稱、使用者名稱、Discord ID、是否 Bot、"
            "身分組數量、所有身分組。"
        ),
        file=file,
        ephemeral=True,
    )


@bot.tree.command(
    name="同步現有團隊身分組",
    description="掃描目前伺服器成員已有的團隊 Role，匯入 Bot 管理資料庫"
)
@manager_only()
@app_commands.guild_only()
async def sync_existing_team_roles(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("只能在伺服器內使用。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = interaction.guild
    members = await load_all_members(guild)

    # 只把符合指定前綴的 Role 視為團隊 Role。
    # 預設 TEAM_ROLE_PREFIXES=丸子，可在 .env 改成：
    # TEAM_ROLE_PREFIXES=丸子,女皇,遠征
    prefixes = tuple(normalize_name(x) for x in TEAM_ROLE_PREFIXES)

    candidate_roles = []
    for role in guild.roles:
        if role.is_default():
            continue
        normalized = normalize_name(role.name)
        if prefixes and any(normalized.startswith(prefix) for prefix in prefixes):
            candidate_roles.append(role)

    if not candidate_roles:
        await interaction.followup.send(
            "找不到符合團隊 Role 前綴的身分組。\n"
            f"目前設定：`TEAM_ROLE_PREFIXES={','.join(TEAM_ROLE_PREFIXES)}`",
            ephemeral=True,
        )
        return

    candidate_role_ids = {role.id for role in candidate_roles}
    role_member_counts = {role.id: 0 for role in candidate_roles}
    member_role_pairs = 0
    members_with_team_role = set()

    # 單次掃描 member.roles，避免「Role 數 × 成員數」的巢狀掃描。
    for member in members:
        member_has_team_role = False
        for role in member.roles:
            if role.id not in candidate_role_ids:
                continue
            role_member_counts[role.id] += 1
            member_role_pairs += 1
            member_has_team_role = True
        if member_has_team_role:
            members_with_team_role.add(member.id)

    # 匯入 managed_roles，讓之後清除功能能管理「Bot 以前沒記到」的團隊 Role。
    # 直接沿用 remember_role()，確保 created_at 等欄位完整。
    synced = 0
    for role in candidate_roles:
        if role_member_counts.get(role.id, 0) <= 0:
            continue
        remember_role(guild.id, role)
        synced += 1

    active_roles = [
        role for role in candidate_roles
        if role_member_counts.get(role.id, 0) > 0
    ]

    lines = [
        "## 現有團隊 Role 同步完成",
        f"已納入管理的團隊 Role：**{len(active_roles)}**",
        f"目前至少有一個團隊 Role 的成員：**{len(members_with_team_role)}**",
        f"成員 × Role 關係數：**{member_role_pairs}**",
        "",
        "### 已同步",
    ]

    for role in sorted(active_roles, key=lambda r: r.position, reverse=True):
        lines.append(f"- `{role.name}`：{role_member_counts[role.id]} 人")

    lines.extend([
        "",
        "這些 Role 現在已寫入 Bot 的 managed_roles。",
        "之後執行 `/清空團隊身分組` 時，會一起清除這些過往已存在但 Bot 原本沒記憶到的團隊 Role。",
    ])

    message = "\n".join(lines)
    if len(message) > 1900:
        # Avoid Discord message length limit.
        message = "\n".join(lines[:20]) + "\n\n（團隊 Role 太多，僅顯示前段；同步仍已完整完成。）"

    await interaction.followup.send(message, ephemeral=True)


@bot.tree.error
async def global_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    # 讓所有 slash command 即使出錯也一定回覆，不再只顯示「該申請未受回應」。
    original = getattr(error, "original", None)
    shown_error = original or error

    if isinstance(error, app_commands.CheckFailure):
        message = (
            f"❌ 你沒有使用此 Bot 管理指令的權限。\n"
            f"需要身分組：`{MANAGER_ROLE_NAME}`，或必須是伺服器擁有者。"
        )
    else:
        log.error(
            "Slash command failed: %s",
            repr(shown_error),
            exc_info=(type(shown_error), shown_error, shown_error.__traceback__),
        )
        message = (
            "❌ 指令執行失敗。\n"
            f"錯誤：`{type(shown_error).__name__}`\n"
            "詳細錯誤已寫入 Bot PowerShell 視窗。"
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.exception("Failed to send command error response")


if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("缺少 DISCORD_TOKEN，請先設定 .env")
    bot.run(TOKEN)
