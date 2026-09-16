#!/usr/bin/env node

// Standalone Result/Audit workbook exporter for the local OCR demo.

import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const SUPPORTED_TYPES = new Set([
  "text",
  "integer",
  "number",
  "boolean",
  "datetime",
  "json",
]);

const HEADER_FILL = "#1F4E78";
const HEADER_TEXT = "#FFFFFF";
const BODY_TEXT = "#203040";
const LIGHT_BORDER = "#D9E2F3";
const MAX_EXCEL_TEXT = 32760;

function usage() {
  return [
    "Usage:",
    "  node export_workbook.mjs --input <payload.json> --output <result.xlsx>",
    "       [--verify-dir <render-directory>]",
    "  node export_workbook.mjs --self-test [--self-test-dir <directory>]",
    "",
    "Payload:",
    "  { schema_version, run, result: { columns, rows }, audit: { columns, rows } }",
    "  columns: [{ key, header, type, format?, width? }]",
  ].join("\n");
}

function parseArgs(argv) {
  const args = {
    input: null,
    output: null,
    selfTest: false,
    selfTestDir: null,
    verifyDir: null,
    help: false,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (token === "--self-test") {
      args.selfTest = true;
    } else if (token === "--help" || token === "-h") {
      args.help = true;
    } else if (token === "--input" || token === "--output" || token === "--self-test-dir" || token === "--verify-dir") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new Error(`${token} requires a value`);
      }
      index += 1;
      if (token === "--input") args.input = value;
      if (token === "--output") args.output = value;
      if (token === "--self-test-dir") args.selfTestDir = value;
      if (token === "--verify-dir") args.verifyDir = value;
    } else {
      throw new Error(`Unknown argument: ${token}`);
    }
  }

  return args;
}

function assertPlainObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
}

function normalizeColumn(column, index, sectionName) {
  assertPlainObject(column, `${sectionName}.columns[${index}]`);
  const key = String(column.key ?? "").trim();
  const header = String(column.header ?? "").trim();
  const type = String(column.type ?? "text").trim().toLowerCase();

  if (!key) throw new Error(`${sectionName}.columns[${index}].key is required`);
  if (!header) throw new Error(`${sectionName}.columns[${index}].header is required`);
  if (!SUPPORTED_TYPES.has(type)) {
    throw new Error(
      `${sectionName}.columns[${index}].type must be one of ${[...SUPPORTED_TYPES].join(", ")}`,
    );
  }

  let width = null;
  if (column.width !== undefined && column.width !== null) {
    width = Number(column.width);
    if (!Number.isFinite(width) || width <= 0) {
      throw new Error(`${sectionName}.columns[${index}].width must be a positive number`);
    }
    width = Math.min(80, Math.max(6, width));
  }

  const format = column.format === undefined || column.format === null
    ? null
    : String(column.format).trim() || null;

  return { key, header, type, format, width };
}

function normalizeSection(section, sectionName) {
  assertPlainObject(section, sectionName);
  if (!Array.isArray(section.columns) || section.columns.length === 0) {
    throw new Error(`${sectionName}.columns must be a non-empty array`);
  }
  if (!Array.isArray(section.rows)) {
    throw new Error(`${sectionName}.rows must be an array`);
  }

  const columns = section.columns.map((column, index) =>
    normalizeColumn(column, index, sectionName));
  const duplicateKeys = columns
    .map((column) => column.key)
    .filter((key, index, keys) => keys.indexOf(key) !== index);
  if (duplicateKeys.length > 0) {
    throw new Error(`${sectionName}.columns contains duplicate key: ${duplicateKeys[0]}`);
  }

  const rows = section.rows.map((row, index) => {
    assertPlainObject(row, `${sectionName}.rows[${index}]`);
    return row;
  });

  return { columns, rows };
}

function normalizePayload(payload) {
  assertPlainObject(payload, "payload");
  if (payload.schema_version === undefined || payload.schema_version === null) {
    throw new Error("payload.schema_version is required");
  }
  assertPlainObject(payload.run, "payload.run");

  const result = normalizeSection(payload.result, "payload.result");
  const audit = normalizeSection(payload.audit, "payload.audit");
  if (result.columns[0].key !== "platform") {
    throw new Error("payload.result.columns[0].key must be platform");
  }

  return {
    schemaVersion: String(payload.schema_version),
    run: payload.run,
    result,
    audit,
  };
}

function sanitizeText(value) {
  let text = String(value)
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, "")
    .slice(0, MAX_EXCEL_TEXT);

  // Treat all external text as literal content, never as an Excel formula.
  if (/^[\s]*[=+\-@]/.test(text)) text = `'${text}`;
  return text;
}

