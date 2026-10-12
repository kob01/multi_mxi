"""语义层真值同步: ``semantic-defs/*.yaml`` -> PostgreSQL, 带版本递增与漂移检测。

这是方案里"YAML 为 source of truth, CI 同步到 PG"的那一步。三个模式:

- ``--dry-run``  : 只打印将要改什么(人 review 用);
- ``--check``    : **CI 模式** —— 真值与库不一致就退码非 0, 一个字节都不写。
  它守的是"有人改了库里没改文件"(那下一次同步会静默覆盖他的改动)与
  "有人改了文件没同步"(那线上口径和 git 里不是同一份)两种漂移;
- 默认(写模式)  : 逐行比对, 内容有变才 UPDATE 且 ``version+1``, 并写 ``sem_change_log``。

为什么不"整表重建"或"每次都 UPDATE": 前者会连人工新增的定义一起删掉;
后者会让 version 号变成"部署次数"而不是"口径变更次数", 于是 semver 一路漂,
badcase 回查时看不出到底哪一版真的不一样。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.semantic import defs as semantic_defs  # noqa: E402
from app.semantic import registry  # noqa: E402

# ``(表, 真值属性, 主键列, 参与比对的列, 有无 version 列)``: 主键用来定位行,
# 比对列决定要不要 +1 版本; version 只在三张"定义主体"表上存在(terms/枚举值没有版本号,
# 它们的变更靠 sem_change_log 追)。
_TABLES = (
    ("sem_metrics", "metrics", ("metric_id",), semantic_defs.METRIC_KEYS, True),
    ("sem_dimensions", "dimensions", ("dim_id",), semantic_defs.DIM_KEYS, True),
    ("sem_terms", "terms", ("term", "term_type", "target_id"), semantic_defs.TERM_KEYS, False),
    ("sem_dim_values", "dim_values", ("dim_id", "value"), semantic_defs.VALUE_KEYS, False),
    ("sem_metric_dim_compat", "compat", ("metric_id", "dim_id"), semantic_defs.COMPAT_KEYS, True),
    ("sem_time_semantics", "time_semantics", ("cal_type",), semantic_defs.TIME_KEYS, True),
    ("sem_row_policies", "row_policies", ("policy_id",), semantic_defs.ROW_POLICY_KEYS, True),
    ("sem_col_policies", "col_policies", ("role", "table_name"), semantic_defs.COL_POLICY_KEYS, True),
)


def _norm(value: Any) -> Any:
    """把 YAML 值转成"能与库里读出来的值直接比"的形状。

    Decimal 必须单独处理: `weight`/比率列在 PG 是 Numeric(4,3), 读出来是
    Decimal('1.000') 而 YAML 里是 1.0 —— 落回 `str()` 会比出"每行都变了", 于是
    version 号每次同步都涨, badcase 回查时看不出到底哪里改过(真坑过这件事)。
    """
    from decimal import Decimal

    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return round(float(value), 3)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 3)
    if isinstance(value, (list, tuple)):
        return [_norm(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in sorted(value.items())}
    if value is None:
        return None
    return str(value)


def _db_rows(conn, table: str, columns: tuple[str, ...], *, with_version: bool) -> list[dict[str, Any]]:
    want = list(columns) + (["version"] if with_version else [])
    select = ", ".join(f'"{c}"' if c == "column" else c for c in want)
    return [dict(row._mapping) for row in conn.execute(text(f"SELECT {select} FROM {table}"))]


def _plan(conn, bundle) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 ``(变更清单, 只在库里的行, 待插行)``(都是带 ``table``/``key`` 的结构化条目)。"""
    changes: list[dict[str, Any]] = []
    inserts: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    for table, attribute, pk, columns, with_version in _TABLES:
        wanted_keys = [c for c in columns if c not in pk]
        existing_rows = _db_rows(conn, table, columns, with_version=with_version)
        index: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in existing_rows:
            index[tuple(str(row.get(key) or "") for key in pk)] = row
        seen: set[tuple[Any, ...]] = set()
        for item in getattr(bundle, attribute):
            key = tuple(str(item.get(name) or "") for name in pk)
            seen.add(key)
            current = index.get(key)
            if current is None:
                inserts.append({"table": table, "key": key, "row": item})
                continue
            diff: dict[str, tuple[Any, Any]] = {}
            for column in wanted_keys:
                if column not in item:
                    continue  # 文件里没写这个键 = 不表态(空字段被导出时剔掉了), 不是"要清空"
                db_value = _norm(current.get(column))
                new_value = _norm(item.get(column))
                if db_value != new_value:
                    diff[column] = (db_value, new_value)
            if diff:
                changes.append({
                    "table": table, "key": key, "diff": diff,
                    "version": int(current.get("version") or 1) if with_version else 0,
                    "bump_version": with_version,
                })
        for key, row in index.items():
            if key not in seen:
                extra.append({"table": table, "key": key})
    return changes, extra, inserts


