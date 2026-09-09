#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
膳待家 Umami 健康数据 CLI（单文件、零依赖，仅 Python 标准库）。

把「膳待家 Umami」健康管家的确定性数据操作打包成命令行脚本，
供 AI 编程助手（ZCode / Codex / Claude Code 等）作为 Agent Skill 调用：
所有数据读写都走本脚本落库到本地 SQLite，模型只负责理解与判断。

数据文件位置（优先级从高到低）：
  1. --db 参数
  2. 环境变量 UMAMI_HEALTH_DB
  3. 默认 ~/.umami/health.db

输出统一为 JSON：成功 {"ok": true, "data": ...}；失败 {"ok": false, "error": "..."}。
退出码：0 成功；1 参数/校验错误；2 目标不存在。

用法示例：
  python health_db.py init
  python health_db.py stats
  python health_db.py export --output umami-health.json
  python health_db.py import-preview --file umami-health.json
  python health_db.py ingredients add --name 鸡蛋 --quantity 5个
  python health_db.py diet log --meal 午餐 --foods '米饭,清蒸鲈鱼' --note "公司食堂"
  python health_db.py body log --weight 72.5 --fat 20.1
  python health_db.py foods search 番茄
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

SCHEMA_VERSION = 1
EXPORT_VERSION = 1
MEAL_TYPES = ("早餐", "午餐", "晚餐", "加餐")
GOAL_STATUSES = ("进行中", "已完成", "已暂停", "已取消")

# 各类食材冷藏参考保质期（天）；冷冻按 8 倍估算（默认 -18°C / 4°C，不另做温度修正）
SHELF_FRIDGE = {"蔬菜": 5, "水果": 7, "肉类": 3, "蛋奶": 12, "水产": 2, "主食": 30,
                "豆制品": 5, "菌菇": 5, "调味": 180, "坚果": 120, "其他": 30}
FREEZER_MULT = 8
DEFAULT_SEED_PATH = Path(__file__).resolve().parent / "seed_foods.json"


class ValidationError(Exception):
    """参数或取值不合法（退出码 1）。"""


class NotFoundError(Exception):
    """目标记录不存在（退出码 2）。"""


# ---------------------------------------------------------------- 基础工具

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return date.today().isoformat()


def require_text(field: str, value, max_len: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()) or len(value) > max_len:
        raise ValidationError(f"{field} 必须是 1 到 {max_len} 个字符")
    return value


def require_date(field: str, value) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{field} 必须是有效的 YYYY-MM-DD 日期，收到：{value!r}")
    if parsed.isoformat() != value:
        raise ValidationError(f"{field} 必须是有效的 YYYY-MM-DD 日期，收到：{value!r}")
    return value


def require_number(field: str, value, min_v: float, max_v: float, nullable: bool = False):
    if value is None and nullable:
        return None
    if isinstance(value, str):
        try:
            value = float(value) if "." in value else int(value)
        except ValueError:
            raise ValidationError(f"{field} 必须是 {min_v} 到 {max_v} 之间的数字")
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not (min_v <= value <= max_v):
        raise ValidationError(f"{field} 必须在 {min_v} 到 {max_v} 之间")
    return value


def default_zone(category: str) -> str:
    return "freezer" if category in ("肉类", "水产") else "fridge"


def shelf_life_days(category: str, zone: str) -> int:
    base = SHELF_FRIDGE.get(category, 14)
    return max(1, round(base * FREEZER_MULT) if zone == "freezer" else base)


def freshness(days_in: int, life: int) -> str:
    if days_in >= life:
        return "已过期"
    if days_in >= life * 0.7:
        return "临期"
    return "新鲜"


def parse_foods(raw: str) -> list:
    """--foods 接受 JSON 数组，也接受逗号/顿号分隔的名称简写。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationError("foods 必须包含 1 到 100 项食物")
    text = raw.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValidationError(f"foods 不是合法 JSON：{e}")
    else:
        items = [{"name": part.strip()} for part in text.replace("，", ",").split(",") if part.strip()]
    if not isinstance(items, list) or not (1 <= len(items) <= 100):
        raise ValidationError("foods 必须包含 1 到 100 项食物")
    for item in items:
        if not isinstance(item, dict):
            raise ValidationError("foods 中的每一项必须是对象")
        require_text("foods.name", item.get("name"), 120)
        q = item.get("quantity")
        if q is not None and (not isinstance(q, str) or len(q) > 80):
            raise ValidationError("foods.quantity 必须是不超过 80 个字符的字符串")
    return items


def output(data, exit_code: int = 0):
    print(json.dumps({"ok": True, "data": data}, ensure_ascii=False, indent=2, default=str))
    sys.exit(exit_code)


def fail(message: str, exit_code: int = 1):
    print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, indent=2))
    sys.exit(exit_code)
    sys.exit(exit_code)


# ---------------------------------------------------------------- 数据库

class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.isolation_level = None  # 自动提交；显式事务用 with self.conn

    def q(self, sql: str, params=()) -> list:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def run(self, sql: str, params=()) -> int:
        cur = self.conn.execute(sql, params)
        return cur.lastrowid or cur.rowcount

    def close(self):
        self.conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preferences(
  id INTEGER PRIMARY KEY, people_count INTEGER DEFAULT 2, taste_preference TEXT DEFAULT '家常',
  allergies TEXT DEFAULT '', cuisine_style TEXT DEFAULT '中餐', days INTEGER DEFAULT 7,
  height_cm REAL, age INTEGER, gender TEXT DEFAULT '', activity_level TEXT DEFAULT '久坐');
CREATE TABLE IF NOT EXISTS foods(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT NOT NULL,
  emoji TEXT NOT NULL DEFAULT '🍽️', unit TEXT NOT NULL DEFAULT '份');
CREATE INDEX IF NOT EXISTS idx_foods_category ON foods(category, id);
CREATE TABLE IF NOT EXISTS ingredients(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, quantity TEXT DEFAULT '',
  category TEXT DEFAULT '其他', source TEXT DEFAULT 'manual', zone TEXT DEFAULT 'fridge',
  added_at TEXT, note TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE INDEX IF NOT EXISTS idx_ingredients_active ON ingredients(archived_at, id);
CREATE TABLE IF NOT EXISTS diet_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, meal_type TEXT NOT NULL DEFAULT '早餐',
  foods TEXT NOT NULL DEFAULT '[]', note TEXT DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE INDEX IF NOT EXISTS idx_diet_logs_date ON diet_logs(archived_at, date);
CREATE TABLE IF NOT EXISTS workout_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, activity_type TEXT NOT NULL,
  duration_min INTEGER NOT NULL, detail TEXT DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE INDEX IF NOT EXISTS idx_workouts_active_date ON workout_logs(archived_at, date);
CREATE TABLE IF NOT EXISTS body_metrics(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, weight_kg REAL NOT NULL,
  body_fat_pct REAL, note TEXT DEFAULT '',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE INDEX IF NOT EXISTS idx_body_active_date ON body_metrics(archived_at, date);
CREATE TABLE IF NOT EXISTS health_goals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT DEFAULT '健康',
  target TEXT DEFAULT '', unit TEXT DEFAULT '', status TEXT DEFAULT '进行中',
  target_value REAL, current_value REAL, start_date TEXT, end_date TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE INDEX IF NOT EXISTS idx_goals_active ON health_goals(archived_at, id);
CREATE TABLE IF NOT EXISTS habit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, habit TEXT NOT NULL,
  value TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE INDEX IF NOT EXISTS idx_habits_active_date ON habit_logs(archived_at, date);
CREATE TABLE IF NOT EXISTS shopping_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, quantity TEXT DEFAULT '',
  checked INTEGER NOT NULL DEFAULT 0 CHECK(checked IN (0, 1)),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE TABLE IF NOT EXISTS recipes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, ingredients TEXT NOT NULL DEFAULT '[]',
  steps TEXT NOT NULL DEFAULT '[]', nutrition_estimate TEXT, source TEXT DEFAULT 'manual',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
"""