function parseFiniteNumber(value, label) {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error(`${label} must be finite`);
    return value;
  }
  if (typeof value === "string") {
    const normalized = value.trim().replace(/,/g, "");
    if (!normalized) return null;
    const parsed = Number(normalized);
    if (!Number.isFinite(parsed)) throw new Error(`${label} is not a valid number`);
    return parsed;
  }
  throw new Error(`${label} must be a number or numeric string`);
}

function parseBoolean(value, label) {
  if (typeof value === "boolean") return value;
  if (value === 1 || value === "1") return true;
  if (value === 0 || value === "0") return false;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "yes", "y", "是"].includes(normalized)) return true;
    if (["false", "no", "n", "否"].includes(normalized)) return false;
  }
  throw new Error(`${label} is not a valid boolean`);
}

function parseDateTime(value, label) {
  if (value instanceof Date && Number.isFinite(value.getTime())) return value;
  if (typeof value !== "string" && typeof value !== "number") {
    throw new Error(`${label} must be an ISO date/time string or timestamp`);
  }
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error(`${label} is not a valid date/time`);
  return date;
}

function convertCell(value, column, label) {
  if (value === null || value === undefined || value === "") return null;

  if (column.type === "text") return sanitizeText(value);
  if (column.type === "integer") {
    const number = parseFiniteNumber(value, label);
    if (number === null) return null;
    if (!Number.isSafeInteger(number)) {
      throw new Error(`${label} must be a safe integer; identifiers must use type text`);
    }
    return number;
  }
  if (column.type === "number") return parseFiniteNumber(value, label);
  if (column.type === "boolean") return parseBoolean(value, label);
  if (column.type === "datetime") return parseDateTime(value, label);
  if (column.type === "json") {
    return sanitizeText(typeof value === "string" ? value : JSON.stringify(value));
  }
  throw new Error(`Unsupported column type: ${column.type}`);
}

function sectionMatrix(section, sectionName) {
  const header = section.columns.map((column) => column.header);
  const rows = section.rows.map((row, rowIndex) =>
    section.columns.map((column) =>
      convertCell(row[column.key], column, `${sectionName}.rows[${rowIndex}].${column.key}`)));
  return [header, ...rows];
}