# 默认只允许 prune 这张表: 它是纯规则集(由内置真值推导), 陈旧的兼容行不仅多余而且**有害**
# —— 它会让校验器放过一个编译器必定拒绍的维度(编译器现在拿别名闸拦住了没 JOIN 的列,
# 于是那条规则会变成"校验通过 → 编译拒 → 降级沙箱"的路径)。
# terms / dim_values 不放进默认集: 那里常有真人补过的黑话与动态取值, 删错了没人知道。
PRUNE_DEFAULT_TABLES = ("sem_metric_dim_compat",)


def _prune(conn, extra, prune_tables, *, dry_run: bool) -> list[dict[str, Any]]:
    """把"真值里没有"的行从库里删掉(逐行留版本记录)。"""
    from sqlalchemy import delete

    targets = [item for item in extra if item["table"] in set(prune_tables)]
    deleted: list[dict[str, Any]] = []
    for item in targets:
        table = registry.table_obj(item["table"])
        pk_names = [c.name for c in table.primary_key.columns]
        if dry_run:
            print(f"  - {item['table']} {item['key']}")
            deleted.append(item)
            continue
        conn.execute(delete(table).where(
            *[table.c[name] == key for name, key in zip(pk_names, item["key"])]
        ))
        deleted.append(item)
    return deleted


def _apply(conn, plan_changes, plan_inserts, *, dry_run: bool) -> None:
    """执行同步: UPDATE(带 version+1) 与 INSERT。"""
    from sqlalchemy import update
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    for item in plan_inserts:
        table = registry.table_obj(item["table"])
        columns = {c.name for c in table.columns}
        row = {k: v for k, v in item["row"].items() if k in columns and k != "version"}
        if not row:
            continue
        if dry_run:
            print(f"  + {item['table']} {item['key']}")
            continue
        pk_names = [c.name for c in table.primary_key.columns]
        conn.execute(pg_insert(table).values(**row).on_conflict_do_nothing(index_elements=pk_names))

    for change in plan_changes:
        table = registry.table_obj(change["table"])
        pk_cols = [c.name for c in table.primary_key.columns]
        values = {}
        for column, (_old, new) in change["diff"].items():
            if column in {c.name for c in table.columns}:
                values[column] = new
        if change["bump_version"] and not dry_run:
            current = conn.execute(
                text(f"SELECT version FROM {change['table']} WHERE "
                     + " AND ".join(f"{name} = :k{i}" for i, name in enumerate(pk_cols))),
                {f"k{i}": key for i, key in enumerate(change["key"])},
            ).scalar()
            values["version"] = int(current or 1) + 1
        if not values:
            continue
        if dry_run:
            print(f"  ~ {change['table']} {change['key']} -> {sorted(values)}")
            continue
        stmt = update(table).where(
            *[table.c[name] == key for name, key in zip(pk_cols, change["key"])]
        ).values(**values)
        conn.execute(stmt)