def load_seed() -> dict:
    if not DEFAULT_SEED_PATH.exists():
        raise ValidationError(f"种子数据缺失：{DEFAULT_SEED_PATH}")
    with open(DEFAULT_SEED_PATH, encoding="utf-8") as f:
        return json.load(f)


def db_path(args) -> Path:
    raw = getattr(args, "db", None) or os.environ.get("UMAMI_HEALTH_DB") \
        or str(Path.home() / ".umami" / "health.db")
    return Path(raw).expanduser()


# ---------------------------------------------------------------- 各子命令

def cmd_init(args, db: DB):
    db.conn.executescript(SCHEMA)
    version = db.one("SELECT value FROM meta WHERE key='schema_version'")
    first_run = not bool(version)
    if not version:
        db.run("INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    count = db.one("SELECT COUNT(*) AS c FROM foods")["c"]
    seeded = 0
    if count == 0:
        seed = load_seed()
        with db.conn:
            for f in seed["foods"]:
                db.run("INSERT INTO foods(name, category, emoji, unit) VALUES(?,?,?,?)",
                       (f["name"], f["category"], f["emoji"], f["unit"]))
            seeded = len(seed["foods"])
    db.run("INSERT OR IGNORE INTO preferences(id) VALUES(1)")
    output({
        "database": str(db_path(args)),
        "schema_version": SCHEMA_VERSION,
        "first_run": first_run,
        "seeded_foods": seeded,
        "message": "数据库已就绪" + (f"，已内置 {seeded} 种常见食材" if seeded else "（已有数据，未重复灌入种子）"),
    })


def _ingredient_row(row) -> dict:
    item = dict(row)
    added = (item.get("added_at") or "")[:10]
    try:
        days_in = max(0, (date.today() - date.fromisoformat(added)).days) if added else 0
    except ValueError:
        days_in = 0
    life = shelf_life_days(item.get("category") or "其他", item.get("zone") or "fridge")
    item["days_in"] = days_in
    item["shelf_life_days"] = life
    item["freshness"] = freshness(days_in, life)
    item["freshness_badge"] = f"🟢 新鲜" if item["freshness"] == "新鲜" \
        else ("🟡 临期" if item["freshness"] == "临期" else "🔴 已过期")
    return item


def cmd_ingredients_list(args, db: DB):
    sql = "SELECT * FROM ingredients WHERE archived_at IS NULL"
    params: list = []
    if args.zone:
        sql += " AND zone=?"
        params.append(args.zone)
    sql += " ORDER BY created_at DESC, id DESC"
    rows = [_ingredient_row(r) for r in db.q(sql, params)]
    near = sum(1 for r in rows if r["freshness"] != "新鲜")
    output({"items": rows, "count": len(rows), "near_expiry_or_expired": near})


def cmd_ingredients_add(args, db: DB):
    name = require_text("name", args.name, 120)
    quantity = args.quantity if args.quantity is not None else "若干"
    if not isinstance(quantity, str) or len(quantity) > 80:
        raise ValidationError("quantity 必须是不超过 80 个字符的字符串")
    food = db.one("SELECT * FROM foods WHERE name=?", (name,))
    category = args.category or (food["category"] if food else "其他")
    zone = args.zone or default_zone(category)
    ts = now()
    item_id = db.run(
        "INSERT INTO ingredients(name, quantity, category, source, zone, added_at, note, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (name, quantity, category, args.source or "manual", zone, args.added_at or today(),
         args.note, ts, ts))
    output(_ingredient_row(db.one("SELECT * FROM ingredients WHERE id=?", (item_id,))))


def cmd_ingredients_update(args, db: DB):
    if not db.one("SELECT 1 FROM ingredients WHERE id=? AND archived_at IS NULL", (args.id,)):
        raise NotFoundError(f"食材 #{args.id} 不存在")
    fields, params = [], []
    if args.name is not None:
        fields.append("name=?"); params.append(require_text("name", args.name, 120))
    if args.quantity is not None:
        if len(args.quantity) > 80:
            raise ValidationError("quantity 必须是不超过 80 个字符的字符串")
        fields.append("quantity=?"); params.append(args.quantity)
    if args.category is not None:
        fields.append("category=?"); params.append(args.category)
    if args.zone is not None:
        fields.append("zone=?"); params.append(args.zone)
    if args.note is not None:
        fields.append("note=?"); params.append(args.note)
    if not fields:
        raise ValidationError("没有要更新的字段（--name/--quantity/--category/--zone/--note）")
    fields.append("updated_at=?"); params.append(now())
    params.append(args.id)
    db.run(f"UPDATE ingredients SET {', '.join(fields)} WHERE id=?", params)
    output(_ingredient_row(db.one("SELECT * FROM ingredients WHERE id=?", (args.id,))))


def cmd_ingredients_archive(args, db: DB):
    changed = db.run("UPDATE ingredients SET archived_at=?, updated_at=? WHERE id=? AND archived_at IS NULL",
                     (now(), now(), args.id))
    if not changed:
        raise NotFoundError(f"食材 #{args.id} 不存在或已归档")
    output({"id": args.id, "archived": True})


