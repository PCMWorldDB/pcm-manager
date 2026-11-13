#!/usr/bin/env node

// src/cli.ts
import { realpathSync } from "fs";
import { mkdir, readFile, writeFile } from "fs/promises";
import { dirname, extname, resolve } from "path";
import process from "process";
import { fileURLToPath } from "url";
import initSqlJs from "sql.js";

// src/compression.ts
import { deflate, inflate } from "pako";
function toUint8Array(data) {
  return data instanceof Uint8Array ? data : new Uint8Array(data);
}
function decompressCdb(data) {
  const bytes = toUint8Array(data);
  if (bytes.byteLength < 4) {
    return bytes.slice().buffer;
  }
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(0, true) !== 4294967295 /* COMPRESSION_MAGIC */) {
    return bytes.slice().buffer;
  }
  if (bytes.byteLength < 12) {
    throw new Error("Malformed compressed CDB: incomplete compression header");
  }
  const uncompressedSize = view.getUint32(4, true);
  const compressedSize = view.getUint32(8, true);
  const compressedEnd = 12 + compressedSize;
  if (compressedEnd > bytes.byteLength) {
    throw new Error(
      "Malformed compressed CDB: compressed payload exceeds input length"
    );
  }
  const compressedData = bytes.subarray(12, compressedEnd);
  let decompressed;
  try {
    decompressed = inflate(compressedData);
  } catch {
    throw new Error("Malformed compressed CDB: invalid compressed payload");
  }
  if (decompressed.byteLength !== uncompressedSize) {
    throw new Error(
      "Malformed compressed CDB: uncompressed size does not match header"
    );
  }
  const result = new Uint8Array(decompressed.byteLength);
  result.set(decompressed);
  return result.buffer;
}
function compressCdb(data) {
  const uncompressedData = toUint8Array(data);
  const compressed = deflate(uncompressedData);
  const result = new Uint8Array(12 + compressed.length);
  const view = new DataView(result.buffer);
  view.setUint32(0, 4294967295 /* COMPRESSION_MAGIC */, true);
  view.setUint32(4, uncompressedData.length, true);
  view.setUint32(8, compressed.length, true);
  result.set(compressed, 12);
  return result.buffer.slice(
    result.byteOffset,
    result.byteOffset + result.byteLength
  );
}

