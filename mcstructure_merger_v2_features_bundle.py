#!/usr/bin/env python3
"""Minecraft Bedrock .mcstructure v2 merger (pure Python, standard library only).

- Reads only format_version 2 (version 1 is deliberately rejected).
- Merges palettes by full NBT contents, not only block names.
- Remaps both IntArrayTag block layers.
- Preserves and relocates block_position_data (including block entity XYZ fields).
- Preserves entities and translates their common Pos vectors.
- GUI lets the user choose files and edit each source's X/Y/Z placement.

When sources overlap, later files in the list overwrite earlier non-void blocks.
A source index of -1 means "void" and does not overwrite a previous block.
The merged structure is normalized so its smallest X/Y/Z coordinate is zero.
"""

from __future__ import annotations

import array
import copy
from collections import OrderedDict
import json
import hashlib
import shutil
import math
import os
import struct
import sys
import zlib
import tempfile
import zipfile
from pathlib import PurePosixPath, Path
import tkinter as tk
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from typing import Any

# Protect against accidental allocations that could exhaust the machine.
# 45 million cells requires about 360 MB for the two output IntArrayTags alone.
# NBT output is streamed to disk so serialization does not duplicate those arrays.
MAX_OUTPUT_CELLS = 45_000_000


@dataclass
class Tag:
    tag_id: int
    value: Any


class NBTError(ValueError):
    pass