def cmd_ingredients_clear(args, db: DB):
    if not args.yes:
        raise ValidationError("clear 会清空全部冰箱食材，确认请加 --yes（高风险操作，应先向用户确认）")
    ids = [r["id"] for r in db.q("SELECT id FROM ingredients WHERE archived_at IS NULL")]
    for i in ids:
        db.run("UPDATE ingredients SET archived_at=?, updated_at=? WHERE id=?", (now(), now(), i))
    output({"cleared": len(ids), "ids": ids})


def cmd_diet_log(args, db: DB):
    meal = args.meal
    if meal not in MEAL_TYPES:
        raise ValidationError(f"meal_type 必须是 {'、'.join(MEAL_TYPES)} 之一")
    items = parse_foods(args.foods)
    d = require_date("date", args.date or today())
    note = args.note if args.note is not None else ""
    if len(note) > 2000:
        raise ValidationError("note 必须是不超过 2000 个字符的字符串")
    ts = now()
    log_id = db.run(
        "INSERT INTO diet_logs(date, meal_type, foods, note, created_at, updated_at) VALUES(?,?,?,?,?,?)",
        (d, meal, json.dumps(items, ensure_ascii=False), note, ts, ts))
    output({"id": log_id, "date": d, "meal_type": meal, "foods": items, "note": note,
            "message": f"已记录{meal}：{'、'.join(i['name'] for i in items)}"})


def _days_clause(args, params: list) -> str:
    if getattr(args, "date", None):
        require_date("date", args.date)
        params.append(args.date)
        return " AND date=?"
    days = getattr(args, "days", None) or 7
    from datetime import timedelta
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    params.append(since)
    return " AND date>=?"


def cmd_diet_list(args, db: DB):
    params: list = []
    rows = db.q("SELECT * FROM diet_logs WHERE archived_at IS NULL" + _days_clause(args, params)
                + " ORDER BY date DESC, id DESC", params)
    for r in rows:
        r["foods"] = json.loads(r["foods"])
    output({"items": rows, "count": len(rows)})


def cmd_workouts_log(args, db: DB):
    d = require_date("date", args.date or today())
    activity = require_text("activity_type", args.activity, 120)
    duration = require_number("duration_min", args.duration, 1, 1440)
    detail = args.detail if args.detail is not None else ""
    if len(detail) > 2000:
        raise ValidationError("detail 最多 2000 个字符")
    ts = now()
    log_id = db.run(
        "INSERT INTO workout_logs(date, activity_type, duration_min, detail, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?)", (d, activity, duration, detail, ts, ts))
    output({"id": log_id, "date": d, "activity_type": activity, "duration_min": duration,
            "message": f"已记录训练：{activity} {duration} 分钟"})


def cmd_workouts_list(args, db: DB):
    params: list = []
    rows = db.q("SELECT * FROM workout_logs WHERE archived_at IS NULL" + _days_clause(args, params)
                + " ORDER BY date DESC, id DESC", params)
    total = sum(r["duration_min"] for r in rows)
    output({"items": rows, "count": len(rows), "total_minutes": total})


def cmd_body_log(args, db: DB):
    d = require_date("date", args.date or today())
    weight = require_number("weight_kg", args.weight, 20, 500)
    fat = require_number("body_fat_pct", args.fat, 1, 75, nullable=True)
    note = args.note if args.note is not None else ""
    if len(note) > 1000:
        raise ValidationError("note 最多 1000 个字符")
    ts = now()
    log_id = db.run(
        "INSERT INTO body_metrics(date, weight_kg, body_fat_pct, note, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?)", (d, weight, fat, note, ts, ts))
    output({"id": log_id, "date": d, "weight_kg": weight, "body_fat_pct": fat,
            "message": f"已记录体重 {weight}kg" + (f"、体脂 {fat}%" if fat is not None else "")})


def cmd_body_list(args, db: DB):
    params: list = []
    rows = db.q("SELECT * FROM body_metrics WHERE archived_at IS NULL" + _days_clause(args, params)
                + " ORDER BY date DESC, id DESC", params)
    trend = ""
    if len(rows) >= 2:
        newest, oldest = rows[0]["weight_kg"], rows[-1]["weight_kg"]
        delta = round(newest - oldest, 1)
        trend = f"近 {len(rows)} 次记录体重{'上升' if delta > 0 else '下降' if delta < 0 else '持平'} {abs(delta)}kg"
    output({"items": rows, "count": len(rows), "trend": trend})


def _validate_goal(d: dict, required: bool = False):
    if required or "name" in d:
        require_text("name", d.get("name"), 120)
    for field, max_len in (("category", 80), ("target", 200), ("unit", 40)):
        if field in d and d[field] is not None:
            require_text(field, d[field], max_len, allow_empty=True)
    if "status" in d and d["status"] not in GOAL_STATUSES:
        raise ValidationError(f"status 必须是 {'、'.join(GOAL_STATUSES)} 之一")
    for field in ("target_value", "current_value"):
        if field in d:
            require_number(field, d[field], 0, 1e9, nullable=True)
    for field in ("start_date", "end_date"):
        if d.get(field) is not None:
            require_date(field, d[field])
    if d.get("start_date") and d.get("end_date") and d["start_date"] > d["end_date"]:
        raise ValidationError("end_date 不能早于 start_date")


def cmd_goals_set(args, db: DB):
    data = {"name": args.name, "category": args.category or "健康", "target": args.target or "",
            "unit": args.unit or "", "status": "进行中",
            "target_value": args.target_value, "current_value": args.current_value,
            "start_date": args.start_date, "end_date": args.end_date}
    _validate_goal(data, required=True)
    ts = now()
    goal_id = db.run(
        "INSERT INTO health_goals(name, category, target, unit, status, target_value, current_value,"
        " start_date, end_date, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (data["name"], data["category"], data["target"], data["unit"], data["status"],
         data["target_value"], data["current_value"], data["start_date"], data["end_date"], ts, ts))
    output(dict(db.one("SELECT * FROM health_goals WHERE id=?", (goal_id,))),
           )


def cmd_goals_list(args, db: DB):
    rows = db.q("SELECT * FROM health_goals WHERE archived_at IS NULL ORDER BY id DESC")
    output({"items": rows, "count": len(rows),
            "active": sum(1 for r in rows if r["status"] == "进行中")})


