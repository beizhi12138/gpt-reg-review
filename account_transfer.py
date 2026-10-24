"""Versioned, offline account exchange contract shared by both applications.

Keep this file identical in gpt-reg-review and turb-gpt-free-register.
ChatGPT credentials are top-level; mailbox credentials belong only in ``mail``.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
import math
import re

FORMAT = "gpt-account-transfer"
VERSION = 1
MAX_ACCOUNTS = 100000
MAX_BYTES = 512 * 1024 * 1024
STATE_KEY = "_account_transfer"
CREDENTIAL_FIELDS = (
    "password", "access_token", "session_token", "refresh_token", "id_token",
    "device_id", "csrf_token", "cookie_header", "totp_secret", "totp_factor_id",
)
MAIL_FIELDS = ("kind", "password", "client_id", "refresh_token", "relay_url")
# Passive account metadata only: never import jobs, local paths or queue states.
METADATA_FIELDS = (
    "user_id", "user_name", "plan_type", "current_plan_type", "expires_at",
    "note", "note_updated_at", "registration_country", "session_info",
    "plus_trial_eligible", "plan_checked_at",
)


def object_value(value) -> dict:
    if isinstance(value, dict):
        return deepcopy(value)
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def timestamp(value) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("created_at 必须是时间戳或 ISO 日期")
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, OverflowError, OSError):
            raise ValueError("created_at 必须是时间戳或 ISO 日期") from None
    if not math.isfinite(result) or not 0 <= result <= 253402214400:
        raise ValueError("created_at 超出有效范围")
    return result


def normalize_account(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("账号必须是 JSON 对象")
    email = raw.get("email")
    if not isinstance(email, str) or len(email.strip()) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip()):
        raise ValueError("邮箱格式不正确")
    row = {"email": email.strip().lower()}
    for name in CREDENTIAL_FIELDS:
        value = raw.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} 必须是字符串")
        row[name] = value or ""
    for name in ("mail", "extra", "legacy_extra", "metadata"):
        value = raw.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"{name} 必须是 JSON 对象")
        row[name] = deepcopy(value)
    for name in MAIL_FIELDS:
        value = row["mail"].get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"mail.{name} 必须是字符串")
    mail_email = row["mail"].get("email")
    if mail_email is not None and (not isinstance(mail_email, str) or mail_email.strip().lower() != row["email"]):
        raise ValueError("邮箱凭证与账号邮箱不一致")
    row["mail"]["email"] = row["email"]
    row["metadata"] = {k: v for k, v in row["metadata"].items() if k in METADATA_FIELDS}
    for name, value in row["metadata"].items():
        if name == "session_info":
            if not isinstance(value, dict):
                raise ValueError("metadata.session_info 必须是 JSON 对象")
        elif name == "plus_trial_eligible":
            if value is not None and not isinstance(value, bool):
                raise ValueError("metadata.plus_trial_eligible 必须是布尔值")
        elif value is not None and not isinstance(value, (str, int, float)):
            raise ValueError(f"metadata.{name} 类型不正确")
    row["created_at"] = timestamp(raw.get("created_at"))
    return row


def read_document(value) -> tuple[list[tuple[int, dict]], dict]:
    if isinstance(value, (str, bytes)):
        if len(value.encode("utf-8") if isinstance(value, str) else value) > MAX_BYTES:
            raise ValueError(f"迁移文件不能超过 {MAX_BYTES // (1024 * 1024)} MB")
        try:
            value = json.loads(value.lstrip("\ufeff") if isinstance(value, str) else value.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("迁移文件不是有效的 UTF-8 JSON") from None
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ValueError("请选择「账号互转」导出的迁移 JSON 文件")
    if type(value.get("version")) is not int or value["version"] != VERSION:
        raise ValueError("不支持的迁移文件版本，请更新两边系统后重新导出")
    rows = value.get("accounts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("迁移文件的 accounts 必须是非空数组")
    if len(rows) > MAX_ACCOUNTS:
        raise ValueError(f"单次最多迁移 {MAX_ACCOUNTS} 个账号，请分批导出")
    report = {"ok": True, "total": len(rows), "inserted": 0, "updated": 0,
              "skipped": 0, "failed": 0, "incomplete": 0, "details": []}
    prepared, seen = [], set()
    for index, raw in enumerate(rows, 1):
        try:
            row = normalize_account(raw)
        except ValueError as exc:
            record_result(report, "failed", index, "", str(exc))
            continue
        if row["email"] in seen:
            record_result(report, "skipped", index, row["email"], "文件内重复邮箱")
            continue
        seen.add(row["email"])
        prepared.append((index, row))
    return prepared, report


def record_result(report, status, index, email, reason=""):
    report[status] += 1
    if reason and len(report["details"]) < 100:
        report["details"].append({"index": index, "email": email, "status": status, "reason": reason})


def merge_missing(existing: dict, incoming: dict) -> dict:
    """Fill absent values; False and zero are real values, not missing data."""
    result = deepcopy(existing)
    for name, value in incoming.items():
        current = result.get(name)
        if isinstance(current, dict) and isinstance(value, dict):
            result[name] = merge_missing(current, value)
        elif current is None or current == "" or current == []:
            result[name] = deepcopy(value)
    # A factor identifier belongs to its TOTP secret, not another enrollment.
    if "totp_secret" in incoming and existing.get("totp_secret") and existing["totp_secret"] != incoming.get("totp_secret"):
        result["totp_factor_id"] = existing.get("totp_factor_id") or ""
    if existing.get("access_token") and incoming.get("access_token") and existing["access_token"] != incoming["access_token"]:
        # Session/CSRF/cookies/device can belong to the incoming access token's
        # login session. Do not combine them with a different retained token.
        for name in ("session_token", "csrf_token", "cookie_header", "device_id"):
            if name in incoming:
                result[name] = existing.get(name) or ""
        if isinstance(result.get("metadata"), dict):
            result["metadata"].pop("session_info", None)
            if isinstance(existing.get("metadata"), dict) and "session_info" in existing["metadata"]:
                result["metadata"]["session_info"] = deepcopy(existing["metadata"]["session_info"])
    return result


def make_document(rows: list[dict], source: str) -> dict:
    if not rows:
        raise ValueError("没有可导出的账号")
    if len(rows) > MAX_ACCOUNTS:
        raise ValueError(f"单次最多迁移 {MAX_ACCOUNTS} 个账号，请勾选后分批导出")
    return {"format": FORMAT, "version": VERSION, "source": source,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "accounts": rows}


def document_chunks(document: dict):
    """Serialize to bounded chunks so large backup exports need no giant JSON string."""
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2, allow_nan=False)
    for chunk in encoder.iterencode(document):
        content = chunk.encode("utf-8")
        size += len(content)
        if size > MAX_BYTES:
            raise ValueError(f"迁移文件超过 {MAX_BYTES // (1024 * 1024)} MB，请勾选后分批导出")
        yield content


def document_bytes(document: dict) -> bytes:
    with BytesIO() as stream:
        stream.writelines(document_chunks(document))
        return stream.getvalue()
