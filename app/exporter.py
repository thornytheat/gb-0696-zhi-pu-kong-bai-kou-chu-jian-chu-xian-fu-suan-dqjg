import csv, io, json


def _writer():
    out = io.StringIO()
    return out, csv.writer(out)


def evidence_csv(run):
    """实验室全量证据 CSV：保留原始样品编号与全部隔离原因，供内部复核。"""
    out, writer = _writer()
    writer.writerow(["run_id", "sha256", "rule_version", "kind", "line",
                     "owner", "reason", "sample_alias", "evidence"])
    attribution = run.get("attribution") or {}
    writer.writerow([
        run["id"], run["digest"], run["rule_version"], "manifest", "", "", "", "",
        json.dumps({
            "balance": run["result"]["balance"],
            "decision": run["decision"],
            "revision": run["revision"],
            "scope": "lab-full",
        }, ensure_ascii=False),
    ])
    for row in run["result"].get("final", []):
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "result",
            row.get("line", ""), attribution.get(str(row.get("line", "")), ""),
            row.get("status", ""), "",
            json.dumps(row, ensure_ascii=False),
        ])
    for row in run["result"].get("quarantine", []):
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "quarantine",
            row.get("line", ""), attribution.get(str(row.get("line", "")), ""),
            row["reason"], "",
            json.dumps(row, ensure_ascii=False),
        ])
    return "﻿" + out.getvalue()


def client_evidence_csv(run, alias_by_line, principal):
    """委托方证据 CSV：只含本机构行，原始编号替换为机构内稳定别名。

    - 别名对每个委托方独立铸号并持久化，重试导出逐行一致；
    - 隔离行随证据输出 reason，委托方可核对为何该行未参与计算；
    - 不含任何其他机构或未归属行，manifest 只声明本机构可见口径。
    """
    out, writer = _writer()
    client_id = principal["client_id"]
    writer.writerow(["run_id", "sha256", "rule_version", "kind", "line",
                     "owner", "reason", "sample_alias", "evidence"])
    final, quarantined = [], []
    for bucket, sink in (("final", final), ("quarantine", quarantined)):
        for row in run["result"].get(bucket, []):
            if str(row.get("line", "")) not in alias_by_line:
                continue
            sink.append(row)
    writer.writerow([
        run["id"], run["digest"], run["rule_version"], "manifest", "", "", "", "",
        json.dumps({
            "scope": "client",
            "client_id": client_id,
            "client_name": principal["name"],
            "decision": run["decision"],
            "revision": run["revision"],
            "rows_result": len(final),
            "rows_quarantine": len(quarantined),
            "note": "原始样品编号由实验室留存；本证据使用机构内稳定别名，隔离原因见 reason 列",
        }, ensure_ascii=False),
    ])
    for row in final:
        line = str(row.get("line", ""))
        redacted = dict(row)
        redacted.pop("sample_id", None)  # 不向外暴露仪器原始编号
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "result",
            row.get("line", ""), client_id, row.get("status", ""),
            alias_by_line[line], json.dumps(redacted, ensure_ascii=False),
        ])
    for row in quarantined:
        line = str(row.get("line", ""))
        redacted = dict(row)
        redacted.pop("id", None)
        if isinstance(redacted.get("raw"), dict):
            redacted["raw"] = {k: v for k, v in redacted["raw"].items() if k != "sample_id"}
        writer.writerow([
            run["id"], run["digest"], run["rule_version"], "quarantine",
            row.get("line", ""), client_id, row["reason"],
            alias_by_line[line], json.dumps(redacted, ensure_ascii=False),
        ])
    return "﻿" + out.getvalue()