def cmd_goals_status(args, db: DB):
    name = require_text("name", args.name, 120)
    if args.status not in GOAL_STATUSES:
        raise ValidationError(f"status 必须是 {'、'.join(GOAL_STATUSES)} 之一")
    matches = db.q("SELECT id, status FROM health_goals WHERE name=? AND archived_at IS NULL", (name,))
    if not matches:
        raise NotFoundError(f"目标「{name}」不存在")
    changes = []
    with db.conn:
        for m in matches:
            db.run("UPDATE health_goals SET status=?, updated_at=? WHERE id=?", (args.status, now(), m["id"]))
            changes.append({"id": m["id"], "from": m["status"], "to": args.status})
    output({"updated": changes})


def cmd_goals_update(args, db: DB):
    row = db.one("SELECT * FROM health_goals WHERE id=? AND archived_at IS NULL", (args.id,))
    if not row:
        raise NotFoundError(f"目标 #{args.id} 不存在")
    data = {}
    if args.name is not None: data["name"] = args.name
    if args.category is not None: data["category"] = args.category
    if args.target is not None: data["target"] = args.target
    if args.unit is not None: data["unit"] = args.unit
    if args.status is not None: data["status"] = args.status
    if args.target_value is not None: data["target_value"] = args.target_value
    if args.current_value is not None: data["current_value"] = args.current_value
    if args.start_date is not None: data["start_date"] = args.start_date
    if args.end_date is not None: data["end_date"] = args.end_date
    _validate_goal(data)
    if not data:
        raise ValidationError("没有要更新的字段")
    fields = ", ".join(f"{k}=?" for k in data)
    params = list(data.values()) + [now(), args.id]
    db.run(f"UPDATE health_goals SET {fields}, updated_at=? WHERE id=?", params)
    output(dict(db.one("SELECT * FROM health_goals WHERE id=?", (args.id,))))


def cmd_goals_archive(args, db: DB):
    changed = db.run("UPDATE health_goals SET archived_at=?, updated_at=? WHERE id=? AND archived_at IS NULL",
                     (now(), now(), args.id))
    if not changed:
        raise NotFoundError(f"目标 #{args.id} 不存在或已归档")
    output({"id": args.id, "archived": True})


def cmd_habits_log(args, db: DB):
    d = require_date("date", args.date or today())
    habit = require_text("habit", args.habit, 120)
    value = require_text("value", args.value, 200)
    ts = now()
    log_id = db.run("INSERT INTO habit_logs(date, habit, value, created_at, updated_at) VALUES(?,?,?,?,?)",
                    (d, habit, value, ts, ts))
    output({"id": log_id, "date": d, "habit": habit, "value": value,
            "message": f"已打卡：{habit} {value}"})


def cmd_habits_list(args, db: DB):
    params: list = []
    rows = db.q("SELECT * FROM habit_logs WHERE archived_at IS NULL" + _days_clause(args, params)
                + " ORDER BY date DESC, id DESC", params)
    output({"items": rows, "count": len(rows)})


def cmd_shopping_add(args, db: DB):
    name = require_text("name", args.name, 120)
    quantity = args.quantity if args.quantity is not None else ""
    if len(quantity) > 80:
        raise ValidationError("quantity 最多 80 个字符")
    food = db.one("SELECT * FROM foods WHERE name=?", (name,))
    if food and not quantity:
        quantity = f"1{food['unit']}"
    ts = now()
    item_id = db.run(
        "INSERT INTO shopping_items(name, quantity, checked, created_at, updated_at) VALUES(?,?,0,?,?)",
        (name, quantity, ts, ts))
    output({"id": item_id, "name": name, "quantity": quantity, "checked": False,
            "message": f"已加入购物清单：{name} {quantity}"})


def cmd_shopping_list(args, db: DB):
    rows = db.q("SELECT * FROM shopping_items WHERE archived_at IS NULL ORDER BY checked, id DESC")
    for r in rows:
        r["checked"] = bool(r["checked"])
    output({"items": rows, "count": len(rows),
            "unchecked": sum(1 for r in rows if not r["checked"])})


def cmd_shopping_check(args, db: DB):
    state = 0 if args.uncheck else 1
    changed = db.run("UPDATE shopping_items SET checked=?, updated_at=? WHERE id=? AND archived_at IS NULL",
                     (state, now(), args.id))
    if not changed:
        raise NotFoundError(f"购物项 #{args.id} 不存在")
    output({"id": args.id, "checked": bool(state)})


def cmd_shopping_archive(args, db: DB):
    changed = db.run("UPDATE shopping_items SET archived_at=?, updated_at=? WHERE id=? AND archived_at IS NULL",
                     (now(), now(), args.id))
    if not changed:
        raise NotFoundError(f"购物项 #{args.id} 不存在或已归档")
    output({"id": args.id, "archived": True})


def cmd_shopping_clear(args, db: DB):
    if not args.yes:
        raise ValidationError("clear 会清空全部购物清单，确认请加 --yes（高风险操作，应先向用户确认）")
    ids = [r["id"] for r in db.q("SELECT id FROM shopping_items WHERE archived_at IS NULL")]
    for i in ids:
        db.run("UPDATE shopping_items SET archived_at=?, updated_at=? WHERE id=?", (now(), now(), i))
    output({"cleared": len(ids), "ids": ids})


PREF_FIELDS = {
    "people_count": ("people_count", lambda v: require_number("people_count", v, 1, 20)),
    "taste": ("taste_preference", lambda v: require_text("taste_preference", v, 200)),
    "allergies": ("allergies", lambda v: require_text("allergies", v, 1000, allow_empty=True)),
    "cuisine": ("cuisine_style", lambda v: require_text("cuisine_style", v, 120)),
    "days": ("days", lambda v: require_number("days", v, 1, 31)),
    "height_cm": ("height_cm", lambda v: require_number("height_cm", v, 80, 250, nullable=True)),
    "age": ("age", lambda v: require_number("age", v, 1, 120, nullable=True)),
    "gender": ("gender", lambda v: require_text("gender", v, 20, allow_empty=True)),
    "activity_level": ("activity_level", lambda v: require_text("activity_level", v, 40, allow_empty=True)),
}


def cmd_prefs_get(args, db: DB):
    row = db.one("SELECT * FROM preferences WHERE id=1")
    if not row:
        raise NotFoundError("偏好记录缺失，请先运行 init")
    output(dict(row))


