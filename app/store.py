import sqlite3, json, hashlib
from datetime import datetime, timezone
from app import config
from app.config import RULE_VERSION
from app.errors import BusinessError
from app import identity


def connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.DATA_DIR / "runs.sqlite", timeout=15)
    db.row_factory = sqlite3.Row
    _schema(db)
    return db


def _schema(db):
    db.execute(
        "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT NOT NULL,"
        " result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,"
        " decision TEXT NOT NULL DEFAULT '待复核', created TEXT NOT NULL)"
    )
    # 升级旧库：归属与导出列在旧快照中不存在，逐列补齐。
    existing = {r[1] for r in db.execute("PRAGMA table_info(runs)")}
    if "attribution" not in existing:
        db.execute("ALTER TABLE runs ADD COLUMN attribution TEXT")
    db.execute(
        "CREATE TABLE IF NOT EXISTS clients ("
        "client_id TEXT PRIMARY KEY, name TEXT NOT NULL, token TEXT NOT NULL UNIQUE)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS sample_aliases ("
        "client_id TEXT NOT NULL, original_sample_id TEXT NOT NULL,"
        " alias TEXT NOT NULL, seq INTEGER NOT NULL,"
        " created TEXT NOT NULL,"
        " PRIMARY KEY(client_id, original_sample_id),"
        " UNIQUE(client_id, alias))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS exports ("
        "run_id TEXT NOT NULL, client_id TEXT NOT NULL,"
        " created TEXT NOT NULL, rows_exported INTEGER NOT NULL,"
        " aliases_minted INTEGER NOT NULL, withheld_unassigned INTEGER NOT NULL,"
        " PRIMARY KEY(run_id, client_id))"
    )
    identity.seed_clients(db)
    db.commit()


def save(text, result, client_id=None, line_clients=None):
    digest = hashlib.sha256((RULE_VERSION + "\n" + text).encode()).hexdigest()
    ident = digest[:16]
    with connect() as db:
        known_ids = identity.known_client_ids(db)
        attribution = identity.build_attribution(
            text, known_ids, client_id=client_id, line_clients=line_clients)
        payload = json.dumps(dict(result, rule_version=RULE_VERSION), ensure_ascii=False)
        created = datetime.now(timezone.utc).isoformat()
        db.execute(
            "INSERT OR IGNORE INTO runs(id,digest,result,created,attribution)"
            " VALUES(?,?,?,?,?)",
            (ident, digest, payload, created,
             json.dumps(attribution, ensure_ascii=False, sort_keys=True)),
        )
        row = db.execute("SELECT attribution FROM runs WHERE id=?", (ident,)).fetchone()
        stored = json.loads(row[0]) if row and row[0] else {}
        merged = _merge_attribution(stored, attribution)
        if merged != stored:
            # 未归属（UNASSIGNED）表示归属缺失，允许后续补登；
            # 已确认的机构归属受保护，不被重复提交覆盖。
            db.execute("UPDATE runs SET attribution=? WHERE id=?",
                       (json.dumps(merged, ensure_ascii=False, sort_keys=True), ident))
        db.commit()  # 显式落盘，保证后续独立连接立即可见
    return get(ident)


def _merge_attribution(stored, incoming):
    # 归属表统一以字符串物理行号为键（入库 JSON 序列化后也是字符串）。
    stored = {str(k): v for k, v in stored.items()}
    incoming = {str(k): v for k, v in incoming.items()}
    merged = dict(stored)
    for line, owner in incoming.items():
        if owner == identity.UNASSIGNED:
            # 未归属表示本次提交没有该行走访信息，保留既有结论而非冲掉它。
            merged.setdefault(line, identity.UNASSIGNED)
            continue
        previous = merged.get(line)
        if previous and previous != identity.UNASSIGNED and previous != owner:
            raise BusinessError(
                "OWNERSHIP_CONFLICT",
                f"物理行 {line} 已归属 {previous}，不能改挂 {owner}", 409)
        if previous != owner:
            merged[line] = owner
    return merged


def get(ident):
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise BusinessError("NOT_FOUND", "批次不存在", 404)
    item = dict(row)
    item["result"] = json.loads(item["result"])
    item["rule_version"] = item["result"].get("rule_version", "2026.11.18.1")
    # 升级前的旧记录没有归属列：按未归属处理，实验室仍可复核全部原始记录，
    # 但任何委托方导出都会被拦截，杜绝猜测归属导致的整行泄露。
    item["attribution"] = (
        json.loads(item["attribution"]) if item.get("attribution") else {}
    )
    return item