function columnLetter(oneBasedColumn) {
  let value = oneBasedColumn;
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

function visualTextWidth(value) {
  if (value === null || value === undefined) return 0;
  const text = value instanceof Date ? "0000-00-00 00:00:00" : String(value);
  return [...text].reduce((width, character) =>
    width + (character.codePointAt(0) > 0xFF ? 2 : 1), 0);
}

function isLongTextColumn(column) {
  return column.type === "json"
    || /message|error|notes?|artifact|evidence|详情|说明|错误|证据/i.test(`${column.key} ${column.header}`);
}

function defaultWidth(column, values) {
  if (column.width !== null) return column.width;
  const measured = Math.max(
    visualTextWidth(column.header),
    ...values.slice(0, 200).map((value) => visualTextWidth(value)),
  );
  const cap = isLongTextColumn(column) ? 52 : column.type === "datetime" ? 21 : 34;
  return Math.min(cap, Math.max(10, measured + 2));
}

function defaultNumberFormat(column) {
  if (column.format) return column.format;
  if (column.type === "text" || column.type === "json") return "@";
  if (column.type === "integer") return "#,##0";
  if (column.type === "datetime") return "yyyy-mm-dd hh:mm:ss";
  if (column.type === "number") {
    if (/duration_ms|count|qty|quantity|数量|次数/i.test(`${column.key} ${column.header}`)) {
      return "#,##0";
    }
    return "#,##0.00";
  }
  return null;
}

function applyColumnFormatting(sheet, section, matrix, lastRow) {
  section.columns.forEach((column, index) => {
    const letter = columnLetter(index + 1);
    const wholeColumn = sheet.getRange(`${letter}1:${letter}${lastRow}`);
    const body = lastRow > 1 ? sheet.getRange(`${letter}2:${letter}${lastRow}`) : null;
    wholeColumn.format.columnWidth = defaultWidth(column, matrix.slice(1).map((row) => row[index]));

    if (!body) return;
    if (column.type === "integer" || column.type === "number") {
      body.format.horizontalAlignment = "right";
    } else if (column.type === "boolean") {
      body.format.horizontalAlignment = "center";
    } else {
      body.format.horizontalAlignment = "left";
    }

    const numberFormat = defaultNumberFormat(column);
    if (numberFormat) body.format.numberFormat = numberFormat;
    if (isLongTextColumn(column)) {
      body.format.wrapText = true;
      body.format.autofitRows();
    }
  });
}

function addSectionSheet(workbook, sheetName, tableName, section) {
  const sheet = workbook.worksheets.add(sheetName);
  const matrix = sectionMatrix(section, sheetName);
  const rowCount = matrix.length;
  const columnCount = section.columns.length;
  const lastColumn = columnLetter(columnCount);
  const lastRow = Math.max(1, rowCount);
  const usedRange = sheet.getRange(`A1:${lastColumn}${lastRow}`);

  // Set text formats before the block write so Excel never coerces long IDs
  // or leading-zero identifiers into numbers/scientific notation.
  section.columns.forEach((column, index) => {
    if (column.type === "text" || column.type === "json") {
      const letter = columnLetter(index + 1);
      sheet.getRange(`${letter}1:${letter}${lastRow}`).format.numberFormat = "@";
    }
  });
  usedRange.values = matrix;
  sheet.showGridLines = false;

  const header = sheet.getRange(`A1:${lastColumn}1`);
  header.format = {
    fill: HEADER_FILL,
    font: { bold: true, color: HEADER_TEXT },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { bottom: { style: "medium", color: "#163A5C" } },
  };
  header.format.rowHeight = 30;

  if (lastRow > 1) {
    const body = sheet.getRange(`A2:${lastColumn}${lastRow}`);
    body.format.font = { color: BODY_TEXT };
    body.format.verticalAlignment = "center";
    body.format.borders = {
      insideHorizontal: { style: "thin", color: LIGHT_BORDER },
    };
  }

  const table = sheet.tables.add(`A1:${lastColumn}${lastRow}`, true, tableName);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  table.showBandedColumns = false;

  applyColumnFormatting(sheet, section, matrix, lastRow);
  sheet.freezePanes.freezeRows(1);
  return { sheet, matrix, range: `A1:${lastColumn}${lastRow}` };
}

function createWorkbook(payload) {
  const normalized = normalizePayload(payload);
  const workbook = Workbook.create();
  const result = addSectionSheet(workbook, "Result", "ResultTable", normalized.result);
  const audit = addSectionSheet(workbook, "Audit", "AuditTable", normalized.audit);

  return {
    workbook,
    normalized,
    sheets: { Result: result, Audit: audit },
  };
}

async function exportWorkbook(workbook, outputPath) {
  const absoluteOutput = path.resolve(outputPath);
  if (path.extname(absoluteOutput).toLowerCase() !== ".xlsx") {
    throw new Error("--output must end with .xlsx");
  }
  await fs.mkdir(path.dirname(absoluteOutput), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(absoluteOutput);
  return absoluteOutput;
}

async function renderSheets(workbook, outputDirectory, sheets = null) {
  await fs.mkdir(outputDirectory, { recursive: true });
  const renders = [];
  const specs = [
    { sheetName: "Result", fileName: "Result.png", range: null },
    { sheetName: "Audit", fileName: "Audit.png", range: null },
  ];
  if (sheets?.Result?.matrix?.[0]?.length > 18) {
    const lastColumn = columnLetter(sheets.Result.matrix[0].length);
    const lastRow = Math.min(8, sheets.Result.matrix.length);
    specs.push(
      { sheetName: "Result", fileName: "Result-left.png", range: `A1:R${lastRow}` },
      { sheetName: "Result", fileName: "Result-right.png", range: `S1:${lastColumn}${lastRow}` },
    );
  }
  for (const spec of specs) {
    const preview = await workbook.render({
      sheetName: spec.sheetName,
      ...(spec.range ? { range: spec.range } : { autoCrop: "all" }),
      scale: 1,
      format: "png",
    });
    const previewPath = path.join(outputDirectory, spec.fileName);
    const bytes = new Uint8Array(await preview.arrayBuffer());
    if (bytes.byteLength === 0) throw new Error(`${spec.fileName} render is empty`);
    await fs.writeFile(previewPath, bytes);
    renders.push(previewPath);
  }
  return renders;
}

async function inspectWorkbook(workbook, sheets) {
  const checks = {};
  for (const [sheetName, details] of Object.entries(sheets)) {
    const inspection = await workbook.inspect({
      kind: "table",
      range: `${sheetName}!${details.range}`,
      include: "values,formulas",
      tableMaxRows: 8,
      tableMaxCols: 12,
      maxChars: 6000,
    });
    checks[sheetName] = inspection.ndjson;
  }

  const formulaErrors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 50 },
    summary: "formula error scan",
  });
  if (/\"matchCount\"\s*:\s*[1-9]/.test(formulaErrors.ndjson)) {
    throw new Error("Formula error scan found one or more errors");
  }
  return checks;
}