// src/keyInference.ts
var TABLE_PREFIXES = ["DYN_", "STA_", "GAM_", "INF_"];
var PREFIX_PRIORITY = {
  DYN_: 0,
  STA_: 1,
  GAM_: 2,
  INF_: 3
};
var FK_MODIFIERS = [
  "old",
  "new",
  "previous",
  "prev",
  "next",
  "main",
  "first",
  "last",
  "booked",
  "current",
  "u23"
];
var NON_FK_PATTERNS = [
  /^fkIDloc_/,
  /^fkIDtga_/,
  /^fkIDformated/,
  /_string$/
];
var NON_FK_COLUMNS = /* @__PURE__ */ new Set([
  "fkIDgamemode",
  "fkIDgame_state",
  "fkIDrace_difficulty",
  "fkIDequipment_type",
  "fkIDstate_roster",
  "fkIDitem"
]);
var FK_EXCEPTIONS = {
  // Bilingual naming gaps: "marque" is French for brand, the "trainer" staff
  // lives in DYN_coach, a cyclist's "workplan" is a training exercise.
  fkIDequipment_marque: "STA_brand",
  fkIDstaff_trainer: "DYN_coach",
  fkIDworkplan: "STA_training_exercise",
  fkIDyear_progression: "DYN_cyclist_progression",
  // Equipment slots hold techno ids, even though model ids happen to cover
  // the same value range.
  fkIDequipment_frame: "DYN_equipment_techno",
  fkIDequipment_front_wheel: "DYN_equipment_techno",
  fkIDequipment_rear_wheel: "DYN_equipment_techno",
  fkIDequipment_helmet: "DYN_equipment_techno",
  fkIDequipment_glasses: "DYN_equipment_techno",
  // Discriminator of the polymorphic fkIDitem pair (CYCLIST/TEAM/COUNTRY).
  fkIDitem_type: "STA_ranking_item",
  // National-selection invitations (world/european championships), not race
  // invitations (STA_invitation_state).
  fkIDinvitation_state_WC: "STA_national_team_invitation_state",
  fkIDinvitation_state_WC_ITT: "STA_national_team_invitation_state",
  fkIDinvitation_state_EC: "STA_national_team_invitation_state",
  fkIDinvitation_state_EC_ITT: "STA_national_team_invitation_state",
  // Never observed populated in real saves; the stage-booking table is the
  // only target consistent with the training-camp lifecycle.
  fkIDtraining_camp: "DYN_training_stage_booking"
};
function getPrefix(tableName) {
  return TABLE_PREFIXES.find((p) => tableName.startsWith(p)) ?? null;
}
function baseNameOf(tableName) {
  const prefix = getPrefix(tableName);
  return prefix ? tableName.slice(prefix.length) : null;
}
function normalize(name) {
  return name.toLowerCase().replace(/_/g, "");
}
function tokenSet(tableName) {
  const base = baseNameOf(tableName) ?? tableName;
  return new Set(
    base.toLowerCase().split("_").filter(Boolean).map((token) => token.replace(/s$/, ""))
  );
}
function tokenOverlap(a, b) {
  let count = 0;
  for (const token of a) if (b.has(token)) count++;
  return count;
}
function detectPrimaryKey(table) {
  const base = baseNameOf(table.name);
  if (base) {
    const conventional = `ID${base}`;
    if (table.columns.some((c) => c.name === conventional)) return conventional;
  }
  const first = table.columns[0]?.name;
  if (first && (first === "Id" || first === "UID" || /^ID./.test(first))) {
    return first;
  }
  return null;
}
function pickTarget(candidates, key) {
  if (candidates.length === 0) return null;
  const ranked = [...candidates].sort((a, b) => {
    const aOwns = a.base === key ? 0 : 1;
    const bOwns = b.base === key ? 0 : 1;
    if (aOwns !== bOwns) return aOwns - bOwns;
    const aPriority = PREFIX_PRIORITY[getPrefix(a.table) ?? ""] ?? 99;
    const bPriority = PREFIX_PRIORITY[getPrefix(b.table) ?? ""] ?? 99;
    if (aPriority !== bPriority) return aPriority - bPriority;
    return a.table.length - b.table.length;
  });
  return ranked[0];
}
function preferCatalogueTwin(target, sourceTable, index) {
  const sourceBase = baseNameOf(sourceTable);
  if (!sourceBase) return target;
  const key = normalize(sourceBase);
  if (target.base !== key && target.entity !== key) return target;
  const twin = (index.get(key) ?? []).find(
    (c) => c.table.startsWith("STA_") && (c.base === key || c.entity === key)
  );
  return twin ?? target;
}
function lookup(index, rawKey) {
  const key = normalize(rawKey);
  const direct = index.get(key);
  if (direct) return pickTarget(direct, key);
  if (key.length < 4) return null;
  const variant = key.endsWith("s") ? key.slice(0, -1) : `${key}s`;
  const alternate = index.get(variant);
  return alternate ? pickTarget(alternate, variant) : null;
}
function resolveByTruncation(rawKeys, sourceTable, index) {
  const sourceTokens = tokenSet(sourceTable);
  for (const rawKey of rawKeys) {
    const key = normalize(rawKey);
    if (key.length < 4) continue;
    const matches = [];
    for (const [indexKey, targets] of index) {
      if (indexKey === key) continue;
      if (!indexKey.startsWith(key) && !indexKey.endsWith(key)) continue;
      const target = pickTarget(targets, indexKey);
      if (target && !matches.some((m) => m.table === target.table)) {
        matches.push(target);
      }
    }
    if (matches.length === 0) continue;
    if (matches.length === 1) return matches[0];
    const scored = matches.map((target) => ({
      target,
      overlap: tokenOverlap(tokenSet(target.table), sourceTokens)
    })).sort(
      (a, b) => b.overlap - a.overlap || a.target.table.length - b.target.table.length
    );
    const [best, runnerUp] = scored;
    if (best.overlap > runnerUp.overlap || best.target.table.length < runnerUp.target.table.length) {
      return best.target;
    }
  }
  return null;
}
function resolveForeignKey(column, sourceTable, index, pkByTable) {
  if (NON_FK_COLUMNS.has(column) || NON_FK_PATTERNS.some((p) => p.test(column))) {
    return null;
  }
  const curated = FK_EXCEPTIONS[column];
  if (curated) {
    const refColumn = pkByTable.get(curated);
    return refColumn ? { column, refTable: curated, refColumn } : null;
  }
  const suffix = column.slice("fkID".length).replace(/^_+/, "");
  if (!suffix) return null;
  const toForeignKey = (target) => {
    const resolved = preferCatalogueTwin(target, sourceTable, index);
    return { column, refTable: resolved.table, refColumn: resolved.idColumn };
  };
  const directKeys = [suffix];
  const modifierStripped = [];
  for (const modifier of FK_MODIFIERS) {
    if (!suffix.startsWith(modifier)) continue;
    let rest = suffix.slice(modifier.length);
    if (rest.startsWith("_")) rest = rest.slice(1);
    if (rest) modifierStripped.push(rest);
  }
  directKeys.push(...modifierStripped);
  const withoutDigits = suffix.replace(/\d+$/, "");
  if (withoutDigits !== suffix && withoutDigits) directKeys.push(withoutDigits);
  for (const key of directKeys) {
    const target = lookup(index, key);
    if (target) return toForeignKey(target);
  }
  const tokens = suffix.split("_").filter(Boolean);
  if (tokens.length > 1) {
    const dropKeys = [tokens.slice(1).join("_")];
    for (let end = tokens.length - 1; end >= 1; end--) {
      dropKeys.push(tokens.slice(0, end).join("_"));
    }
    const sourceTokens = tokenSet(sourceTable);
    let best = null;
    let bestOverlap = -1;
    for (const key of dropKeys) {
      const target = lookup(index, key);
      if (!target) continue;
      const overlap = tokenOverlap(tokenSet(target.table), sourceTokens);
      if (overlap > bestOverlap) {
        best = target;
        bestOverlap = overlap;
      }
    }
    if (best) return toForeignKey(best);
  }
  const truncated = resolveByTruncation(
    [suffix, ...modifierStripped],
    sourceTable,
    index
  );
  return truncated ? toForeignKey(truncated) : null;
}
function inferKeys(tables) {
  const pkByTable = new Map(
    tables.map((t) => [t.name, detectPrimaryKey(t)])
  );
  const index = /* @__PURE__ */ new Map();
  const register = (key, target) => {
    if (!key) return;
    const bucket = index.get(key);
    if (bucket) bucket.push(target);
    else index.set(key, [target]);
  };
  for (const table of tables) {
    const idColumn = pkByTable.get(table.name);
    if (!idColumn) continue;
    const base = baseNameOf(table.name);
    const target = {
      table: table.name,
      idColumn,
      base: base ? normalize(base) : null,
      entity: /^ID./.test(idColumn) ? normalize(idColumn.slice(2)) : null
    };
    register(target.entity, target);
    if (target.base !== target.entity) register(target.base, target);
  }
  const result = /* @__PURE__ */ new Map();
  for (const table of tables) {
    const primaryKey = pkByTable.get(table.name) ?? null;
    const foreignKeys = [];
    if (primaryKey && /^ID./.test(primaryKey)) {
      const entityKey = normalize(primaryKey.slice(2));
      const base = baseNameOf(table.name);
      if (base && normalize(base) !== entityKey) {
        const owners = (index.get(entityKey) ?? []).filter(
          (c) => c.base === entityKey
        );
        const owner = pickTarget(owners, entityKey);
        if (owner && owner.table !== table.name) {
          foreignKeys.push({
            column: primaryKey,
            refTable: owner.table,
            refColumn: owner.idColumn
          });
        }
      }
    }
    for (const col of table.columns) {
      if (!col.name.startsWith("fkID")) continue;
      const fk = resolveForeignKey(col.name, table.name, index, pkByTable);
      if (fk) foreignKeys.push(fk);
    }
    result.set(table.name, { primaryKey, foreignKeys });
  }
  return result;
}

