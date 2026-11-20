"""委托方归属与样品别名注册。

设计要点：
- 原始样品编号（仪器文件中的 sample_id）始终保留在内部记录中，实验室复核旧记录可追溯；
- 对每个委托方单独维护一套稳定别名，同一委托方历次导出拿到同一别名（可核对历次测值），
  不同委托方之间别名序列互不相干，仅凭别名无法把样品历史串到一起；
- 别名一旦铸出即持久化，重试导出只读取既有别名，绝不新增编号。
"""
import csv
import io
import sqlite3
from datetime import datetime, timezone
from app.errors import BusinessError

# 演示用委托方名册；生产应由受控注册流程维护。
# token 仅作演示凭据，真实环境应通过密钥管理下发并轮换。
SEED_CLIENTS = [
    ("CL-ACME", "甲方环境监测", "demo-token-acme"),
    ("CL-GREEN", "绿源检测委托", "demo-token-green"),
]
UNASSIGNED = "UNASSIGNED"


def seed_clients(db):
    db.executemany(
        "INSERT OR IGNORE INTO clients(client_id, name, token) VALUES(?,?,?)",
        SEED_CLIENTS,
    )


def known_client_ids(db):
    return {r[0] for r in db.execute("SELECT client_id FROM clients")}


def client_by_token(db, token):
    if not isinstance(token, str) or not token:
        return None
    row = db.execute(
        "SELECT client_id, name FROM clients WHERE token=?", (token,)
    ).fetchone()
    return dict(row) if row is not None else None


def _physical_lines(text):
    """按 CSV 物理行（含表头、空行、坏行）编号，与 reader.read_rows 的 line 对齐。"""
    rows = csv.reader(io.StringIO(text.lstrip("﻿")))  # 去 BOM，与 reader 口径一致
    lines = []
    try:
        for cells in rows:
            lines.append((rows.line_num, cells))
    except csv.Error:
        raise BusinessError("CSV", "引号或换行结构错误")
    return lines


def build_attribution(text, known_ids, client_id=None, line_clients=None):
    """生成 {物理行号: 委托方编号} 的归属表。

    client_id 为整文件归属；line_clients 可按物理行覆盖。未显式归属的数据行标记为
    UNASSIGNED，绝不猜测归属——外发时这些行会被拒绝，避免猜错导致整行泄露给错误机构。
    known_ids 为当前名册中的委托方编号集合，由调用方从数据库读取后传入。
    """
    if line_clients is None:
        line_clients = {}
    if not isinstance(line_clients, dict):
        raise BusinessError("ATTRIBUTION", "行归属须为以行号为键的对象")
    normalized = {}
    for key, value in line_clients.items():
        try:
            line_no = int(key)
        except (TypeError, ValueError):
            raise BusinessError("ATTRIBUTION", "行号须为整数")
        if not isinstance(value, str) or not value.strip():
            raise BusinessError("ATTRIBUTION", "委托方编号须为文本")
        normalized[line_no] = value.strip()
    lines = _physical_lines(text)
    if not lines:
        raise BusinessError("BLANK_FILE", "文件只有空行")
    data_lines = [n for n, cells in lines[1:] if cells and any(x.strip() for x in cells)]
    attribution = {}
    for line_no in data_lines:
        owner = normalized.get(line_no, client_id)
        attribution[line_no] = owner.strip() if isinstance(owner, str) and owner.strip() else UNASSIGNED
    unknown = sorted(n for n, owner in attribution.items()
                     if owner != UNASSIGNED and owner not in known_ids)
    if unknown:
        raise BusinessError(
            "CLIENT_UNKNOWN",
            "存在未注册委托方，行: " + ",".join(map(str, unknown))
        )
    return attribution


def mint_alias(db, client_id, original_sample_id):
    """读取或铸出委托方内稳定别名，全程幂等。

    唯一约束 (client_id, original_sample_id) 保证同一委托方同一原编号永远得到同一别名；
    序列号按委托方各自递增，机构之间无法从别名推断样品总量或相互对应关系。
    """
    row = db.execute(
        "SELECT alias FROM sample_aliases WHERE client_id=? AND original_sample_id=?",
        (client_id, original_sample_id),
    ).fetchone()
    if row is not None:
        return row["alias"], False
    last = db.execute(
        "SELECT seq FROM sample_aliases WHERE client_id=? ORDER BY seq DESC LIMIT 1",
        (client_id,),
    ).fetchone()
    next_seq = (last[0] if last else 0) + 1
    alias = f"{client_id}-S{next_seq:06d}"
    # 用保存点隔离唯一约束冲突：失败只回退本条插入，不影响外层导出事务。
    db.execute("SAVEPOINT mint_alias")
    try:
        db.execute(
            "INSERT INTO sample_aliases(client_id, original_sample_id, alias, seq, created)"
            " VALUES(?,?,?,?,?)",
            (client_id, original_sample_id, alias, next_seq,
             datetime.now(timezone.utc).isoformat()),
        )
        db.execute("RELEASE SAVEPOINT mint_alias")
        return alias, True
    except sqlite3.IntegrityError:
        db.execute("ROLLBACK TO SAVEPOINT mint_alias")
        db.execute("RELEASE SAVEPOINT mint_alias")
        # 并发下被其他事务抢先铸号，读取既有别名即可，编号由唯一约束保证不重复。
        row = db.execute(
            "SELECT alias FROM sample_aliases WHERE client_id=? AND original_sample_id=?",
            (client_id, original_sample_id),
        ).fetchone()
        if row is not None:
            return row["alias"], False
        raise BusinessError("ALIAS", "别名登记冲突，请重试")