def _write_change_log(conn, semver: str, changes, inserts, actor: str, note: str,
                      deletions=None) -> int:
    """每个被改/新增/删除的对象留一行 sem_change_log(catalog 级再留一行总结)。"""
    rows = []
    for item in deletions or []:
        rows.append({
            "semver": semver, "object_kind": item["table"],
            "object_id": "|".join(map(str, item["key"]))[:96],
            "version": 0, "actor": actor, "source": "yaml_sync",
            "diff_json": {"delete": True}, "note": note or "真值里已不存在, 本次 prune 删除",
        })
    for item in inserts:
        rows.append({
            "semver": semver, "object_kind": item["table"], "object_id": "|".join(map(str, item["key"])),
            "version": 1, "actor": actor, "source": "yaml_sync",
            "diff_json": {"insert": _compact(item["row"])}, "note": note,
        })
    for change in changes:
        rows.append({
            "semver": semver, "object_kind": change["table"], "object_id": "|".join(map(str, change["key"])),
            "version": int(change["version"]) + (1 if change["bump_version"] else 0),
            "actor": actor, "source": "yaml_sync",
            "diff_json": {"changed": {k: [_norm(o), _norm(n)] for k, (o, n) in change["diff"].items()}},
            "note": note,
        })
    if not rows:
        return 0
    rows.append({
        "semver": semver, "object_kind": "catalog", "object_id": "all", "version": 0,
        "actor": actor, "source": "yaml_sync",
        "diff_json": {"changed_objects": len(rows)}, "note": note,
    })
    # diff_json 先序列化成字符串再交给 CAST(:... AS JSON): 传 dict 会让 psycopg 报
    # "can't adapt type 'dict'"(裸 text() 语句不像 ORM 那样知道列是 JSON 类型)。
    payload = [{**item, "diff_json": json.dumps(item["diff_json"], ensure_ascii=False, default=str)}
               for item in rows]
    conn.execute(
        text("INSERT INTO sem_change_log (semver, object_kind, object_id, version, actor, "
             "source, diff_json, note) VALUES (:semver, :object_kind, :object_id, :version, "
             ":actor, :source, CAST(:diff_json AS JSON), :note)"),
        payload,
    )
    return len(rows)


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    """变更日志里不带超长字段(表达式够用就行, 全量定义在 YAML 里)。"""
    out = {}
    for key, value in row.items():
        text_value = json.dumps(_norm(value), ensure_ascii=False, default=str)
        out[key] = text_value if len(text_value) <= 400 else text_value[:400] + "...(截断)"
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步智能问数语义层真值 YAML 到数据库")
    parser.add_argument("--dir", default="", help="真值目录(默认取 settings.semantic_defs_dir)")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划, 不写库")
    parser.add_argument("--check", action="store_true", help="CI 模式: 有漂移就退码 1, 不写库")
    parser.add_argument("--actor", default="sync-semantic-defs", help="变更人(进 sem_change_log)")
    parser.add_argument("--note", default="", help="变更说明(建议带 PR/需求号)")
    parser.add_argument("--prune", action="store_true",
                        help='删除「真值里没有」的行(默认只删兼容规则表, 其余表一律保留人工新增)')
    parser.add_argument("--prune-tables", default="",
                        help="限定 --prune 作用范围(逗号分隔的表名, 默认 sem_metric_dim_compat)")
    args = parser.parse_args(argv)

    from app.db import sync as dbsync

    directory = Path(args.dir) if args.dir else registry.defs_dir()
    bundle = semantic_defs.load_bundle(directory)  # 内部已 validate, 不合法直接抛
    engine = dbsync.get_sync_engine()
    with engine.begin() as conn:
        changes, extra, inserts = _plan(conn, bundle)
        print(f"真值目录: {directory}")
        print(f"计划: 新增 {len(inserts)} 行, 变更 {len(changes)} 行")
        extra_labels = [f"{item['table']}:{'|'.join(item['key'])}" for item in extra]
        for line in extra_labels[:20]:
            print(f"  ? 只在库里(真值没有, 默认保留): {line}")
        if len(extra_labels) > 20:
            print(f"  ? ...另有 {len(extra_labels) - 20} 条")

        if args.check:
            if changes or inserts or extra_labels:
                print("--check: 检测到漂移, 未做任何写入。"
                      " 请修 YAML(或从库里回写: scripts.export_semantic_defs --from-db);"
                      " 如果这些行确实该消失, 加 --prune 跑一次同步。")
                return 1
            print("--check: 真值与库一致, 无漂移。")
            return 0

        _apply(conn, changes, inserts, dry_run=args.dry_run)
        prune_tables = tuple(
            name.strip() for name in (args.prune_tables or "").split(",") if name.strip()
        ) or PRUNE_DEFAULT_TABLES
        deleted = _prune(conn, extra, prune_tables, dry_run=args.dry_run) if args.prune else []
        if args.dry_run:
            print("--dry-run: 未写库(上面的 ~ 行会触发 version+1 与 sem_change_log)")
            return 0
        catalog = registry.get_catalog(force_refresh=True)
        logged = _write_change_log(conn, catalog.semver, changes, inserts, args.actor, args.note,
                                   deletions=deleted)
    print(f"同步完成: semver={catalog.semver}, 版本记录 {logged} 行")
    if changes or inserts or deleted:
        print("提示: askdata MCP server 进程里的快照要等 TTL 或重启才刷新"
              " (registry.invalidate() 只作用于本进程)。")
    if not args.prune and extra_labels:
        print(f"注意: 库里有 {len(extra_labels)} 行是真值里没有的(已保留);"
              " 陈旧的兼容规则会让校验放过编译器必拒的维度, 确认该删就加 --prune。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
