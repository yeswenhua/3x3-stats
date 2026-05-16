"""
  3X3 数据提取脚本
  从 3X3 APP 的 SQLite 备份数据库中提取统计数据，输出为前端可视化的 JSON

  用法:
    python extract.py today3x3-db       # 从本地文件读取
    python extract.py --webdav URL USER PASS  # 从坚果云 WebDAV 下载后处理

  输出: 标准输出 JSON

  关键数据结构:
    调度 (C_Schedule) → 标签 (ScheduleWithTagCrossRef → C_TAG)
                      → Emoji (EmojiWithTagCrossRef → C_Emoji)
                      → 分组 (C_Emoji_Group)
"""

import sqlite3
import json
import sys
import os
import re
import urllib.request
import base64
from datetime import datetime, timedelta
from collections import defaultdict

# 清理文本中的控制字符（保留换行和制表符）
def sanitize(text):
    if not text:
        return text
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', text)


def load_from_webdav(url, user, password):
    """从 WebDAV 下载 SQLite 数据库到临时文件"""
    cache_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "today3x3-db")
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)

    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")

    with urllib.request.urlopen(req) as resp:
        with open(cache_path, "wb") as f:
            f.write(resp.read())
    return cache_path


def extract(db_path):
    """主提取逻辑"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # ─────────────────────────────────────────────
    # 1. 读取分类层级: 分组 → Emoji → 标签
    # ─────────────────────────────────────────────
    groups = {}
    for r in c.execute("SELECT emoji_group_id, group_name FROM C_Emoji_Group").fetchall():
        groups[r["emoji_group_id"]] = {
            "id": r["emoji_group_id"],
            "name": r["group_name"]
        }

    emojis = {}
    for r in c.execute("SELECT emoji_id, emoji, desc, emoji_group FROM C_Emoji").fetchall():
        gid = r["emoji_group"]
        emojis[r["emoji_id"]] = {
            "id": r["emoji_id"],
            "icon": r["emoji"],
            "name": r["desc"] or r["emoji"],
            "group_id": gid,
            "group_name": groups.get(gid, {}).get("name", "其他") if gid else "其他"
        }

    tags = {}
    for r in c.execute("SELECT tag_id, tag_name, tag_color, tag_bg_color FROM C_TAG").fetchall():
        tags[r["tag_id"]] = {
            "id": r["tag_id"],
            "name": r["tag_name"],
            "color": r["tag_color"],
            "bg_color": r["tag_bg_color"]
        }

    # 建立 Tag → Emoji 映射 (通过 EmojiWithTagCrossRef)
    tag_to_emoji = {}
    for r in c.execute("SELECT tag_id, emoji_id FROM EmojiWithTagCrossRef").fetchall():
        tag_to_emoji[r["tag_id"]] = r["emoji_id"]

    # 辅助函数: 将毫秒时间戳转为 ISO 周标记
    def _to_week_key(ts_ms):
        dt = datetime.fromtimestamp(ts_ms / 1000)
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    # ─────────────────────────────────────────────
    # 2. 读取所有调度记录并解析分类
    # ─────────────────────────────────────────────
    schedules = c.execute("""
        SELECT s.schedule_id, s.create_date, s.end_date, s.create_date_key,
               s.schedule_title, s.schedule_comment, s.emoji_id,
               (SELECT st.tag_id FROM ScheduleWithTagCrossRef st
                WHERE st.schedule_id = s.schedule_id LIMIT 1) as tag_id
        FROM C_Schedule s
    """).fetchall()

    blocks = []
    uncategorized_count = 0
    zero_duration_count = 0

    for s in schedules:
        duration_ms = (s["end_date"] or 0) - (s["create_date"] or 0)
        if duration_ms <= 0:
            zero_duration_count += 1
            continue

        duration_min = round(duration_ms / 60000, 1)

        # 解析分类：标签名 = 分类名，Emoji 提供图标和分组
        category_name = "未分类"
        category_icon = "📋"
        group_name = "其他"

        tag_id = s["tag_id"]
        emoji_id = s["emoji_id"]

        # 通过 ScheduleWithTagCrossRef → Tag → Emoji (可能)
        linked_emoji = None
        if tag_id and tag_id in tag_to_emoji:
            linked_emoji = tag_to_emoji[tag_id]

        # 以标签名为分类名
        if tag_id and tag_id in tags:
            category_name = tags[tag_id]["name"]
            # 尝试用 Emoji 图标
            emoji_for_icon = linked_emoji or emoji_id
            if emoji_for_icon and emoji_for_icon in emojis:
                cat = emojis[emoji_for_icon]
                category_icon = cat["icon"]
                group_name = cat["group_name"]
        elif emoji_id and emoji_id in emojis:
            # 没有标签但有直接 Emoji（少数情况）
            cat = emojis[emoji_id]
            category_name = cat["name"]
            category_icon = cat["icon"]
            group_name = cat["group_name"]
        else:
            uncategorized_count += 1

        blocks.append({
            "date": s["create_date_key"],
            "ts": s["create_date"],
            "week": _to_week_key(s["create_date"]),
            "month": s["create_date_key"][:7],
            "year": s["create_date_key"][:4],
            "title": sanitize(s["schedule_title"] or ""),
            "duration_min": duration_min,
            "category": sanitize(category_name),
            "icon": category_icon,
            "group": sanitize(group_name)
        })

    # ─────────────────────────────────────────────
    # 3. 构建分类列表 (按分组组织)
    # ─────────────────────────────────────────────
    category_set = {}
    for b in blocks:
        key = b["category"]
        if key not in category_set:
            category_set[key] = {
                "name": key,
                "icon": b["icon"],
                "group": b["group"]
            }

    categories = sorted(category_set.values(), key=lambda x: (x["group"], x["name"]))

    # ─────────────────────────────────────────────
    # 4. 预计算各时间粒度的聚合数据
    # ─────────────────────────────────────────────
    def aggregate(blocks, key_fn):
        """按 key_fn 分组，统计每个分类的时长"""
        result = defaultdict(lambda: {"categories": defaultdict(float), "total": 0.0, "count": 0})
        for b in blocks:
            k = key_fn(b)
            result[k]["categories"][b["category"]] += b["duration_min"]
            result[k]["total"] += b["duration_min"]
            result[k]["count"] += 1
        # 转换为可序列化的格式
        out = []
        for k in sorted(result.keys()):
            entry = dict(result[k])
            entry["categories"] = dict(entry["categories"])
            # 保留 key 信息
            if isinstance(k, str) and "-" in k:
                entry["key"] = k
            else:
                entry["key"] = str(k)
            # 格式化为前端友好的数值
            for cat in list(entry["categories"].keys()):
                entry["categories"][cat] = round(entry["categories"][cat], 1)
            entry["total"] = round(entry["total"], 1)
            out.append(entry)
        return out

    daily = aggregate(blocks, lambda b: b["date"])

    weekly = aggregate(blocks, lambda b: _to_week_key(b["ts"]))

    monthly = aggregate(blocks, lambda b: b["date"][:7])

    yearly = aggregate(blocks, lambda b: b["date"][:4])

    # ─────────────────────────────────────────────
    # 5. 组装输出
    # ─────────────────────────────────────────────
    output = {
        "meta": {
            "generated_at": datetime.now().isoformat(),
            "date_range": {
                "from": blocks[0]["date"] if blocks else None,
                "to": blocks[-1]["date"] if blocks else None
            },
            "total_blocks": len(blocks),
            "total_hours": round(sum(b["duration_min"] for b in blocks) / 60, 1),
            "uncategorized": uncategorized_count,
            "zero_duration_skipped": zero_duration_count,
            "source_db_size_mb": round(os.path.getsize(db_path) / 1024 / 1024, 1)
        },
        "categories": categories,
        "blocks": blocks,
        "aggregates": {
            "daily": daily,
            "weekly": weekly,
            "monthly": monthly,
            "yearly": yearly
        }
    }

    conn.close()
    return output


# ─── CLI 入口 ───────────────────────────────────

def main():
    # Windows 下强制 UTF-8 输出，确保 Emoji 正常显示
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print("用法: python extract.py <db_path>", file=sys.stderr)
        print("       python extract.py --webdav <url> <user> <password>", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--webdav":
        if len(sys.argv) != 5:
            print("--webdav 需要: url user password", file=sys.stderr)
            sys.exit(1)
        db_path = load_from_webdav(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        db_path = sys.argv[1]

    if not os.path.exists(db_path):
        print(f"文件不存在: {db_path}", file=sys.stderr)
        sys.exit(1)

    result = extract(db_path)

    # 输出到文件或标准输出
    out_path = None
    for i, arg in enumerate(sys.argv):
        if arg == '-o' and i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]
            break

    if out_path:
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Written to {out_path}", file=sys.stderr)
    else:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