function selfTestPayload() {
  return {
    schema_version: 1,
    run: {
      run_id: "self-test-001",
      generated_at: "2026-08-27T08:00:00Z",
      template_version: "template-v2",
    },
    result: {
      columns: [
        { key: "platform", header: "平台", type: "text", width: 12 },
        { key: "product_id", header: "商品 ID", type: "text", width: 16 },
        { key: "product_name", header: "商品名称", type: "text", width: 30 },
        { key: "brand", header: "品牌", type: "text" },
        { key: "category", header: "类目", type: "text", width: 22 },
        { key: "lowest_promo_price", header: "价格", type: "number", format: "#,##0.00" },
        { key: "attribute_count", header: "属性数", type: "integer" },
        { key: "is_complete", header: "是否完整", type: "boolean" },
        { key: "snapshot_at", header: "数据时间", type: "datetime" },
        { key: "validation_status", header: "校验状态", type: "text" },
        { key: "notes", header: "备注", type: "text", width: 32 },
        { key: "evidence", header: "来源证据", type: "json", width: 42 },
      ],
      rows: [
        {
          platform: "jd",
          product_id: "497394",
          product_name: "Swisse 示例商品",
          brand: "Swisse",
          category: "营养保健",
          lowest_promo_price: "352.75",
          attribute_count: 18,
          is_complete: true,
          snapshot_at: "2026-08-27T07:58:12Z",
          validation_status: "通过",
          notes: "价格来自月度表 latest valid row",
          evidence: { source_table: "monthly", source_column: "lowest_promo_price" },
        },
        {
          platform: "tmall",
          product_id: "00010222877714516",
          product_name: "演示商品 B",
          brand: "Demo",
          category: "个护",
          lowest_promo_price: 88,
          attribute_count: 0,
          is_complete: false,
          snapshot_at: "2026-08-27T08:01:33Z",
          validation_status: "待复核",
          notes: "=HYPERLINK(\"https://invalid.example\",\"不得执行\")",
          evidence: { fallback: true },
        },
      ],
    },
    audit: {
      columns: [
        { key: "run_id", header: "运行 ID", type: "text", width: 18 },
        { key: "platform", header: "平台", type: "text" },
        { key: "product_id", header: "商品 ID", type: "text", width: 18 },
        { key: "stage", header: "节点", type: "text" },
        { key: "status", header: "状态", type: "text" },
        { key: "severity", header: "级别", type: "text" },
        { key: "error_code", header: "错误码", type: "text" },
        { key: "message", header: "说明", type: "text", width: 44 },
        { key: "attempts", header: "尝试次数", type: "integer" },
        { key: "cached", header: "命中缓存", type: "boolean" },
        { key: "duration_ms", header: "耗时(ms)", type: "integer" },
        { key: "source_table", header: "来源表", type: "text", width: 28 },
        { key: "source_column", header: "来源字段", type: "text", width: 24 },
        { key: "source_time", header: "来源时间", type: "datetime" },
        { key: "artifact_ref", header: "产物引用", type: "text", width: 40 },
        { key: "created_at", header: "记录时间", type: "datetime" },
      ],
      rows: [
        {
          run_id: "self-test-001",
          platform: "jd",
          product_id: "497394",
          stage: "redshift_enrichment",
          status: "SUCCESS",
          severity: "INFO",
          error_code: null,
          message: "platform_goods_key=22129910；价格来源已记录。",
          attempts: 1,
          cached: false,
          duration_ms: 842,
          source_table: "mv_com_goods_statistics_monthly_v2_internal_ssv4",
          source_column: "lowest_promo_price",
          source_time: "2026-07-01T00:00:00Z",
          artifact_ref: "products/497394.json",
          created_at: "2026-08-27T08:00:04Z",
        },
        {
          run_id: "self-test-001",
          platform: "tmall",
          product_id: "00010222877714516",
          stage: "validation",
          status: "REVIEW",
          severity: "WARNING",
          error_code: "NO_ATTRIBUTE",
          message: "未找到有效属性，商品进入待复核队列。",
          attempts: 1,
          cached: true,
          duration_ms: 31,
          source_table: null,
          source_column: null,
          source_time: null,
          artifact_ref: "products/00010222877714516.json",
          created_at: "2026-08-27T08:00:05Z",
        },
      ],
    },
  };
}

