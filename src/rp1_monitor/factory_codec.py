"""Bounded, lossless JSON payload compression; legacy JSON text remains readable."""

import json
import zlib

MAGIC = b"RP1Z1\x00"
MAX_PAYLOAD_BYTES = 1024 * 1024


def encode_payload(value: dict) -> bytes:
    if not isinstance(value, dict):
        raise ValueError("片段样本必须是JSON对象")
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("单个片段样本超过1 MiB限制")
    return MAGIC + zlib.compress(raw)


def decode_payload(raw: str | bytes) -> dict:
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("单个片段样本超过1 MiB限制")
        document = raw
    elif isinstance(raw, bytes):
        if not raw.startswith(MAGIC) or len(raw) > MAX_PAYLOAD_BYTES + 1024:
            raise ValueError("片段压缩样本标记或长度无效")
        try:
            decoder = zlib.decompressobj()
            document = decoder.decompress(raw[len(MAGIC):], MAX_PAYLOAD_BYTES + 1)
            if (len(document) > MAX_PAYLOAD_BYTES or decoder.unconsumed_tail
                    or not decoder.eof or decoder.unused_data):
                raise ValueError("片段样本解压超限、未完整结束或包含尾随数据")
            document = document.decode("utf-8")
        except (zlib.error, UnicodeDecodeError) as exc:
            raise ValueError("片段压缩样本损坏") from exc
    else:
        raise ValueError("片段样本必须是JSON文本或受支持的压缩字节")

    def invalid_constant(_):
        raise ValueError("片段样本含非有限数值")

    try:
        result = json.loads(document, parse_constant=invalid_constant)
    except (ValueError, RecursionError) as exc:
        raise ValueError("片段样本不是有效JSON对象") from exc
    if not isinstance(result, dict):
        raise ValueError("片段样本必须是JSON对象")
    return result
