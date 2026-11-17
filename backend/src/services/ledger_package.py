"""核验包与哈希链的纯计算逻辑。

这个模块刻意不导入数据库、配置或 Web 框架：离线核验只需要核验包文件
（以及可选的可信锚点），不依赖原库、后端进程或共享卷。

链式凭据只使用标准库即可复算；锚点签名额外使用 ``cryptography``，
若离线环境没有该依赖，链本身仍会被完整检查，只是无法确认锚点签名。
"""
from __future__ import annotations

import base64
import hashlib
import json

GENESIS_HASH = "0" * 64
PACKAGE_FORMAT = "guide-chi-ledger-package"
ANCHOR_FORMAT = "guide-chi-ledger-anchor"
VERSION = 1

STATUS_LABELS = {
    "complete": "完整",
    "damaged": "受损",
    "missing_anchor": "缺少可信依据",
}

ENTRY_FIELDS = ("seq", "event", "actor", "subject", "payload", "payload_hash", "prev_hash", "entry_hash", "created_at")
PACKAGE_HEADER_FIELDS = ("format", "version", "package_id", "cutoff_seq", "entry_count", "head_hash", "created_at")


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(value) -> str:
    data = value if isinstance(value, bytes) else str(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalize_time(value) -> float:
    return round(float(value), 6)


def payload_hash(payload) -> str:
    if not isinstance(payload, dict):
        payload = {}
    return sha256_hex(canonical_json(payload))


def entry_core(seq, event, actor, subject, payload_digest, prev_hash, created_at) -> dict:
    return {
        "seq": int(seq),
        "event": str(event),
        "actor": str(actor),
        "subject": str(subject),
        "payload_hash": str(payload_digest),
        "prev_hash": str(prev_hash),
        "created_at": normalize_time(created_at),
    }


def entry_hash(core: dict) -> str:
    return sha256_hex(canonical_json(core))


def entry_document(seq, event, actor, subject, payload, prev_hash, created_at) -> dict:
    digest = payload_hash(payload)
    core = entry_core(seq, event, actor, subject, digest, prev_hash, created_at)
    return {
        "seq": core["seq"],
        "event": core["event"],
        "actor": core["actor"],
        "subject": core["subject"],
        "payload": payload,
        "payload_hash": core["payload_hash"],
        "prev_hash": core["prev_hash"],
        "entry_hash": entry_hash(core),
        "created_at": core["created_at"],
    }


def package_header(package_id, cutoff_seq, entry_count, head_hash, created_at) -> dict:
    return {
        "format": PACKAGE_FORMAT,
        "version": VERSION,
        "package_id": int(package_id),
        "cutoff_seq": int(cutoff_seq),
        "entry_count": int(entry_count),
        "head_hash": str(head_hash),
        "created_at": normalize_time(created_at),
    }


def package_digest(header: dict, entries: list) -> str:
    core = {key: header.get(key) for key in PACKAGE_HEADER_FIELDS}
    core["entries"] = entries
    return sha256_hex(canonical_json(core))


def build_package_document(package_id, cutoff_seq, created_at, entries: list) -> dict:
    ordered = sorted(entries, key=lambda item: item["seq"])
    head_hash = ordered[-1]["entry_hash"] if ordered else GENESIS_HASH
    header = package_header(package_id, cutoff_seq, len(ordered), head_hash, created_at)
    document = dict(header)
    document["entries"] = ordered
    document["package_hash"] = package_digest(header, ordered)
    return document


def anchor_core(anchor: dict) -> dict:
    return {key: value for key, value in anchor.items() if key != "signature"}


def anchor_document(package_id, package_hash_value, cutoff_seq, entry_count, head_hash, created_at, public_key_b64, signature_b64) -> dict:
    return {
        "format": ANCHOR_FORMAT,
        "version": VERSION,
        "package_id": int(package_id),
        "package_hash": str(package_hash_value),
        "cutoff_seq": int(cutoff_seq),
        "entry_count": int(entry_count),
        "head_hash": str(head_hash),
        "created_at": normalize_time(created_at),
        "public_key": public_key_b64,
        "key_id": sha256_hex(base64.b64decode(public_key_b64))[:16],
        "signature": signature_b64,
    }


def sign_anchor(anchor: dict, private_key) -> str:
    return base64.b64encode(private_key.sign(canonical_json(anchor_core(anchor)).encode("utf-8"))).decode("ascii")


def verify_anchor_signature(anchor: dict):
    """返回 (状态, 原因)，状态为 valid / invalid / unavailable。

    离线环境没有 cryptography 时无法判真伪，但不能把包误判为“受损”——
    这种情况归入“缺少可信依据”，提示补装校验组件或改用可信渠道核对指纹。
    """
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except Exception:
        return "unavailable", "当前环境缺少签名校验组件 cryptography，无法确认锚点真伪"
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(anchor["public_key"]))
        public_key.verify(base64.b64decode(anchor["signature"]), canonical_json(anchor_core(anchor)).encode("utf-8"))
        return "valid", ""
    except (InvalidSignature, ValueError, KeyError, TypeError):
        return "invalid", "锚点签名无效，可信依据不可用"