async function runSelfTest(requestedDirectory) {
  const outputDirectory = requestedDirectory
    ? path.resolve(requestedDirectory)
    : await fs.mkdtemp(path.join(os.tmpdir(), "ocr-demo-workbook-"));
  await fs.mkdir(outputDirectory, { recursive: true });

  const payload = selfTestPayload();
  const built = createWorkbook(payload);

  const emptyPayload = selfTestPayload();
  emptyPayload.result.rows = [];
  emptyPayload.audit.rows = [];
  const emptyBuilt = createWorkbook(emptyPayload);
  if (emptyBuilt.sheets.Result.matrix.length !== 1 || emptyBuilt.sheets.Audit.matrix.length !== 1) {
    throw new Error("Self-test failed: zero-row sheets must retain their headers");
  }
  const emptyExport = await SpreadsheetFile.exportXlsx(emptyBuilt.workbook);
  const emptyCheckPath = path.join(outputDirectory, "zero-row-check.xlsx");
  await emptyExport.save(emptyCheckPath);
  const emptyStat = await fs.stat(emptyCheckPath);
  await fs.unlink(emptyCheckPath);
  if (emptyStat.size === 0) {
    throw new Error("Self-test failed: zero-row workbook export is empty");
  }

  const resultPrice = built.sheets.Result.matrix[1][5];
  const resultBoolean = built.sheets.Result.matrix[1][7];
  const resultDate = built.sheets.Result.matrix[1][8];
  const safeFormulaText = built.sheets.Result.matrix[2][10];
  if (typeof resultPrice !== "number" || resultPrice !== 352.75) {
    throw new Error("Self-test failed: number conversion");
  }
  if (resultBoolean !== true) throw new Error("Self-test failed: boolean conversion");
  if (!(resultDate instanceof Date)) throw new Error("Self-test failed: datetime conversion");
  if (!String(safeFormulaText).startsWith("'=")) {
    throw new Error("Self-test failed: formula injection protection");
  }

  const inspections = await inspectWorkbook(built.workbook, built.sheets);
  const renders = await renderSheets(built.workbook, outputDirectory, built.sheets);
  const workbookPath = await exportWorkbook(
    built.workbook,
    path.join(outputDirectory, "result.xlsx"),
  );

  const stats = await Promise.all([workbookPath, ...renders].map((filePath) => fs.stat(filePath)));
  if (stats.some((stat) => stat.size === 0)) {
    throw new Error("Self-test failed: one or more output artifacts are empty");
  }
  if (!inspections.Result || !inspections.Audit) {
    throw new Error("Self-test failed: workbook inspection returned no data");
  }

  return {
    ok: true,
    output_directory: outputDirectory,
    workbook: workbookPath,
    renders,
    result_rows: built.normalized.result.rows.length,
    audit_rows: built.normalized.audit.rows.length,
  };
}

async function readPayload(inputPath) {
  const absoluteInput = path.resolve(inputPath);
  const raw = (await fs.readFile(absoluteInput, "utf8")).replace(/^\uFEFF/, "");
  try {
    return { payload: JSON.parse(raw), absoluteInput };
  } catch (error) {
    throw new Error(`Invalid JSON in ${absoluteInput}: ${error.message}`);
  }
}

function redactError(message) {
  return String(message)
    .replace(/(authorization|token|api[_-]?key|password)\s*[:=]\s*[^\s,;]+/gi, "$1=[REDACTED]")
    .replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]");
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }

  if (args.selfTest) {
    const result = await runSelfTest(args.selfTestDir);
    process.stdout.write(`${JSON.stringify(result)}\n`);
    return;
  }

  if (!args.input) throw new Error("--input is required\n\n" + usage());
  const { payload, absoluteInput } = await readPayload(args.input);
  const outputPath = args.output
    ? path.resolve(args.output)
    : path.join(path.dirname(absoluteInput), "result.xlsx");
  const built = createWorkbook(payload);
  await inspectWorkbook(built.workbook, built.sheets);
  const renders = args.verifyDir
    ? await renderSheets(built.workbook, path.resolve(args.verifyDir), built.sheets)
    : [];
  const exportedPath = await exportWorkbook(built.workbook, outputPath);

  process.stdout.write(`${JSON.stringify({
    ok: true,
    output: exportedPath,
    schema_version: built.normalized.schemaVersion,
    result_rows: built.normalized.result.rows.length,
    audit_rows: built.normalized.audit.rows.length,
    renders,
  })}\n`);
}

main().catch((error) => {
  process.stderr.write(`Workbook export failed: ${redactError(error?.message ?? error)}\n`);
  process.exitCode = 1;
});