class NBTReader:
    """Reader for standard, uncompressed, little-endian Bedrock NBT."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise NBTError(f"NBTデータが途中で終わっています (位置 {self.pos}, 長さ {n})")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def unpack(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        return struct.unpack("<" + fmt, self.take(size))[0]

    def read_string(self) -> str:
        n = self.unpack("H")
        try:
            return self.take(n).decode("utf-8")
        except UnicodeDecodeError as e:
            raise NBTError(f"NBT文字列をUTF-8として解釈できません: {e}") from e

    def read_payload(self, tag_id: int) -> Tag:
        if tag_id == 1:
            return Tag(tag_id, self.unpack("b"))
        if tag_id == 2:
            return Tag(tag_id, self.unpack("h"))
        if tag_id == 3:
            return Tag(tag_id, self.unpack("i"))
        if tag_id == 4:
            return Tag(tag_id, self.unpack("q"))
        if tag_id == 5:
            return Tag(tag_id, self.unpack("f"))
        if tag_id == 6:
            return Tag(tag_id, self.unpack("d"))
        if tag_id == 7:
            n = self.unpack("i")
            if n < 0:
                raise NBTError("負のByteArray長です")
            return Tag(tag_id, self.take(n))
        if tag_id == 8:
            return Tag(tag_id, self.read_string())
        if tag_id == 9:
            element_type = self.unpack("B")
            n = self.unpack("i")
            if n < 0:
                raise NBTError("負のList長です")
            if element_type not in range(13):
                raise NBTError(f"Listの要素型が不正です: {element_type}")
            if element_type == 0 and n != 0:
                raise NBTError("非空Listの要素型がEndです")
            # For empty lists, any defined NBT element type (0-12) is valid.
            # The type is only a hint when there are no payload elements.
            items = [self.read_payload(element_type) for _ in range(n)]
            return Tag(tag_id, (element_type, items))
        if tag_id == 10:
            items: dict[str, Tag] = {}
            while True:
                child_type = self.unpack("B")
                if child_type == 0:
                    break
                name = self.read_string()
                items[name] = self.read_payload(child_type)
            return Tag(tag_id, items)
        if tag_id == 11:
            n = self.unpack("i")
            if n < 0:
                raise NBTError("負のIntArray長です")
            raw = self.take(n * 4)
            values = array.array("i")
            if values.itemsize != 4:
                raise NBTError("この環境のarray('i')は4バイトではありません")
            values.frombytes(raw)
            if sys.byteorder != "little":
                values.byteswap()
            return Tag(tag_id, values)
        if tag_id == 12:
            n = self.unpack("i")
            if n < 0:
                raise NBTError("負のLongArray長です")
            raw = self.take(n * 8)
            values = array.array("q")
            if values.itemsize != 8:
                raise NBTError("この環境のarray('q')は8バイトではありません")
            values.frombytes(raw)
            if sys.byteorder != "little":
                values.byteswap()
            return Tag(tag_id, values)
        raise NBTError(f"未対応のNBTタグ型です: {tag_id}")


def read_nbt_bytes(data: bytes) -> tuple[str, Tag]:
    """Parse a full uncompressed little-endian NBT stream from bytes."""
    reader = NBTReader(data)
    root_type = reader.unpack("B")
    if root_type != 10:
        raise NBTError("ルートタグがCompoundではありません")
    root_name = reader.read_string()
    root = reader.read_payload(root_type)
    if reader.pos != len(data):
        raise NBTError(f"NBTの末尾に未解析データがあります ({len(data) - reader.pos} bytes)")
    return root_name, root


def read_nbt(path: str) -> tuple[str, Tag]:
    with open(path, "rb") as f:
        data = f.read()
    return read_nbt_bytes(data)


def _read_compressed_structure(data: bytes) -> Tag:
    """Decode the compressed structure compound introduced in Bedrock 26.60.

    The game stores the structure compound as zlib-compressed NBT bytes when
    root-level `compression` is 1. Accept a full NBT root stream, and also the
    payload-only form to tolerate producer differences.
    """
    try:
        raw = zlib.decompress(data)
    except zlib.error as e:
        raise NBTError(f"compression=1 の structure をzlib展開できません: {e}") from e

    # Most expected form: a complete NBT Compound stream.
    try:
        _, compound = read_nbt_bytes(raw)
        if compound.tag_id == 10:
            return compound
    except NBTError:
        pass

    # Fallback: NBT compound payload without the root type/name header.
    try:
        reader = NBTReader(raw)
        compound = reader.read_payload(10)
        if reader.pos == len(raw) and compound.tag_id == 10:
            return compound
    except NBTError:
        pass
    raise NBTError("圧縮されたstructureの展開後データがNBT Compoundとして解釈できません")


def _write_string(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) > 65535:
        raise NBTError("NBT文字列が65535バイトを超えています")
    return struct.pack("<H", len(data)) + data


def _write_payload(tag: Tag) -> bytes:
    t, v = tag.tag_id, tag.value
    if t == 1:
        return struct.pack("<b", v)
    if t == 2:
        return struct.pack("<h", v)
    if t == 3:
        return struct.pack("<i", v)
    if t == 4:
        return struct.pack("<q", v)
    if t == 5:
        return struct.pack("<f", v)
    if t == 6:
        return struct.pack("<d", v)
    if t == 7:
        return struct.pack("<i", len(v)) + bytes(v)
    if t == 8:
        return _write_string(v)
    if t == 9:
        element_type, items = v
        if element_type not in range(13) or (element_type == 0 and items):
            raise NBTError(f"Listの要素型が不正です: {element_type}")
        out = bytearray(struct.pack("<Bi", element_type, len(items)))
        for item in items:
            if item.tag_id != element_type:
                raise NBTError("List内のタグ型が一致していません")
            out.extend(_write_payload(item))
        return bytes(out)
    if t == 10:
        out = bytearray()
        for name, child in v.items():
            out.extend(struct.pack("<B", child.tag_id))
            out.extend(_write_string(name))
            out.extend(_write_payload(child))
        out.extend(b"\x00")
        return bytes(out)
    if t in (11, 12):
        code = "i" if t == 11 else "q"
        vals = array.array(code, v)
        expected = 4 if t == 11 else 8
        if vals.itemsize != expected:
            raise NBTError(f"array('{code}')の要素サイズが{expected}バイトではありません")
        if sys.byteorder != "little":
            vals.byteswap()
        return struct.pack("<i", len(vals)) + vals.tobytes()
    raise NBTError(f"未対応のNBTタグ型です: {t}")


def _write_payload_stream(file_obj, tag: Tag) -> None:
    """Write an NBT payload incrementally to avoid large temporary byte copies."""
    tag_id, value = tag.tag_id, tag.value
    if tag_id in (1, 2, 3, 4, 5, 6):
        fmt = {1: "b", 2: "h", 3: "i", 4: "q", 5: "f", 6: "d"}[tag_id]
        file_obj.write(struct.pack("<" + fmt, value))
        return
    if tag_id == 7:
        file_obj.write(struct.pack("<i", len(value)))
        file_obj.write(bytes(value))
        return
    if tag_id == 8:
        file_obj.write(_write_string(value))
        return
    if tag_id == 9:
        element_type, items = value
        if element_type not in range(13) or (element_type == 0 and items):
            raise NBTError(f"Listの要素型が不正です: {element_type}")
        file_obj.write(struct.pack("<Bi", element_type, len(items)))
        for item in items:
            if item.tag_id != element_type:
                raise NBTError("List内のタグ型が一致していません")
            _write_payload_stream(file_obj, item)
        return
    if tag_id == 10:
        for name, child in value.items():
            file_obj.write(struct.pack("<B", child.tag_id))
            file_obj.write(_write_string(name))
            _write_payload_stream(file_obj, child)
        file_obj.write(b"\x00")
        return
    if tag_id in (11, 12):
        code = "i" if tag_id == 11 else "q"
        expected = 4 if tag_id == 11 else 8
        values = value if isinstance(value, array.array) and value.typecode == code else array.array(code, value)
        if values.itemsize != expected:
            raise NBTError(f"array('{code}')の要素サイズが{expected}バイトではありません")
        file_obj.write(struct.pack("<i", len(values)))
        if sys.byteorder == "little":
            values.tofile(file_obj)
        else:
            little_endian_values = array.array(code, values)
            little_endian_values.byteswap()
            little_endian_values.tofile(file_obj)
        return
    raise NBTError(f"未対応のNBTタグ型です: {tag_id}")


def write_nbt(path: str, root: Tag, root_name: str = "") -> None:
    if root.tag_id != 10:
        raise NBTError("ルートタグはCompoundである必要があります")
    with open(path, "wb") as f:
        f.write(struct.pack("<B", 10))
        f.write(_write_string(root_name))
        _write_payload_stream(f, root)


def tag_list(tag: Tag, expected_element_type: int | None = None) -> list[Tag]:
    """Return ListTag items, treating the element type as irrelevant when empty.

    Empty NBT lists produced by Bedrock and other tools may retain a type hint
    such as TAG_Byte (1), TAG_Compound (10), or TAG_End (0), even though there
    are no elements. Since no payload is read or interpreted for an empty list,
    accept any valid NBT type id in that case. For non-empty lists, keep strict
    type validation so malformed or incompatible data is still rejected.
    """
    if tag.tag_id != 9:
        raise NBTError("NBTタグがListではありません")
    element_type, items = tag.value
    if element_type not in range(13):
        raise NBTError(f"Listの要素型が不正です: {element_type}")
    if expected_element_type is not None and items and element_type != expected_element_type:
        raise NBTError(f"Listの要素型が{element_type}です。{expected_element_type}が必要です")
    return items


def int_list(tag: Tag, label: str) -> list[int]:
    if tag.tag_id != 9:
        raise NBTError(f"{label}がListではありません")
    element_type, items = tag.value
    if element_type not in (1, 2, 3, 4):
        raise NBTError(f"{label}の要素が整数タグではありません")
    return [int(item.value) for item in items]


def make_int_list(values: list[int]) -> Tag:
    return Tag(9, (3, [Tag(3, int(v)) for v in values]))


def tag_signature(tag: Tag):
    """Hashable structural signature; distinguishes NBT numeric types."""
    t, v = tag.tag_id, tag.value
    if t == 10:
        return (t, tuple((k, tag_signature(vv)) for k, vv in sorted(v.items())))
    if t == 9:
        et, items = v
        return (t, et, tuple(tag_signature(item) for item in items))
    if t == 7:
        return (t, bytes(v))
    if t in (11, 12):
        return (t, tuple(v))
    return (t, v)


def get_compound(tag: Tag, key: str, where: str = "NBT") -> Tag:
    if tag.tag_id != 10:
        raise NBTError(f"{where}がCompoundではありません")
    try:
        result = tag.value[key]
    except KeyError as e:
        raise NBTError(f"{where}に'{key}'がありません") from e
    if result.tag_id != 10:
        raise NBTError(f"{where}.{key}がCompoundではありません")
    return result


@dataclass
class LoadedStructure:
    path: str
    root_name: str
    root: Tag
    size: tuple[int, int, int]
    origin: tuple[int, int, int]
    layers: list[array.array]
    block_palette: list[Tag]
    block_position_data: dict[str, Tag]
    entities: list[Tag]


def load_structure(path: str) -> LoadedStructure:
    root_name, root = read_nbt(path)
    if root.tag_id != 10:
        raise NBTError("ルートタグがCompoundではありません")

    compression_tag = root.value.get("compression")
    compression = 0
    if compression_tag is not None:
        if compression_tag.tag_id != 3 or compression_tag.value not in (0, 1):
            raise NBTError("compressionはIntTagの0または1である必要があります")
        compression = compression_tag.value
    if compression == 1:
        stored_structure = root.value.get("structure")
        if stored_structure is None or stored_structure.tag_id != 7:
            raise NBTError("compression=1 ですが、structureが圧縮バイト配列ではありません")
        root.value["structure"] = _read_compressed_structure(bytes(stored_structure.value))

    version = root.value.get("format_version")
    if version is None or version.tag_id != 3 or version.value != 2:
        value = version.value if version else "なし"
        raise NBTError(
            f"{os.path.basename(path)} はformat_version 2ではありません (検出値: {value})。"
            "このツールはバージョン1を読み込みません。"
        )

    size_values = int_list(root.value.get("size", Tag(1, 0)), "size")
    if len(size_values) != 3 or any(v <= 0 for v in size_values):
        raise NBTError("sizeは正の整数3個 [X,Y,Z] である必要があります")
    sx, sy, sz = size_values
    cells = sx * sy * sz

    origin_tag = root.value.get("structure_world_origin")
    origin_values = int_list(origin_tag, "structure_world_origin") if origin_tag else [0, 0, 0]
    if len(origin_values) != 3:
        raise NBTError("structure_world_originは整数3個 [X,Y,Z] である必要があります")

    structure = get_compound(root, "structure", "ルート")
    indices_tag = structure.value.get("block_indices")
    if indices_tag is None or indices_tag.tag_id != 9:
        raise NBTError("structure.block_indicesがListではありません")
    elem_type, layers_tags = indices_tag.value
    if elem_type != 11:
        raise NBTError(
            "block_indicesがIntArrayTagではありません。format_version 2のファイルを指定してください。"
        )
    if len(layers_tags) not in (1, 2):
        raise NBTError("format_version 2のblock_indicesは1層または2層である必要があります")
    layers: list[array.array] = []
    for i, layer in enumerate(layers_tags):
        if layer.tag_id != 11 or len(layer.value) != cells:
            raise NBTError(f"block_indices[{i}]の要素数がsizeの積({cells})と一致しません")
        layers.append(layer.value)

    palette_root = get_compound(structure, "palette", "structure")
    if set(palette_root.value.keys()) != {"default"}:
        raise NBTError("default以外のpalette variantを持つmcstructureは未対応です")
    default = get_compound(palette_root, "default", "structure.palette")
    palette_tag = default.value.get("block_palette")
    if palette_tag is None or palette_tag.tag_id != 9:
        raise NBTError("palette.default.block_paletteがListではありません")
    block_palette = tag_list(palette_tag, 10)
    if not block_palette:
        raise NBTError("block_paletteが空です")
    if not any(
        ent.tag_id == 10
        and ent.value.get("name") is not None
        and ent.value["name"].tag_id == 8
        and ent.value["name"].value == "minecraft:air"
        for ent in block_palette
    ):
        raise NBTError("block_paletteにminecraft:airがありません")

    bpd_tag = default.value.get("block_position_data", Tag(10, {}))
    if bpd_tag.tag_id != 10:
        raise NBTError("block_position_dataがCompoundではありません")
    for key in bpd_tag.value:
        try:
            index = int(key)
        except ValueError as e:
            raise NBTError(f"block_position_dataのキー'{key}'が整数ではありません") from e
        if index < 0 or index >= cells:
            raise NBTError(f"block_position_dataのキー'{key}'がsizeの範囲外です")

    entities_tag = structure.value.get("entities")
    if entities_tag is None or entities_tag.tag_id != 9:
        raise NBTError("structure.entitiesがListではありません")
    entities = tag_list(entities_tag, 10)

    # Every non-void palette index used by either layer must exist.
    for layer_no, layer in enumerate(layers):
        for idx in layer:
            if idx != -1 and (idx < 0 or idx >= len(block_palette)):
                raise NBTError(f"block_indices[{layer_no}]に無効なpalette index {idx}があります")

    return LoadedStructure(
        path=path,
        root_name=root_name,
        root=root,
        size=(sx, sy, sz),
        origin=tuple(origin_values),
        layers=layers,
        block_palette=block_palette,
        block_position_data=bpd_tag.value,
        entities=entities,
    )


def _add_numeric_if_present(compound: Tag, key: str, delta: int) -> None:
    """Translate an existing numeric coordinate while preserving its NBT type."""
    if compound.tag_id != 10:
        return
    t = compound.value.get(key)
    if t is None or t.tag_id not in (1, 2, 3, 4, 5, 6):
        return
    value = t.value + delta
    if t.tag_id == 1 and not -128 <= value <= 127:
        t.tag_id, t.value = 3, int(value)
    elif t.tag_id == 2 and not -32768 <= value <= 32767:
        t.tag_id, t.value = 3, int(value)
    elif t.tag_id in (1, 2, 3, 4):
        t.value = int(value)
    else:
        t.value = float(value)


def _translate_entity(entity: Tag, source_origin: tuple[int, int, int], target_offset: tuple[int, int, int]) -> Tag:
    """Copy an entity and move its common position fields into merged coordinates."""
    result = copy.deepcopy(entity)
    shift = tuple(target_offset[i] - source_origin[i] for i in range(3))
    if result.tag_id != 10:
        return result

    # In mcstructure, entity Pos values are saved in world coordinates. The output
    # origin is [0,0,0], so shift them by target offset minus their original origin.
    pos = result.value.get("Pos")
    if pos is not None and pos.tag_id == 9:
        et, items = pos.value
        if len(items) >= 3 and et in (3, 4, 5, 6):
            for axis in range(3):
                items[axis].value = items[axis].value + shift[axis]

    # Handle wrapper-style data should a producer use these optional field names.
    for key in ("pos", "block_position"):
        p = result.value.get(key)
        if p is not None and p.tag_id == 9:
            et, items = p.value
            if len(items) >= 3 and et in (1, 2, 3, 4, 5, 6):
                for axis in range(3):
                    items[axis].value = items[axis].value + shift[axis]
    return result


# --------------------------- Mirror helpers ---------------------------

def _mirror_palette_entry(entry: Tag, axis: str) -> Tag:
    """Mirror common horizontal Bedrock block states; unknown states are preserved."""
    result = copy.deepcopy(entry)
    if result.tag_id != 10:
        return result
    states = result.value.get("states")
    if states is None or states.tag_id != 10:
        return result
    st = states.value
    four_way = (
        "facing_direction" in st and (
            (result.value.get("name") is not None and result.value["name"].tag_id == 8 and (
                result.value["name"].value.endswith("_glazed_terracotta")
                or result.value["name"].value.endswith("_head")
                or result.value["name"].value.endswith("_skull")
                or "hanging_sign" in result.value["name"].value
            ))
        )
    )
    for key, value_tag in st.items():
        if key in {"cardinal_direction", "minecraft:cardinal_direction", "facing", "minecraft:facing", "minecraft:block_face", "block_face", "torch_facing_direction"} and value_tag.tag_id == 8:
            swaps = {"east": "west", "west": "east"} if axis == "x" else {"north": "south", "south": "north"}
            value_tag.value = swaps.get(value_tag.value, value_tag.value)
        elif key == "facing_direction" and value_tag.tag_id in (1, 2, 3, 4):
            value = int(value_tag.value)
            if four_way and 0 <= value <= 3:
                # Legacy four-way encoding: 0=south, 1=west, 2=north, 3=east.
                swaps = {1: 3, 3: 1} if axis == "x" else {0: 2, 2: 0}
                value_tag.value = swaps.get(value, value)
            else:
                swaps = {4: 5, 5: 4} if axis == "x" else {2: 3, 3: 2}
                value_tag.value = swaps.get(value, value)
        elif key in {"direction", "weirdo_direction"} and value_tag.tag_id in (1, 2, 3, 4):
            value = int(value_tag.value)
            if key == "weirdo_direction":
                swaps = {0: 1, 1: 0} if axis == "x" else {2: 3, 3: 2}
            else:
                swaps = {1: 3, 3: 1} if axis == "x" else {0: 2, 2: 0}
            value_tag.value = swaps.get(value, value)
        elif key in {"orientation", "minecraft:orientation"} and value_tag.tag_id == 8:
            swaps = {"east": "west", "west": "east"} if axis == "x" else {"north": "south", "south": "north"}
            parts = value_tag.value.split("_")
            value_tag.value = "_".join(swaps.get(part, part) for part in parts)
        elif key in {"hinge", "door_hinge"} and value_tag.tag_id == 8:
            value_tag.value = {"left": "right", "right": "left"}.get(value_tag.value, value_tag.value)
        elif key == "door_hinge_bit" and value_tag.tag_id in (1, 2, 3, 4):
            value_tag.value = 0 if int(value_tag.value) else 1
        elif key == "rail_direction" and value_tag.tag_id in (1, 2, 3, 4):
            rail = int(value_tag.value)
            rail_x = {2: 3, 3: 2, 6: 7, 7: 6, 8: 9, 9: 8}
            rail_z = {4: 5, 5: 4, 6: 9, 9: 6, 7: 8, 8: 7}
            value_tag.value = (rail_x if axis == "x" else rail_z).get(rail, rail)
        elif key == "ground_sign_direction" and value_tag.tag_id in (1, 2, 3, 4):
            direction = int(value_tag.value) % 16
            value_tag.value = (-direction if axis == "x" else 8 - direction) % 16
    # Wall connection states are paired once after the loop so they are not swapped twice.
    for north_key in ("wall_connection_type_north", "minecraft:wall_connection_type_north"):
        south_key = north_key.replace("north", "south")
        if axis == "z" and north_key in st and south_key in st:
            st[north_key], st[south_key] = st[south_key], st[north_key]
    for east_key in ("wall_connection_type_east", "minecraft:wall_connection_type_east"):
        west_key = east_key.replace("east", "west")
        if axis == "x" and east_key in st and west_key in st:
            st[east_key], st[west_key] = st[west_key], st[east_key]
    return result


def _mirror_entity(entity: Tag, source: LoadedStructure, axis: str) -> Tag:
    result = copy.deepcopy(entity)
    if result.tag_id != 10:
        return result
    sx, _sy, sz = source.size
    ox, _oy, oz = source.origin
    pos = result.value.get("Pos")
    if pos is not None and pos.tag_id == 9:
        et, values = pos.value
        if len(values) >= 3 and et in (3, 4, 5, 6):
            if axis == "x":
                _set_number(values[0], ox + sx - (float(values[0].value) - ox))
            else:
                _set_number(values[2], oz + sz - (float(values[2].value) - oz))
    for key in ("pos", "block_position"):
        p = result.value.get(key)
        if p is not None and p.tag_id == 9:
            et, values = p.value
            if len(values) >= 3 and et in (1, 2, 3, 4, 5, 6):
                if axis == "x":
                    _set_number(values[0], ox + sx - (float(values[0].value) - ox))
                else:
                    _set_number(values[2], oz + sz - (float(values[2].value) - oz))
    motion = result.value.get("Motion")
    if motion is not None and motion.tag_id == 9:
        et, values = motion.value
        if len(values) >= 3 and et in (5, 6):
            idx = 0 if axis == "x" else 2
            _set_number(values[idx], -float(values[idx].value))
    rotation = result.value.get("Rotation")
    if rotation is not None and rotation.tag_id == 9:
        et, values = rotation.value
        if values and et in (5, 6):
            yaw = float(values[0].value)
            _set_number(values[0], (180.0 - yaw) % 360.0 if axis == "x" else (-yaw) % 360.0)
    return result


def _mirror_loaded_structure_in_place(result: LoadedStructure, axis: str) -> LoadedStructure:
    """Reflect an already-isolated structure in place across its local X/Z plane."""
    axis = axis.lower()
    if axis not in {"x", "z"}:
        raise NBTError("ミラー軸はXまたはZにしてください")
    sx, sy, sz = result.size
    new_layers: list[array.array] = []
    for old_layer in result.layers:
        new_layer = array.array("i", [-1]) * (sx * sy * sz)
        for x in range(sx):
            nx = sx - 1 - x if axis == "x" else x
            for y in range(sy):
                start = (x * sy + y) * sz
                nstart = (nx * sy + y) * sz
                if axis == "z":
                    for z in range(sz):
                        new_layer[nstart + (sz - 1 - z)] = old_layer[start + z]
                else:
                    new_layer[nstart:nstart + sz] = old_layer[start:start + sz]
        new_layers.append(new_layer)
    result.layers = new_layers
    result.block_palette = [_mirror_palette_entry(entry, axis) for entry in result.block_palette]
    new_bpd: dict[str, Tag] = {}
    for old_key, record in result.block_position_data.items():
        old_index = int(old_key)
        x = old_index // (sy * sz)
        y = (old_index // sz) % sy
        z = old_index % sz
        nx = sx - 1 - x if axis == "x" else x
        nz = sz - 1 - z if axis == "z" else z
        new_index = (nx * sy + y) * sz + nz
        relocated = copy.deepcopy(record)
        if relocated.tag_id == 10:
            bed = relocated.value.get("block_entity_data")
            if bed is not None and bed.tag_id == 10:
                if axis == "x":
                    _set_number(bed.value.get("x", Tag(0, None)), result.origin[0] + nx)
                    pair = bed.value.get("pairx")
                    if pair is not None and pair.tag_id in (1, 2, 3, 4, 5, 6):
                        px = float(pair.value) - result.origin[0]
                        if 0 <= px < sx:
                            _set_number(pair, result.origin[0] + sx - 1 - px)
                else:
                    _set_number(bed.value.get("z", Tag(0, None)), result.origin[2] + nz)
                    pair = bed.value.get("pairz")
                    if pair is not None and pair.tag_id in (1, 2, 3, 4, 5, 6):
                        pz = float(pair.value) - result.origin[2]
                        if 0 <= pz < sz:
                            _set_number(pair, result.origin[2] + sz - 1 - pz)
        new_bpd[str(new_index)] = relocated
    result.block_position_data = new_bpd
    result.entities = [_mirror_entity(entity, result, axis) for entity in result.entities]
    result.root.value["size"] = make_int_list(list(result.size))
    structure = get_compound(result.root, "structure", "ミラー済みルート")
    structure.value["block_indices"] = Tag(9, (11, [Tag(11, layer) for layer in result.layers]))
    structure.value["entities"] = Tag(9, (10, result.entities))
    palette_root = get_compound(structure, "palette", "ミラー済みstructure")
    default = get_compound(palette_root, "default", "ミラー済みpalette")
    default.value["block_palette"] = Tag(9, (10, result.block_palette))
    default.value["block_position_data"] = Tag(10, result.block_position_data)
    return result


def mirror_loaded_structure(src: LoadedStructure, axis: str) -> LoadedStructure:
    """Return a mirrored copy of a structure, leaving the source untouched."""
    return _mirror_loaded_structure_in_place(copy.deepcopy(src), axis)


def transform_loaded_structure(src: LoadedStructure, degrees: int = 0, mirror_x: bool = False, mirror_z: bool = False) -> LoadedStructure:
    """Return an isolated transformed copy; never mutate a GUI/source cache object."""
    transformed = copy.deepcopy(src)
    if mirror_x:
        transformed = _mirror_loaded_structure_in_place(transformed, "x")
    if mirror_z:
        transformed = _mirror_loaded_structure_in_place(transformed, "z")
    if degrees:
        transformed = rotate_loaded_structure(transformed, degrees)
    return transformed


# --------------------------- Rotation helpers ---------------------------

_CARDINAL_CW = {"north": "east", "east": "south", "south": "west", "west": "north"}
_FACING6_CW = {2: 5, 5: 3, 3: 4, 4: 2, 0: 0, 1: 1}  # down/up unchanged


def _rotate_local_xz(x: int, z: int, sx: int, sz: int, turns: int) -> tuple[int, int]:
    """Rotate a block coordinate clockwise in the X/Z plane around the box."""
    t = turns % 4
    if t == 0:
        return x, z
    if t == 1:
        return sz - 1 - z, x
    if t == 2:
        return sx - 1 - x, sz - 1 - z
    return z, sx - 1 - x


def _rotate_continuous_xz(x: float, z: float, sx: int, sz: int, turns: int) -> tuple[float, float]:
    """Rotate a continuous position (entity coordinates, not block indices)."""
    t = turns % 4
    if t == 0:
        return x, z
    if t == 1:
        return sz - z, x
    if t == 2:
        return sx - x, sz - z
    return z, sx - x


def _set_number(tag: Tag, value: float | int) -> None:
    """Set a numeric NBT value without unnecessarily changing its tag type."""
    if tag.tag_id not in (1, 2, 3, 4, 5, 6):
        return
    if tag.tag_id in (1, 2, 3, 4):
        v = int(round(value))
        limits = {1: (-128, 127), 2: (-32768, 32767), 3: (-2147483648, 2147483647)}
        if tag.tag_id in limits and not limits[tag.tag_id][0] <= v <= limits[tag.tag_id][1]:
            tag.tag_id = 3 if -(2**31) <= v < 2**31 else 4
        tag.value = v
    else:
        tag.value = float(value)


def _rotate_palette_entry(entry: Tag, turns: int) -> Tag:
    """Rotate known vanilla Bedrock directional block states; preserve all other NBT."""
    result = copy.deepcopy(entry)
    if turns % 4 == 0 or result.tag_id != 10:
        return result
    states = result.value.get("states")
    if states is None or states.tag_id != 10:
        return result
    block_name_tag = result.value.get("name")
    block_name = block_name_tag.value if block_name_tag and block_name_tag.tag_id == 8 else ""
    st = states.value
    t = turns % 4

    # Six-direction facing_direction (0=down, 1=up, 2=north, 3=south, 4=west, 5=east).
    facing = st.get("facing_direction")
    is_four_way_facing = (
        block_name.endswith("_glazed_terracotta")
        or block_name in {
            "minecraft:ladder", "minecraft:wall_sign", "minecraft:wall_banner",
            "minecraft:melon_stem", "minecraft:pumpkin_stem", "minecraft:standing_sign",
        }
        or "hanging_sign" in block_name
        or block_name.endswith("_head")
        or block_name.endswith("_skull")
    )
    if facing is not None and facing.tag_id in (1, 2, 3, 4):
        v = int(facing.value)
        if is_four_way_facing and 0 <= v <= 3:
            # Legacy 4-way Bedrock states: 0=south, 1=west, 2=north, 3=east.
            facing.value = (v + t) % 4
        elif 0 <= v <= 5:
            facing.value = _FACING6_CW.get(v, v) if t == 1 else _rotate_facing6(v, t)

    # Older and newer string state names for horizontal directions.
    horizontal_string_keys = {
        "minecraft:cardinal_direction", "cardinal_direction", "minecraft:facing_direction",
        "torch_facing_direction", "facing", "minecraft:block_face", "block_face",
    }
    for key, value_tag in st.items():
        if value_tag.tag_id != 8:
            continue
        value = value_tag.value
        if key in horizontal_string_keys and value in ("north", "east", "south", "west"):
            for _ in range(t):
                value = _CARDINAL_CW[value]
            value_tag.value = value
        elif key in ("pillar_axis", "axis") and value in ("x", "y", "z") and t % 2:
            value_tag.value = "z" if value == "x" else "x" if value == "z" else "y"
        elif key in ("orientation", "minecraft:orientation"):
            # Newer orientation state strings often encode two directions (e.g. east_up).
            parts = value.split("_")
            if len(parts) == 2 and all(part in {"down", "up", "north", "south", "east", "west"} for part in parts):
                for i, part in enumerate(parts):
                    for _ in range(t):
                        part = _CARDINAL_CW.get(part, part)
                    parts[i] = part
                value_tag.value = "_".join(parts)

        elif key == "minecraft:corner" and value in ("inner_left", "inner_right", "outer_left", "outer_right"):
            # With clockwise geometry rotation, handedness is preserved; only direction-coded
            # corner states need block-trait-specific handling, so leave this value unchanged.
            pass

    # Sixteen-way standing sign/banner/head rotation. Bedrock 0=south; +4 is west.
    for key in ("ground_sign_direction", "minecraft:sixteen_way_rotation", "rotation", "minecraft:rotation"):
        value_tag = st.get(key)
        if value_tag is not None and value_tag.tag_id in (1, 2, 3, 4):
            v = int(value_tag.value)
            if 0 <= v <= 15:
                value_tag.value = (v + 4 * t) % 16

    # Legacy four-way orientation states used by stairs and other vanilla blocks.
    for key in ("weirdo_direction", "direction"):
        value_tag = st.get(key)
        if value_tag is not None and value_tag.tag_id in (1, 2, 3, 4):
            v = int(value_tag.value)
            if 0 <= v <= 3:
                if key == "weirdo_direction":
                    # Stairs: 0=east, 1=west, 2=south, 3=north.
                    mapping = {0: 2, 2: 1, 1: 3, 3: 0}
                    for _ in range(t):
                        v = mapping[v]
                    value_tag.value = v
                else:
                    value_tag.value = (v + t) % 4

    return result


def _rotate_facing6(value: int, turns: int) -> int:
    for _ in range(turns % 4):
        value = _FACING6_CW.get(value, value)
    return value


def _rotate_entity(entity: Tag, source: LoadedStructure, turns: int) -> Tag:
    """Rotate entity position/motion/yaw while retaining all unrelated entity NBT."""
    result = copy.deepcopy(entity)
    if turns % 4 == 0 or result.tag_id != 10:
        return result
    sx, _sy, sz = source.size
    ox, oy, oz = source.origin
    comp = result.value
    for key in ("Pos", "pos", "block_position"):
        pos = comp.get(key)
        if pos is None or pos.tag_id != 9:
            continue
        element_type, values = pos.value
        if len(values) < 3 or element_type not in (1, 2, 3, 4, 5, 6):
            continue
        x, z = float(values[0].value) - ox, float(values[2].value) - oz
        rx, rz = _rotate_continuous_xz(x, z, sx, sz, turns)
        _set_number(values[0], ox + rx)
        _set_number(values[2], oz + rz)
    motion = comp.get("Motion")
    if motion is not None and motion.tag_id == 9:
        et, values = motion.value
        if len(values) >= 3 and et in (1, 2, 3, 4, 5, 6):
            vx, vz = float(values[0].value), float(values[2].value)
            for _ in range(turns % 4):
                vx, vz = -vz, vx
            _set_number(values[0], vx)
            _set_number(values[2], vz)
    rot = comp.get("Rotation")
    if rot is not None and rot.tag_id == 9:
        et, values = rot.value
        if values and et in (1, 2, 3, 4, 5, 6):
            # Bedrock entity yaw follows the opposite sign to this X/Z coordinate turn.
            _set_number(values[0], (float(values[0].value) - 90 * turns) % 360)
    return result


def rotate_loaded_structure(src: LoadedStructure, degrees: int) -> LoadedStructure:
    """Rotate a loaded structure clockwise by 0/90/180/270 degrees in the X/Z plane."""
    if degrees not in (0, 90, 180, 270):
        raise NBTError("回転角度は0、90、180、270度のいずれかにしてください")
    turns = degrees // 90
    if turns == 0:
        return src
    sx, sy, sz = src.size
    out_sx, out_sz = (sz, sx) if turns % 2 else (sx, sz)

    # Rotate each palette entry, preserving its other tags and per-state NBT types.
    src.block_palette = [_rotate_palette_entry(entry, turns) for entry in src.block_palette]

    rotated_layers: list[array.array] = []
    for old_layer in src.layers:
        new_layer = array.array("i", [-1]) * (out_sx * sy * out_sz)
        for x in range(sx):
            for y in range(sy):
                old_row = (x * sy + y) * sz
                for z in range(sz):
                    rx, rz = _rotate_local_xz(x, z, sx, sz, turns)
                    new_layer[(rx * sy + y) * out_sz + rz] = old_layer[old_row + z]
        rotated_layers.append(new_layer)
    src.layers = rotated_layers

    new_bpd: dict[str, Tag] = {}
    for old_key, record in src.block_position_data.items():
        old_idx = int(old_key)
        x = old_idx // (sy * sz)
        y = (old_idx // sz) % sy
        z = old_idx % sz
        rx, rz = _rotate_local_xz(x, z, sx, sz, turns)
        new_idx = (rx * sy + y) * out_sz + rz
        relocated = copy.deepcopy(record)
        if relocated.tag_id == 10:
            bed = relocated.value.get("block_entity_data")
            if bed is not None and bed.tag_id == 10:
                # Standard block-entity x/y/z are absolute source-world coordinates.
                for key, value in (("x", src.origin[0] + rx), ("y", src.origin[1] + y), ("z", src.origin[2] + rz)):
                    if key in bed.value:
                        _set_number(bed.value[key], value)
                # Double-container pair links are absolute block coordinates too.
                pairx, pairz = bed.value.get("pairx"), bed.value.get("pairz")
                if pairx is not None and pairz is not None and pairx.tag_id in (1, 2, 3, 4) and pairz.tag_id in (1, 2, 3, 4):
                    lx, lz = int(pairx.value) - src.origin[0], int(pairz.value) - src.origin[2]
                    prx, prz = _rotate_local_xz(lx, lz, sx, sz, turns)
                    _set_number(pairx, src.origin[0] + prx)
                    _set_number(pairz, src.origin[2] + prz)
        new_bpd[str(new_idx)] = relocated
    src.block_position_data = new_bpd

    src.entities = [_rotate_entity(entity, src, turns) for entity in src.entities]
    src.size = (out_sx, sy, out_sz)

    # Keep the copied root internally coherent before it becomes the output template.
    src.root.value["size"] = make_int_list(list(src.size))
    structure = get_compound(src.root, "structure", "回転済みルート")
    structure.value["block_indices"] = Tag(9, (11, [Tag(11, layer) for layer in src.layers]))
    structure.value["entities"] = Tag(9, (10, src.entities))
    palette_root = get_compound(structure, "palette", "回転済みstructure")
    default = get_compound(palette_root, "default", "回転済みpalette")
    default.value["block_palette"] = Tag(9, (10, src.block_palette))
    default.value["block_position_data"] = Tag(10, src.block_position_data)
    return src


def calculate_repeat_positions(
    start: tuple[int, int, int],
    rotated_size: tuple[int, int, int],
    count: int,
    mode: str,
    columns: int = 1,
) -> list[tuple[int, int, int]]:
    """Create gapless repeated placements; count includes the original instance.

    Spacing uses the rotated X/Z dimensions, so adjacent copies touch exactly
    without a gap (unless the source structure itself contains air blocks).
    """
    if not 1 <= int(count) <= 100:
        raise NBTError("連続配置の個数は1～100個にしてください")
    count = int(count)
    x0, y0, z0 = (int(v) for v in start)
    sx, _sy, sz = (int(v) for v in rotated_size)
    if sx <= 0 or sz <= 0:
        raise NBTError("構造物のX/Zサイズが不正です")
    if mode not in {"x", "z", "grid"}:
        raise NBTError("配置方法が不正です")
    if mode == "grid":
        columns = int(columns)
        if not 1 <= columns <= 100:
            raise NBTError("グリッドの列数は1～100にしてください")
    else:
        columns = count if mode == "x" else 1

    positions: list[tuple[int, int, int]] = []
    for i in range(count):
        if mode == "x":
            dx, dz = i * sx, 0
        elif mode == "z":
            dx, dz = 0, i * sz
        else:
            dx, dz = (i % columns) * sx, (i // columns) * sz
        positions.append((x0 + dx, y0, z0 + dz))
    return positions


def merge_structures(inputs: list[tuple], output_path: str, overlap_mode: str = "last_defined") -> dict[str, Any]:
    """Merge ``[(path, target_origin_xyz), ...]`` to a v2 mcstructure file."""
    if len(inputs) < 1:
        raise NBTError("入力ファイルを1つ以上指定してください")
    if overlap_mode not in {"last_defined", "first_non_air", "last_including_void"}:
        raise NBTError("重なり処理の設定が不正です")

    sources: list[tuple[LoadedStructure, tuple[int, int, int]]] = []
    loaded_cache: dict[tuple[str, int, bool, bool], LoadedStructure] = {}
    for item in inputs:
        if len(item) == 2:
            path, offset = item
            rotation, mirror_x, mirror_z = 0, False, False
        elif len(item) == 3:
            path, offset, rotation = item
            mirror_x, mirror_z = False, False
        elif len(item) == 5:
            path, offset, rotation, mirror_x, mirror_z = item
        else:
            raise NBTError("入力指定は(path, 座標[, 回転角度[, ミラーX, ミラーZ]])です")
        rotation = int(rotation)
        mirror_x, mirror_z = bool(mirror_x), bool(mirror_z)
        cache_key = (os.path.normcase(os.path.abspath(path)), rotation, mirror_x, mirror_z)
        src = loaded_cache.get(cache_key)
        if src is None:
            src = transform_loaded_structure(load_structure(path), rotation, mirror_x, mirror_z)
            loaded_cache[cache_key] = src
        sources.append((src, tuple(int(v) for v in offset)))

    # Normalize the overall placement so the minimum X/Y/Z is 0 in the output.
    min_pos = tuple(min(offset[a] for _, offset in sources) for a in range(3))
    normalized: list[tuple[LoadedStructure, tuple[int, int, int]]] = [
        (src, tuple(offset[a] - min_pos[a] for a in range(3)))
        for src, offset in sources
    ]
    out_x = max(pos[0] + src.size[0] for src, pos in normalized)
    out_y = max(pos[1] + src.size[1] for src, pos in normalized)
    out_z = max(pos[2] + src.size[2] for src, pos in normalized)
    total_cells = out_x * out_y * out_z
    if total_cells > MAX_OUTPUT_CELLS:
        raise NBTError(
            f"結合後のサイズは{out_x}×{out_y}×{out_z}={total_cells:,}ブロックです。"
            f"安全上限({MAX_OUTPUT_CELLS:,})を超えています。2層のブロック配列だけで約{total_cells * 8 / (1024 ** 2):,.0f} MiB必要です。"
        )

    # Construct a unified palette. Air is deliberately placed at output index 0
    # because empty, unoccupied areas of the merged volume must be air.
    output_palette: list[Tag] = []
    palette_index_by_signature: dict[Any, int] = {}
    for src, _ in normalized:
        for entry in src.block_palette:
            name = entry.value.get("name") if entry.tag_id == 10 else None
            if name is not None and name.tag_id == 8 and name.value == "minecraft:air":
                sig = tag_signature(entry)
                if sig not in palette_index_by_signature:
                    palette_index_by_signature[sig] = len(output_palette)
                    output_palette.append(copy.deepcopy(entry))
                break
        else:
            raise NBTError(f"{src.path} のblock_paletteにminecraft:airがありません")
        break

    for src, _ in normalized:
        for entry in src.block_palette:
            sig = tag_signature(entry)
            if sig not in palette_index_by_signature:
                palette_index_by_signature[sig] = len(output_palette)
                output_palette.append(copy.deepcopy(entry))

    remaps: list[list[int]] = []
    for src, _ in normalized:
        remaps.append([palette_index_by_signature[tag_signature(entry)] for entry in src.block_palette])

    air_index = 0
    out_layers = [array.array("i", [air_index]) * total_cells, array.array("i", [-1]) * total_cells]
    bpd_out: dict[str, Tag] = {}
    entities_out: list[Tag] = []

    # File order controls conflict resolution: later files overwrite earlier
    # blocks when their index is not -1. A void (-1) never erases another input.
    for source_number, (src, (ox, oy, oz)) in enumerate(normalized):
        sx, sy, sz = src.size
        remap = remaps[source_number]
        source_primary = src.layers[0]
        source_bpd_indices = {int(key) for key in src.block_position_data}
        primary_bpd_wins: set[int] = set()

        # Copy each contiguous Z row, respecting the Bedrock index formula:
        # index = (x * sizeY + y) * sizeZ + z.
        for x in range(sx):
            dx = ox + x
            for y in range(sy):
                dy = oy + y
                src_start = (x * sy + y) * sz
                dst_start = (dx * out_y + dy) * out_z + oz
                for layer_no in range(2):
                    source_layer = src.layers[layer_no] if layer_no < len(src.layers) else None
                    dest_layer = out_layers[layer_no]
                    for z in range(sz):
                        out_index = dst_start + z
                        old_idx = source_layer[src_start + z] if source_layer is not None else -1
                        if old_idx == -1:
                            if overlap_mode == "last_including_void":
                                dest_layer[out_index] = air_index if layer_no == 0 else -1
                            continue
                        if old_idx < 0 or old_idx >= len(remap):
                            raise NBTError(f"{os.path.basename(src.path)} に無効なpalette index {old_idx}があります")
                        mapped_idx = remap[old_idx]
                        if overlap_mode == "first_non_air":
                            if layer_no == 0:
                                existing = dest_layer[out_index]
                                existing_name = output_palette[existing].value.get("name") if 0 <= existing < len(output_palette) and output_palette[existing].tag_id == 10 else None
                                existing_is_air = existing_name is not None and existing_name.tag_id == 8 and existing_name.value in {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
                                incoming_name = src.block_palette[old_idx].value.get("name") if src.block_palette[old_idx].tag_id == 10 else None
                                incoming_is_air = incoming_name is not None and incoming_name.tag_id == 8 and incoming_name.value in {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}
                                if not existing_is_air or incoming_is_air:
                                    continue
                            elif dest_layer[out_index] != -1:
                                continue
                        dest_layer[out_index] = mapped_idx
                        if layer_no == 0 and src_start + z in source_bpd_indices:
                            primary_bpd_wins.add(src_start + z)

                # A later explicit primary-layer write supersedes previous block-entity data.
                for z in range(sz):
                    out_index = (dx * out_y + dy) * out_z + (oz + z)
                    primary_idx = source_primary[src_start + z] if len(src.layers) else -1
                    if overlap_mode == "last_including_void":
                        # Every cell inside the later structure's bounding region is
                        # authoritative in this mode, including a void (-1) cell.
                        bpd_out.pop(str(out_index), None)
                    elif primary_idx != -1 and overlap_mode != "first_non_air":
                        bpd_out.pop(str(out_index), None)

        # Relocate block_position_data by decoded local coordinates. X/Y/Z stored
        # inside block_entity_data are rewritten to the resulting output coordinate.
        for old_key, record in src.block_position_data.items():
            old_index = int(old_key)
            if overlap_mode == "first_non_air" and old_index not in primary_bpd_wins:
                continue
            x = old_index // (sy * sz)
            y = (old_index // sz) % sy
            z = old_index % sz
            dx, dy, dz = ox + x, oy + y, oz + z
            new_index = (dx * out_y + dy) * out_z + dz
            relocated = copy.deepcopy(record)
            if relocated.tag_id == 10:
                bed = relocated.value.get("block_entity_data")
                if bed is not None and bed.tag_id == 10:
                    # block_entity_data coordinates are saved in the source world's
                    # coordinate space. Convert them to the output's normalized space.
                    shift = (ox - src.origin[0], oy - src.origin[1], oz - src.origin[2])
                    for coordinate, delta in zip(("x", "y", "z"), shift):
                        _add_numeric_if_present(bed, coordinate, delta)
                    # Double-container links (e.g. chest pair coordinates) are
                    # absolute positions too, when present in the source data.
                    _add_numeric_if_present(bed, "pairx", shift[0])
                    _add_numeric_if_present(bed, "pairz", shift[2])
            if overlap_mode != "first_non_air" or str(new_index) not in bpd_out:
                bpd_out[str(new_index)] = relocated

        for entity in src.entities:
            entities_out.append(_translate_entity(entity, src.origin, (ox, oy, oz)))

    # Deep-copy the first source so unknown non-position metadata is retained.
    output_root = copy.deepcopy(sources[0][0].root)
    root_values = output_root.value
    root_values["format_version"] = Tag(3, 2)
    # Bedrock 26.60 introduced optional zlib compression for v2 structures.
    # Explicit 0 ensures this output is uncompressed and readable by v2 builds
    # both before and after that change.
    root_values["compression"] = Tag(3, 0)
    root_values["size"] = make_int_list([out_x, out_y, out_z])
    root_values["structure_world_origin"] = make_int_list([0, 0, 0])

    out_structure = get_compound(output_root, "structure", "出力ルート")
    out_structure.value["block_indices"] = Tag(
        9,
        (11, [Tag(11, out_layers[0]), Tag(11, out_layers[1])]),
    )
    out_structure.value["entities"] = Tag(9, (10, entities_out))
    out_palette = get_compound(out_structure, "palette", "出力structure")
    out_default = get_compound(out_palette, "default", "出力palette")
    out_default.value["block_palette"] = Tag(9, (10, output_palette))
    out_default.value["block_position_data"] = Tag(10, bpd_out)

    write_nbt(output_path, output_root, root_name="")

    # Drop references to the output IntArrays before re-reading the saved file.
    # This bounds peak memory for large repeated layouts.
    import gc
    del out_structure, out_default, out_palette, root_values, output_root, out_layers
    gc.collect()

    # Verify the saved file can be parsed again and that the core v2 dimensions
    # and layer lengths match. This is a structural validation, not an in-game test.
    check = load_structure(output_path)
    if check.size != (out_x, out_y, out_z):
        raise NBTError("保存後検証に失敗しました: sizeが一致しません")
    if len(check.layers) != 2 or any(len(layer) != total_cells for layer in check.layers):
        raise NBTError("保存後検証に失敗しました: block_indicesの配列長が一致しません")

    return {
        "size": (out_x, out_y, out_z),
        "cells": total_cells,
        "palette_entries": len(output_palette),
        "block_position_data": len(bpd_out),
        "entities": len(entities_out),
        "overlap_mode": overlap_mode,
        "output": output_path,
    }


def get_application_data_dir() -> Path:
    """Return a per-user persistent directory for autosave state and source copies."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        root = Path(base) / "McStructureMerger"
    else:
        root = Path.home() / ".mcstructure_merger"
    root.mkdir(parents=True, exist_ok=True)
    return root


class MergerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("mcstructure v2 結合ツール")
        self.root.geometry("1160x700")
        self.rows: list[dict[str, Any]] = []
        self.temp_dirs: list[tempfile.TemporaryDirectory] = []
        self.preview_window: tk.Toplevel | None = None
        self.preview_canvas: tk.Canvas | None = None
        self.surface_canvas: tk.Canvas | None = None
        self.preview_notebook: ttk.Notebook | None = None
        self.surface_tab: ttk.Frame | None = None
        self.surface_image: tk.PhotoImage | None = None
        self.surface_after_id: str | None = None
        # Cache top-down column results so zooming and panning do not repeatedly
        # walk every Y level of every structure. A bounded LRU avoids unbounded
        # memory growth when viewing very large projects.
        self.surface_column_cache: OrderedDict[tuple[int, int], tuple[int, int, int] | None] = OrderedDict()
        self.surface_cache_signature: tuple[Any, ...] | None = None
        self.surface_cache_limit = 180_000
        self.preview_transform: dict[str, float] = {}
        self.surface_transform: dict[str, float] = {}
        self.preview_views: dict[str, dict[str, float]] = {
            "layout": {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0},
            "surface": {"zoom": 1.0, "pan_x": 0.0, "pan_y": 0.0},
        }
        self.preview_drag: dict[str, Any] | None = None
        self.preview_pan_drag: dict[str, Any] | None = None
        self.preview_selected_id: int | None = None
        self._next_instance_id = 1
        self._next_repeat_group_id = 1
        self.selected_ids: set[int] = set()
        self.mirror_cache: dict[tuple[str, int, bool, bool], LoadedStructure] = {}
        self.overlap_mode_var = tk.StringVar(value="last_defined")
        self.overlap_display_var = tk.StringVar(value="後の構造物を優先（未設定セルは保持）")
        self.camera_yaw = math.radians(45.0)
        self.camera_pitch = math.radians(32.0)
        self.camera_zoom = 1.0
        self.camera_pan_x = 0.0
        self.camera_pan_y = 0.0
        self.iso_canvas: tk.Canvas | None = None
        self.iso_image = None
        self.iso_after_id: str | None = None
        self.iso_drag_state: dict[str, Any] | None = None
        self._browser_start_path = str(Path.home())
        self.app_data_dir = get_application_data_dir()
        self.autosave_file = self.app_data_dir / "autosave.json"
        self.autosave_sources_dir = self.app_data_dir / "sources"
        self.autosave_sources_dir.mkdir(parents=True, exist_ok=True)
        self._autosave_after_id = None
        self._history_restoring = False
        self.history: list[dict[str, Any]] = []
        self.history_index = -1
        self.history_limit = 60

        title = ttk.Label(root, text="Minecraft Bedrock .mcstructure v2 結合ツール（Python）", font=("Yu Gothic UI", 14, "bold"))
        title.pack(anchor="w", padx=12, pady=(12, 3))
        note = ttk.Label(
            root,
            text=(
                "version 2専用。『参照…』から.mcstructureとZIPを複数選択できます。作業は自動一時保存され、次回起動時に復元されます。\n"
                "チェック欄で複数選択し、移動・90度回転・X/Zミラー・整列が可能です。Undo/RedoはボタンまたはCtrl+Z/Ctrl+Y。重なりルールも選べます。\n"
                "『連続配置…』で最大100個までX/Z/グリッド配置。配置プレビューは64ブロック単位のスナップ、上面図、簡易マテリアル表示付き3D表示に対応します。"
            ),
            justify="left",
        )
        note.pack(anchor="w", padx=12, pady=(0, 8))

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(buttons, text="参照…", command=self.open_reference_dialog).pack(side="left")
        ttk.Button(buttons, text="連続配置…", command=self.open_repeat_dialog).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="配置プレビュー…", command=self.open_preview).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="全て削除", command=self.clear_rows).pack(side="left", padx=(6, 0))
        self.undo_button = ttk.Button(buttons, text="元に戻す", command=self.undo, state="disabled")
        self.undo_button.pack(side="right", padx=(4, 0))
        self.redo_button = ttk.Button(buttons, text="やり直す", command=self.redo, state="disabled")
        self.redo_button.pack(side="right", padx=(4, 0))

        editbar = ttk.Frame(root)
        editbar.pack(fill="x", padx=12, pady=(0, 7))
        ttk.Label(editbar, text="選択操作:").pack(side="left")
        ttk.Button(editbar, text="選択を移動…", command=self.move_selected_dialog).pack(side="left", padx=(5, 0))
        ttk.Button(editbar, text="90°回転", command=self.rotate_selected_group).pack(side="left", padx=(5, 0))
        ttk.Button(editbar, text="Xミラー", command=lambda: self.mirror_selected("x")).pack(side="left", padx=(5, 0))
        ttk.Button(editbar, text="Zミラー", command=lambda: self.mirror_selected("z")).pack(side="left", padx=(5, 0))
        ttk.Button(editbar, text="X左揃え", command=lambda: self.align_selected("x")).pack(side="left", padx=(5, 0))
        ttk.Button(editbar, text="Z手前揃え", command=lambda: self.align_selected("z")).pack(side="left", padx=(5, 0))
        ttk.Button(editbar, text="中央揃え", command=lambda: self.align_selected("center")).pack(side="left", padx=(5, 0))
        ttk.Button(editbar, text="選択解除", command=self.clear_selection).pack(side="left", padx=(5, 0))
        ttk.Label(editbar, text="重なり:").pack(side="left", padx=(14, 3))
        overlap_combo = ttk.Combobox(
            editbar, textvariable=self.overlap_display_var, state="readonly", width=34,
            values=("後の構造物を優先（未設定セルは保持）", "最初の構造物を優先（空気以外）", "後の構造物で領域全体を置換"),
        )
        overlap_combo.pack(side="left")
        overlap_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_overlap_mode_changed())
        overlap_combo.bind("<MouseWheel>", lambda _e: "break")

        headers = ttk.Frame(root)
        headers.pack(fill="x", padx=12)
        ttk.Label(headers, text="", width=4).grid(row=0, column=0)
        ttk.Label(headers, text="ファイル / 反転", width=39).grid(row=0, column=1, sticky="w")
        ttk.Label(headers, text="サイズ（回転後）", width=16).grid(row=0, column=2, sticky="w")
        ttk.Label(headers, text="X", width=8).grid(row=0, column=3)
        ttk.Label(headers, text="Y", width=8).grid(row=0, column=4)
        ttk.Label(headers, text="Z", width=8).grid(row=0, column=5)
        ttk.Label(headers, text="回転", width=9).grid(row=0, column=6)
        ttk.Label(headers, text="順序 / 操作", width=15).grid(row=0, column=7)

        area = ttk.Frame(root)
        area.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        self.canvas = tk.Canvas(area, highlightthickness=0)
        scrollbar = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window_id, width=e.width))
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        footer = ttk.Frame(root)
        footer.pack(fill="x", padx=12, pady=(0, 12))
        self.status = tk.StringVar(value="format_version 2 のファイルを追加してください。自動一時保存が有効です。")
        ttk.Label(footer, textvariable=self.status).pack(side="left", fill="x", expand=True)
        ttk.Button(footer, text="結合して保存…", command=self.merge).pack(side="right")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind_all("<Control-z>", lambda _e: (self.undo(), "break")[1])
        self.root.bind_all("<Control-y>", lambda _e: (self.redo(), "break")[1])
        self.root.bind_all("<Control-Shift-Z>", lambda _e: (self.redo(), "break")[1])
        self._load_autosave_on_startup()
        self._reset_history_to_current()

    def _cache_source_for_resume(self, path: str) -> str:
        """Persist selected source bytes so ZIP temporary extraction survives restart."""
        digest = hashlib.sha256()
        with open(path, "rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        target = self.autosave_sources_dir / f"{digest.hexdigest()}.mcstructure"
        if not target.exists():
            temp_target = target.with_suffix(".tmp")
            shutil.copyfile(path, temp_target)
            os.replace(temp_target, target)
        return str(target)

    def _capture_history_state(self) -> dict[str, Any]:
        rows = []
        for row in self.rows:
            rows.append({
                "instance_id": row["instance_id"], "path": row["path"],
                "display_path": row.get("display_path", row["path"]),
                "resume_path": row.get("resume_path"), "loaded": row["loaded"],
                "src_size": tuple(row["src_size"]), "origin": tuple(row["origin"]),
                "palette_names": list(row["palette_names"]),
                "x": row["x"].get(), "y": row["y"].get(), "z": row["z"].get(),
                "rotation": row["rotation"].get(),
                "mirror_x": bool(row.get("mirror_x", False)),
                "mirror_z": bool(row.get("mirror_z", False)),
                "repeat_group": row.get("repeat_group"),
                "repeat_number": row.get("repeat_number"),
                "repeat_total": row.get("repeat_total"),
            })
        return {"rows": rows, "selected_ids": sorted(self.selected_ids), "overlap_mode": self.overlap_mode_var.get()}

    @staticmethod
    def _history_signature(state: dict[str, Any]):
        return (
            tuple((r["instance_id"], r["path"], r["x"], r["y"], r["z"], r["rotation"], r["mirror_x"], r["mirror_z"], r["repeat_group"], r["repeat_number"], r["repeat_total"]) for r in state["rows"]),
            state["overlap_mode"],
        )

    def _reset_history_to_current(self):
        self.history = [self._capture_history_state()]
        self.history_index = 0
        self._update_history_buttons()

    def _commit_history(self, _label: str = "変更"):
        if self._history_restoring:
            return
        state = self._capture_history_state()
        if self.history and self._history_signature(state) == self._history_signature(self.history[self.history_index]):
            self._schedule_autosave()
            return
        del self.history[self.history_index + 1:]
        self.history.append(state)
        if len(self.history) > self.history_limit:
            self.history.pop(0)
        self.history_index = len(self.history) - 1
        self._update_history_buttons()
        self._schedule_autosave()

    def _update_history_buttons(self):
        if hasattr(self, "undo_button"):
            self.undo_button.configure(state="normal" if self.history_index > 0 else "disabled")
        if hasattr(self, "redo_button"):
            self.redo_button.configure(state="normal" if 0 <= self.history_index < len(self.history) - 1 else "disabled")

    def _apply_history_state(self, state: dict[str, Any]):
        self._history_restoring = True
        try:
            self.rows = []
            for item in state["rows"]:
                row = self._make_row_data(
                    item["path"], item["loaded"], (int(item["x"]), int(item["y"]), int(item["z"])),
                    int(item["rotation"]), item["palette_names"],
                )
                row["instance_id"] = int(item["instance_id"])
                row["display_path"] = item.get("display_path", item["path"])
                row["resume_path"] = item.get("resume_path")
                row["mirror_x"] = bool(item.get("mirror_x", False))
                row["mirror_z"] = bool(item.get("mirror_z", False))
                row["repeat_group"] = item.get("repeat_group")
                row["repeat_number"] = item.get("repeat_number")
                row["repeat_total"] = item.get("repeat_total")
                self.rows.append(row)
            self._next_instance_id = max((r["instance_id"] for r in self.rows), default=0) + 1
            self._next_repeat_group_id = max((r.get("repeat_group") or 0 for r in self.rows), default=0) + 1
            self.selected_ids = set(state.get("selected_ids", []))
            self.overlap_mode_var.set(state.get("overlap_mode", "last_defined"))
            self.overlap_display_var.set({
                "last_defined": "後の構造物を優先（未設定セルは保持）",
                "first_non_air": "最初の構造物を優先（空気以外）",
                "last_including_void": "後の構造物で領域全体を置換",
            }.get(self.overlap_mode_var.get(), "後の構造物を優先（未設定セルは保持）"))
            self.render_rows()
            self.redraw_preview_if_open()
        finally:
            self._history_restoring = False
        self._update_history_buttons()

    def undo(self):
        if self.history_index <= 0:
            return
        self.history_index -= 1
        self._apply_history_state(self.history[self.history_index])
        self.status.set("操作を元に戻しました。")
        self._schedule_autosave()

    def redo(self):
        if self.history_index >= len(self.history) - 1:
            return
        self.history_index += 1
        self._apply_history_state(self.history[self.history_index])
        self.status.set("操作をやり直しました。")
        self._schedule_autosave()

    def _autosave_payload(self) -> dict[str, Any]:
        state = self._capture_history_state()
        records = []
        for row in state["rows"]:
            source_path = row.get("resume_path")
            if not source_path or not os.path.isfile(source_path):
                # Cache the current path before it can be a ZIP temporary extraction.
                source_path = self._cache_source_for_resume(row["path"])
                row["resume_path"] = source_path
            records.append({k: v for k, v in row.items() if k not in {"loaded", "path", "src_size", "origin", "palette_names"}} | {
                "source_cache": os.path.relpath(source_path, self.app_data_dir),
                "display_path": row.get("display_path", row["path"]),
            })
        return {
            "format": "mcstructure-merger-autosave", "version": 1,
            "saved_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "overlap_mode": self.overlap_mode_var.get(),
            "selected_ids": sorted(self.selected_ids),
            "rows": records,
            "camera": {"yaw": self.camera_yaw, "pitch": self.camera_pitch, "zoom": self.camera_zoom, "pan_x": self.camera_pan_x, "pan_y": self.camera_pan_y},
        }

    def _save_autosave_now(self):
        try:
            payload = self._autosave_payload()
            tmp = self.autosave_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.autosave_file)
        except Exception as exc:
            self.status.set(f"一時保存に失敗: {exc}")

    def _schedule_autosave(self):
        if self._history_restoring:
            return
        try:
            if self._autosave_after_id:
                self.root.after_cancel(self._autosave_after_id)
            self._autosave_after_id = self.root.after(500, self._autosave_callback)
        except tk.TclError:
            self._autosave_after_id = None

    def _autosave_callback(self):
        self._autosave_after_id = None
        self._save_autosave_now()

    def _load_autosave_on_startup(self):
        if not self.autosave_file.is_file():
            return
        try:
            with open(self.autosave_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if payload.get("format") != "mcstructure-merger-autosave" or payload.get("version") != 1:
                return
            restored = []
            loaded_cache = {}
            for item in payload.get("rows", []):
                cached = (self.app_data_dir / item["source_cache"]).resolve()
                if not cached.is_file():
                    continue
                key = str(cached)
                loaded = loaded_cache.get(key)
                if loaded is None:
                    loaded = load_structure(key)
                    loaded_cache[key] = loaded
                row = self._make_row_data(
                    key, loaded, (int(item["x"]), int(item["y"]), int(item["z"])),
                    int(item.get("rotation", 0)),
                )
                row["instance_id"] = int(item.get("instance_id", self._next_instance_id))
                row["display_path"] = item.get("display_path", key)
                row["resume_path"] = key
                row["mirror_x"] = bool(item.get("mirror_x", False))
                row["mirror_z"] = bool(item.get("mirror_z", False))
                row["repeat_group"] = item.get("repeat_group")
                row["repeat_number"] = item.get("repeat_number")
                row["repeat_total"] = item.get("repeat_total")
                restored.append(row)
            if restored:
                self.rows = restored
                self._next_instance_id = max(r["instance_id"] for r in restored) + 1
                self._next_repeat_group_id = max((r.get("repeat_group") or 0 for r in restored), default=0) + 1
                self.selected_ids = set(payload.get("selected_ids", []))
                self.overlap_mode_var.set(payload.get("overlap_mode", "last_defined"))
                self.overlap_display_var.set({
                    "last_defined": "後の構造物を優先（未設定セルは保持）",
                    "first_non_air": "最初の構造物を優先（空気以外）",
                    "last_including_void": "後の構造物で領域全体を置換",
                }.get(self.overlap_mode_var.get(), "後の構造物を優先（未設定セルは保持）"))
                camera = payload.get("camera", {})
                self.camera_yaw = float(camera.get("yaw", self.camera_yaw))
                self.camera_pitch = float(camera.get("pitch", self.camera_pitch))
                self.camera_zoom = float(camera.get("zoom", self.camera_zoom))
                self.camera_pan_x = float(camera.get("pan_x", 0.0))
                self.camera_pan_y = float(camera.get("pan_y", 0.0))
                self.render_rows()
                self.status.set(f"前回の作業を復元しました（{len(restored)}個の配置）。")
            else:
                self.status.set("前回の一時保存はありますが、復元できる構造物がありませんでした。")
        except Exception as exc:
            self.status.set(f"前回の作業を復元できませんでした: {exc}")

    def _selected_rows(self):
        return [row for row in self.rows if row["instance_id"] in self.selected_ids]

    def on_row_selection_changed(self, instance_id: int, value: bool):
        if value:
            self.selected_ids.add(instance_id)
        else:
            self.selected_ids.discard(instance_id)
        self._schedule_autosave()

    def clear_selection(self):
        self.selected_ids.clear()
        self.render_rows()
        self._schedule_autosave()

    def _on_overlap_mode_changed(self):
        mapping = {
            "後の構造物を優先（未設定セルは保持）": "last_defined",
            "最初の構造物を優先（空気以外）": "first_non_air",
            "後の構造物で領域全体を置換": "last_including_void",
        }
        self.overlap_mode_var.set(mapping.get(self.overlap_display_var.get(), "last_defined"))
        self._commit_history("重なり設定")
        self.redraw_preview_if_open()

    def move_selected_dialog(self):
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo("グループ移動", "一覧のチェック欄で移動する構造物を選択してください。", parent=self.root)
            return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"選択した{len(rows)}個を移動")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both", expand=True)
        vars_ = {axis: tk.StringVar(value="0") for axis in "xyz"}
        for i, axis in enumerate("xyz"):
            ttk.Label(body, text=f"{axis.upper()}移動量").grid(row=i, column=0, sticky="w", padx=(0, 10), pady=3)
            ttk.Entry(body, textvariable=vars_[axis], width=12).grid(row=i, column=1, pady=3)
        def apply():
            try:
                delta = {axis: int(var.get()) for axis, var in vars_.items()}
            except ValueError:
                messagebox.showerror("入力エラー", "移動量は整数で指定してください。", parent=dialog)
                return
            for row in rows:
                for axis in "xyz":
                    row[axis].set(str(int(row[axis].get()) + delta[axis]))
            self._commit_history("グループ移動")
            self.render_rows()
            self.redraw_preview_if_open()
            dialog.destroy()
        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="移動", command=apply).pack(side="right")
        dialog.bind("<Return>", lambda _e: apply())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def rotate_selected_group(self):
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo("グループ回転", "回転する構造物を選択してください。", parent=self.root)
            return
        try:
            entries = []
            for row in rows:
                x, z = int(row["x"].get()), int(row["z"].get())
                sx, _sy, sz = self._row_dimensions(row)
                entries.append((row, x, z, sx, sz))
            min_x = min(e[1] for e in entries)
            min_z = min(e[2] for e in entries)
            max_z = max(e[2] + e[4] for e in entries)
            for row, x, z, sx, sz in entries:
                new_x = min_x + (max_z - (z + sz))
                new_z = min_z + (x - min_x)
                row["x"].set(str(new_x))
                row["z"].set(str(new_z))
                row["rotation"].set(str((int(row["rotation"].get()) + 90) % 360))
            self._commit_history("グループ90度回転")
            self.render_rows()
            self.redraw_preview_if_open()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("グループ回転", str(exc), parent=self.root)

    def mirror_selected(self, axis: str):
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo("ミラー", "反転する構造物を選択してください。", parent=self.root)
            return
        key = "mirror_x" if axis == "x" else "mirror_z"
        for row in rows:
            row[key] = not bool(row.get(key, False))
        self._commit_history(f"{axis.upper()}ミラー")
        self.render_rows()
        self.redraw_preview_if_open()

    def align_selected(self, mode: str):
        rows = self._selected_rows()
        if len(rows) < 2:
            messagebox.showinfo("整列", "2個以上の構造物を選択してください。", parent=self.root)
            return
        entries = []
        for row in rows:
            x, z = int(row["x"].get()), int(row["z"].get())
            sx, _sy, sz = self._row_dimensions(row)
            entries.append((row, x, z, sx, sz))
        min_x = min(e[1] for e in entries)
        min_z = min(e[2] for e in entries)
        max_x = max(e[1] + e[3] for e in entries)
        max_z = max(e[2] + e[4] for e in entries)
        if mode == "x":
            for row, _x, _z, _sx, _sz in entries:
                row["x"].set(str(min_x))
        elif mode == "z":
            for row, _x, _z, _sx, _sz in entries:
                row["z"].set(str(min_z))
        elif mode == "center":
            center_x, center_z = (min_x + max_x) / 2, (min_z + max_z) / 2
            for row, _x, _z, sx, sz in entries:
                row["x"].set(str(round(center_x - sx / 2)))
                row["z"].set(str(round(center_z - sz / 2)))
        self._commit_history("グループ整列")
        self.render_rows()
        self.redraw_preview_if_open()

    def _add_paths(self, paths: list[str]) -> None:
        existing = {os.path.abspath(row["path"]) for row in self.rows}
        added = 0
        for path in paths:
            apath = os.path.abspath(path)
            if apath in existing:
                continue
            try:
                src = load_structure(path)
            except Exception as e:
                messagebox.showerror("読み込みエラー", f"{os.path.basename(path)}\n\n{e}")
                continue
            palette_names = []
            for entry in src.block_palette:
                name_tag = entry.value.get("name") if entry.tag_id == 10 else None
                palette_names.append(name_tag.value if name_tag is not None and name_tag.tag_id == 8 else "unknown:block")
            row_data = self._make_row_data(path, src, (src.origin[0], src.origin[1], src.origin[2]), 0, palette_names)
            try:
                row_data["resume_path"] = self._cache_source_for_resume(path)
            except OSError as cache_error:
                # Loading must remain possible even when the user's app-data folder
                # is read-only or temporarily full; the close-time autosave will
                # report that it could not persist the source copy.
                row_data["resume_path"] = None
                self.status.set(f"一時保存用コピーを作成できません: {cache_error}")
            row_data["display_path"] = path
            self.rows.append(row_data)
            existing.add(apath)
            added += 1
        if self.preview_selected_id is not None and not any(r["instance_id"] == self.preview_selected_id for r in self.rows):
            self.preview_selected_id = None
        self.render_rows()
        self.status.set(f"{len(self.rows)}ファイル選択中。今回追加: {added}")
        self.redraw_preview_if_open()
        if added:
            self._commit_history("ファイル追加")

    def _make_row_data(
        self,
        path: str,
        src: LoadedStructure,
        position: tuple[int, int, int],
        rotation: int = 0,
        palette_names: list[str] | None = None,
    ) -> dict[str, Any]:
        if palette_names is None:
            palette_names = []
            for entry in src.block_palette:
                name_tag = entry.value.get("name") if entry.tag_id == 10 else None
                palette_names.append(name_tag.value if name_tag is not None and name_tag.tag_id == 8 else "unknown:block")
        instance_id = self._next_instance_id
        self._next_instance_id += 1
        return {
            "instance_id": instance_id,
            "path": path,
            "src_size": src.size,
            "origin": src.origin,
            "loaded": src,
            "palette_names": list(palette_names),
            "x": tk.StringVar(value=str(int(position[0]))),
            "y": tk.StringVar(value=str(int(position[1]))),
            "z": tk.StringVar(value=str(int(position[2]))),
            "rotation": tk.StringVar(value=str(int(rotation))),
            "repeat_group": None,
            "repeat_number": None,
            "repeat_total": None,
            "display_path": path,
            "resume_path": None,
            "mirror_x": False,
            "mirror_z": False,
            "selected_var": tk.BooleanVar(value=False),
        }

    def open_repeat_dialog(self):
        """Duplicate one selected input up to 100 total instances in X/Z/grid layout."""
        if not self.rows:
            messagebox.showinfo("連続配置", "先に構造物ファイルを追加してください。", parent=self.root)
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("同じストラクチャーを連続配置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        selected_index = next(
            (i for i, r in enumerate(self.rows) if r["instance_id"] == self.preview_selected_id),
            0,
        )
        choices = []
        for i, row in enumerate(self.rows):
            base = os.path.basename(row["path"])
            try:
                position = f"X={row['x'].get()} Y={row['y'].get()} Z={row['z'].get()}"
            except tk.TclError:
                position = "座標未設定"
            choices.append(f"{i + 1}. {base}  ({position})")
        source_var = tk.StringVar(value=choices[selected_index])
        count_var = tk.StringVar(value="10")
        mode_var = tk.StringVar(value="x")
        columns_var = tk.StringVar(value="10")
        preview_text = tk.StringVar(value="X方向に、構造物の幅分ずつ隙間なく並べます。")

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="複製元のストラクチャー").grid(row=0, column=0, sticky="w", pady=(0, 4))
        source_combo = ttk.Combobox(body, textvariable=source_var, values=choices, state="readonly", width=58)
        source_combo.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        source_combo.current(selected_index)

        ttk.Label(body, text="配置する合計個数（元の1個を含む）").grid(row=2, column=0, sticky="w")
        count_spin = ttk.Spinbox(body, from_=1, to=100, increment=1, textvariable=count_var, width=8)
        count_spin.grid(row=2, column=1, sticky="w", padx=(8, 0))

        ttk.Label(body, text="配置方法").grid(row=3, column=0, sticky="w", pady=(14, 4))
        radio_frame = ttk.Frame(body)
        radio_frame.grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(radio_frame, text="横一列（X方向）", variable=mode_var, value="x").pack(anchor="w", pady=2)
        ttk.Radiobutton(radio_frame, text="奥方向（Z方向）", variable=mode_var, value="z").pack(anchor="w", pady=2)
        ttk.Radiobutton(radio_frame, text="複数行（グリッド）", variable=mode_var, value="grid").pack(anchor="w", pady=2)

        columns_frame = ttk.Frame(body)
        columns_frame.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(columns_frame, text="グリッドの列数（X方向）").pack(side="left")
        columns_spin = ttk.Spinbox(columns_frame, from_=1, to=100, increment=1, textvariable=columns_var, width=6)
        columns_spin.pack(side="left", padx=(8, 0))
        ttk.Label(body, textvariable=preview_text, foreground="#475569", wraplength=430, justify="left").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(12, 8)
        )
        ttk.Label(
            body,
            text="配置間隔は回転後の実寸を使います。グリッドは左から右へ並び、列数に達すると次の行へ進みます。",
            wraplength=440, justify="left",
        ).grid(row=7, column=0, columnspan=2, sticky="w")

        def update_mode(*_args):
            is_grid = mode_var.get() == "grid"
            columns_spin.configure(state="normal" if is_grid else "disabled")
            if mode_var.get() == "x":
                preview_text.set("X方向に、構造物の幅分ずつ隙間なく並べます。")
            elif mode_var.get() == "z":
                preview_text.set("Z方向（奥方向）に、構造物の奥行き分ずつ隙間なく並べます。")
            else:
                preview_text.set("X方向に指定列数並べ、次の行はZ方向へ構造物の奥行き分ずらします。")
        mode_var.trace_add("write", update_mode)
        update_mode()

        buttons = ttk.Frame(body)
        buttons.grid(row=8, column=0, columnspan=2, sticky="e", pady=(18, 0))

        def generate():
            try:
                selected = source_combo.current()
                if selected < 0 or selected >= len(self.rows):
                    raise NBTError("複製元を選択してください")
                count = int(count_var.get())
                if not 1 <= count <= 100:
                    raise NBTError("個数は1～100にしてください")
                mode = mode_var.get()
                columns = int(columns_var.get()) if mode == "grid" else 1
                if mode == "grid":
                    if not 1 <= columns <= 100:
                        raise NBTError("グリッドの列数は1～100にしてください")
                    columns = min(columns, count)
                    columns_var.set(str(columns))
                template = self.rows[selected]
                start = tuple(int(template[key].get()) for key in ("x", "y", "z"))
                rotation = int(template["rotation"].get())
                rotated_size = self._row_dimensions(template)
                positions = calculate_repeat_positions(start, rotated_size, count, mode, columns)
            except (ValueError, NBTError, tk.TclError) as exc:
                messagebox.showerror("連続配置の設定エラー", str(exc), parent=dialog)
                return

            # Keep the selected row as instance #1; append only the remaining copies.
            template["x"].set(str(positions[0][0]))
            template["y"].set(str(positions[0][1]))
            template["z"].set(str(positions[0][2]))
            group_id = self._next_repeat_group_id
            self._next_repeat_group_id += 1
            template["repeat_group"] = group_id
            template["repeat_number"] = 1
            template["repeat_total"] = count
            for number, position in enumerate(positions[1:], start=2):
                clone = self._make_row_data(
                    template["path"],
                    template["loaded"],
                    position,
                    rotation,
                    template["palette_names"],
                )
                clone["repeat_group"] = group_id
                clone["repeat_number"] = number
                clone["repeat_total"] = count
                clone["display_path"] = template.get("display_path", template["path"])
                clone["resume_path"] = template.get("resume_path")
                clone["mirror_x"] = bool(template.get("mirror_x", False))
                clone["mirror_z"] = bool(template.get("mirror_z", False))
                self.rows.append(clone)

            self.render_rows()
            self.redraw_preview_if_open()
            self.status.set(
                f"{os.path.basename(template.get('display_path', template['path']))} を合計{count}個に配置しました。"
                f"方式: {'X方向' if mode == 'x' else 'Z方向' if mode == 'z' else f'グリッド（{columns}列）'}"
            )
            self._commit_history("連続配置")
            dialog.destroy()

        ttk.Button(buttons, text="キャンセル", command=dialog.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="配置を生成", command=generate).pack(side="right")
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.bind("<Return>", lambda _e: generate())
        dialog.wait_window()

    def open_reference_dialog(self):
        """Open the native Windows file dialog to multi-select .mcstructure files and ZIPs."""
        # Reuse the last directory selected in the native dialog. This avoids opening
        # a temporary extraction directory when the last-added item came from a ZIP.
        initialdir = self._browser_start_path
        try:
            selected = filedialog.askopenfilenames(
                parent=self.root,
                title=".mcstructure ファイル／ZIPを選択（複数選択可）",
                initialdir=initialdir if os.path.isdir(initialdir) else str(Path.home()),
                filetypes=[
                    ("構造ファイル・ZIP (*.mcstructure, *.zip)", "*.mcstructure *.zip"),
                    ("構造ファイル (*.mcstructure)", "*.mcstructure"),
                    ("ZIPアーカイブ (*.zip)", "*.zip"),
                    ("すべてのファイル (*.*)", "*.*"),
                ],
            )
        except Exception as exc:
            messagebox.showerror("ファイル選択エラー", str(exc), parent=self.root)
            return
        paths = list(selected or ())
        if not paths:
            return

        # Remember the directory for the next dialog invocation.
        first_dir = os.path.dirname(os.path.abspath(paths[0]))
        if os.path.isdir(first_dir):
            self._browser_start_path = first_dir
        self._process_browser_paths(paths)

    def _process_browser_paths(self, paths: list[str]) -> None:
        """Interpret selected standard-dialog paths by type: structure file or ZIP."""
        structure_paths: list[str] = []
        archive_paths: list[str] = []
        seen_structures: set[str] = set()
        seen_archives: set[str] = set()

        def collect_file(path: str):
            apath = os.path.abspath(path)
            low = apath.lower()
            if os.path.isdir(apath):
                return
            if low.endswith(".mcstructure"):
                if apath not in seen_structures:
                    seen_structures.add(apath)
                    structure_paths.append(apath)
            elif low.endswith(".zip") and apath not in seen_archives:
                seen_archives.add(apath)
                archive_paths.append(apath)

        for path in paths:
            apath = os.path.abspath(path)
            if os.path.isdir(apath):
                for base, dirs, files in os.walk(apath):
                    dirs.sort(key=str.casefold)
                    for name in sorted(files, key=str.casefold):
                        collect_file(os.path.join(base, name))
            else:
                collect_file(apath)

        if structure_paths:
            self._add_paths(structure_paths)
        if archive_paths:
            self._add_archives(archive_paths)
        if not structure_paths and not archive_paths:
            messagebox.showinfo("対象ファイルなし", "選択内容に.mcstructureファイルまたはZIPが見つかりませんでした。")

    def _add_archives(self, archives: list[str]) -> None:
        all_paths: list[str] = []
        errors: list[str] = []
        for archive in archives:
            temp_dir = None
            try:
                temp_dir = tempfile.TemporaryDirectory(prefix="mcstructure_zip_")
                with zipfile.ZipFile(archive, "r") as zf:
                    members = sorted(
                        (m for m in zf.infolist() if not m.is_dir() and m.filename.lower().endswith(".mcstructure")),
                        key=lambda m: m.filename.casefold(),
                    )
                    if len(members) > 10000:
                        raise NBTError("ZIP内の.mcstructureファイルが10,000件を超えています")
                    total_size = sum(m.file_size for m in members)
                    if total_size > 2 * 1024 * 1024 * 1024:
                        raise NBTError("ZIP内の.mcstructure合計サイズが2GBを超えています")
                    extracted_paths = []
                    root = Path(temp_dir.name).resolve()
                    for member in members:
                        rel = PurePosixPath(member.filename)
                        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
                            continue
                        target = (root / Path(*rel.parts)).resolve()
                        if os.path.commonpath([str(root), str(target)]) != str(root):
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member, "r") as src_file, open(target, "wb") as dst_file:
                            import shutil
                            shutil.copyfileobj(src_file, dst_file)
                        extracted_paths.append(str(target))
                if extracted_paths:
                    self.temp_dirs.append(temp_dir)
                    all_paths.extend(extracted_paths)
                else:
                    temp_dir.cleanup()
                    errors.append(f"{os.path.basename(archive)}: .mcstructure が見つかりません")
            except Exception as exc:
                try:
                    if temp_dir is not None:
                        temp_dir.cleanup()
                except Exception:
                    pass
                errors.append(f"{os.path.basename(archive)}: {exc}")
        if all_paths:
            self._add_paths(all_paths)
        if errors:
            messagebox.showwarning("ZIPの読み込み結果", "\n".join(errors[:12]) + ("\n…" if len(errors) > 12 else ""))

    def clear_rows(self):
        self.rows.clear()
        self.preview_selected_id = None
        self.selected_ids.clear()
        self.render_rows()
        self.status.set("入力ファイルを全て削除しました。")
        self.redraw_preview_if_open()
        self._commit_history("全て削除")

    def move_row(self, index: int, delta: int):
        new_index = index + delta
        if 0 <= new_index < len(self.rows):
            self.rows[index], self.rows[new_index] = self.rows[new_index], self.rows[index]
            self.render_rows()
            self.redraw_preview_if_open()
            self._commit_history("一覧順序変更")

    def remove_row(self, instance_id: int):
        self.rows = [r for r in self.rows if r["instance_id"] != instance_id]
        if self.preview_selected_id == instance_id:
            self.preview_selected_id = None
        self.selected_ids.discard(instance_id)
        self.render_rows()
        self.status.set(f"{len(self.rows)}ファイル選択中")
        self.redraw_preview_if_open()
        self._commit_history("構造物削除")

    def _row_dimensions(self, row: dict[str, Any]) -> tuple[int, int, int]:
        sx, sy, sz = row["src_size"]
        turns = int(row["rotation"].get()) // 90
        return (sz, sy, sx) if turns % 2 else (sx, sy, sz)

    def render_rows(self):
        for child in self.inner.winfo_children():
            child.destroy()
        for i, row in enumerate(self.rows):
            line = ttk.Frame(self.inner)
            line.grid(row=i, column=0, sticky="ew", pady=2)
            line.columnconfigure(1, weight=1)
            row["selected_var"].set(row["instance_id"] in self.selected_ids)
            check = ttk.Checkbutton(
                line, variable=row["selected_var"],
                command=lambda uid=row["instance_id"], var=row["selected_var"]: self.on_row_selection_changed(uid, bool(var.get())),
            )
            check.grid(row=0, column=0, sticky="w")
            name = os.path.basename(row.get("display_path", row["path"]))
            if row.get("repeat_number") is not None:
                name = f"{name}  [{row['repeat_number']}/{row['repeat_total']}]"
            else:
                same_path_total = sum(1 for candidate in self.rows if os.path.abspath(candidate.get("display_path", candidate["path"])) == os.path.abspath(row.get("display_path", row["path"])))
                if same_path_total > 1:
                    same_path_number = 1 + sum(
                        1 for candidate in self.rows[:i]
                        if os.path.abspath(candidate.get("display_path", candidate["path"])) == os.path.abspath(row.get("display_path", row["path"]))
                    )
                    name = f"{name}  [{same_path_number}]"
            mirrors = ("X" if row.get("mirror_x") else "") + ("Z" if row.get("mirror_z") else "")
            if mirrors:
                name += f"  [ミラー:{mirrors}]"
            tip = f"{row.get('display_path', row['path'])}\n元の保存位置: {row['origin']}"
            lbl = ttk.Label(line, text=name, width=39)
            lbl.grid(row=0, column=1, sticky="w")
            lbl.bind("<Enter>", lambda _e, text=tip: self.status.set(text))
            lbl.bind("<Leave>", lambda _e: self.status.set(f"{len(self.rows)}ファイル選択中"))
            sx, sy, sz = self._row_dimensions(row)
            ttk.Label(line, text=f"{sx}×{sy}×{sz}", width=16).grid(row=0, column=2, sticky="w")
            for col, key in zip((3, 4, 5), ("x", "y", "z")):
                entry = ttk.Entry(line, textvariable=row[key], width=7)
                entry.grid(row=0, column=col, padx=2)
                entry.bind("<KeyRelease>", lambda _e: (self.redraw_preview_if_open(), self._schedule_autosave()))
                entry.bind("<FocusOut>", lambda _e: self._row_field_committed())
            rot = ttk.Combobox(line, textvariable=row["rotation"], values=("0", "90", "180", "270"), width=6, state="readonly")
            rot.grid(row=0, column=6, padx=3)
            rot.bind("<<ComboboxSelected>>", lambda _e: self.on_rotation_changed())
            rot.bind("<MouseWheel>", lambda _e: "break")
            rot.bind("<Button-4>", lambda _e: "break")
            rot.bind("<Button-5>", lambda _e: "break")
            order = ttk.Frame(line)
            order.grid(row=0, column=7, sticky="w")
            ttk.Button(order, text="↑", width=3, command=lambda idx=i: self.move_row(idx, -1)).pack(side="left")
            ttk.Button(order, text="↓", width=3, command=lambda idx=i: self.move_row(idx, 1)).pack(side="left", padx=(2, 0))
            ttk.Button(order, text="削除", width=5, command=lambda uid=row["instance_id"]: self.remove_row(uid)).pack(side="left", padx=(4, 0))

    def _row_field_committed(self):
        self.redraw_preview_if_open()
        self._commit_history("座標変更")

    def on_rotation_changed(self):
        self.render_rows()
        self.redraw_preview_if_open()
        self._commit_history("回転変更")

    def open_preview(self):
        if not self.rows:
            messagebox.showinfo("配置プレビュー", "先にファイルを追加してください。")
            return
        if self.preview_window is not None and self.preview_window.winfo_exists():
            self.preview_window.deiconify()
            self.preview_window.lift()
            self.redraw_preview()
            return

        top = tk.Toplevel(self.root)
        self.preview_window = top
        top.title("配置プレビュー・ブロック上面図")
        top.geometry("1000x760")

        notebook = ttk.Notebook(top)
        notebook.pack(fill="both", expand=True, padx=10, pady=(10, 5))
        self.preview_notebook = notebook

        layout_tab = ttk.Frame(notebook)
        self.surface_tab = ttk.Frame(notebook)
        iso_tab = ttk.Frame(notebook)
        notebook.add(layout_tab, text="配置図（64ブロック単位）")
        notebook.add(self.surface_tab, text="ブロック上面図")
        notebook.add(iso_tab, text="3Dブロック表示")

        guide = ttk.Label(
            layout_tab,
            text="構造物をクリックして選択し、再度クリック＋ドラッグでリアルタイムに移動（離すと64ブロック単位にスナップ）。右クリック＋ドラッグで画面移動、ホイールで拡大・縮小します。赤は3D範囲の重なりです。",
            justify="left",
            wraplength=950,
        )
        guide.pack(anchor="w", padx=8, pady=(8, 3))
        self.preview_canvas = tk.Canvas(layout_tab, background="#f8fafc", highlightthickness=0)
        self.preview_canvas.pack(fill="both", expand=True, padx=5, pady=5)
        self.preview_canvas.bind("<ButtonPress-1>", self.preview_press)
        self.preview_canvas.bind("<B1-Motion>", self.preview_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self.preview_release)
        self._bind_view_navigation(self.preview_canvas, "layout")

        surface_guide = ttk.Label(
            self.surface_tab,
            text="各X/Z位置の最上部ブロックを色分けします。ホイールで拡大・縮小、右クリック＋ドラッグで表示範囲を移動します。色は近似色で、Minecraftのテクスチャそのものではありません。",
            justify="left",
            wraplength=950,
        )
        surface_guide.pack(anchor="w", padx=8, pady=(8, 3))
        self.surface_canvas = tk.Canvas(self.surface_tab, background="#e2e8f0", highlightthickness=0)
        self.surface_canvas.pack(fill="both", expand=True, padx=5, pady=5)
        self._bind_view_navigation(self.surface_canvas, "surface")

        iso_guide = ttk.Label(
            iso_tab,
            text="ブロック形状を立体表示します。左ドラッグで視点回転、右ドラッグで表示移動、ホイールで拡大縮小。ブロック種別ごとの色・模様・面の陰影を使った軽量プレビューです（Minecraftの公式テクスチャそのものではありません）。",
            justify="left", wraplength=950,
        )
        iso_guide.pack(anchor="w", padx=8, pady=(8, 3))
        self.iso_canvas = tk.Canvas(iso_tab, background="#dbe4ec", highlightthickness=0)
        self.iso_canvas.pack(fill="both", expand=True, padx=5, pady=5)
        self.iso_canvas.bind("<ButtonPress-1>", self._iso_press)
        self.iso_canvas.bind("<B1-Motion>", self._iso_motion)
        self.iso_canvas.bind("<ButtonRelease-1>", self._iso_release)
        self.iso_canvas.bind("<ButtonPress-3>", self._iso_pan_press)
        self.iso_canvas.bind("<B3-Motion>", self._iso_pan_motion)
        self.iso_canvas.bind("<ButtonRelease-3>", self._iso_pan_release)
        self.iso_canvas.bind("<MouseWheel>", self._iso_wheel)
        self.iso_canvas.bind("<Button-4>", lambda e: self._iso_zoom_at(e, 1.15))
        self.iso_canvas.bind("<Button-5>", lambda e: self._iso_zoom_at(e, 1 / 1.15))

        bottom = ttk.Frame(top)
        bottom.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(bottom, text="現在の表示を更新", command=self.refresh_current_preview).pack(side="left")
        ttk.Label(bottom, text="右ドラッグ: 画面移動 / ホイール: ズーム / 配置は64ブロック単位", foreground="#555555").pack(side="left", padx=10)
        ttk.Button(bottom, text="3D視点リセット", command=self.reset_3d_view).pack(side="right", padx=(0, 8))
        ttk.Button(bottom, text="全体表示に戻す", command=self.reset_preview_views).pack(side="right", padx=(0, 8))
        ttk.Button(bottom, text="閉じる", command=top.destroy).pack(side="right")

        notebook.bind("<<NotebookTabChanged>>", self._preview_tab_changed)
        top.protocol("WM_DELETE_WINDOW", top.destroy)
        top.bind("<Destroy>", self._preview_destroyed, add="+")
        top.after(100, self.redraw_preview)

    def _preview_tab_changed(self, _event=None):
        if self.preview_notebook is None:
            return
        try:
            selected = self.preview_notebook.nametowidget(self.preview_notebook.select())
        except (KeyError, tk.TclError):
            return
        if selected is self.surface_tab:
            self._schedule_surface_redraw()
        elif self.iso_canvas is not None and selected is self.iso_canvas.master:
            self._schedule_3d_redraw()
        else:
            self.redraw_preview()

    def _preview_destroyed(self, event):
        if event.widget is self.preview_window:
            if self.surface_after_id and self.preview_window is not None:
                try:
                    self.preview_window.after_cancel(self.surface_after_id)
                except tk.TclError:
                    pass
            self.surface_after_id = None
            if self.iso_after_id and self.preview_window is not None:
                try:
                    self.preview_window.after_cancel(self.iso_after_id)
                except tk.TclError:
                    pass
            self.iso_after_id = None
            self.preview_window = None
            self.preview_canvas = None
            self.surface_canvas = None
            self.iso_canvas = None
            self.iso_image = None
            self.preview_notebook = None
            self.surface_tab = None
            self.surface_image = None
            self.preview_drag = None
            self.preview_pan_drag = None

    def redraw_preview_if_open(self):
        if self.preview_canvas is not None and self.preview_canvas.winfo_exists() and not self.preview_drag:
            self.preview_canvas.after_idle(self.redraw_preview)
        if self._surface_tab_is_selected():
            self._schedule_surface_redraw()
        if self._iso_tab_is_selected():
            self._schedule_3d_redraw()

    def _iso_tab_is_selected(self) -> bool:
        if self.preview_notebook is None or self.iso_canvas is None:
            return False
        try:
            return self.preview_notebook.nametowidget(self.preview_notebook.select()) is self.iso_canvas.master
        except (KeyError, tk.TclError):
            return False

    def _schedule_3d_redraw(self):
        if self.preview_window is None or self.iso_canvas is None:
            return
        try:
            if self.iso_after_id:
                self.preview_window.after_cancel(self.iso_after_id)
            self.iso_after_id = self.preview_window.after(100, self.redraw_3d_preview)
        except tk.TclError:
            self.iso_after_id = None

    def reset_3d_view(self):
        self.camera_yaw = math.radians(45.0)
        self.camera_pitch = math.radians(32.0)
        self.camera_zoom = 1.0
        self.camera_pan_x = 0.0
        self.camera_pan_y = 0.0
        self._schedule_3d_redraw()
        self._schedule_autosave()

    def _iso_press(self, event):
        self.iso_drag_state = {"mode": "rotate", "x": event.x, "y": event.y, "yaw": self.camera_yaw, "pitch": self.camera_pitch}

    def _iso_motion(self, event):
        state = self.iso_drag_state
        if not state or state.get("mode") != "rotate":
            return
        dx, dy = event.x - state["x"], event.y - state["y"]
        self.camera_yaw = state["yaw"] + dx * 0.012
        self.camera_pitch = max(math.radians(12), min(math.radians(78), state["pitch"] - dy * 0.009))
        self._schedule_3d_redraw()

    def _iso_release(self, _event):
        self.iso_drag_state = None
        self._schedule_autosave()

    def _iso_pan_press(self, event):
        self.iso_drag_state = {"mode": "pan", "x": event.x, "y": event.y, "pan_x": self.camera_pan_x, "pan_y": self.camera_pan_y}

    def _iso_pan_motion(self, event):
        state = self.iso_drag_state
        if not state or state.get("mode") != "pan":
            return
        self.camera_pan_x = state["pan_x"] + event.x - state["x"]
        self.camera_pan_y = state["pan_y"] + event.y - state["y"]
        self._schedule_3d_redraw()

    def _iso_pan_release(self, _event):
        self.iso_drag_state = None
        self._schedule_autosave()

    def _iso_wheel(self, event):
        self._iso_zoom_at(event, 1.15 if event.delta > 0 else 1 / 1.15)
        return "break"

    def _iso_zoom_at(self, _event, factor: float):
        self.camera_zoom = max(0.08, min(8.0, self.camera_zoom * factor))
        self._schedule_3d_redraw()
        self._schedule_autosave()

    def _collect_3d_voxels(self, max_voxels: int = 7000):
        """Build an ordered sparse voxel scene from both Bedrock block layers."""
        total_cells = 0
        for row in self.rows:
            sx, sy, sz = row["src_size"]
            total_cells += sx * sy * sz
        cell_step = max(1, int(math.ceil((total_cells / 2_500_000) ** (1 / 3)))) if total_cells > 2_500_000 else 1
        voxels: dict[tuple[int, int, int], str] = {}
        scanned = 0
        sampled = False
        for row in self.rows:
            src: LoadedStructure = row["loaded"]
            sx, sy, sz = src.size
            ox, oy, oz = (int(row[axis].get()) for axis in "xyz")
            turns = int(row["rotation"].get()) // 90
            mirror_x = bool(row.get("mirror_x", False))
            mirror_z = bool(row.get("mirror_z", False))
            names = row["palette_names"]
            primary = src.layers[0]
            secondary = src.layers[1] if len(src.layers) > 1 else None
            for x in range(0, sx, cell_step):
                for y in range(0, sy, cell_step):
                    base = (x * sy + y) * sz
                    for z in range(0, sz, cell_step):
                        scanned += 1
                        idx = base + z
                        pidx = primary[idx]
                        sidx = secondary[idx] if secondary is not None else -1
                        if pidx == -1 and sidx == -1:
                            if self.overlap_mode_var.get() == "last_including_void":
                                mx = sx - 1 - x if mirror_x else x
                                mz = sz - 1 - z if mirror_z else z
                                rx, rz = _rotate_local_xz(mx, mz, sx, sz, turns)
                                voxels.pop((ox + rx, oy + y, oz + rz), None)
                            continue
                        pname = names[pidx] if 0 <= pidx < len(names) else "unknown:block" if pidx != -1 else None
                        sname = names[sidx] if 0 <= sidx < len(names) else "unknown:block" if sidx != -1 else None
                        chosen = pname if pname and not self._is_transparent_block(pname) else sname if sname and not self._is_transparent_block(sname) else None
                        mx = sx - 1 - x if mirror_x else x
                        mz = sz - 1 - z if mirror_z else z
                        rx, rz = _rotate_local_xz(mx, mz, sx, sz, turns)
                        world = (ox + rx, oy + y, oz + rz)
                        if self.overlap_mode_var.get() == "first_non_air":
                            if chosen and world not in voxels:
                                voxels[world] = chosen
                        else:
                            if chosen:
                                voxels[world] = chosen
                            elif pname is not None or self.overlap_mode_var.get() == "last_including_void":
                                voxels.pop(world, None)
                        if len(voxels) > max_voxels * 2:
                            sampled = True
                            # Deterministic spatial thinning retains a useful preview while bounding Canvas load.
                            keep = {}
                            step = 2
                            for pos, block_name in voxels.items():
                                if all((v % step) == 0 for v in pos):
                                    keep[pos] = block_name
                            voxels = keep
            if len(voxels) > max_voxels:
                sampled = True
                stride = max(2, int(math.ceil((len(voxels) / max_voxels) ** (1 / 3))))
                voxels = {pos: name for pos, name in voxels.items() if all(v % stride == 0 for v in pos)}
        return voxels, sampled, cell_step

    @staticmethod
    def _darken_color(rgb: tuple[int, int, int], factor: float) -> str:
        vals = [max(0, min(255, int(c * factor))) for c in rgb]
        return "#%02x%02x%02x" % tuple(vals)

    @staticmethod
    def _texture_stipple(block_name: str) -> str:
        name = block_name.lower()
        if any(word in name for word in ("leaves", "grass", "moss", "vine")):
            return "gray50"
        if any(word in name for word in ("stone", "cobble", "ore", "gravel", "deepslate", "brick")):
            return "gray25"
        if any(word in name for word in ("wood", "log", "planks", "chest", "barrel")):
            return "gray12"
        if any(word in name for word in ("glass", "ice")):
            return "gray12"
        return "gray12"

    def redraw_3d_preview(self):
        self.iso_after_id = None
        canvas = self.iso_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.update_idletasks()
        width = max(400, canvas.winfo_width())
        height = max(300, canvas.winfo_height())
        canvas.delete("all")
        if not self.rows:
            canvas.create_text(width / 2, height / 2, text="構造物を追加してください", fill="#475569")
            return
        try:
            voxels, sampled, cell_step = self._collect_3d_voxels()
        except Exception as exc:
            canvas.create_text(width / 2, height / 2, text=f"3Dプレビューの生成に失敗しました: {exc}", fill="#b91c1c", width=width - 40)
            return
        if not voxels:
            canvas.create_text(width / 2, height / 2, text="表示できるブロックがありません", fill="#475569")
            return

        xs = [p[0] for p in voxels]
        ys = [p[1] for p in voxels]
        zs = [p[2] for p in voxels]
        min_x, max_x = min(xs), max(xs) + 1
        min_y, max_y = min(ys), max(ys) + 1
        min_z, max_z = min(zs), max(zs) + 1
        yaw, pitch = self.camera_yaw, self.camera_pitch
        cy, syaw = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        cam_x, cam_z = syaw * cp, cy * cp
        face_x = 1 if cam_x >= 0 else -1
        face_z = 1 if cam_z >= 0 else -1

        def project(x, y, z):
            horizontal = x * syaw + z * cy
            right = x * cy - z * syaw
            screen_y = horizontal * sp - y * cp
            depth = horizontal * cp + y * sp
            return right, screen_y, depth

        corners = {
            "top": lambda x,y,z: [(x,y+1,z),(x+1,y+1,z),(x+1,y+1,z+1),(x,y+1,z+1)],
            "xp": lambda x,y,z: [(x+1,y,z),(x+1,y,z+1),(x+1,y+1,z+1),(x+1,y+1,z)],
            "xn": lambda x,y,z: [(x,y,z+1),(x,y,z),(x,y+1,z),(x,y+1,z+1)],
            "zp": lambda x,y,z: [(x+1,y,z+1),(x,y,z+1),(x,y+1,z+1),(x+1,y+1,z+1)],
            "zn": lambda x,y,z: [(x,y,z),(x+1,y,z),(x+1,y+1,z),(x,y+1,z)],
        }
        faces = []
        for (x, y, z), name in voxels.items():
            if (x, y + 1, z) not in voxels:
                faces.append(("top", (x, y, z), name, corners["top"](x, y, z)))
            side = "xp" if face_x > 0 else "xn"
            neighbour = (x + face_x, y, z)
            if neighbour not in voxels:
                faces.append((side, (x, y, z), name, corners[side](x, y, z)))
            side = "zp" if face_z > 0 else "zn"
            neighbour = (x, y, z + face_z)
            if neighbour not in voxels:
                faces.append((side, (x, y, z), name, corners[side](x, y, z)))

        projected = []
        min_u = min_v = float("inf")
        max_u = max_v = float("-inf")
        for face, pos, name, verts in faces:
            pts = [project(*v) for v in verts]
            depth = sum(pt[2] for pt in pts) / 4
            projected.append((depth, face, pos, name, pts))
            for u, v, _d in pts:
                min_u, max_u = min(min_u, u), max(max_u, u)
                min_v, max_v = min(min_v, v), max(max_v, v)
        if not projected:
            canvas.create_text(width / 2, height / 2, text="描画可能な面がありません", fill="#475569")
            return
        span_u, span_v = max(1.0, max_u - min_u), max(1.0, max_v - min_v)
        scale = min((width - 80) / span_u, (height - 100) / span_v, 28.0) * self.camera_zoom
        scale = max(0.05, scale)
        center_x = width / 2 + self.camera_pan_x
        center_y = height / 2 + self.camera_pan_y + 12
        shift_u = (min_u + max_u) / 2
        shift_v = (min_v + max_v) / 2

        # Painter's algorithm: draw far faces before near faces.
        projected.sort(key=lambda item: item[0])
        for depth, face, pos, name, pts in projected:
            poly = []
            for u, v, _d in pts:
                poly.extend((center_x + (u - shift_u) * scale, center_y + (v - shift_v) * scale))
            rgb = self._block_color(name)
            factor = 1.0 if face == "top" else (0.78 if face.startswith("x") else 0.62)
            fill = self._darken_color(rgb, factor)
            outline = self._darken_color(rgb, max(0.28, factor * 0.55))
            stipple = self._texture_stipple(name)
            canvas.create_polygon(*poly, fill=fill, outline=outline, width=1, stipple=stipple)
            # A few deterministic face marks provide material texture without external assets.
            seed = zlib.crc32(f"{name}:{pos}:{face}".encode("utf-8"))
            if scale >= 5 and (seed & 3) == 0 and any(k in name for k in ("stone", "ore", "wood", "log", "planks", "leaves", "brick", "cobble")):
                p0, p1, p2, p3 = pts
                a0 = (center_x + (p0[0] - shift_u) * scale, center_y + (p0[1] - shift_v) * scale)
                a1 = (center_x + (p1[0] - shift_u) * scale, center_y + (p1[1] - shift_v) * scale)
                a3 = (center_x + (p3[0] - shift_u) * scale, center_y + (p3[1] - shift_v) * scale)
                for mark in range(1):
                    f1 = 0.22 + ((seed >> (mark * 5)) & 15) / 24
                    f2 = 0.22 + ((seed >> (mark * 7 + 3)) & 15) / 24
                    px = a0[0] + (a1[0] - a0[0]) * f1 + (a3[0] - a0[0]) * f2
                    py = a0[1] + (a1[1] - a0[1]) * f1 + (a3[1] - a0[1]) * f2
                    mark_color = self._darken_color(rgb, factor * (0.55 if mark == 0 else 1.25))
                    canvas.create_oval(px - 1, py - 1, px + 1, py + 1, fill=mark_color, outline="")
        label = f"3D表示: {len(voxels):,}ブロック / {len(faces):,}面  | 左ドラッグ:回転 右ドラッグ:移動 ホイール:拡大縮小"
        if sampled or cell_step > 1:
            label += "  | 表示負荷を抑えるため一部を間引いています"
        canvas.create_text(10, height - 10, anchor="sw", text=label, fill="#334155", font=("Segoe UI", 8))

    def _surface_tab_is_selected(self) -> bool:
        if self.preview_notebook is None or self.surface_tab is None:
            return False
        try:
            return self.preview_notebook.nametowidget(self.preview_notebook.select()) is self.surface_tab
        except (KeyError, tk.TclError):
            return False

    def _schedule_surface_redraw(self):
        if self.preview_window is None or self.surface_canvas is None:
            return
        try:
            if self.surface_after_id:
                self.preview_window.after_cancel(self.surface_after_id)
            # Debounce rapid wheel and right-drag events. The latest view state
            # is rendered once the pointer movement settles, rather than redrawing
            # the heavy top-down raster for every input event.
            self.surface_after_id = self.preview_window.after(70, self.redraw_topdown_preview)
        except tk.TclError:
            self.surface_after_id = None

    def refresh_current_preview(self):
        if self._surface_tab_is_selected():
            self.redraw_topdown_preview()
        elif self._iso_tab_is_selected():
            self.redraw_3d_preview()
        else:
            self.redraw_preview()

    def _preview_entries(self):
        entries = []
        for i, row in enumerate(self.rows):
            x, y, z = int(row["x"].get()), int(row["y"].get()), int(row["z"].get())
            sx, sy, sz = self._row_dimensions(row)
            entries.append((i, row, x, y, z, sx, sy, sz))
        return entries

    @staticmethod
    def _grid_step(span_x: int, span_z: int) -> int:
        # Keep 64 as the fundamental snap unit. Very large maps use a coarser
        # displayed grid that remains an exact multiple of 64.
        span = max(span_x, span_z)
        return 64 * max(1, (span + 64 * 119 - 1) // (64 * 119))

    def _bind_view_navigation(self, canvas: tk.Canvas, view_key: str) -> None:
        canvas.bind("<MouseWheel>", lambda e, key=view_key: self._on_view_wheel(e, key))
        canvas.bind("<Button-4>", lambda e, key=view_key: self._zoom_view_at(e, key, 1.15))
        canvas.bind("<Button-5>", lambda e, key=view_key: self._zoom_view_at(e, key, 1 / 1.15))
        canvas.bind("<ButtonPress-3>", lambda e, key=view_key: self._view_pan_press(e, key))
        canvas.bind("<B3-Motion>", lambda e, key=view_key: self._view_pan_motion(e, key))
        canvas.bind("<ButtonRelease-3>", lambda e, key=view_key: self._view_pan_release(e, key))

    def _on_view_wheel(self, event, view_key: str):
        delta = getattr(event, "delta", 0)
        factor = 1.15 if delta > 0 else 1 / 1.15
        # Windows may report multiples of 120; a single notch per event is more controllable.
        return self._zoom_view_at(event, view_key, factor)

    def _zoom_view_at(self, event, view_key: str, factor: float):
        state = self.preview_views[view_key]
        old_zoom = state["zoom"]
        new_zoom = max(0.08, min(32.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-9:
            return "break"
        canvas_width = max(event.widget.winfo_width(), 1)
        canvas_height = max(event.widget.winfo_height(), 1)
        # Keep the world point below the mouse cursor fixed while the zoom changes.
        ratio = new_zoom / old_zoom
        state["pan_x"] = event.x - canvas_width / 2 - (event.x - canvas_width / 2 - state["pan_x"]) * ratio
        state["pan_y"] = event.y - canvas_height / 2 - (event.y - canvas_height / 2 - state["pan_y"]) * ratio
        state["zoom"] = new_zoom
        if view_key == "layout":
            self.redraw_preview()
        else:
            self._schedule_surface_redraw()
        return "break"

    def _view_pan_press(self, event, view_key: str):
        state = self.preview_views[view_key]
        self.preview_pan_drag = {
            "view_key": view_key,
            "mouse_x": event.x,
            "mouse_y": event.y,
            "start_pan_x": state["pan_x"],
            "start_pan_y": state["pan_y"],
        }
        try:
            event.widget.configure(cursor="fleur")
        except tk.TclError:
            pass

    def _view_pan_motion(self, event, view_key: str):
        drag = self.preview_pan_drag
        if not drag or drag["view_key"] != view_key:
            return
        state = self.preview_views[view_key]
        state["pan_x"] = drag["start_pan_x"] + event.x - drag["mouse_x"]
        state["pan_y"] = drag["start_pan_y"] + event.y - drag["mouse_y"]
        if view_key == "layout":
            self.redraw_preview()
        else:
            self._schedule_surface_redraw()

    def _view_pan_release(self, event, view_key: str):
        if self.preview_pan_drag and self.preview_pan_drag["view_key"] == view_key:
            self.preview_pan_drag = None
            try:
                event.widget.configure(cursor="")
            except tk.TclError:
                pass

    def reset_preview_views(self):
        for view in self.preview_views.values():
            view["zoom"] = 1.0
            view["pan_x"] = 0.0
            view["pan_y"] = 0.0
        self.redraw_preview()
        if self.surface_canvas is not None:
            self.redraw_topdown_preview()

    @staticmethod
    def _volumes_overlap(a, b) -> bool:
        # Entries: (index,row,x,y,z,sx,sy,sz)
        return (
            a[2] < b[2] + b[5] and b[2] < a[2] + a[5]
            and a[3] < b[3] + b[6] and b[3] < a[3] + a[6]
            and a[4] < b[4] + b[7] and b[4] < a[4] + a[7]
        )

    def _overlap_indices(self, entries):
        result: set[int] = set()
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                if self._volumes_overlap(entries[i], entries[j]):
                    result.add(entries[i][0])
                    result.add(entries[j][0])
        return result

    def redraw_preview(self):
        canvas = self.preview_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), 300)
        canvas.delete("all")
        if not self.rows:
            canvas.create_text(width / 2, height / 2, text="ファイルを追加してください", fill="#475569")
            return
        try:
            entries = self._preview_entries()
        except (ValueError, tk.TclError):
            canvas.create_text(width / 2, height / 2, text="X/Y/Zに整数を入力してください", fill="#b91c1c")
            return

        min_x = min(e[2] for e in entries)
        max_x = max(e[2] + e[5] for e in entries)
        min_z = min(e[4] for e in entries)
        max_z = max(e[4] + e[7] for e in entries)
        span_x, span_z = max(1, max_x - min_x), max(1, max_z - min_z)
        margin_x, margin_y = 58, 48
        base_scale = min((width - 2 * margin_x) / span_x, (height - 2 * margin_y) / span_z)
        base_scale = max(0.001, min(20.0, base_scale))
        state = self.preview_views["layout"]
        scale = base_scale * state["zoom"]
        center_x = (min_x + max_x) / 2
        center_z = (min_z + max_z) / 2
        left = width / 2 - center_x * scale + state["pan_x"]
        top = height / 2 - center_z * scale + state["pan_y"]
        map_w, map_h = span_x * scale, span_z * scale
        self.preview_transform = {
            "min_x": min_x, "min_z": min_z, "scale": scale,
            "left": left, "top": top, "center_x": center_x, "center_z": center_z,
            "width": float(width), "height": float(height),
        }

        grid_step = self._grid_step(span_x, span_z)
        view_min_x = (0 - left) / scale
        view_max_x = (width - left) / scale
        view_min_z = (0 - top) / scale
        view_max_z = (height - top) / scale
        gx_start = math.floor(view_min_x / grid_step) * grid_step
        gx_end = math.ceil(view_max_x / grid_step) * grid_step
        gz_start = math.floor(view_min_z / grid_step) * grid_step
        gz_end = math.ceil(view_max_z / grid_step) * grid_step
        # Guard against drawing a huge grid if extreme zoom/pan values are supplied.
        if (gx_end - gx_start) // grid_step > 400:
            grid_step *= math.ceil(((gx_end - gx_start) // grid_step) / 400)
            gx_start = math.floor(view_min_x / grid_step) * grid_step
            gx_end = math.ceil(view_max_x / grid_step) * grid_step
        if (gz_end - gz_start) // grid_step > 400:
            grid_step *= math.ceil(((gz_end - gz_start) // grid_step) / 400)
            gz_start = math.floor(view_min_z / grid_step) * grid_step
            gz_end = math.ceil(view_max_z / grid_step) * grid_step
        for gx in range(int(gx_start), int(gx_end) + grid_step, grid_step):
            px = left + gx * scale
            canvas.create_line(px, 0, px, height, fill="#e2e8f0", tags=("grid",))
            if 22 <= px <= width - 15:
                canvas.create_text(px, 12, text=str(gx), fill="#64748b", font=("Segoe UI", 8), tags=("grid",))
        for gz in range(int(gz_start), int(gz_end) + grid_step, grid_step):
            py = top + gz * scale
            canvas.create_line(0, py, width, py, fill="#e2e8f0", tags=("grid",))
            if 12 <= py <= height - 12:
                canvas.create_text(28, py, text=str(gz), fill="#64748b", font=("Segoe UI", 8), tags=("grid",))

        palette = ["#60a5fa", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#fb923c", "#22d3ee", "#94a3b8", "#4ade80", "#c084fc"]
        overlaps = self._overlap_indices(entries)
        rects = []
        for i, row, x, y, z, sx, sy, sz in entries:
            x1, z1 = left + x * scale, top + z * scale
            x2, z2 = left + (x + sx) * scale, top + (z + sz) * scale
            tag = f"structure_{i}"
            row_is_selected = row["instance_id"] == self.preview_selected_id
            if i in overlaps:
                fill, outline = "#fca5a5", "#dc2626"
            else:
                fill, outline = palette[i % len(palette)], "#334155"
            width_line = 3 if row_is_selected else (3 if i in overlaps else 1)
            if row_is_selected and i not in overlaps:
                outline = "#f59e0b"
            canvas.create_rectangle(x1, z1, x2, z2, fill=fill, stipple="gray50" if i in overlaps else "", outline=outline, width=width_line, tags=(tag, "structure"))
            rects.append((i, row, x, y, z, sx, sy, sz, x1, z1, x2, z2, tag))
        for i, row, x, y, z, sx, sy, sz, x1, z1, x2, z2, tag in rects:
            name = os.path.basename(row["path"])
            label = f"{i + 1}. {name}\nX={x} Y={y} Z={z}  回転={row['rotation'].get()}°"
            canvas.create_text((x1 + x2) / 2, (z1 + z2) / 2, text=label, fill="#0f172a", font=("Yu Gothic UI", 9, "bold"), justify="center", tags=(tag, "structure_label"))
        if overlaps:
            canvas.create_text(width - 12, 12, anchor="ne", text=f"⚠ 重なりあり：{len(overlaps)}個の構造物を赤で表示（配置は可能）", fill="#b91c1c", font=("Yu Gothic UI", 9, "bold"))
        canvas.create_text(10, height - 10, anchor="sw", text=f"範囲 X:{min_x}～{max_x} / Z:{min_z}～{max_z}   倍率: {state['zoom']:.2f}×   スナップ:64", fill="#475569", font=("Segoe UI", 8))

    @staticmethod
    def _is_transparent_block(name: str) -> bool:
        lower = name.lower()
        return lower in {"minecraft:air", "minecraft:cave_air", "minecraft:void_air", "minecraft:structure_void"} or lower.endswith(":air")

    @staticmethod
    def _block_color(name: str) -> tuple[int, int, int]:
        """Return a simple, deterministic material color for the block map."""
        import zlib as _zlib
        n = name.lower().split(":", 1)[-1]
        if any(c in n for c in ("water", "bubble_column")):
            return (66, 146, 220)
        if "lava" in n or "magma" in n:
            return (238, 104, 37)
        for cname, rgb in {
            "white": (225, 225, 220), "orange": (226, 133, 55), "magenta": (185, 82, 176),
            "light_blue": (105, 174, 214), "yellow": (232, 204, 70), "lime": (145, 199, 63),
            "pink": (224, 147, 169), "gray": (112, 116, 119), "light_gray": (181, 183, 181),
            "cyan": (69, 171, 174), "purple": (137, 86, 171), "blue": (62, 94, 187),
            "brown": (124, 89, 60), "green": (88, 143, 59), "red": (191, 61, 55), "black": (48, 48, 51),
        }.items():
            if cname in n and any(s in n for s in ("wool", "concrete", "terracotta", "glazed", "carpet", "stained_glass", "shulker_box")):
                return rgb
        if any(word in n for word in ("grass", "leaves", "moss", "azalea", "sapling", "vine", "fern", "slime", "sea_pickle")):
            return (84, 166, 74) if "slime" not in n else (113, 209, 82)
        if any(word in n for word in ("redstone", "redstone_lamp", "redstone_wire")):
            return (191, 54, 49)
        if "diamond" in n or "prismarine" in n:
            return (71, 190, 185)
        if "emerald" in n:
            return (73, 190, 106)
        if "gold" in n:
            return (230, 190, 57)
        if "copper" in n:
            return (178, 112, 72)
        if any(word in n for word in ("wood", "log", "planks", "fence", "gate", "chest", "barrel", "crafting_table", "sign")):
            return (151, 105, 64)
        if any(word in n for word in ("sand", "sandstone", "end_stone", "birch")):
            return (205, 190, 132)
        if any(word in n for word in ("dirt", "mud", "podzol", "mycelium", "soul_sand")):
            return (126, 91, 66)
        if any(word in n for word in ("glass", "ice", "snow", "powder_snow")):
            return (145, 194, 218)
        if any(word in n for word in ("piston", "observer", "dispenser", "dropper", "hopper", "furnace", "iron", "anvil", "stone", "deepslate", "cobble", "ore", "gravel")):
            return (132, 139, 145)
        if "obsidian" in n:
            return (75, 56, 108)
        if any(word in n for word in ("brick", "nether", "netherrack")):
            return (150, 73, 65)
        seed = _zlib.crc32(name.encode("utf-8"))
        return (70 + (seed & 0x7F), 70 + ((seed >> 8) & 0x7F), 70 + ((seed >> 16) & 0x7F))

    def _sample_top_column(self, world_x: int, world_z: int) -> tuple[int, int, int] | None:
        """Resolve one top-down column using merge order and both block layers."""
        # world_y -> [primary block name, secondary block name]. None denotes
        # no source wrote to that layer at that coordinate (void index -1).
        column: dict[int, list[str | None]] = {}
        for row in self.rows:
            src: LoadedStructure = row["loaded"]
            ox, oy, oz = (int(row[axis].get()) for axis in ("x", "y", "z"))
            turns = int(row["rotation"].get()) // 90
            sx, sy, sz = src.size
            rx_width, _ry, rz_depth = self._row_dimensions(row)
            lx_rot, lz_rot = world_x - ox, world_z - oz
            if not (0 <= lx_rot < rx_width and 0 <= lz_rot < rz_depth):
                continue
            if turns == 0:
                lx, lz = lx_rot, lz_rot
            elif turns == 1:
                lx, lz = lz_rot, sz - 1 - lx_rot
            elif turns == 2:
                lx, lz = sx - 1 - lx_rot, sz - 1 - lz_rot
            else:
                lx, lz = sx - 1 - lz_rot, lx_rot
            if row.get("mirror_x", False):
                lx = sx - 1 - lx
            if row.get("mirror_z", False):
                lz = sz - 1 - lz
            if not (0 <= lx < sx and 0 <= lz < sz):
                continue
            names = row["palette_names"]
            for ly in range(sy):
                src_index = (lx * sy + ly) * sz + lz
                world_y = oy + ly
                slot = column.get(world_y)
                if slot is None:
                    slot = [None, None]
                    column[world_y] = slot
                for layer_no in (0, 1):
                    overlap_mode = self.overlap_mode_var.get()
                    if layer_no >= len(src.layers):
                        if overlap_mode == "last_including_void":
                            slot[layer_no] = None
                        continue
                    idx = src.layers[layer_no][src_index]
                    if idx == -1:
                        if overlap_mode == "last_including_void":
                            slot[layer_no] = None
                        continue
                    incoming = names[idx] if 0 <= idx < len(names) else "unknown:block"
                    if overlap_mode == "first_non_air":
                        if slot[layer_no] is None and not self._is_transparent_block(incoming):
                            slot[layer_no] = incoming
                    else:
                        slot[layer_no] = incoming
        if not column:
            return None
        for world_y in sorted(column, reverse=True):
            primary, secondary = column[world_y]
            if primary is not None and not self._is_transparent_block(primary):
                return self._block_color(primary)
            if secondary is not None and not self._is_transparent_block(secondary):
                return self._block_color(secondary)
        return None

    def _surface_cache_prepare(self, entries) -> None:
        """Invalidate cached top columns only when the loaded layout changes."""
        signature = (self.overlap_mode_var.get(),) + tuple(
            (
                os.path.abspath(row["path"]), x, y, z, sx, sy, sz,
                int(row["rotation"].get()), bool(row.get("mirror_x")), bool(row.get("mirror_z")), id(row["loaded"]),
            )
            for _i, row, x, y, z, sx, sy, sz in entries
        )
        if signature != self.surface_cache_signature:
            self.surface_column_cache.clear()
            self.surface_cache_signature = signature

    def _cached_top_column(self, world_x: int, world_z: int):
        key = (world_x, world_z)
        cached = self.surface_column_cache.get(key, ...)
        if cached is not ...:
            self.surface_column_cache.move_to_end(key)
            return cached
        result = self._sample_top_column(world_x, world_z)
        self.surface_column_cache[key] = result
        if len(self.surface_column_cache) > self.surface_cache_limit:
            self.surface_column_cache.popitem(last=False)
        return result

    def redraw_topdown_preview(self):
        """Render the top-down map as one cached raster image, not thousands of Canvas items."""
        self.surface_after_id = None
        canvas = self.surface_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.update_idletasks()
        width = max(canvas.winfo_width(), 400)
        height = max(canvas.winfo_height(), 300)
        canvas.delete("all")
        if not self.rows:
            self.surface_image = None
            canvas.create_text(width / 2, height / 2, text="ファイルを追加してください", fill="#475569")
            return
        try:
            entries = self._preview_entries()
        except (ValueError, tk.TclError):
            self.surface_image = None
            canvas.create_text(width / 2, height / 2, text="X/Y/Zに整数を入力してください", fill="#b91c1c")
            return

        self._surface_cache_prepare(entries)
        min_x = min(e[2] for e in entries)
        max_x = max(e[2] + e[5] for e in entries)
        min_z = min(e[4] for e in entries)
        max_z = max(e[4] + e[7] for e in entries)
        span_x, span_z = max(1, max_x - min_x), max(1, max_z - min_z)
        max_img_w, max_img_h = max(1, width - 80), max(1, height - 80)
        base_scale = min(6.0, max_img_w / span_x, max_img_h / span_z)
        state = self.preview_views["surface"]
        scale = max(0.0001, base_scale * state["zoom"])
        center_x = (min_x + max_x) / 2
        center_z = (min_z + max_z) / 2
        left = width / 2 - center_x * scale + state["pan_x"]
        top = height / 2 - center_z * scale + state["pan_y"]
        self.surface_transform = {
            "min_x": min_x, "min_z": min_z, "scale": scale,
            "left": left, "top": top, "center_x": center_x, "center_z": center_z,
            "width": float(width), "height": float(height),
        }

        # Keep the same bounded sampling density as the previous renderer, but
        # paint it into one PhotoImage. This reduces thousands of Canvas rectangle
        # objects to a single image item and makes zoom/pan redraws much lighter.
        screen_min_x = left + min_x * scale
        screen_min_z = top + min_z * scale
        local_x0 = max(0, int(math.floor((0 - screen_min_x) / scale)))
        local_x1 = min(span_x, int(math.ceil((width - screen_min_x) / scale)))
        local_z0 = max(0, int(math.floor((0 - screen_min_z) / scale)))
        local_z1 = min(span_z, int(math.ceil((height - screen_min_z) / scale)))
        if local_x1 <= local_x0 or local_z1 <= local_z0:
            self.surface_image = None
            canvas.create_text(width / 2, height / 2,
                               text="表示範囲外です。右ドラッグで戻すか『全体表示に戻す』を押してください。",
                               fill="#475569", width=width - 40)
            return

        visible_x = local_x1 - local_x0
        visible_z = local_z1 - local_z0
        sample_step_x = max(1, int(math.ceil(visible_x / 320)))
        sample_step_z = max(1, int(math.ceil(visible_z / 240)))
        if scale < 1.0:
            sample_step_x = max(sample_step_x, int(math.ceil(1 / scale)))
            sample_step_z = max(sample_step_z, int(math.ceil(1 / scale)))
        sample_cols = (visible_x + sample_step_x - 1) // sample_step_x
        sample_rows = (visible_z + sample_step_z - 1) // sample_step_z
        background = (226, 232, 240)
        background_hex = "#e2e8f0"
        color_hex_cache: dict[tuple[int, int, int], str] = {background: background_hex}
        sampled_rows: list[list[str]] = []

        # Expensive NBT column resolution runs at most once per sampled world
        # column. Results survive all view-only redraws and are invalidated when
        # a file, offset, or rotation changes.
        for row_idx in range(sample_rows):
            cell_z0 = local_z0 + row_idx * sample_step_z
            cell_z1 = min(local_z1, cell_z0 + sample_step_z)
            wz = min_z + (cell_z0 + cell_z1 - 1) // 2
            sampled: list[str] = []
            for col_idx in range(sample_cols):
                cell_x0 = local_x0 + col_idx * sample_step_x
                cell_x1 = min(local_x1, cell_x0 + sample_step_x)
                wx = min_x + (cell_x0 + cell_x1 - 1) // 2
                color = self._cached_top_column(wx, wz) or background
                hex_color = color_hex_cache.get(color)
                if hex_color is None:
                    hex_color = "#%02x%02x%02x" % color
                    color_hex_cache[color] = hex_color
                sampled.append(hex_color)
            sampled_rows.append(sampled)

        # Expand the small sampled colour grid to the current viewport. A single
        # Tk PhotoImage is considerably faster to draw than thousands of canvas
        # rectangles, while retaining crisp nearest-neighbour block colours.
        render_width = width
        render_height = height
        x_sample_for_pixel: list[int | None] = []
        for px in range(render_width):
            wx = math.floor((px - left) / scale)
            local_x = wx - min_x
            if local_x0 <= local_x < local_x1:
                col_idx = (local_x - local_x0) // sample_step_x
                x_sample_for_pixel.append(min(sample_cols - 1, int(col_idx)))
            else:
                x_sample_for_pixel.append(None)

        bg_row = "{" + " ".join([background_hex] * render_width) + "}"
        expanded_rows: dict[int, str] = {}
        photo_rows: list[str] = []
        for py in range(render_height):
            wz = math.floor((py - top) / scale)
            local_z = wz - min_z
            if local_z0 <= local_z < local_z1:
                row_idx = min(sample_rows - 1, int((local_z - local_z0) // sample_step_z))
                row_text = expanded_rows.get(row_idx)
                if row_text is None:
                    samples = sampled_rows[row_idx]
                    pixels = [samples[idx] if idx is not None else background_hex for idx in x_sample_for_pixel]
                    row_text = "{" + " ".join(pixels) + "}"
                    expanded_rows[row_idx] = row_text
                photo_rows.append(row_text)
            else:
                photo_rows.append(bg_row)

        try:
            photo = tk.PhotoImage(master=canvas, width=render_width, height=render_height)
            photo.put(" ".join(photo_rows))
            self.surface_image = photo
            canvas.create_image(0, 0, anchor="nw", image=photo, tags=("surface_raster",))
        except tk.TclError:
            # Fallback to a simple background if a platform's Tk image parser
            # rejects a very large raster string; outlines and grid remain usable.
            self.surface_image = None

        grid_step = self._grid_step(span_x, span_z)
        view_min_x = (0 - left) / scale
        view_max_x = (width - left) / scale
        view_min_z = (0 - top) / scale
        view_max_z = (height - top) / scale
        if (view_max_x - view_min_x) / grid_step > 400 or (view_max_z - view_min_z) / grid_step > 400:
            grid_step *= max(1, int(math.ceil(max((view_max_x - view_min_x) / grid_step, (view_max_z - view_min_z) / grid_step) / 400)))
        gx_start = math.floor(view_min_x / grid_step) * grid_step
        gx_end = math.ceil(view_max_x / grid_step) * grid_step
        gz_start = math.floor(view_min_z / grid_step) * grid_step
        gz_end = math.ceil(view_max_z / grid_step) * grid_step
        for gx in range(int(gx_start), int(gx_end) + grid_step, grid_step):
            px = left + gx * scale
            canvas.create_line(px, 0, px, height, fill="#f8fafc", width=1, tags=("surface_grid",))
            if 22 <= px <= width - 15:
                canvas.create_text(px, 11, text=str(gx), fill="#334155", font=("Segoe UI", 8), tags=("surface_grid",))
        for gz in range(int(gz_start), int(gz_end) + grid_step, grid_step):
            py = top + gz * scale
            canvas.create_line(0, py, width, py, fill="#f8fafc", width=1, tags=("surface_grid",))
            if 12 <= py <= height - 12:
                canvas.create_text(28, py, text=str(gz), fill="#334155", font=("Segoe UI", 8), tags=("surface_grid",))

        overlaps = self._overlap_indices(entries)
        outline_palette = ["#2563eb", "#059669", "#d97706", "#db2777", "#7c3aed", "#ea580c", "#0891b2", "#475569"]
        for i, row, x, y, z, sx, sy, sz in entries:
            x1, z1 = left + x * scale, top + z * scale
            x2, z2 = left + (x + sx) * scale, top + (z + sz) * scale
            if i in overlaps:
                outline = "#dc2626"
                line_width = 3
            else:
                outline = outline_palette[i % len(outline_palette)]
                line_width = 3 if row["instance_id"] == self.preview_selected_id else 2
            if row["instance_id"] == self.preview_selected_id and i not in overlaps:
                outline = "#f59e0b"
            canvas.create_rectangle(x1, z1, x2, z2, outline=outline, width=line_width)
        if overlaps:
            canvas.create_text(width - 12, 12, anchor="ne", text=f"⚠ 重なりあり：{len(overlaps)}個（赤枠）", fill="#b91c1c", font=("Yu Gothic UI", 9, "bold"))
        canvas.create_text(10, height - 10, anchor="sw", text=f"上面図 {span_x}×{span_z} blocks | グリッド {grid_step} blocks | 倍率 {state['zoom']:.2f}×", fill="#334155", font=("Segoe UI", 8))

    def preview_press(self, event):
        canvas = self.preview_canvas
        if canvas is None:
            return
        items = canvas.find_overlapping(event.x, event.y, event.x, event.y)
        chosen = None
        for item in reversed(items):
            for tag in canvas.gettags(item):
                if tag.startswith("structure_"):
                    raw = tag[len("structure_"):]
                    if raw.isdigit() and int(raw) < len(self.rows):
                        chosen = int(raw)
                        break
            if chosen is not None:
                break
        if chosen is None:
            return
        row = self.rows[chosen]
        instance_id = row["instance_id"]
        if instance_id != self.preview_selected_id:
            self.preview_selected_id = instance_id
            self.preview_drag = None
            self.redraw_preview()
            if self._surface_tab_is_selected():
                self._schedule_surface_redraw()
            self.status.set(f"選択: {os.path.basename(row['path'])}。移動するには選択した構造物をもう一度クリックしてドラッグしてください。")
            return
        try:
            start_x = int(row["x"].get())
            start_z = int(row["z"].get())
        except ValueError:
            return
        tag = f"structure_{chosen}"
        item_coords = {item: canvas.coords(item) for item in canvas.find_withtag(tag)}
        self.preview_drag = {
            "index": chosen,
            "mouse_x": event.x,
            "mouse_y": event.y,
            "last_mouse_x": event.x,
            "last_mouse_y": event.y,
            "start_x": start_x,
            "start_z": start_z,
            "tag": tag,
        }
        # Keep receiving motion/release events even if the pointer briefly leaves
        # the canvas. Moving a tag is substantially cheaper than updating each
        # rectangle/text item's coordinates individually.
        try:
            canvas.tag_raise(tag)
            canvas.grab_set()
        except tk.TclError:
            pass
        canvas.configure(cursor="fleur")

    def preview_motion(self, event):
        canvas = self.preview_canvas
        drag = self.preview_drag
        if canvas is None or drag is None:
            return
        # Apply only the delta since the previous motion event. This keeps the
        # selected structure visually attached to the cursor at every event and
        # avoids a full preview rebuild while dragging.
        dx = event.x - drag["last_mouse_x"]
        dz = event.y - drag["last_mouse_y"]
        if dx or dz:
            canvas.move(drag["tag"], dx, dz)
            drag["last_mouse_x"] = event.x
            drag["last_mouse_y"] = event.y

    def preview_release(self, event):
        canvas = self.preview_canvas
        drag = self.preview_drag
        if canvas is None or drag is None:
            return
        # Include the final pointer delta in case the last motion event was
        # coalesced by Windows/Tk immediately before mouse release.
        final_dx = event.x - drag["last_mouse_x"]
        final_dz = event.y - drag["last_mouse_y"]
        if final_dx or final_dz:
            canvas.move(drag["tag"], final_dx, final_dz)
            drag["last_mouse_x"] = event.x
            drag["last_mouse_y"] = event.y
        scale = self.preview_transform.get("scale", 1.0)
        dx, dz = event.x - drag["mouse_x"], event.y - drag["mouse_y"]
        if abs(dx) < 3 and abs(dz) < 3:
            self.preview_drag = None
            try:
                canvas.grab_release()
            except tk.TclError:
                pass
            canvas.configure(cursor="")
            self.redraw_preview()
            return
        raw_x = drag["start_x"] + dx / scale
        raw_z = drag["start_z"] + dz / scale

        def snap64(value: float) -> int:
            units = value / 64.0
            nearest = math.floor(units + 0.5) if units >= 0 else math.ceil(units - 0.5)
            return int(nearest * 64)

        snapped_x = snap64(raw_x)
        snapped_z = snap64(raw_z)
        row = self.rows[drag["index"]]
        row["x"].set(str(snapped_x))
        row["z"].set(str(snapped_z))
        self.preview_drag = None
        try:
            canvas.grab_release()
        except tk.TclError:
            pass
        canvas.configure(cursor="")
        self.render_rows()
        self.redraw_preview()
        if self._surface_tab_is_selected():
            self._schedule_surface_redraw()
        overlap_count = len(self._overlap_indices(self._preview_entries())) if self.rows else 0
        msg = f"{os.path.basename(row['path'])} を X={snapped_x}, Z={snapped_z} に移動しました（64ブロック単位）"
        if overlap_count:
            msg += f"。重なりあり: {overlap_count}個の構造物を赤表示しています。"
        self.status.set(msg)
        self._commit_history("プレビュー移動")

    def merge(self):
        if not self.rows:
            messagebox.showwarning("入力がありません", "先にmcstructure v2ファイルを追加してください。")
            return
        inputs: list[tuple] = []
        try:
            for row in self.rows:
                offset = (int(row["x"].get()), int(row["y"].get()), int(row["z"].get()))
                rotation = int(row["rotation"].get())
                inputs.append((row["path"], offset, rotation, bool(row.get("mirror_x")), bool(row.get("mirror_z"))))
        except ValueError:
            messagebox.showerror("設定エラー", "配置X/Y/Zには整数、回転には0/90/180/270を指定してください。")
            return

        output_path = filedialog.asksaveasfilename(
            title="結合後のmcstructureを保存",
            defaultextension=".mcstructure",
            initialfile="merged_v2.mcstructure",
            filetypes=[("Minecraft structure", "*.mcstructure"), ("All files", "*.*")],
        )
        if not output_path:
            return
        if any(os.path.abspath(output_path) == os.path.abspath(item[0]) for item in inputs):
            messagebox.showerror("保存先エラー", "入力ファイルと同じ場所・名前には保存できません。別名を指定してください。")
            return

        self.status.set("結合中…")
        self.root.update_idletasks()
        try:
            result = merge_structures(inputs, output_path, self.overlap_mode_var.get())
        except Exception as e:
            self.status.set("結合に失敗しました")
            messagebox.showerror("結合エラー", str(e))
            return
        self.status.set(f"完了: {result['size'][0]}×{result['size'][1]}×{result['size'][2]}")
        messagebox.showinfo(
            "結合完了",
            "結合ファイルを保存しました。\n\n"
            f"保存先: {result['output']}\n"
            f"サイズ: {result['size'][0]}×{result['size'][1]}×{result['size'][2]}\n"
            f"パレット: {result['palette_entries']}種類\n"
            f"ブロック位置データ: {result['block_position_data']}件\n"
            f"通常エンティティ: {result['entities']}件\n\n"
            "※NBT構造の検証を行いますが、Minecraft内での読み込みテストは別途必要です。"
        )

    def on_close(self):
        if self._autosave_after_id:
            try:
                self.root.after_cancel(self._autosave_after_id)
            except tk.TclError:
                pass
            self._autosave_after_id = None
        self._save_autosave_now()
        for temp_dir in self.temp_dirs:
            try:
                temp_dir.cleanup()
            except Exception:
                pass
        self.temp_dirs.clear()
        self.root.destroy()

def main():
    root = tk.Tk()
    MergerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