def package_text(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def anchor_text(anchor: dict) -> str:
    return json.dumps(anchor, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def _problem(code: str, message: str, seq=None) -> dict:
    item = {"code": code, "message": message}
    if seq is not None:
        item["seq"] = seq
    return item


def _check_entries(package: dict) -> tuple[list, str]:
    problems = []
    entries = package.get("entries")
    if not isinstance(entries, list):
        return [_problem("entries_invalid", "核验包缺少凭据列表")], GENESIS_HASH

    cutoff = package.get("cutoff_seq")
    if not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff < 1:
        problems.append(_problem("cutoff_invalid", "截至序号无效"))
        cutoff = None

    expected_count = package.get("entry_count")
    if len(entries) != expected_count:
        problems.append(
            _problem(
                "entry_count_mismatch",
                f"凭据数量与声明不符：声明 {expected_count} 条，实际 {len(entries)} 条",
            )
        )

    seqs = [entry.get("seq") for entry in entries if isinstance(entry, dict)]
    bad_seq = [seq for seq in seqs if not isinstance(seq, int) or isinstance(seq, bool)]
    if bad_seq:
        problems.append(_problem("seq_invalid", "存在无法识别的凭据序号"))

    valid_seqs = [seq for seq in seqs if isinstance(seq, int) and not isinstance(seq, bool)]
    if valid_seqs != sorted(valid_seqs):
        problems.append(_problem("out_of_order", "凭据顺序被打乱：序号未按升序排列"))
    if len(valid_seqs) != len(set(valid_seqs)):
        problems.append(_problem("duplicate_seq", "存在重复序号，凭据可能被替换或调序"))

    if cutoff is not None:
        expected_seqs = list(range(1, cutoff + 1))
        missing = sorted(set(expected_seqs) - set(valid_seqs))
        if missing:
            problems.append(
                _problem("missing_entry", f"缺少第 {missing[0]} 条凭据，共缺 {len(missing)} 条", missing[0])
            )
        extra = sorted(set(valid_seqs) - set(expected_seqs))
        if extra:
            problems.append(_problem("unexpected_entry", f"出现截至序号之后的凭据：第 {extra[0]} 条", extra[0]))
        if valid_seqs and max(valid_seqs) < cutoff:
            problems.append(
                _problem(
                    "tail_truncated",
                    f"尾部被截断：声明截至第 {cutoff} 条，实际止于第 {max(valid_seqs)} 条",
                    max(valid_seqs),
                )
            )

    prev = GENESIS_HASH
    expected = 1
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            problems.append(_problem("entry_invalid", f"第 {index + 1} 条凭据结构无效", index + 1))
            continue
        seq = entry.get("seq")
        position = seq if isinstance(seq, int) else index + 1
        if isinstance(seq, int) and not isinstance(seq, bool) and seq != expected:
            if seq > expected:
                problems.append(_problem("missing_entry", f"第 {expected} 条凭据缺失或跳号", expected))
            else:
                problems.append(_problem("out_of_order", f"第 {position} 条凭据位置错误", position))
            expected = seq + 1
        else:
            expected += 1

        if entry.get("prev_hash") != prev:
            problems.append(_problem("chain_break", f"第 {position} 条凭据与上一条衔接断开", position))

        try:
            recomputed_payload = payload_hash(entry.get("payload"))
        except (TypeError, ValueError):
            recomputed_payload = ""
            problems.append(_problem("payload_invalid", f"第 {position} 条凭据内容无法解析", position))
        if recomputed_payload != entry.get("payload_hash"):
            problems.append(_problem("payload_modified", f"第 {position} 条凭据正文被改动", position))

        try:
            core = entry_core(
                entry.get("seq"),
                entry.get("event"),
                entry.get("actor"),
                entry.get("subject"),
                entry.get("payload_hash"),
                entry.get("prev_hash"),
                entry.get("created_at"),
            )
            recomputed_entry = entry_hash(core)
        except (TypeError, ValueError):
            recomputed_entry = ""
            problems.append(_problem("entry_invalid", f"第 {position} 条凭据字段缺失或类型错误", position))
        if recomputed_entry != entry.get("entry_hash"):
            problems.append(_problem("entry_modified", f"第 {position} 条凭据摘要被改写", position))

        prev = entry.get("entry_hash") or prev

    declared_head = package.get("head_hash")
    if prev != declared_head:
        problems.append(_problem("head_hash_mismatch", "凭据链末端哈希与核验包声明不一致"))
    return problems, prev


def _check_package_digest(package: dict) -> list:
    problems = []
    if package.get("format") != PACKAGE_FORMAT:
        problems.append(_problem("format_invalid", "不是场馆凭据核验包"))
    if package.get("version") != VERSION:
        problems.append(_problem("version_unsupported", f"不支持的核验包版本：{package.get('version')}"))
    header = {key: package.get(key) for key in PACKAGE_HEADER_FIELDS}
    try:
        digest = package_digest(header, package.get("entries"))
    except (TypeError, ValueError):
        return problems + [_problem("package_invalid", "核验包内容无法规范化，文件可能已损坏")]
    if digest != package.get("package_hash"):
        problems.append(_problem("package_hash_mismatch", "核验包整体摘要不符：文件被改写或损坏"))
    return problems


def _check_anchor(package: dict, anchor: dict | None, expect_key_id: str | None) -> tuple[list, dict]:
    if anchor is None:
        return [], {"checked": False, "valid": False}
    problems = []
    if not isinstance(anchor, dict) or anchor.get("format") != ANCHOR_FORMAT:
        return [_problem("anchor_invalid", "锚点文件格式无效")], {"checked": True, "valid": False}
    signature, reason = verify_anchor_signature(anchor)
    if signature == "invalid":
        problems.append(_problem("anchor_signature_invalid", reason))
    if expect_key_id and anchor.get("key_id") != expect_key_id:
        problems.append(
            _problem("anchor_key_unexpected", "锚点公钥与预先登记的可信公钥不一致，疑似被替换")
        )
    comparisons = (
        ("package_id", "锚点与核验包的包号不一致"),
        ("package_hash", "锚点与核验包的整体摘要不一致，文件可能被替换或改写"),
        ("cutoff_seq", "锚点与核验包的截至序号不一致"),
        ("head_hash", "锚点与核验包的链末端哈希不一致，尾部可能被截断"),
    )
    for key, message in comparisons:
        if anchor.get(key) != package.get(key):
            problems.append(_problem("anchor_mismatch", message))
            break
    # 锚点条数多于包内条数：尾部被裁剪；少于包内条数：包被补写
    if anchor.get("entry_count") != package.get("entry_count"):
        try:
            truncated = package["entry_count"] < anchor["entry_count"]
        except (KeyError, TypeError):
            truncated = False
        if truncated:
            problems.append(
                _problem("tail_truncated", f"锚点记录 {anchor['entry_count']} 条，核验包只有 {package['entry_count']} 条，尾部被截断")
            )
        else:
            problems.append(_problem("anchor_mismatch", "锚点与核验包的凭据数量不一致"))
    return problems, {
        "checked": True,
        "valid": signature == "valid",
        "unavailable": signature == "unavailable",
        "key_id": anchor.get("key_id"),
        "reason": reason,
    }


def _unique(problems: list) -> list:
    seen, result = set(), []
    for problem in problems:
        marker = (problem.get("code"), problem.get("seq"), problem.get("message"))
        if marker in seen:
            continue
        seen.add(marker)
        result.append(problem)
    return result


def verify_package_document(package, anchor=None, expect_key_id=None) -> dict:
    """离线核验一个核验包。

    - anchor 为 None：只做链自检，状态为“缺少可信依据”。
    - expect_key_id：调用方预先登记的可信公钥指纹；提供时锚点公钥必须一致，
      否则攻击者可以自带一把公钥重签整个包。
    """
    if not isinstance(package, dict):
        return {
            "status": "damaged",
            "label": STATUS_LABELS["damaged"],
            "problems": [_problem("package_invalid", "核验包不是有效的 JSON 对象")],
            "anchor": {"checked": False, "valid": False},
            "summary": "核验包无法解析，已判为受损。",
        }
    problems = _check_package_digest(package)
    entry_problems, computed_head = _check_entries(package)
    problems.extend(entry_problems)
    anchor_problems, anchor_state = _check_anchor(package, anchor, expect_key_id)
    problems.extend(anchor_problems)
    problems = _unique(problems)

    if problems:
        status = "damaged"
    elif anchor_state.get("checked") and anchor_state.get("valid"):
        status = "complete"
    else:
        status = "missing_anchor"

    if status == "complete":
        summary = (
            f"核验包完整：{package.get('entry_count')} 条凭据首尾衔接，"
            f"截至第 {package.get('cutoff_seq')} 条，链末端与独立锚点一致。"
        )
    elif status == "damaged":
        first = problems[0]
        summary = f"核验包受损：{first['message']}。共发现 {len(problems)} 处问题。"
    elif anchor_state.get("unavailable"):
        summary = (
            "凭据链自洽，锚点也已附上，但当前环境无法校验签名（缺少 cryptography）。"
            "请装入校验组件后重试，或改由可信渠道核对锚点公钥指纹 "
            f"{anchor_state.get('key_id') or '—'}。"
        )
    else:
        summary = (
            "凭据链自洽，但未提供独立的可信锚点，无法排除整包被重建或尾部被截断，"
            "请另附负责人保存的锚点文件。"
        )

    return {
        "status": status,
        "label": STATUS_LABELS[status],
        "summary": summary,
        "problems": problems,
        "anchor": anchor_state,
        "package": {
            "package_id": package.get("package_id"),
            "cutoff_seq": package.get("cutoff_seq"),
            "entry_count": package.get("entry_count"),
            "head_hash": package.get("head_hash"),
            "computed_head": computed_head,
            "package_hash": package.get("package_hash"),
        },
    }