// src/reader.ts
var CDBReader = class {
  constructor(arrayBuffer) {
    const bytes = arrayBuffer instanceof Uint8Array ? arrayBuffer : new Uint8Array(arrayBuffer);
    this.data = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    this.pos = 0;
  }
  getChunkChildren(chunk) {
    if (!chunk.children) {
      throw new Error(`Chunk ${chunk.type} is missing children`);
    }
    return chunk.children;
  }
  getChunkHeader(chunk) {
    if (!chunk.header) {
      throw new Error(`Chunk ${chunk.type} is missing a header`);
    }
    return chunk.header;
  }
  getRequiredDescription(chunk, label) {
    const description = this.getChunkHeader(chunk).description;
    if (description === null) {
      throw new Error(`Invalid ${label} chunk: missing ${label} description`);
    }
    return description;
  }
  getRequiredChild(chunk, childType) {
    const children = this.getChunkChildren(chunk);
    const value = children[childType];
    if (value === void 0) {
      throw new Error(`Chunk ${chunk.type} is missing child ${childType}`);
    }
    return value;
  }
  read32() {
    if (this.pos + 4 > this.data.byteLength) {
      throw new Error(`Read past end of file at position ${this.pos}`);
    }
    const value = this.data.getUint32(this.pos, true);
    this.pos += 4;
    return value;
  }
  readBytes(length) {
    if (this.pos + length > this.data.byteLength) {
      throw new Error(`Read past end of file at position ${this.pos}`);
    }
    const bytes = new Uint8Array(
      this.data.buffer,
      this.data.byteOffset + this.pos,
      length
    );
    this.pos += length;
    return bytes;
  }
  readPadding() {
    const padding = 4 - (this.pos & 3) & 3;
    this.pos += padding;
  }
  readMagic(expected, label) {
    const actual = this.read32();
    if (actual !== expected) {
      throw new Error(
        `Invalid ${label} magic: expected 0x${expected.toString(16)}, got 0x${actual.toString(16)}`
      );
    }
  }
  readChunkHeader() {
    this.readMagic(2863311530 /* CHUNK_BEGIN */, "CHUNK_BEGIN");
    const chunkSize = this.read32();
    const chunkType = this.read32();
    const flags = this.read32();
    const hasDescription = this.read32();
    let description = null;
    if (hasDescription) {
      const descLength = this.read32();
      const descBytes = this.readBytes(descLength - 1);
      description = new TextDecoder().decode(descBytes);
      this.pos++;
    }
    this.readPadding();
    this.readMagic(3149642683 /* CHUNK_SEPARATOR */, "CHUNK_SEPARATOR");
    return { chunkSize, chunkType, flags, description };
  }
  readChunk() {
    const chunkStartPos = this.pos;
    const header = this.readChunkHeader();
    const chunkEndPos = chunkStartPos + header.chunkSize;
    let result;
    switch (header.chunkType) {
      case 17 /* ROW_COUNT */:
      case 21 /* TABLE_ID */:
      case 22 /* TABLE_FLAGS */:
      case 2 /* DATABASE_FLAGS */:
      case 36 /* COLUMN_INDEX */:
      case 33 /* COLUMN_DATA_TYPE */:
        result = { type: header.chunkType, value: this.read32() };
        break;
      case 34 /* COLUMN_VALUES */:
        {
          const dataBytes = chunkEndPos - this.pos - 4;
          const values = [];
          for (let i = 0; i < dataBytes / 4; i++) {
            values.push(this.read32());
          }
          result = { type: header.chunkType, value: values };
        }
        break;
      case 35 /* COLUMN_BLOB_DATA */:
        {
          const sizedDataBytes = chunkEndPos - this.pos - 4;
          result = {
            type: header.chunkType,
            value: this.readBytes(sizedDataBytes)
          };
        }
        break;
      case 1 /* DATABASE_TABLES */:
        {
          const tables = this.readArray(() => {
            const tableChunk = this.readChunk();
            const rowCount = this.getRequiredChild(tableChunk, 17 /* ROW_COUNT */) || 0;
            const columnDefinitions = this.getRequiredChild(
              tableChunk,
              18 /* COLUMN_DEFINITIONS */
            );
            const columns = columnDefinitions.map((column) => ({
              name: column.name,
              type: column.type,
              columnIndex: column.columnIndex,
              data: column.columnChunk ? this.convertColumnData(column.columnChunk, rowCount) : column.data ?? []
            }));
            const tableName = this.getRequiredDescription(tableChunk, "table");
            return {
              name: tableName,
              rowCount,
              columns,
              tableId: this.getRequiredChild(
                tableChunk,
                21 /* TABLE_ID */
              ),
              tableFlags: this.getRequiredChild(
                tableChunk,
                22 /* TABLE_FLAGS */
              )
            };
          });
          result = { type: header.chunkType, value: tables };
        }
        break;
      case 18 /* COLUMN_DEFINITIONS */:
        {
          const columns = this.readArray(() => {
            const columnChunk = this.readChunk();
            const colName = this.getRequiredDescription(columnChunk, "column");
            return {
              name: colName,
              type: this.getRequiredChild(
                columnChunk,
                33 /* COLUMN_DATA_TYPE */
              ),
              columnIndex: this.getRequiredChild(
                columnChunk,
                36 /* COLUMN_INDEX */
              ),
              columnChunk
              // Store for later conversion
            };
          });
          result = { type: header.chunkType, value: columns };
        }
        break;
      case 0 /* WRAPPER */:
      case 16 /* TABLE */:
      case 32 /* COLUMN */:
        {
          const children = {};
          while (this.pos < chunkEndPos) {
            if (chunkEndPos - this.pos < 20) {
              break;
            }
            const chunk = this.readChunk();
            children[chunk.type] = chunk.value;
          }
          result = {
            type: header.chunkType,
            header,
            children
          };
        }
        break;
      default:
        {
          if (typeof console !== "undefined") {
            console.warn(
              `Skipping unknown chunk type: 0x${header.chunkType.toString(16)} at position ${chunkStartPos}`
            );
          }
          const skippedBytes = chunkEndPos - this.pos - 4;
          if (skippedBytes < 0) {
            throw new Error(
              `Invalid chunk size for unknown chunk type 0x${header.chunkType.toString(16)} at position ${chunkStartPos}`
            );
          }
          result = {
            type: header.chunkType,
            value: this.readBytes(skippedBytes)
          };
        }
        break;
    }
    this.readPadding();
    this.readMagic(3435973836 /* CHUNK_END */, "CHUNK_END");
    return result;
  }
  readArray(itemReader) {
    this.readMagic(3722304989 /* ARRAY_BEGIN */, "ARRAY_BEGIN");
    const count = this.read32();
    const items = [];
    for (let i = 0; i < count; i++) {
      items.push(itemReader());
    }
    this.readMagic(4008636142 /* ARRAY_END */, "ARRAY_END");
    return items;
  }
  convertColumnData(columnChunk, rowCount) {
    const dataType = this.getRequiredChild(
      columnChunk,
      33 /* COLUMN_DATA_TYPE */
    );
    const rawData = this.getChunkChildren(columnChunk)[34 /* COLUMN_VALUES */] ?? [];
    const sizedData = this.getChunkChildren(columnChunk)[35 /* COLUMN_BLOB_DATA */] ?? new Uint8Array([0, 0, 0, 0]);
    if (rawData.length === 0 && rowCount !== void 0) {
      switch (dataType) {
        case 2 /* STRING */:
          return Array(rowCount).fill("");
        case 1 /* FLOAT */:
          return Array(rowCount).fill(0);
        case 10 /* FLOAT_LIST */:
        case 11 /* INTEGER_LIST */:
          return Array(rowCount).fill("()");
        default:
          return Array(rowCount).fill(0);
      }
    }
    switch (dataType) {
      case 0 /* INTEGER */:
        return rawData.map((value) => value | 0);
      case 3 /* BOOLEAN */: {
        if (rowCount === void 0) {
          throw new Error("Row count required for boolean type");
        }
        const bytes = new Uint8Array(new Uint32Array(rawData).buffer);
        const boolValues = [];
        for (let i = 0; i < rowCount; i++) {
          const byteIndex = Math.floor(i / 8);
          const bitIndex = i % 8;
          boolValues.push(bytes[byteIndex] >> bitIndex & 1);
        }
        return boolValues;
      }
      case 4 /* INTEGER_BYTE */: {
        const bytes = new Uint8Array(new Uint32Array(rawData).buffer).slice(
          0,
          rowCount
        );
        return Array.from(bytes, (b) => b > 127 ? b - 256 : b);
      }
      case 5 /* INTEGER_SHORT */: {
        const bytes = new Uint8Array(new Uint32Array(rawData).buffer);
        const int16Values = [];
        for (let i = 0; i < rowCount; i++) {
          const value = bytes[i * 2] | bytes[i * 2 + 1] << 8;
          int16Values.push(value > 32767 ? value - 65536 : value);
        }
        return int16Values;
      }
      case 1 /* FLOAT */: {
        const view = new DataView(new ArrayBuffer(4));
        return rawData.map((intValue) => {
          view.setUint32(0, intValue, true);
          return view.getFloat32(0, true);
        });
      }
      case 2 /* STRING */:
        return this.parseStrings(sizedData, rawData);
      case 11 /* INTEGER_LIST */:
        return this.parseNumericLists(sizedData, rawData, (view, offset) => {
          return view.getUint32(offset, true) | 0;
        });
      case 10 /* FLOAT_LIST */:
        return this.parseNumericLists(
          sizedData,
          rawData,
          (view, offset, count) => {
            const value = view.getFloat32(offset, true);
            let formatted = this.formatFloat32(value);
            if (count > 1 && !formatted.includes(".") && !formatted.includes("e")) {
              formatted += ".0";
            }
            return formatted;
          }
        );
      default:
        throw new Error(`Unknown data type: ${dataType}`);
    }
  }
  formatFloat32(value) {
    for (let precision = 1; precision <= 9; precision++) {
      const candidate = Number(value.toPrecision(precision));
      if (Math.fround(candidate) === value) {
        return candidate.toString();
      }
    }
    return value.toString();
  }
  parseStrings(sizedData, lengths) {
    let currentOffset = 4;
    return lengths.map((stringLength) => {
      const stringBytes = sizedData.subarray(
        currentOffset,
        currentOffset + stringLength - 1
      );
      currentOffset += stringLength;
      return new TextDecoder().decode(stringBytes);
    });
  }
  parseNumericLists(sizedData, counts, readValue) {
    const view = new DataView(
      sizedData.buffer,
      sizedData.byteOffset,
      sizedData.byteLength
    );
    let currentOffset = 4;
    return counts.map((count) => {
      const values = Array.from({ length: count }, () => {
        const value = readValue(view, currentOffset, count);
        currentOffset += 4;
        return value;
      });
      return `(${values.join(",")})`;
    });
  }
};

