from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.errors import BusinessError, object_payload
from app.pipeline import run, sample_text
from app import store, security
from app.exporter import evidence_csv, client_evidence_csv
from pathlib import Path
import importlib

app = FastAPI(title='质谱空白扣除与检出限复算')
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
MODULES = ['仪器接收', '内标归一', '标曲拟合', '空白扣除', '浓度复算', '质控判定', '结果复核']
for n, slug in enumerate(['intake', 'internal', 'curve', 'blank', 'concentration', 'quality', 'review'], 1):
    app.include_router(importlib.import_module(f"app.modules.m{n}_{slug}.handler").router)


@app.exception_handler(BusinessError)
async def domain_error(request, exc):
    return JSONResponse({"error": exc.code, "message": str(exc)}, status_code=exc.status)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return page(request, 1)


@app.get("/module/{module}", response_class=HTMLResponse)
def page(request: Request, module: int):
    if module not in range(1, 8):
        raise BusinessError("MODULE", "模块不存在", 404)
    ident = request.query_params.get("run_id")
    item = store.get(ident) if ident else store.save(sample_text(), run(sample_text()))
    owners = {}
    for owner in item["attribution"].values():
        owners[owner] = owners.get(owner, 0) + 1
    return templates.TemplateResponse(
        request=request, name="workbench.html",
        context={"title": '质谱空白扣除与检出限复算', "modules": MODULES,
                 "active": module, "run": item,
                 "report": item["result"]["reports"][module - 1],
                 "owners": owners})


@app.post("/api/run")
def calculate(request: Request, payload: object = Body(default=None)):
    principal_obj = security.principal(request)
    payload = object_payload(payload)
    text = payload.get("text", sample_text())
    if not isinstance(text, str):
        raise BusinessError("TEXT", "输入须为文本")
    # 委托方可以提交本机构文件，但归属登记属于实验室职责；委托方只能声明整文件归自己，
    # 不能把行挂到其他机构名下。
    client_id = payload.get("client_id")
    line_clients = payload.get("line_clients")
    if principal_obj["role"] == "client":
        if client_id is not None and client_id != principal_obj["client_id"]:
            raise BusinessError("FORBIDDEN", "不能代其他机构登记归属", 403)
        if line_clients is not None:
            raise BusinessError("FORBIDDEN", "行级归属登记仅限实验室", 403)
        client_id = principal_obj["client_id"]
    if client_id is not None and (not isinstance(client_id, str) or not client_id.strip()):
        raise BusinessError("ATTRIBUTION", "委托方编号须为文本")
    return store.save(text, run(text), client_id=client_id, line_clients=line_clients)


@app.get("/api/runs")
def list_runs(request: Request):
    return store.history(security.principal(request))


@app.get("/api/runs/{ident}")
def run_detail(ident: str, request: Request):
    principal_obj = security.principal(request)
    item = store.get(ident)
    if principal_obj["role"] == "client":
        owners = set(item["attribution"].values())
        if principal_obj["client_id"] not in owners:
            # 统一 404，避免委托方探测其他机构批次是否存在。
            raise BusinessError("NOT_FOUND", "批次不存在或无权访问", 404)
        item["attribution"] = {
            line: owner for line, owner in item["attribution"].items()
            if owner == principal_obj["client_id"]
        }
    return item


@app.post("/api/review")
def review(request: Request, payload: object = Body(...)):
    security.require_lab(security.principal(request))
    payload = object_payload(payload)
    if not isinstance(payload.get("id"), str) or not isinstance(payload.get("decision"), str):
        raise BusinessError("REVIEW", "复核编号与决定须为文本")
    if type(payload.get("revision")) is not int:
        raise BusinessError("REVISION", "版本须为整数")
    return store.review(str(payload.get("id", "")), payload["revision"], payload.get("decision"))


@app.get("/api/export/{ident}")
def export(ident: str, request: Request):
    principal_obj = security.principal(request)
    if principal_obj["role"] == "lab":
        run_item = store.get(ident)
        return Response(
            evidence_csv(run_item), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=evidence-lab.csv"})
    scoped, alias_by_line, _ = store.prepare_client_export(ident, principal_obj)
    body = client_evidence_csv(scoped, alias_by_line, principal_obj)
    filename = f"evidence-{principal_obj['client_id']}.csv"
    return Response(
        body, media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/api/exports")
def exports(request: Request):
    principal_obj = security.principal(request)
    if principal_obj["role"] == "client":
        return store.export_history(principal_obj["client_id"])
    return store.export_history()


@app.get("/api/clients")
def clients(request: Request):
    security.require_lab(security.principal(request))
    with store.connect() as db:
        return [{"client_id": r[0], "name": r[1]}
                for r in db.execute("SELECT client_id,name FROM clients ORDER BY client_id")]


@app.get("/api/sample")
def sample():
    return {"text": sample_text()}
