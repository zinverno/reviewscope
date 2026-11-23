"""Bounded local file and table readers, with no inference of encodings or fields."""

import csv
import io
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .contract import (
    MAX_BYTES,
    MAX_COLUMNS,
    MAX_DEPTH,
    MAX_FIELD,
    MAX_ROWS,
    PreviewError,
)

MAX_JSON_MARKS = 240_000


def read_local(path: Path, limit: int) -> bytes:
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise PreviewError("io_error")
            if info.st_size > limit:
                raise PreviewError("over_limit")
            data = stream.read(limit + 1)
    except OSError:
        raise PreviewError("io_error") from None
    if len(data) > limit:
        raise PreviewError("over_limit")
    return data


@dataclass(frozen=True, slots=True)
class JsonNumber:
    raw: str


def check_string(value: str):
    if len(value) > MAX_FIELD:
        raise PreviewError("over_limit")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise PreviewError("invalid_json") from None


def distinct(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreviewError("duplicate_key")
        result[key] = value
    return result


def guard_json(text: str):
    # Count structural separators outside strings before allocating Python containers.
    stack = []
    quoted = False
    marks = 0
    for match in re.finditer(r'\\.|["\[\]{},]', text):
        token = match[0]
        if token == '"':
            quoted = not quoted
        elif not quoted:
            marks += 1
            if marks > MAX_JSON_MARKS:
                raise PreviewError("over_limit")
            if token in ("[", "{"):
                # Only the top array or the wrapper's direct array can hold input rows.
                table = token == "[" and (not stack or len(stack) == 1 and stack[0][0] == "{")
                stack.append([token, 1, MAX_ROWS if table else MAX_COLUMNS])
                if len(stack) > MAX_DEPTH:
                    raise PreviewError("over_limit")
            elif token == "," and stack:
                stack[-1][1] += 1
                if stack[-1][1] > stack[-1][2]:
                    raise PreviewError("over_limit")
            elif token in ("]", "}"):
                if not stack or stack.pop()[0] != {"]": "[", "}": "{"}[token]:
                    raise PreviewError("invalid_json")


def decode(data: bytes, encoding: str) -> str:
    try:
        return data.decode(encoding)
    except UnicodeDecodeError:
        raise PreviewError("invalid_encoding") from None


def load_json(data: bytes, *, numbers=False):
    text = decode(data, "utf-8-sig")
    guard_json(text)

    def number(token):
        check_string(token)
        return JsonNumber(token)

    def invalid_constant(_):
        raise PreviewError("invalid_json")

    try:
        return json.loads(
            text,
            object_pairs_hook=distinct,
            parse_constant=invalid_constant,
            parse_float=number if numbers else float,
            parse_int=number if numbers else int,
        )
    except (json.JSONDecodeError, RecursionError):
        raise PreviewError("invalid_json") from None
    except ValueError as exc:
        if isinstance(exc, PreviewError):
            raise
        raise PreviewError("invalid_json") from None


def check_json_fields(item):
    if isinstance(item, str):
        check_string(item)
    elif isinstance(item, dict):
        for key, value in item.items():
            check_string(key)
            check_json_fields(value)
    elif isinstance(item, list):
        for value in item:
            check_json_fields(value)


def records(data: bytes, fmt: str, encoding: str, delimiter: str | None, columns: dict):
    """Yield every record including structural failures. A file-level error invalidates all."""
    if not data:
        raise PreviewError("empty_input")
    if len(data) > MAX_BYTES:
        raise PreviewError("over_limit")
    if fmt == "json":
        document = load_json(data, numbers=True)
        if isinstance(document, dict) and set(document) == {"reviews"}:
            document = document["reviews"]
        if not isinstance(document, list):
            raise PreviewError("invalid_shape")
        if len(document) > MAX_ROWS:
            raise PreviewError("over_limit")
        seen = set()
        has_objects = False
        for item in document:
            check_json_fields(item)
            if isinstance(item, dict):
                has_objects = True
                seen.update(key for key in item if key in columns)
                yield item, [] if item.keys() <= columns.keys() else ["unexpected_column"], item
            else:
                yield {}, ["invalid_json_item"], item
        if has_objects and seen != columns.keys():
            raise PreviewError("unknown_column")
    else:
        reader = csv.reader(
            io.StringIO(decode(data, encoding), newline=""), delimiter=delimiter, strict=True
        )
        try:
            header = next(reader, [])
            if not header or any(not k.strip() for k in header):
                raise PreviewError("invalid_header")
            if len(header) > MAX_COLUMNS or any(len(k) > MAX_FIELD for k in header):
                raise PreviewError("over_limit")
            if len(header) != len(set(header)):
                raise PreviewError("duplicate_header")
            if columns.keys() - header:
                raise PreviewError("unknown_column")
            if set(header) - columns.keys():
                raise PreviewError("unexpected_column")
            for position, cells in enumerate(reader, 1):
                if (
                    position > MAX_ROWS
                    or len(cells) > MAX_COLUMNS
                    or any(len(v) > MAX_FIELD for v in cells)
                ):
                    raise PreviewError("over_limit")
                errors = [] if len(cells) == len(header) else ["row_width"]
                yield dict(zip(header, cells, strict=False)), errors, cells
        except csv.Error as exc:
            code = "over_limit" if "field larger than field limit" in str(exc) else "invalid_csv"
            raise PreviewError(code) from None