// src/sqlUtils.ts
function escapeSqlIdentifier(identifier) {
  return identifier.replace(/"/g, '""');
}

// src/cdbToSql.ts
function isUniqueNonNull(data) {
  const seen = /* @__PURE__ */ new Set();
  for (const value of data) {
    if (value === null || value === void 0) return false;
    if (seen.has(value)) return false;
    seen.add(value);
  }
  return true;
}
function buildTableConstraints(table, keys, columnDefs, escapedTableName, options, keyMap, enforcedPkByTable) {
  if (!keys) return { tableBody: columnDefs, indexes: [] };
  const constraints = [];
  const indexes = [];
  if (keys.primaryKey) {
    const pkColumn = table.columns.find((col) => col.name === keys.primaryKey);
    if (pkColumn && enforcedPkByTable.get(table.name)) {
      constraints.push(
        `PRIMARY KEY ("${escapeSqlIdentifier(keys.primaryKey)}")`
      );
    } else if (pkColumn) {
      indexes.push(
        `CREATE INDEX IF NOT EXISTS "${escapeSqlIdentifier(
          `${table.name}__${keys.primaryKey}_pk_idx`
        )}" ON "${escapedTableName}" ("${escapeSqlIdentifier(
          keys.primaryKey
        )}")`
      );
    }
  }
  for (const fk of keys.foreignKeys) {
    const refTablePk = keyMap?.get(fk.refTable)?.primaryKey;
    const refIsEnforcedPk = refTablePk === fk.refColumn && (enforcedPkByTable.get(fk.refTable) ?? false);
    if (refIsEnforcedPk) {
      constraints.push(
        `FOREIGN KEY ("${escapeSqlIdentifier(fk.column)}") REFERENCES "${escapeSqlIdentifier(fk.refTable)}" ("${escapeSqlIdentifier(
          fk.refColumn
        )}")`
      );
    }
    if (options?.indexForeignKeys) {
      indexes.push(
        `CREATE INDEX IF NOT EXISTS "${escapeSqlIdentifier(
          `${table.name}__${fk.column}_fk_idx`
        )}" ON "${escapedTableName}" ("${escapeSqlIdentifier(fk.column)}")`
      );
    }
  }
  const tableBody = constraints.length > 0 ? `${columnDefs}, ${constraints.join(", ")}` : columnDefs;
  return { tableBody, indexes };
}
function cdbToSql(cdbData, SQL, options) {
  const decompressedData = decompressCdb(cdbData);
  const reader = new CDBReader(decompressedData);
  const db = new SQL.Database();
  db.run("PRAGMA foreign_keys = OFF");
  const wrapperChunk = reader.readChunk();
  const wrapperChildren = wrapperChunk.children;
  if (!wrapperChildren) {
    throw new Error("Missing wrapper chunk children");
  }
  const tables = wrapperChildren[1 /* DATABASE_TABLES */] ?? [];
  const databaseFlags = wrapperChildren[2 /* DATABASE_FLAGS */];
  db.run(
    options?.preciseTypes ? `CREATE TABLE DB_STRUCTURE (TableName TEXT '274', ID INTEGER, Flags INTEGER)` : `CREATE TABLE DB_STRUCTURE (TableName '274',ID '0')`
  );
  if (databaseFlags !== void 0) {
    db.run(`CREATE TABLE DB_METADATA (DatabaseFlags INTEGER)`);
    db.run(`INSERT INTO DB_METADATA VALUES (?)`, [databaseFlags]);
  }
  const keyMap = options?.normalize ? inferKeys(tables) : null;
  const tablesByName = new Map(tables.map((t) => [t.name, t]));
  const deferredIndexes = [];
  const enforcedPkByTable = /* @__PURE__ */ new Map();
  if (keyMap) {
    for (const [tableName, keys] of keyMap) {
      if (!keys.primaryKey) continue;
      const pkColumn = tablesByName.get(tableName)?.columns.find((col) => col.name === keys.primaryKey);
      enforcedPkByTable.set(
        tableName,
        pkColumn !== void 0 && isUniqueNonNull(pkColumn.data)
      );
    }
  }
  db.run("BEGIN TRANSACTION");
  tables.forEach((table) => {
    if (options?.preciseTypes) {
      db.run(`INSERT INTO DB_STRUCTURE VALUES (?, ?, ?)`, [
        table.name,
        table.tableId,
        table.tableFlags
      ]);
    } else {
      db.run(`INSERT INTO DB_STRUCTURE VALUES (?, ?)`, [
        table.name,
        table.tableId
      ]);
    }
    const escapedTableName = escapeSqlIdentifier(table.name);
    const columnDefs = table.columns.map((col) => {
      const escapedColumnName = escapeSqlIdentifier(col.name);
      let baseType;
      let encodedType;
      switch (col.type) {
        case 1 /* FLOAT */:
          baseType = "REAL";
          encodedType = col.type;
          break;
        case 2 /* STRING */:
        case 11 /* INTEGER_LIST */:
        case 10 /* FLOAT_LIST */:
          baseType = "TEXT";
          encodedType = col.type;
          break;
        case 3 /* BOOLEAN */:
          baseType = options?.preciseTypes ? "NUMERIC" : "INTEGER";
          encodedType = options?.preciseTypes ? col.type : 0 /* INTEGER */;
          break;
        case 4 /* INTEGER_BYTE */:
        case 5 /* INTEGER_SHORT */:
          baseType = "INTEGER";
          encodedType = options?.preciseTypes ? col.type : 0 /* INTEGER */;
          break;
        default:
          baseType = "INTEGER";
          encodedType = 0 /* INTEGER */;
          break;
      }
      const encodedValue = (table.tableId * 256 + col.columnIndex) * 16 + (encodedType & 15);
      return `"${escapedColumnName}" '${baseType} ${encodedValue}'`;
    }).join(", ");
    const { tableBody, indexes } = buildTableConstraints(
      table,
      keyMap?.get(table.name),
      columnDefs,
      escapedTableName,
      options,
      keyMap,
      enforcedPkByTable
    );
    deferredIndexes.push(...indexes);
    db.run(`CREATE TABLE "${escapedTableName}" (${tableBody})`);
    if (table.rowCount > 0) {
      const columnsPerRow = table.columns.length;
      const maxRowsPerBatch = Math.max(1, Math.floor(999 / columnsPerRow));
      const placeholders = table.columns.map(() => "?").join(", ");
      for (let i = 0; i < table.rowCount; i += maxRowsPerBatch) {
        const end = Math.min(i + maxRowsPerBatch, table.rowCount);
        const batchCount = end - i;
        const valueSets = Array(batchCount).fill(`(${placeholders})`).join(", ");
        const params = [];
        for (let rowIdx = i; rowIdx < end; rowIdx++) {
          for (const col of table.columns) {
            params.push(col.data[rowIdx]);
          }
        }
        db.run(`INSERT INTO "${escapedTableName}" VALUES ${valueSets}`, params);
      }
    }
  });
  for (const indexStatement of deferredIndexes) {
    db.run(indexStatement);
  }
  db.run("COMMIT");
  return db;
}

// src/writer.ts
var CDBWriter = class {
  constructor(estimatedSize = 1024 * 1024) {
    this.buffer = new Uint8Array(estimatedSize);
    this.view = new DataView(this.buffer.buffer);
    this.chunkStack = [];
    this.closedChunks = [];
    this.pos = 0;
  }
  ensureCapacity(additionalBytes) {
    if (this.pos + additionalBytes > this.buffer.length) {
      const newSize = Math.max(
        Math.ceil(this.buffer.length * 1.5),
        this.pos + additionalBytes
      );
      const newBuffer = new Uint8Array(newSize);
      newBuffer.set(this.buffer);
      this.buffer = newBuffer;
      this.view = new DataView(this.buffer.buffer);
    }
  }
  write32(value) {
    this.ensureCapacity(4);
    this.view.setUint32(this.pos, value, true);
    this.pos += 4;
  }
  writeBytes(bytes) {
    this.ensureCapacity(bytes.length);
    this.buffer.set(bytes, this.pos);
    this.pos += bytes.length;
  }
  writePadding() {
    const padding = 4 - (this.pos & 3) & 3;
    if (padding > 0) {
      this.ensureCapacity(padding);
      this.pos += padding;
    }
  }
  writeChunkOpen(chunkType, description = null) {
    const chunkStart = this.pos;
    this.write32(2863311530 /* CHUNK_BEGIN */);
    this.write32(0);
    this.write32(chunkType);
    this.write32(0);
    this.write32(description ? 1 : 0);
    if (description) {
      const descBytes = new TextEncoder().encode(description);
      this.write32(descBytes.length + 1);
      this.writeBytes(descBytes);
      this.ensureCapacity(1);
      this.buffer[this.pos] = 0;
      this.pos += 1;
    }
    this.writePadding();
    this.write32(3149642683 /* CHUNK_SEPARATOR */);
    this.chunkStack.push({ type: chunkType, startPos: chunkStart });
  }
  writeChunkClose() {
    const chunk = this.chunkStack.pop();
    if (!chunk) {
      throw new Error("Chunk stack underflow");
    }
    this.writePadding();
    this.write32(3435973836 /* CHUNK_END */);
    chunk.size = this.pos - chunk.startPos;
    this.closedChunks.push(chunk);
  }
  getData() {
    if (this.chunkStack.length > 0) {
      throw new Error(
        `Cannot get CDB data with ${this.chunkStack.length} open chunk${this.chunkStack.length === 1 ? "" : "s"}`
      );
    }
    for (const chunk of this.closedChunks) {
      if (chunk.size !== void 0) {
        this.view.setUint32(chunk.startPos + 4, chunk.size, true);
      }
    }
    return this.buffer.subarray(0, this.pos);
  }
  writeColumnData(dataType, values) {
    const buffer = new ArrayBuffer(4);
    const view = new DataView(buffer);
    this.writeChunkOpen(34 /* COLUMN_VALUES */);
    switch (dataType) {
      case 0 /* INTEGER */:
        for (const value of values) {
          this.write32(value);
        }
        break;
      case 3 /* BOOLEAN */:
        {
          const numBytes = Math.ceil(values.length / 8);
          for (let byteIdx = 0; byteIdx < numBytes; byteIdx++) {
            let byte = 0;
            for (let bitIdx = 0; bitIdx < 8; bitIdx++) {
              const valueIdx = byteIdx * 8 + bitIdx;
              if (valueIdx < values.length && values[valueIdx]) {
                byte |= 1 << bitIdx;
              }
            }
            this.ensureCapacity(1);
            this.buffer[this.pos++] = byte;
          }
        }
        break;
      case 4 /* INTEGER_BYTE */:
        {
          values.forEach((value) => {
            this.ensureCapacity(1);
            const byte = value < 0 ? value + 256 : value;
            this.buffer[this.pos++] = byte & 255;
          });
        }
        break;
      case 5 /* INTEGER_SHORT */:
        {
          values.forEach((value) => {
            this.ensureCapacity(2);
            this.buffer[this.pos++] = value & 255;
            this.buffer[this.pos++] = value >> 8 & 255;
          });
        }
        break;
      case 1 /* FLOAT */:
        {
          values.forEach((value) => {
            view.setFloat32(0, parseFloat(String(value)), true);
            this.write32(view.getUint32(0, true));
          });
        }
        break;
      case 2 /* STRING */:
        this.writeStringData(values);
        break;
      case 11 /* INTEGER_LIST */:
      case 10 /* FLOAT_LIST */:
        this.writeListData(dataType, values);
        break;
    }
    this.writeChunkClose();
  }
  writeStringData(values) {
    const lengths = [];
    const stringData = [];
    const encoder = new TextEncoder();
    values.forEach((value) => {
      const encoded = encoder.encode(value);
      stringData.push(...encoded, 0);
      lengths.push(encoded.length + 1);
    });
    for (const len of lengths) {
      this.write32(len);
    }
    if (stringData.length > 0) {
      this.writeChunkClose();
      this.writeChunkOpen(35 /* COLUMN_BLOB_DATA */);
      this.write32(stringData.length);
      this.writeBytes(new Uint8Array(stringData));
    }
  }
  writeListData(dataType, values) {
    const counts = [];
    const listData = [];
    values.forEach((value) => {
      const parsed = value.slice(1, -1);
      if (!parsed) {
        counts.push(0);
        return;
      }
      const elements = parsed.split(",");
      counts.push(elements.length);
      elements.forEach((elem) => {
        if (dataType === 11 /* INTEGER_LIST */) {
          listData.push(parseInt(elem, 10));
        } else {
          const view = new DataView(new ArrayBuffer(4));
          view.setFloat32(0, parseFloat(elem), true);
          listData.push(view.getUint32(0, true));
        }
      });
    });
    for (const count of counts) {
      this.write32(count);
    }
    if (listData.length > 0) {
      this.writeChunkClose();
      this.writeChunkOpen(35 /* COLUMN_BLOB_DATA */);
      this.write32(listData.length * 4);
      for (const value of listData) {
        this.write32(value);
      }
    }
  }
};

// src/tableMetadata.ts
var TABLE_FLAGS_BY_ID = {
  3: 65,
  4: 50,
  8: 129,
  9: 241,
  10: 209,
  11: 241,
  12: 241,
  14: 129,
  16: 65,
  17: 97,
  18: 49,
  19: 49,
  22: 225,
  23: 161,
  24: 65,
  25: 188,
  26: 113,
  27: 33,
  28: 51,
  29: 33,
  30: 129,
  31: 177,
  35: 129,
  36: 241,
  39: 49,
  40: 241,
  41: 81,
  44: 241,
  45: 113,
  50: 33,
  51: 76,
  52: 97,
  53: 81,
  54: 33,
  55: 81,
  59: 241,
  65: 227,
  80: 33,
  81: 19,
  83: 19,
  84: 19,
  85: 35,
  86: 33,
  87: 241,
  92: 113,
  93: 35,
  94: 82,
  98: 49,
  99: 35,
  100: 65,
  101: 83,
  107: 163,
  108: 241,
  109: 75,
  110: 49,
  111: 81,
  113: 49,
  117: 35,
  118: 129,
  119: 33,
  122: 241,
  124: 65,
  125: 60,
  126: 19,
  127: 19,
  128: 65,
  129: 81,
  131: 241,
  132: 44,
  134: 241,
  135: 51,
  136: 99,
  137: 227,
  142: 195,
  143: 145,
  144: 113,
  145: 33,
  146: 35,
  147: 49,
  149: 19,
  152: 241,
  153: 49,
  155: 33,
  159: 33,
  164: 243,
  166: 241,
  168: 161,
  170: 146,
  172: 241,
  173: 33,
  175: 241,
  177: 130,
  179: 113,
  180: 145,
  181: 97,
  185: 82,
  186: 193,
  192: 65,
  193: 242,
  200: 50,
  202: 98,
  203: 33,
  204: 65,
  205: 49,
  206: 92,
  207: 145,
  208: 65,
  209: 33,
  210: 49,
  211: 34,
  213: 17,
  215: 177,
  217: 81,
  218: 156,
  220: 241,
  221: 145,
  223: 65,
  224: 97,
  225: 65,
  228: 241,
  229: 161,
  230: 49,
  231: 33,
  236: 33,
  240: 241,
  241: 65,
  242: 19,
  244: 178,
  245: 241,
  246: 33,
  249: 115,
  250: 193,
  251: 83,
  252: 193,
  253: 49,
  254: 92,
  255: 83,
  256: 19,
  257: 81,
  258: 97,
  259: 33,
  263: 65,
  264: 129,
  265: 33,
  266: 129,
  268: 33,
  270: 19,
  273: 65,
  274: 129
};

// src/sqlToCdb.ts
function toDataType(value) {
  switch (value) {
    case 0 /* INTEGER */:
    case 1 /* FLOAT */:
    case 2 /* STRING */:
    case 3 /* BOOLEAN */:
    case 4 /* INTEGER_BYTE */:
    case 5 /* INTEGER_SHORT */:
    case 10 /* FLOAT_LIST */:
    case 11 /* INTEGER_LIST */:
      return value;
    default:
      throw new Error(`Unsupported CDB data type: ${value}`);
  }
}
function parseColumnMetadata(colName, colType) {
  const trimmedType = colType.trim();
  const match = trimmedType.match(/^(\S+)\s+(\d+)$/);
  if (!match) {
    throw new Error(
      `Invalid encoded column type for "${colName}": expected "<sqlite type> <encoded number>", got "${colType}"`
    );
  }
  const sqliteType = match[1];
  const encodedValue = Number.parseInt(match[2], 10);
  const dataType = encodedValue & 15;
  const columnIndex = Math.floor(encodedValue / 16) & 255;
  return {
    sqliteType,
    cdbDataType: toDataType(dataType),
    cdbColumnIndex: columnIndex
  };
}
function sqlToCdb(db) {
  const structureInfo = db.exec(`PRAGMA table_info("DB_STRUCTURE")`);
  const hasFlagsColumn = structureInfo.length > 0 && structureInfo[0].values.some((row) => row[1] === "Flags");
  const tablesResult = db.exec(
    hasFlagsColumn ? `SELECT TableName, ID, Flags FROM DB_STRUCTURE ORDER BY ID` : `SELECT TableName, ID FROM DB_STRUCTURE ORDER BY ID`
  );
  if (tablesResult.length === 0) {
    throw new Error("No DB_STRUCTURE table found");
  }
  const tables = tablesResult[0].values.map((row) => ({
    name: row[0],
    id: row[1],
    flags: hasFlagsColumn ? row[2] : null
  }));
  const metadataInfo = db.exec(`PRAGMA table_info("DB_METADATA")`);
  const hasMetadataTable = metadataInfo.length > 0 && metadataInfo[0].values.some((row) => row[1] === "DatabaseFlags");
  const storedDatabaseFlags = hasMetadataTable ? db.exec(`SELECT DatabaseFlags FROM DB_METADATA`)[0]?.values[0]?.[0] : void 0;
  const databaseFlags = storedDatabaseFlags ?? (tables.length > 0 ? Math.max(...tables.map((t) => t.id)) : 274);
  const pageCountResult = db.exec(`PRAGMA page_count`);
  const pageSizeResult = db.exec(`PRAGMA page_size`);
  const pageCount = Number(pageCountResult[0]?.values[0]?.[0]);
  const pageSize = Number(pageSizeResult[0]?.values[0]?.[0]);
  const estimatedSize = pageCount * pageSize;
  const writer = Number.isFinite(estimatedSize) && estimatedSize > 0 ? new CDBWriter(estimatedSize) : new CDBWriter();
  writer.writeChunkOpen(0 /* WRAPPER */, "cyanide database");
  writer.writeChunkOpen(2 /* DATABASE_FLAGS */);
  writer.write32(databaseFlags);
  writer.writeChunkClose();
  writer.writeChunkOpen(1 /* DATABASE_TABLES */);
  writer.write32(3722304989 /* ARRAY_BEGIN */);
  writer.write32(tables.length);
  tables.forEach((tableInfo) => {
    const escapedTableName = escapeSqlIdentifier(tableInfo.name);
    const schemaResult = db.exec(`PRAGMA table_info("${escapedTableName}")`);
    if (schemaResult.length === 0 || schemaResult[0].values.length === 0) {
      throw new Error(
        `No schema information available for table "${tableInfo.name}"`
      );
    }
    const columnInfo = {};
    schemaResult[0].values.forEach((row) => {
      const colName = row[1];
      const colType = row[2];
      columnInfo[colName] = parseColumnMetadata(colName, colType);
    });
    const dataResult = db.exec(`SELECT * FROM "${escapedTableName}"`);
    const rows = dataResult.length > 0 ? dataResult[0].values : [];
    writer.writeChunkOpen(16 /* TABLE */, tableInfo.name);
    writer.writeChunkOpen(21 /* TABLE_ID */);
    writer.write32(tableInfo.id);
    writer.writeChunkClose();
    writer.writeChunkOpen(17 /* ROW_COUNT */);
    writer.write32(rows.length);
    writer.writeChunkClose();
    writer.writeChunkOpen(22 /* TABLE_FLAGS */);
    const tableFlags = tableInfo.flags ?? TABLE_FLAGS_BY_ID[tableInfo.id] ?? 0;
    writer.write32(tableFlags);
    writer.writeChunkClose();
    writer.writeChunkOpen(18 /* COLUMN_DEFINITIONS */);
    writer.write32(3722304989 /* ARRAY_BEGIN */);
    writer.write32(Object.keys(columnInfo).length);
    const columnNames = Object.keys(columnInfo);
    const columnData = columnNames.map(() => []);
    rows.forEach((row) => {
      row.forEach((value, colIdx) => {
        columnData[colIdx].push(value);
      });
    });
    columnNames.forEach((columnName, colIdx) => {
      writer.writeChunkOpen(32 /* COLUMN */, columnName);
      const info = columnInfo[columnName];
      writer.writeChunkOpen(36 /* COLUMN_INDEX */);
      writer.write32(info.cdbColumnIndex);
      writer.writeChunkClose();
      writer.writeChunkOpen(33 /* COLUMN_DATA_TYPE */, columnName);
      writer.write32(info.cdbDataType);
      writer.writeChunkClose();
      const values = columnData[colIdx];
      if (values.length !== rows.length) {
        throw new Error(
          `Missing value(s) in table "${tableInfo.name}", column "${columnName}": expected ${rows.length} rows, got ${values.length}. Ensure every row has a concrete value for every column (the CDB format cannot represent NULL).`
        );
      }
      for (let rowIdx = 0; rowIdx < values.length; rowIdx++) {
        const value = values[rowIdx];
        if (value === null || value === void 0) {
          throw new Error(
            `NULL or missing value in table "${tableInfo.name}", column "${columnName}", row ${rowIdx + 1}. The CDB format cannot represent NULL; supply a concrete value for every cell.`
          );
        }
      }
      writer.writeColumnData(info.cdbDataType, values);
      writer.writeChunkClose();
    });
    writer.write32(4008636142 /* ARRAY_END */);
    writer.writeChunkClose();
    writer.writeChunkClose();
  });
  writer.write32(4008636142 /* ARRAY_END */);
  writer.writeChunkClose();
  writer.writeChunkClose();
  return compressCdb(writer.getData());
}

// src/cli.ts
var VERSION = true ? "0.4.1" : "unknown";
var HELP_TEXT = `cdb-converter \u2014 convert Pro Cycling Manager CDB files to/from SQLite

Usage:
  cdb-converter <input> [output]
  cdb-converter --help
  cdb-converter --version

Conversion direction is auto-detected from the input extension:
  *.cdb              ->  SQLite  (default output: <input>.sqlite)
  *.sqlite | *.db    ->  CDB     (default output: <input>.cdb)

Arguments:
  input              Path to the .cdb, .sqlite or .db file to convert
  output             Optional output path (defaults based on the input)

Options:
  -h, --help         Show this help message
  -v, --version      Show the cdb-converter version
  -n, --normalize    (cdb -> sqlite only) reconstruct PRIMARY KEY / FOREIGN KEY
                     constraints from PCM naming conventions for a normalized
                     schema. Ignored when converting sqlite -> cdb.
      --index-fk     (implies --normalize) also index every foreign-key column
                     for faster JOINs. Roughly doubles the output size.
      --precise-types
                     (cdb -> sqlite only) preserve the exact CDB type (BOOLEAN,
                     INTEGER_BYTE, INTEGER_SHORT) in the SQLite schema instead
                     of collapsing them to plain INTEGER. Off by default so the
                     output stays importable by the official PCM SQLiteExporter
                     tool, which does not recognize those types.

Examples:
  cdb-converter save.cdb
  cdb-converter save.cdb save.sqlite
  cdb-converter save.cdb save.sqlite --normalize
  cdb-converter save.cdb save.sqlite --normalize --index-fk
  cdb-converter save.cdb save.sqlite --precise-types
  cdb-converter -- --data.cdb  (use -- to treat a leading-dash path as a positional argument)
  cdb-converter save.sqlite save.cdb`;
function parseArgs(argv) {
  const positionals = [];
  let normalize2 = false;
  let indexForeignKeys = false;
  let preciseTypes = false;
  let optionsEnded = false;
  for (const arg of argv) {
    if (optionsEnded) {
      positionals.push(arg);
      continue;
    }
    if (arg === "--") {
      optionsEnded = true;
      continue;
    }
    if (arg === "-h" || arg === "--help") {
      return { command: "help" };
    }
    if (arg === "-v" || arg === "--version") {
      return { command: "version" };
    }
    if (arg === "-n" || arg === "--normalize") {
      normalize2 = true;
      continue;
    }
    if (arg === "--index-fk") {
      normalize2 = true;
      indexForeignKeys = true;
      continue;
    }
    if (arg === "--precise-types") {
      preciseTypes = true;
      continue;
    }
    if (arg.startsWith("-") && arg !== "-") {
      throw new Error(
        `Unknown option "${arg}". Run "cdb-converter --help" for usage.`
      );
    }
    positionals.push(arg);
  }
  return {
    command: "convert",
    input: positionals[0],
    output: positionals[1],
    normalize: normalize2,
    indexForeignKeys,
    preciseTypes
  };
}
function detectDirection(inputPath) {
  const extension = extname(inputPath).toLowerCase();
  switch (extension) {
    case ".cdb":
      return "cdb-to-sql";
    case ".sqlite":
    case ".db":
      return "sql-to-cdb";
    default:
      throw new Error(
        `Cannot detect conversion direction from "${inputPath}". Expected a .cdb, .sqlite or .db file.`
      );
  }
}
function getDefaultOutputPath(inputPath, direction) {
  const extension = extname(inputPath);
  const targetExtension = direction === "cdb-to-sql" ? ".sqlite" : ".cdb";
  if (extension.length > 0) {
    return `${inputPath.slice(0, -extension.length)}${targetExtension}`;
  }
  return `${inputPath}${targetExtension}`;
}
async function convert(input, output, normalize2, indexForeignKeys, preciseTypes) {
  const direction = detectDirection(input);
  const inputPath = resolve(process.cwd(), input);
  const outputPath = resolve(
    process.cwd(),
    output ?? getDefaultOutputPath(input, direction)
  );
  const SQL = await initSqlJs();
  const inputBytes = await readFile(inputPath);
  let outputBytes;
  let summary = [];
  if (direction === "cdb-to-sql") {
    const db = cdbToSql(inputBytes, SQL, {
      normalize: normalize2,
      indexForeignKeys,
      preciseTypes
    });
    try {
      const tables = db.exec(
        "SELECT TableName, ID FROM DB_STRUCTURE ORDER BY ID"
      );
      const rows = tables[0]?.values ?? [];
      summary = [
        `Tables : ${rows.length}`,
        `Normalized : ${normalize2 ? "yes" : "no"}`
      ];
      if (normalize2 && indexForeignKeys) {
        summary.push("FK indexes : yes");
      }
      if (preciseTypes) {
        summary.push("Precise types : yes");
      }
      outputBytes = db.export();
    } finally {
      db.close();
    }
  } else {
    const db = new SQL.Database(inputBytes);
    try {
      outputBytes = new Uint8Array(sqlToCdb(db));
    } finally {
      db.close();
    }
  }
  await mkdir(dirname(outputPath), { recursive: true });
  await writeFile(outputPath, outputBytes);
  console.log(`Input  : ${inputPath}`);
  console.log(`Output : ${outputPath}`);
  for (const line of summary) {
    console.log(line);
  }
}
async function run(argv) {
  let parsed;
  try {
    parsed = parseArgs(argv);
  } catch (error) {
    console.error(`Error: ${error instanceof Error ? error.message : error}`);
    process.exitCode = 1;
    return;
  }
  if (parsed.command === "help") {
    console.log(HELP_TEXT);
    return;
  }
  if (parsed.command === "version") {
    console.log(VERSION);
    return;
  }
  if (!parsed.input) {
    console.error("Error: missing input file.\n");
    console.error(HELP_TEXT);
    process.exitCode = 1;
    return;
  }
  try {
    await convert(
      parsed.input,
      parsed.output,
      parsed.normalize ?? false,
      parsed.indexForeignKeys ?? false,
      parsed.preciseTypes ?? false
    );
  } catch (error) {
    console.error(`Error: ${error instanceof Error ? error.message : error}`);
    process.exitCode = 1;
  }
}
function safeRealpath(path) {
  try {
    return realpathSync(path);
  } catch {
    return path;
  }
}
function samePath(a, b) {
  return process.platform === "win32" ? a.toLowerCase() === b.toLowerCase() : a === b;
}
function isDirectRun(argv1, moduleUrl) {
  if (typeof argv1 !== "string" || argv1.length === 0) {
    return false;
  }
  return samePath(
    safeRealpath(resolve(argv1)),
    safeRealpath(fileURLToPath(moduleUrl))
  );
}
if (isDirectRun(process.argv[1], import.meta.url)) {
  void run(process.argv.slice(2));
}
export {
  detectDirection,
  getDefaultOutputPath,
  isDirectRun,
  parseArgs,
  run
};
