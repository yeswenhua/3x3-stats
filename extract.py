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
    # 1. 读取 tag_groups.json 分组配置，建立 tag → group 映射
    # ─────────────────────────────────────────────
    tag_to_group = {}   # tag_name -> {name, color}
    hierarchy = {}      # parent_tag -> [child_tags]
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tag_groups_path = os.path.join(script_dir, "tag_groups.json")
    if os.path.exists(tag_groups_path):
        with open(tag_groups_path, encoding="utf-8") as f:
            cfg = json.load(f)
        for gname, ginfo in cfg["groups"].items():
            def _walk_tags(entries):
                for item in entries:
                    if isinstance(item, str):
                        tag_to_group[item] = {"name": gname, "color": ginfo["color"]}
                    elif isinstance(item, dict):
                        for parent, children in item.items():
                            tag_to_group[parent] = {"name": gname, "color": ginfo["color"]}
                            _walk_tags(children)
            _walk_tags(ginfo["tags"])
        # 提取层级关系
        def _extract_tree(entries):
            for item in entries:
                if isinstance(item, dict):
                    for parent, children in item.items():
                        hierarchy[parent] = []
                        for c in children:
                            if isinstance(c, str):
                                hierarchy[parent].append(c)
                            elif isinstance(c, dict):
                                hierarchy[parent].extend(list(c.keys()))
                                _extract_tree([c])
        for gname, ginfo in cfg["groups"].items():
            _extract_tree(ginfo["tags"])

    # 从数据库中读取标签列表
    all_tags = {}
    for r in c.execute("SELECT tag_id, tag_name FROM C_TAG").fetchall():
        all_tags[r["tag_id"]] = r["tag_name"]

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

        # 解析分类：从 tag_groups.json 映射查找
        tag_id = s["tag_id"]
        category_name = "未分类"
        group_name = "其他"
        group_color = "#94a3b8"

        if tag_id and tag_id in all_tags:
            tagname = all_tags[tag_id]
            category_name = tagname
            if tagname in tag_to_group:
                grp = tag_to_group[tagname]
                group_name = grp["name"]
                group_color = grp["color"]
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
            "group": sanitize(group_name),
            "group_color": group_color
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
                "color": b["group_color"],
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
    # 从 blocks 中提取去重的 group 信息
    group_info = {}
    for b in blocks:
        g = b["group"]
        if g not in group_info:
            group_info[g] = {"name": g, "color": b["group_color"]}

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
        "group_info": list(group_info.values()),
        "categories": categories,
        "hierarchy": hierarchy,
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