def cmd_prefs_set(args, db: DB):
    updates = {}
    for arg_name, (column, validate) in PREF_FIELDS.items():
        value = getattr(args, arg_name)
        if value is not None:
            updates[column] = validate(value)
    if not updates:
        raise ValidationError("没有要更新的字段（可选：" + ", ".join(PREF_FIELDS) + "）")
    fields = ", ".join(f"{k}=?" for k in updates)
    db.run(f"UPDATE preferences SET {fields} WHERE id=1", list(updates.values()))
    output(dict(db.one("SELECT * FROM preferences WHERE id=1")))


def cmd_recipes_save(args, db: DB):
    title = require_text("title", args.title, 200)

    def parse_json_field(raw, field):
        if raw is None:
            return [] if field != "nutrition_estimate" else None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValidationError(f"{field} 不是合法 JSON：{e}")
        return parsed

    ingredients = parse_json_field(args.ingredients, "ingredients")
    steps = parse_json_field(args.steps, "steps")
    nutrition = parse_json_field(args.nutrition, "nutrition_estimate")
    ts = now()
    recipe_id = db.run(
        "INSERT INTO recipes(title, ingredients, steps, nutrition_estimate, source, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (title, json.dumps(ingredients, ensure_ascii=False), json.dumps(steps, ensure_ascii=False),
         json.dumps(nutrition, ensure_ascii=False) if nutrition is not None else None,
         args.source or "agent", ts, ts))
    output({"id": recipe_id, "title": title, "message": f"已保存菜谱「{title}」"})


def _recipe_row(row) -> dict:
    item = dict(row)
    item["ingredients"] = json.loads(item["ingredients"] or "[]")
    item["steps"] = json.loads(item["steps"] or "[]")
    item["nutrition_estimate"] = json.loads(item["nutrition_estimate"]) if item["nutrition_estimate"] else None
    return item


def cmd_recipes_list(args, db: DB):
    limit = args.limit or 20
    rows = db.q("SELECT * FROM recipes WHERE archived_at IS NULL ORDER BY id DESC LIMIT ?", (limit,))
    output({"items": [_recipe_row(r) for r in rows], "count": len(rows)})


def cmd_recipes_show(args, db: DB):
    row = db.one("SELECT * FROM recipes WHERE id=? AND archived_at IS NULL", (args.id,))
    if not row:
        raise NotFoundError(f"菜谱 #{args.id} 不存在")
    output(_recipe_row(row))


def cmd_foods_search(args, db: DB):
    query = (args.query or "").strip()
    sql = "SELECT id, name, category, emoji, unit FROM foods WHERE 1=1"
    params: list = []
    if query:
        sql += " AND name LIKE ?"
        params.append(f"%{query}%")
    if args.category:
        sql += " AND category=?"
        params.append(args.category)
    sql += " ORDER BY category, id LIMIT 500"
    rows = db.q(sql, params)
    output({"items": rows, "count": len(rows)})


def cmd_foods_categories(args, db: DB):
    rows = db.q("SELECT category, COUNT(*) AS count FROM foods GROUP BY category ORDER BY id")
    output({"categories": [dict(r) for r in rows]})


def cmd_stats(args, db: DB):
    d = today()
    ingredients = [_ingredient_row(r) for r in
                   db.q("SELECT * FROM ingredients WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC")]
    near = [r for r in ingredients if r["freshness"] != "新鲜"]
    meals = db.q("SELECT meal_type, foods FROM diet_logs WHERE archived_at IS NULL AND date=?", (d,))
    for m in meals:
        m["foods"] = json.loads(m["foods"])
    workouts = db.q("SELECT * FROM workout_logs WHERE archived_at IS NULL AND date=?", (d,))
    habits = db.q("SELECT * FROM habit_logs WHERE archived_at IS NULL AND date=?", (d,))
    goals = db.q("SELECT * FROM health_goals WHERE archived_at IS NULL AND status='进行中' ORDER BY id DESC")
    body = db.q("SELECT * FROM body_metrics WHERE archived_at IS NULL ORDER BY date DESC, id DESC LIMIT 14")
    shopping = db.q("SELECT * FROM shopping_items WHERE archived_at IS NULL AND checked=0 ORDER BY id DESC")

    recent_activity = []
    for row in db.q("SELECT id, meal_type, foods, created_at FROM diet_logs "
                    "WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 12"):
        foods = json.loads(row["foods"])
        names = "、".join(item.get("name", "") for item in foods[:3] if item.get("name"))
        recent_activity.append({
            "type": "diet", "id": row["id"],
            "label": f"{row['meal_type']}：{names or '饮食记录'}",
            "occurred_at": row["created_at"],
        })
    for row in db.q("SELECT id, activity_type, duration_min, created_at FROM workout_logs "
                    "WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 12"):
        recent_activity.append({
            "type": "workout", "id": row["id"],
            "label": f"{row['activity_type']} · {row['duration_min']} 分钟",
            "occurred_at": row["created_at"],
        })
    for row in db.q("SELECT id, weight_kg, created_at FROM body_metrics "
                    "WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 12"):
        recent_activity.append({
            "type": "body", "id": row["id"],
            "label": f"体重 {row['weight_kg']} kg",
            "occurred_at": row["created_at"],
        })
    for row in db.q("SELECT id, habit, value, created_at FROM habit_logs "
                    "WHERE archived_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 12"):
        recent_activity.append({
            "type": "habit", "id": row["id"],
            "label": f"{row['habit']}：{row['value']}",
            "occurred_at": row["created_at"],
        })
    recent_activity.sort(key=lambda item: (item["occurred_at"], item["id"]), reverse=True)

    next_steps = []
    if near:
        next_steps.append({"type": "fridge", "label": f"先处理临期食材：{near[0]['name']}",
                           "reason": near[0]["freshness_badge"]})
    if not meals:
        next_steps.append({"type": "diet", "label": "记录今天第一餐", "reason": "今天还没有饮食记录"})
    if not workouts:
        next_steps.append({"type": "workout", "label": "记录一次训练或散步", "reason": "今天还没有训练记录"})
    if not habits:
        next_steps.append({"type": "habit", "label": "完成一个习惯打卡", "reason": "今天还没有习惯记录"})
    if goals:
        next_steps.append({"type": "goal", "label": f"回看目标：{goals[0]['name']}", "reason": "保持今天的一小步"})

    output({
        "date": d,
        "ingredients": {"count": len(ingredients), "near_expiry": len(near),
                        "items": ingredients[:12]},
        "today": {"meals": meals, "workouts": workouts, "habits": habits},
        "metrics": {
            "diet_kcal": None,
            "calorie_target": None,
            "meal_count": len(meals),
            "workout_minutes": sum(row["duration_min"] for row in workouts),
            "habit_completed": len(habits),
            "latest_weight": body[0]["weight_kg"] if body else None,
        },
        "next_steps": next_steps[:6],
        "recent_activity": recent_activity[:12],
        "active_goals": goals,
        "body_metrics_recent": body,
        "shopping_unchecked": shopping,
        "data_boundary": {
            "local_scripts_only": True,
            "host_ai_consent_required": True,
            "note": "本地脚本不主动调用外部 API；宿主 AI 助手负责模型与隐私授权。",
        },
    })


