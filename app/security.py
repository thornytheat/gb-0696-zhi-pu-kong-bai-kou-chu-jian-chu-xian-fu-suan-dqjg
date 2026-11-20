"""请求侧机构识别与权限边界。

- 无令牌/实验室令牌：内部角色，可复核全部原始记录（含未归属的升级旧数据）；
- 委托方令牌：只能读取/导出归属本机构的记录，其他机构与未归属行一律不可见。
"""
from fastapi import Request
from app import store
from app.errors import BusinessError

LAB_TOKEN = "demo-token-lab"


def principal(request: Request):
    """从 Authorization: Bearer 或 X-Client-Token 解析主体。"""
    token = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.headers.get("X-Client-Token", "").strip() or None
    if token is None:
        return {"role": "lab", "client_id": None, "name": "实验室"}
    if token == LAB_TOKEN:
        return {"role": "lab", "client_id": None, "name": "实验室"}
    with store.connect() as db:
        client = store.identity.client_by_token(db, token)
    if client is None:
        raise BusinessError("AUTH", "凭据无效", 401)
    return {"role": "client", "client_id": client["client_id"], "name": client["name"]}


def require_lab(principal_obj):
    if principal_obj["role"] != "lab":
        raise BusinessError("FORBIDDEN", "仅实验室内部可执行该操作", 403)