def history(principal=None):
    with connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id,revision,decision,created,attribution FROM runs"
            " ORDER BY created DESC LIMIT 40")]
    if not principal or principal.get("role") != "client":
        return rows  # 实验室内部：全量可见，含归属分布
    client_id = principal["client_id"]
    visible = []
    for item in rows:
        owners = set(json.loads(item["attribution"]).values()) if item.get("attribution") else set()
        if client_id in owners:
            item["attribution"] = None  # 委托方之间不可见彼此的归属分布
            visible.append(item)
    return visible


def review(ident, revision, decision):
    if not isinstance(decision, str) or decision not in {"通过", "退回"}:
        raise BusinessError("DECISION", "复核决定无效")
    current = get(ident)
    if decision == "通过" and current["result"].get("blocked"):
        raise BusinessError("BLOCKED", "存在阻断项，请先修正数据", 409)
    with connect() as db:
        changed = db.execute(
            "UPDATE runs SET revision=revision+1,decision=? WHERE id=? AND revision=?",
            (decision, ident, revision),
        ).rowcount
        if not changed:
            raise BusinessError("STALE", "版本已更新，请刷新", 409)
        db.commit()
    return get(ident)


def prepare_client_export(ident, principal):
    """为委托方生成导出视图并登记导出；重试同一批次复用全部既有编号与登记。"""
    client_id = principal["client_id"]
    with connect() as db:
        row = db.execute("SELECT * FROM runs WHERE id=?", (ident,)).fetchone()
        if row is None:
            # 对委托方统一返回不存在，避免通过接口探测其他机构的批次。
            raise BusinessError("NOT_FOUND", "批次不存在或无权访问", 404)
        run = dict(row)
        run["result"] = json.loads(run["result"])
        run["rule_version"] = run["result"].get("rule_version", RULE_VERSION)
        attribution = json.loads(run["attribution"]) if run["attribution"] else {}

        # 只要该批次还有任何物理数据行未确认归属（含标曲、空白等不进入结果的中间行），
        # 一律阻止外发——归属未核清时放行等于允许猜测，可能把整行泄露给错误机构。
        unassigned_lines = sorted(
            int(line) for line, owner in attribution.items()
            if owner in (identity.UNASSIGNED, None))

        visible = _scope_rows(run, attribution, client_id)
        if not visible:
            raise BusinessError(
                "NO_OWNERSHIP", "该批次没有归属本机构的可导出记录", 403
            )
        if unassigned_lines:
            raise BusinessError(
                "UNASSIGNED_ROWS",
                f"存在 {len(unassigned_lines)} 行未确认归属（物理行 "
                f"{','.join(map(str, unassigned_lines[:10]))}"
                f"{'…' if len(unassigned_lines) > 10 else ''}），已阻止外发；"
                "请由实验室补登归属后重试",
                409,
            )

        # 别名铸号与导出登记须原子提交：任一环节失败整体回滚，不留半份登记。
        db.execute("BEGIN")
        try:
            aliases_minted = 0
            alias_by_line = {}
            for item in visible:
                line = str(item["line"])
                # 坏列/重复表头类隔离行没有样品编号，用物理行号构造不可与真实编号冲突的键。
                original = item.get("sample_id") or item.get("id") or f"__line__{line}"
                alias, created = identity.mint_alias(db, client_id, original)
                aliases_minted += int(created)
                alias_by_line[line] = alias

            ledger = db.execute(
                "SELECT * FROM exports WHERE run_id=? AND client_id=?",
                (ident, client_id),
            ).fetchone()
            if ledger is None:
                db.execute(
                    "INSERT INTO exports(run_id,client_id,created,rows_exported,"
                    "aliases_minted,withheld_unassigned) VALUES(?,?,?,?,?,?)",
                    (ident, client_id, datetime.now(timezone.utc).isoformat(),
                     len(visible), aliases_minted, 0),
                )
                export_record = {"retry": False, "aliases_minted": aliases_minted}
            else:
                # 重试：不新增编号、不新增登记；若本次仍有新铸号说明数据异常，直接报错。
                if aliases_minted:
                    raise BusinessError("ALIAS", "别名登记与既有导出不一致，请联系实验室")
                export_record = {"retry": True, "aliases_minted": 0}
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
    run["attribution"] = attribution
    return run, alias_by_line, export_record


def _scope_rows(run, attribution, client_id):
    """挑出归属本机构的结果行与隔离行；其他机构与未归属行一律不进入导出视图。"""
    visible = []
    for bucket in ("final", "quarantine"):
        for item in run["result"].get(bucket, []):
            if attribution.get(str(item.get("line", ""))) == client_id:
                visible.append(item)
    return visible


def export_history(client_id=None):
    with connect() as db:
        if client_id is None:
            rows = db.execute("SELECT * FROM exports ORDER BY created DESC").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM exports WHERE client_id=? ORDER BY created DESC",
                (client_id,),
            ).fetchall()
    return [dict(r) for r in rows]