# ---------------------------------------------------------------- 导出 / 导入

EXPORT_SPECS = {
    "ingredients": ("ingredients", ("id", "name", "quantity", "category", "source", "zone",
                                       "added_at", "note", "created_at", "updated_at", "archived_at")),
    "dietLogs": ("diet_logs", ("id", "date", "meal_type", "foods", "note",
                                 "created_at", "updated_at", "archived_at")),
    "workouts": ("workout_logs", ("id", "date", "activity_type", "duration_min", "detail",
                                    "created_at", "updated_at", "archived_at")),
    "bodyMetrics": ("body_metrics", ("id", "date", "weight_kg", "body_fat_pct", "note",
                                       "created_at", "updated_at", "archived_at")),
    "goals": ("health_goals", ("id", "name", "category", "target", "unit", "status",
                                 "target_value", "current_value", "start_date", "end_date",
                                 "created_at", "updated_at", "archived_at")),
    "habits": ("habit_logs", ("id", "date", "habit", "value", "created_at", "updated_at", "archived_at")),
    "shoppingItems": ("shopping_items", ("id", "name", "quantity", "checked", "created_at",
                                            "updated_at", "archived_at")),
    "recipes": ("recipes", ("id", "title", "ingredients", "steps", "nutrition_estimate", "source",
                              "created_at", "updated_at", "archived_at")),
}
PREFERENCE_COLUMNS = ("id", "people_count", "taste_preference", "allergies", "cuisine_style", "days",
                      "height_cm", "age", "gender", "activity_level")
DEFAULT_PREFERENCES = {
    "people_count": 2, "taste_preference": "家常", "allergies": "", "cuisine_style": "中餐",
    "days": 7, "height_cm": None, "age": None, "gender": "", "activity_level": "久坐",
}


def _export_payload(db: DB) -> dict:
    data = {"preferences": db.one("SELECT * FROM preferences WHERE id=1") or {}}
    counts = {"preferences": 1 if data["preferences"] else 0}
    for key, (table, columns) in EXPORT_SPECS.items():
        rows = db.q(f"SELECT {', '.join(columns)} FROM {table} ORDER BY id")
        data[key] = rows
        counts[key] = len(rows)
    return {
        "exportVersion": EXPORT_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": data,
        "counts": counts,
    }


def cmd_export(args, db: DB):
    payload = _export_payload(db)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.output:
        destination = Path(args.output).expanduser()
        if destination.resolve() == db_path(args).resolve():
            raise ValidationError("导出文件不能覆盖当前 SQLite 数据库")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded + "\n", encoding="utf-8")
        output({"file": str(destination), "counts": payload["counts"],
                "message": "可恢复健康数据包已导出；未包含模型 Key 或其他系统凭据"})
    output({"package": payload, "message": "可恢复健康数据包已生成；未包含模型 Key 或其他系统凭据"})


def _read_import_package(args) -> dict:
    source = Path(args.file).expanduser()
    if not source.is_file():
        raise ValidationError(f"导入文件不存在：{source}")
    try:
        package = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"导入文件不是可读取的合法 JSON：{exc}")
    if not isinstance(package, dict) or package.get("exportVersion") != EXPORT_VERSION:
        raise ValidationError(f"只支持 exportVersion={EXPORT_VERSION} 的膳待家健康数据包")
    if package.get("schemaVersion") != SCHEMA_VERSION:
        raise ValidationError(f"数据包 schemaVersion 必须为 {SCHEMA_VERSION}")
    data = package.get("data")
    expected = {"preferences", *EXPORT_SPECS.keys()}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValidationError("数据包 data 字段不完整或包含不支持的字段，已拒绝导入")
    preferences = data["preferences"]
    if not isinstance(preferences, dict) or set(preferences) != set(PREFERENCE_COLUMNS):
        raise ValidationError("data.preferences 字段不完整或包含未知字段")
    if preferences.get("id") != 1:
        raise ValidationError("data.preferences.id 必须为 1")
    for key, (_table, columns) in EXPORT_SPECS.items():
        rows = data[key]
        if not isinstance(rows, list):
            raise ValidationError(f"data.{key} 必须是数组")
        seen = set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValidationError(f"data.{key}[{index}] 字段不完整或包含未知字段")
            row_id = row.get("id")
            if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id < 1:
                raise ValidationError(f"data.{key}[{index}].id 必须是正整数")
            if row_id in seen:
                raise ValidationError(f"data.{key} 包含重复 ID：{row_id}")
            seen.add(row_id)
            for field in ("foods", "ingredients", "steps", "nutrition_estimate"):
                if field in row and row[field] is not None:
                    try:
                        json.loads(row[field])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise ValidationError(f"data.{key}[{index}].{field} 不是合法 JSON：{exc}")
    return package


def _preferences_are_default(row: dict | None) -> bool:
    if not row:
        return True
    return all(row.get(key) == value for key, value in DEFAULT_PREFERENCES.items())


def _import_plan(db: DB, package: dict) -> dict:
    plan = {"rows": {}, "counts": {}, "conflicts": {}, "preferences_replace": False}
    total_conflicts = 0
    for key, (table, columns) in EXPORT_SPECS.items():
        existing = {row["id"] for row in db.q(f"SELECT id FROM {table}")}
        next_id = max(existing, default=0) + 1
        planned = []
        conflicts = 0
        for original in package["data"][key]:
            row = dict(original)
            if row["id"] in existing:
                conflicts += 1
                while next_id in existing:
                    next_id += 1
                row["id"] = next_id
                existing.add(next_id)
                next_id += 1
            else:
                existing.add(row["id"])
            planned.append(row)
        plan["rows"][key] = (table, columns, planned)
        plan["counts"][key] = len(planned)
        plan["conflicts"][key] = conflicts
        total_conflicts += conflicts

    current_preferences = db.one("SELECT * FROM preferences WHERE id=1")
    preferences_conflict = not _preferences_are_default(current_preferences)
    plan["preferences_replace"] = not preferences_conflict
    plan["conflicts"]["preferences"] = 1 if preferences_conflict else 0
    plan["counts"]["preferences"] = 1
    plan["total_conflicts"] = total_conflicts + (1 if preferences_conflict else 0)
    return plan


def cmd_import_preview(args, db: DB):
    package = _read_import_package(args)
    plan = _import_plan(db, package)
    output({"counts": plan["counts"], "conflicts": plan["conflicts"],
            "total_conflicts": plan["total_conflicts"],
            "will_write": False,
            "message": "预览只校验数据和冲突，不会修改数据库"})


def _create_backup(db: DB, database: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = database.with_name(f"{database.name}.backup-{timestamp}")
    backup_conn = sqlite3.connect(str(backup))
    try:
        db.conn.backup(backup_conn)
    finally:
        backup_conn.close()
    return backup


def cmd_import(args, db: DB):
    if not args.yes:
        raise ValidationError("导入会合并数据并先创建数据库备份；确认后请加 --yes")
    package = _read_import_package(args)
    plan = _import_plan(db, package)
    database = db_path(args)
    backup = _create_backup(db, database)
    try:
        db.conn.execute("BEGIN IMMEDIATE")
        if plan["preferences_replace"]:
            values = package["data"]["preferences"]
            fields = ", ".join(column for column in PREFERENCE_COLUMNS if column != "id")
            params = [values[column] for column in PREFERENCE_COLUMNS if column != "id"]
            db.conn.execute(f"UPDATE preferences SET {fields} WHERE id=1", params)
        for _key, (table, columns, rows) in plan["rows"].items():
            placeholders = ", ".join("?" for _ in columns)
            fields = ", ".join(columns)
            for row in rows:
                db.conn.execute(f"INSERT INTO {table} ({fields}) VALUES ({placeholders})",
                                 [row[column] for column in columns])
        db.conn.commit()
    except Exception:
        db.conn.rollback()
        raise
    output({"counts": plan["counts"], "conflicts": plan["conflicts"],
            "total_conflicts": plan["total_conflicts"], "backup": str(backup),
            "references_rewritten": 0,
            "message": "健康数据包已合并；原数据库备份已保留。Skill 数据表无跨表 ID 引用。"})


# ---------------------------------------------------------------- 参数解析

def add_days_arg(p, default=7):
    p.add_argument("--days", type=int, default=default, help=f"最近 N 天（默认 {default}）")
    p.add_argument("--date", help="只看指定日期 YYYY-MM-DD（优先于 --days）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="health_db", description="膳待家 Umami 健康数据 CLI")
    parser.add_argument("--db", help="SQLite 数据库路径（默认 ~/.umami/health.db，可用环境变量 UMAMI_HEALTH_DB 覆盖）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库（幂等，首次使用必须先执行）")
    sub.add_parser("stats", help="今日概览：冰箱/三餐/训练/打卡/目标/体重/购物")
    p = sub.add_parser("export", help="导出可恢复健康数据包（不含模型 Key）")
    p.add_argument("--output", help="写入 JSON 文件；不填则将数据包放在标准输出中")
    p = sub.add_parser("import-preview", help="预览健康数据包（只校验，不写数据库）")
    p.add_argument("--file", required=True, help="健康数据包 JSON 文件")
    p = sub.add_parser("import", help="合并健康数据包（先备份，需明确确认）")
    p.add_argument("--file", required=True, help="健康数据包 JSON 文件")
    p.add_argument("--yes", action="store_true", help="确认备份并合并导入")

    p = sub.add_parser("ingredients", help="冰箱食材")
    ip = p.add_subparsers(dest="action", required=True)
    a = ip.add_parser("list", help="列出全部食材（含保鲜估算）")
    a.add_argument("--zone", choices=["fridge", "freezer"], help="只看某一舱")
    a = ip.add_parser("add", help="添加食材")
    a.add_argument("--name", required=True); a.add_argument("--quantity", help="默认「若干」")
    a.add_argument("--category", help="不填则按内置食材大全自动推断")
    a.add_argument("--zone", choices=["fridge", "freezer"], help="默认：肉类/水产冷冻，其余冷藏")
    a.add_argument("--note"); a.add_argument("--added-at", help="存放日期 YYYY-MM-DD，默认今天")
    a.add_argument("--source", help="来源标记，默认 manual；AI 识别保存用 agent")
    a = ip.add_parser("update", help="更新食材")
    a.add_argument("id", type=int)
    a.add_argument("--name"); a.add_argument("--quantity"); a.add_argument("--category")
    a.add_argument("--zone", choices=["fridge", "freezer"]); a.add_argument("--note")
    a = ip.add_parser("archive", help="移除（归档）食材")
    a.add_argument("id", type=int)
    a = ip.add_parser("clear", help="清空冰箱（高风险：先向用户确认）")
    a.add_argument("--yes", action="store_true")

    p = sub.add_parser("diet", help="饮食记录")
    dp = p.add_subparsers(dest="action", required=True)
    a = dp.add_parser("log", help="记录一餐")
    a.add_argument("--meal", required=True, help="早餐/午餐/晚餐/加餐")
    a.add_argument("--foods", required=True,
                   help="JSON 数组，如 '[{\"name\":\"米饭\",\"quantity\":\"1碗\"}]'；也支持简写 '米饭,清蒸鲈鱼'")
    a.add_argument("--date", help="默认今天"); a.add_argument("--note")
    a = dp.add_parser("list", help="查询饮食记录")
    add_days_arg(a)

    p = sub.add_parser("workouts", help="训练记录")
    wp = p.add_subparsers(dest="action", required=True)
    a = wp.add_parser("log", help="记录一次训练")
    a.add_argument("--activity", required=True, help="运动类型，如 慢跑/力量训练")
    a.add_argument("--duration", required=True, help="时长（分钟，1-1440）")
    a.add_argument("--date", help="默认今天"); a.add_argument("--detail", help="细节备注")
    a = wp.add_parser("list", help="查询训练记录")
    add_days_arg(a)

    p = sub.add_parser("body", help="身体数据")
    bp = p.add_subparsers(dest="action", required=True)
    a = bp.add_parser("log", help="记录体重/体脂")
    a.add_argument("--weight", required=True, help="体重 kg（20-500）")
    a.add_argument("--fat", help="体脂率 %%（1-75）"); a.add_argument("--date", help="默认今天")
    a.add_argument("--note")
    a = bp.add_parser("list", help="查询身体数据趋势")
    add_days_arg(a, default=30)

    p = sub.add_parser("goals", help="健康目标")
    gp = p.add_subparsers(dest="action", required=True)
    a = gp.add_parser("set", help="设定目标")
    a.add_argument("--name", required=True)
    a.add_argument("--category", help="默认 健康"); a.add_argument("--target", help="描述，如 减脂到 65kg")
    a.add_argument("--unit"); a.add_argument("--target-value", type=float)
    a.add_argument("--current-value", type=float)
    a.add_argument("--start-date"); a.add_argument("--end-date")
    gp.add_parser("list", help="列出全部目标")
    a = gp.add_parser("status", help="按名称更新目标状态")
    a.add_argument("--name", required=True)
    a.add_argument("--status", required=True, help="进行中/已完成/已暂停/已取消")
    a = gp.add_parser("update", help="按 ID 更新目标")
    a.add_argument("id", type=int)
    a.add_argument("--name"); a.add_argument("--category"); a.add_argument("--target"); a.add_argument("--unit")
    a.add_argument("--status"); a.add_argument("--target-value", type=float)
    a.add_argument("--current-value", type=float); a.add_argument("--start-date"); a.add_argument("--end-date")
    a = gp.add_parser("archive", help="归档目标")
    a.add_argument("id", type=int)

    p = sub.add_parser("habits", help="习惯打卡")
    hp = p.add_subparsers(dest="action", required=True)
    a = hp.add_parser("log", help="打卡")
    a.add_argument("--habit", required=True, help="如 睡眠/饮水/心态")
    a.add_argument("--value", required=True, help="如 睡了7小时 / 8杯")
    a.add_argument("--date", help="默认今天")
    a = hp.add_parser("list", help="查询打卡记录")
    add_days_arg(a)

    p = sub.add_parser("shopping", help="购物清单")
    sp = p.add_subparsers(dest="action", required=True)
    a = sp.add_parser("add", help="加入购物清单")
    a.add_argument("--name", required=True); a.add_argument("--quantity", help="不填按食材单位补 1")
    sp.add_parser("list", help="查看购物清单")
    a = sp.add_parser("check", help="勾选/取消勾选")
    a.add_argument("id", type=int); a.add_argument("--uncheck", action="store_true")
    a = sp.add_parser("archive", help="移除（归档）购物项")
    a.add_argument("id", type=int)
    a = sp.add_parser("clear", help="清空购物清单（高风险：先向用户确认）")
    a.add_argument("--yes", action="store_true")

    p = sub.add_parser("prefs", help="个人偏好/资料")
    pp = p.add_subparsers(dest="action", required=True)
    pp.add_parser("get", help="读取个人资料")
    a = pp.add_parser("set", help="更新个人资料（只传要改的字段）")
    a.add_argument("--people-count", type=int); a.add_argument("--taste", help="口味偏好")
    a.add_argument("--allergies", help="忌口/过敏原（硬约束）"); a.add_argument("--cuisine", help="菜系")
    a.add_argument("--days", type=int, help="计划天数 1-31"); a.add_argument("--height-cm", type=float)
    a.add_argument("--age", type=int); a.add_argument("--gender")
    a.add_argument("--activity-level", help="久坐/轻度活动/中度活动/高强度")

    p = sub.add_parser("recipes", help="菜谱库")
    rp = p.add_subparsers(dest="action", required=True)
    a = rp.add_parser("save", help="保存菜谱")
    a.add_argument("--title", required=True)
    a.add_argument("--ingredients", help="JSON 数组"); a.add_argument("--steps", help="JSON 数组")
    a.add_argument("--nutrition", help="营养估算 JSON"); a.add_argument("--source")
    a = rp.add_parser("list", help="列出最近菜谱"); a.add_argument("--limit", type=int, default=20)
    a = rp.add_parser("show", help="查看菜谱详情"); a.add_argument("id", type=int)

    p = sub.add_parser("foods", help="内置食材大全（165 种）")
    fp = p.add_subparsers(dest="action", required=True)
    a = fp.add_parser("search", help="按名称搜索")
    a.add_argument("query", nargs="?", default=""); a.add_argument("--category")
    fp.add_parser("categories", help="列出全部分类")

    return parser


HANDLERS = {
    ("init", None): cmd_init,
    ("stats", None): cmd_stats,
    ("export", None): cmd_export,
    ("import-preview", None): cmd_import_preview,
    ("import", None): cmd_import,
    ("ingredients", "list"): cmd_ingredients_list,
    ("ingredients", "add"): cmd_ingredients_add,
    ("ingredients", "update"): cmd_ingredients_update,
    ("ingredients", "archive"): cmd_ingredients_archive,
    ("ingredients", "clear"): cmd_ingredients_clear,
    ("diet", "log"): cmd_diet_log,
    ("diet", "list"): cmd_diet_list,
    ("workouts", "log"): cmd_workouts_log,
    ("workouts", "list"): cmd_workouts_list,
    ("body", "log"): cmd_body_log,
    ("body", "list"): cmd_body_list,
    ("goals", "set"): cmd_goals_set,
    ("goals", "list"): cmd_goals_list,
    ("goals", "status"): cmd_goals_status,
    ("goals", "update"): cmd_goals_update,
    ("goals", "archive"): cmd_goals_archive,
    ("habits", "log"): cmd_habits_log,
    ("habits", "list"): cmd_habits_list,
    ("shopping", "add"): cmd_shopping_add,
    ("shopping", "list"): cmd_shopping_list,
    ("shopping", "check"): cmd_shopping_check,
    ("shopping", "archive"): cmd_shopping_archive,
    ("shopping", "clear"): cmd_shopping_clear,
    ("prefs", "get"): cmd_prefs_get,
    ("prefs", "set"): cmd_prefs_set,
    ("recipes", "save"): cmd_recipes_save,
    ("recipes", "list"): cmd_recipes_list,
    ("recipes", "show"): cmd_recipes_show,
    ("foods", "search"): cmd_foods_search,
    ("foods", "categories"): cmd_foods_categories,
}


def main():
    # 统一以 UTF-8 输出，避免 Windows 控制台代码页（如 GBK/936）导致中文乱码
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    path = db_path(args)
    if args.command not in ("init",) and not path.exists():
        fail(f"数据库不存在：{path}。请先运行 init 初始化。")
    db = DB(path)
    try:
        handler = HANDLERS[(args.command, getattr(args, "action", None))]
        handler(args, db)
    except ValidationError as e:
        fail(str(e), 1)
    except NotFoundError as e:
        fail(str(e), 2)
    except sqlite3.Error as e:
        fail(f"数据库错误：{e}", 1)
    except OSError as e:
        fail(f"文件错误：{e}", 1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
