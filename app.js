const state = {
  mode: "shared",
  sharedSamples: [],
  sharedControl: createDatasetState(),
  sharedAnalyses: [],
  activeSampleIndex: 0,
  pairedSets: [],
  pairedAnalyses: [],
  comparisonCustomGroups: [],
  chartTitle: "",
};

const els = {
  analysisMode: document.querySelector("#analysisMode"),
  sharedCountWrap: document.querySelector("#sharedCountWrap"),
  pairCountWrap: document.querySelector("#pairCountWrap"),
  comparisonChartWrap: document.querySelector("#comparisonChartWrap"),
  sampleCount: document.querySelector("#sampleCount"),
  pairCount: document.querySelector("#pairCount"),
  comparisonChartType: document.querySelector("#comparisonChartType"),
  loadExampleButton: document.querySelector("#loadExampleButton"),
  sampleInputs: document.querySelector("#sampleInputs"),
  pairInputs: document.querySelector("#pairInputs"),
  sharedWorkflow: document.querySelector("#sharedWorkflow"),
  comparisonWorkflow: document.querySelector("#comparisonWorkflow"),
  sharedResults: document.querySelector("#sharedResults"),
  comparisonResults: document.querySelector("#comparisonResults"),
  controlFile: document.querySelector("#controlFile"),
  controlFileName: document.querySelector("#controlFileName"),
  controlProtein: document.querySelector("#controlProtein"),
  controlSheet: document.querySelector("#controlSheet"),
  controlLaneColumn: document.querySelector("#controlLaneColumn"),
  controlSignalColumn: document.querySelector("#controlSignalColumn"),
  normalizationLane: document.querySelector("#normalizationLane"),
  groupMode: document.querySelector("#groupMode"),
  groupSize: document.querySelector("#groupSize"),
  groupToggleWrap: document.querySelector("#groupToggleWrap"),
  enableGroupedGraphs: document.querySelector("#enableGroupedGraphs"),
  analyzeButton: document.querySelector("#analyzeButton"),
  sampleTabsPanel: document.querySelector("#sampleTabsPanel"),
  sampleTabs: document.querySelector("#sampleTabs"),
  chartTitle: document.querySelector("#chartTitle"),
  downloadChartButton: document.querySelector("#downloadChartButton"),
  downloadCsvButton: document.querySelector("#downloadCsvButton"),
  foldChart: document.querySelector("#foldChart"),
  resultTableBody: document.querySelector("#resultTableBody"),
  labelPanel: document.querySelector("#labelPanel"),
  labelEditor: document.querySelector("#labelEditor"),
  groupPanel: document.querySelector("#groupPanel"),
  customGroupingPanel: document.querySelector("#customGroupingPanel"),
  customGroups: document.querySelector("#customGroups"),
  addCustomGroupButton: document.querySelector("#addCustomGroupButton"),
  groupSummary: document.querySelector("#groupSummary"),
  blockGraphs: document.querySelector("#blockGraphs"),
  comparisonLabelPanel: document.querySelector("#comparisonLabelPanel"),
  comparisonLabelEditor: document.querySelector("#comparisonLabelEditor"),
  comparisonChartPanel: document.querySelector("#comparisonChartPanel"),
  comparisonChartTitle: document.querySelector("#comparisonChartTitle"),
  comparisonChart: document.querySelector("#comparisonChart"),
  downloadComparisonChartButton: document.querySelector("#downloadComparisonChartButton"),
  comparisonGroupPanel: document.querySelector("#comparisonGroupPanel"),
  comparisonCustomGroupingPanel: document.querySelector("#comparisonCustomGroupingPanel"),
  comparisonCustomGroups: document.querySelector("#comparisonCustomGroups"),
  addComparisonCustomGroupButton: document.querySelector("#addComparisonCustomGroupButton"),
  comparisonGroupSummary: document.querySelector("#comparisonGroupSummary"),
  comparisonGroupedCharts: document.querySelector("#comparisonGroupedCharts"),
};

let sharedSampleControls = [];
let pairControls = [];

els.analysisMode.addEventListener("change", switchMode);
els.sampleCount.addEventListener("input", () => renderSharedSampleInputs(clampInteger(Number(els.sampleCount.value), 1, 12, 1)));
els.pairCount.addEventListener("input", () => renderPairInputs(clampInteger(Number(els.pairCount.value), 2, 12, 2)));
els.comparisonChartType.addEventListener("change", renderComparisonChart);
els.loadExampleButton.addEventListener("click", loadExampleData);
els.controlFile.addEventListener("change", (event) => loadSharedControlFile(event));
els.controlSheet.addEventListener("change", () => selectDatasetSheet(state.sharedControl, sharedControlControls(), els.controlSheet.value));
els.controlLaneColumn.addEventListener("change", refreshNormalizationLanes);
els.analyzeButton.addEventListener("click", runAnalysis);
els.downloadChartButton.addEventListener("click", () => downloadCanvasJpeg(els.foldChart, currentSharedChartFilename()));
els.downloadCsvButton.addEventListener("click", downloadSharedCsv);
els.downloadComparisonChartButton.addEventListener("click", () => downloadCanvasJpeg(els.comparisonChart, "common-lane-comparison.jpg"));
els.enableGroupedGraphs.addEventListener("change", renderCurrentGrouping);
els.groupMode.addEventListener("change", renderCurrentGrouping);
els.groupSize.addEventListener("input", renderCurrentGrouping);
els.labelEditor.addEventListener("input", updateSharedLaneLabel);
els.sampleTabs.addEventListener("click", selectSampleTab);
els.customGroups.addEventListener("input", updateCustomGroups);
els.customGroups.addEventListener("change", updateCustomGroups);
els.addCustomGroupButton.addEventListener("click", addCustomGroup);
els.comparisonLabelEditor.addEventListener("input", updateComparisonLaneLabel);
els.comparisonCustomGroups.addEventListener("input", updateComparisonCustomGroups);
els.comparisonCustomGroups.addEventListener("change", updateComparisonCustomGroups);
els.addComparisonCustomGroupButton.addEventListener("click", addComparisonCustomGroup);
els.blockGraphs.addEventListener("click", downloadGeneratedChart);
els.comparisonGroupedCharts.addEventListener("click", downloadGeneratedChart);

renderSharedSampleInputs(1);
renderPairInputs(2);
drawEmptyChart(els.foldChart);

function createDatasetState() {
  return {
    workbook: null,
    sheetName: "",
    rows: [],
    headers: [],
  };
}

function createAnalysisState(name, title, results) {
  return {
    name,
    title,
    results,
    customGroups: createDefaultCustomGroups(results.length),
  };
}

function sharedControlControls() {
  return {
    fileName: els.controlFileName,
    sheet: els.controlSheet,
    lane: els.controlLaneColumn,
    signal: els.controlSignalColumn,
  };
}

function switchMode() {
  state.mode = els.analysisMode.value;
  const isComparison = state.mode === "comparison";
  els.sharedWorkflow.hidden = isComparison;
  els.sharedResults.hidden = isComparison;
  els.sharedCountWrap.hidden = isComparison;
  els.comparisonWorkflow.hidden = !isComparison;
  els.comparisonResults.hidden = !isComparison;
  els.pairCountWrap.hidden = !isComparison;
  els.comparisonChartWrap.hidden = !isComparison;
  refreshNormalizationLanes();
  if (isComparison) renderComparisonChart();
}

function renderSharedSampleInputs(count) {
  while (state.sharedSamples.length < count) state.sharedSamples.push(createDatasetState());
  state.sharedSamples = state.sharedSamples.slice(0, count);

  els.sampleInputs.innerHTML = state.sharedSamples
    .map((_, index) => sampleCardHtml(index))
    .join("");

  sharedSampleControls = state.sharedSamples.map((_, index) => ({
    file: document.querySelector(`[data-shared-sample-file="${index}"]`),
    fileName: document.querySelector(`[data-shared-sample-file-name="${index}"]`),
    protein: document.querySelector(`[data-shared-sample-protein="${index}"]`),
    sheet: document.querySelector(`[data-shared-sample-sheet="${index}"]`),
    lane: document.querySelector(`[data-shared-sample-lane="${index}"]`),
    signal: document.querySelector(`[data-shared-sample-signal="${index}"]`),
  }));

  sharedSampleControls.forEach((controls, index) => {
    controls.file.addEventListener("change", (event) => loadSharedSampleFile(event, index));
    controls.sheet.addEventListener("change", () => {
      selectDatasetSheet(state.sharedSamples[index], controls, controls.sheet.value);
      refreshNormalizationLanes();
    });
    controls.lane.addEventListener("change", refreshNormalizationLanes);
    hydrateDatasetControls(state.sharedSamples[index], controls);
  });

  refreshNormalizationLanes();
}

function sampleCardHtml(index) {
  return `
    <div class="sample-card">
      <h3>Sample ${index + 1}</h3>
      <label class="file-drop">
        <input data-shared-sample-file="${index}" type="file" accept=".xlsx,.xls,.csv,.tsv" />
        <span data-shared-sample-file-name="${index}">${state.sharedSamples[index]?.fileLabel || "Choose Excel or CSV file"}</span>
      </label>
      <div class="field-row">
        <label>
          Protein name
          <input data-shared-sample-protein="${index}" type="text" placeholder="e.g. pERK" value="${escapeHtml(state.sharedSamples[index]?.proteinName || "")}" />
        </label>
      </div>
      <div class="mapping-grid">
        <label>
          Sheet
          <select data-shared-sample-sheet="${index}"></select>
        </label>
        <label>
          Lane column
          <select data-shared-sample-lane="${index}"></select>
        </label>
        <label>
          Signal column
          <select data-shared-sample-signal="${index}"></select>
        </label>
      </div>
    </div>
  `;
}

function renderPairInputs(count) {
  while (state.pairedSets.length < count) {
    state.pairedSets.push({ sample: createDatasetState(), control: createDatasetState() });
  }
  state.pairedSets = state.pairedSets.slice(0, count);

  els.pairInputs.innerHTML = state.pairedSets
    .map((_, index) => pairCardHtml(index))
    .join("");

  pairControls = state.pairedSets.map((_, index) => ({
    sample: {
      file: document.querySelector(`[data-pair-sample-file="${index}"]`),
      fileName: document.querySelector(`[data-pair-sample-file-name="${index}"]`),
      label: document.querySelector(`[data-pair-sample-label="${index}"]`),
      sheet: document.querySelector(`[data-pair-sample-sheet="${index}"]`),
      lane: document.querySelector(`[data-pair-sample-lane="${index}"]`),
      signal: document.querySelector(`[data-pair-sample-signal="${index}"]`),
    },
    control: {
      file: document.querySelector(`[data-pair-control-file="${index}"]`),
      fileName: document.querySelector(`[data-pair-control-file-name="${index}"]`),
      sheet: document.querySelector(`[data-pair-control-sheet="${index}"]`),
      lane: document.querySelector(`[data-pair-control-lane="${index}"]`),
      signal: document.querySelector(`[data-pair-control-signal="${index}"]`),
    },
  }));

  pairControls.forEach((controls, index) => {
    controls.sample.file.addEventListener("change", (event) => loadPairFile(event, index, "sample"));
    controls.control.file.addEventListener("change", (event) => loadPairFile(event, index, "control"));
    controls.sample.sheet.addEventListener("change", () => {
      selectDatasetSheet(state.pairedSets[index].sample, controls.sample, controls.sample.sheet.value);
      refreshNormalizationLanes();
    });
    controls.control.sheet.addEventListener("change", () => selectDatasetSheet(state.pairedSets[index].control, controls.control, controls.control.sheet.value));
    controls.sample.lane.addEventListener("change", refreshNormalizationLanes);
    hydrateDatasetControls(state.pairedSets[index].sample, controls.sample);
    hydrateDatasetControls(state.pairedSets[index].control, controls.control);
  });

  refreshNormalizationLanes();
}

function pairCardHtml(index) {
  return `
    <div class="pair-card">
      <h3>Pair ${index + 1}</h3>
      <div class="field-row">
        <label>
          Graph label
          <input data-pair-sample-label="${index}" type="text" value="${escapeHtml(state.pairedSets[index]?.label || `Sample ${index + 1}`)}" />
        </label>
      </div>
      <div class="pair-columns">
        <div>
          <span class="badge sample">Sample</span>
          <label class="file-drop">
            <input data-pair-sample-file="${index}" type="file" accept=".xlsx,.xls,.csv,.tsv" />
            <span data-pair-sample-file-name="${index}">${state.pairedSets[index]?.sample?.fileLabel || "Choose sample file"}</span>
          </label>
          <div class="mapping-grid">
            <label>Sheet<select data-pair-sample-sheet="${index}"></select></label>
            <label>Lane column<select data-pair-sample-lane="${index}"></select></label>
            <label>Signal column<select data-pair-sample-signal="${index}"></select></label>
          </div>
        </div>
        <div>
          <span class="badge control">Control</span>
          <label class="file-drop">
            <input data-pair-control-file="${index}" type="file" accept=".xlsx,.xls,.csv,.tsv" />
            <span data-pair-control-file-name="${index}">${state.pairedSets[index]?.control?.fileLabel || "Choose control file"}</span>
          </label>
          <div class="mapping-grid">
            <label>Sheet<select data-pair-control-sheet="${index}"></select></label>
            <label>Lane column<select data-pair-control-lane="${index}"></select></label>
            <label>Signal column<select data-pair-control-signal="${index}"></select></label>
          </div>
        </div>
      </div>
    </div>
  `;
}

async function loadSharedSampleFile(event, index) {
  await loadDatasetFile(event, state.sharedSamples[index], sharedSampleControls[index]);
  refreshNormalizationLanes();
}

async function loadSharedControlFile(event) {
  await loadDatasetFile(event, state.sharedControl, sharedControlControls());
}

async function loadPairFile(event, index, role) {
  await loadDatasetFile(event, state.pairedSets[index][role], pairControls[index][role]);
  refreshNormalizationLanes();
}

async function loadDatasetFile(event, dataset, controls) {
  const file = event.target.files?.[0];
  if (!file) return;

  try {
    dataset.fileLabel = file.name;
    controls.fileName.textContent = file.name;
    dataset.workbook = await readTabularFile(file);
    populateSheetSelect(controls.sheet, dataset.workbook);
    selectDatasetSheet(dataset, controls, dataset.workbook.sheetNames[0]);
  } catch (error) {
    showSharedError(`Could not read file. ${error.message}`);
  }
}

async function readTabularFile(file) {
  const extension = file.name.split(".").pop().toLowerCase();
  if (extension === "csv" || extension === "tsv") {
    const text = await file.text();
    const delimiter = extension === "tsv" ? "\t" : ",";
    return csvToWorkbook(text, delimiter);
  }

  if (!window.XLSX) {
    throw new Error("The Excel parser has not loaded yet. Check the network connection and retry.");
  }

  const bytes = await file.arrayBuffer();
  const workbook = XLSX.read(bytes, { type: "array" });
  return {
    sheetNames: workbook.SheetNames,
    getRows(sheetName) {
      return XLSX.utils.sheet_to_json(workbook.Sheets[sheetName], { defval: "" });
    },
  };
}

function csvToWorkbook(text, delimiter) {
  const lines = text.trim().split(/\r?\n/);
  const headers = splitDelimitedLine(lines[0], delimiter).map((header) => header.trim());
  const rows = lines.slice(1).map((line) => {
    const values = splitDelimitedLine(line, delimiter);
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
  return {
    sheetNames: ["Data"],
    getRows() {
      return rows;
    },
  };
}

function splitDelimitedLine(line, delimiter) {
  const output = [];
  let cell = "";
  let quoted = false;

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    const next = line[index + 1];
    if (char === '"' && next === '"') {
      cell += '"';
      index += 1;
    } else if (char === '"') {
      quoted = !quoted;
    } else if (char === delimiter && !quoted) {
      output.push(cell);
      cell = "";
    } else {
      cell += char;
    }
  }

  output.push(cell);
  return output;
}

function populateSheetSelect(select, workbook) {
  select.innerHTML = workbook.sheetNames
    .map((sheetName) => `<option value="${escapeHtml(sheetName)}">${escapeHtml(sheetName)}</option>`)
    .join("");
}

function selectDatasetSheet(dataset, controls, sheetName) {
  if (!dataset.workbook) return;
  dataset.sheetName = sheetName;
  dataset.rows = dataset.workbook.getRows(sheetName).filter((row) =>
    Object.values(row).some((value) => String(value).trim() !== ""),
  );
  dataset.headers = collectHeaders(dataset.rows);
  populateColumnSelects(controls, dataset.headers);
}

function hydrateDatasetControls(dataset, controls) {
  if (!dataset.workbook) return;
  populateSheetSelect(controls.sheet, dataset.workbook);
  controls.sheet.value = dataset.sheetName || dataset.workbook.sheetNames[0];
  populateColumnSelects(controls, dataset.headers);
}

function collectHeaders(rows) {
  const headers = new Set();
  rows.forEach((row) => Object.keys(row).forEach((key) => headers.add(key)));
  return [...headers];
}

function populateColumnSelects(controls, headers) {
  const guesses = guessColumns(headers);
  setOptions(controls.lane, headers, guesses.lane);
  setOptions(controls.signal, headers, guesses.signal);
}

function setOptions(select, values, selected) {
  select.innerHTML = values
    .map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
    .join("");
  if (selected && values.includes(selected)) select.value = selected;
}

function guessColumns(headers) {
  return {
    lane: findExactHeader(headers, "name") ?? findHeader(headers, ["lane", "well", "sample"]) ?? findHeader(headers, ["name"]) ?? headers[0],
    signal: findHeader(headers, ["signal", "intensity", "density", "volume", "band"]) ?? headers[1],
  };
}

function findExactHeader(headers, term) {
  return headers.find((header) => header.trim().toLowerCase() === term);
}

function findHeader(headers, terms) {
  return headers.find((header) => {
    const normalized = header.toLowerCase();
    return terms.some((term) => normalized.includes(term));
  });
}

function refreshNormalizationLanes() {
  const previous = els.normalizationLane.value;
  const source = state.mode === "comparison" ? state.pairedSets[0]?.sample : state.sharedSamples[0];
  const controls = state.mode === "comparison" ? pairControls[0]?.sample : sharedSampleControls[0];
  const lanes = source && controls ? extractLaneNames(source, controls) : [];
  els.normalizationLane.innerHTML = lanes
    .map((lane) => `<option value="${escapeHtml(lane)}">${escapeHtml(lane)}</option>`)
    .join("");
  if (lanes.includes(previous)) {
    els.normalizationLane.value = previous;
  } else if (lanes.length) {
    els.normalizationLane.value = lanes[0];
  }
}

function extractLaneNames(dataset, controls) {
  const laneColumn = controls?.lane?.value;
  if (!dataset.rows.length || !laneColumn) return [];
  return dataset.rows.map((row, index) => normalizeLaneName(row[laneColumn], index)).filter(Boolean);
}

function runAnalysis() {
  if (state.mode === "comparison") {
    runComparisonAnalysis();
  } else {
    runSharedAnalysis();
  }
}

function renderCurrentGrouping() {
  if (state.mode === "comparison") {
    renderComparisonChart();
  } else {
    renderGroupedGraphs();
  }
}

function runSharedAnalysis() {
  try {
    const controlRows = buildRows(state.sharedControl, sharedControlControls());
    if (!controlRows.length) throw new Error("The shared control file needs readable lanes and signals.");

    state.sharedAnalyses = state.sharedSamples.map((sampleDataset, index) => {
      const controls = sharedSampleControls[index];
      const sampleRows = buildRows(sampleDataset, controls);
      if (!sampleRows.length) throw new Error(`Sample ${index + 1} needs readable lanes and signals.`);

      const sampleProtein = controls.protein.value.trim() || `Sample ${index + 1}`;
      sampleDataset.proteinName = sampleProtein;
      const controlProtein = els.controlProtein.value.trim() || "Loading control";
      const title = `${sampleProtein} - ${controlProtein} fold change`;
      return createAnalysisState(sampleProtein, title, computeFoldChange(sampleRows, controlRows, els.normalizationLane.value));
    });

    state.activeSampleIndex = Math.min(state.activeSampleIndex, state.sharedAnalyses.length - 1);
    renderSampleTabs();
    renderActiveSharedAnalysis();
  } catch (error) {
    showSharedError(error.message);
  }
}

function runComparisonAnalysis() {
  try {
    state.pairedAnalyses = state.pairedSets.map((pair, index) => {
      const sampleRows = buildRows(pair.sample, pairControls[index].sample);
      const controlRows = buildRows(pair.control, pairControls[index].control);
      if (!sampleRows.length || !controlRows.length) {
        throw new Error(`Pair ${index + 1} needs both sample and control lanes/signals.`);
      }

      const label = pairControls[index].sample.label.value.trim() || `Sample ${index + 1}`;
      pair.label = label;
      return createAnalysisState(label, label, computeFoldChange(sampleRows, controlRows, els.normalizationLane.value));
    });
    state.comparisonCustomGroups = createDefaultCustomGroups(getCommonComparisonLabels().length);

    renderComparisonLabelEditor();
    renderComparisonChart();
    els.comparisonLabelPanel.hidden = false;
    els.comparisonChartPanel.hidden = false;
    els.downloadComparisonChartButton.disabled = false;
  } catch (error) {
    els.comparisonChartTitle.innerHTML = `<span class="error-text">${escapeHtml(error.message)}</span>`;
    els.comparisonLabelPanel.hidden = true;
    els.comparisonChartPanel.hidden = false;
    els.downloadComparisonChartButton.disabled = true;
    drawEmptyChart(els.comparisonChart);
  }
}

function buildRows(dataset, controls) {
  const laneColumn = controls?.lane?.value;
  const signalColumn = controls?.signal?.value;
  if (!dataset.rows.length || !laneColumn || !signalColumn) return [];

  return dataset.rows
    .map((row, index) => ({
      lane: normalizeLaneName(row[laneColumn], index),
      displayLane: normalizeLaneName(row[laneColumn], index),
      signal: parseSignal(row[signalColumn]),
    }))
    .filter((row) => row.lane && Number.isFinite(row.signal));
}

function computeFoldChange(sampleRows, controlRows, normalizationLane) {
  const sampleMap = new Map(sampleRows.map((row) => [row.lane, row]));
  const controlMap = new Map(controlRows.map((row) => [row.lane, row]));
  const baselineSample = sampleMap.get(normalizationLane);
  const baselineControl = controlMap.get(normalizationLane);

  if (!baselineSample || !baselineControl) {
    throw new Error("The normalization lane must exist in every sample and control file.");
  }
  if (!baselineSample.signal || !baselineControl.signal) {
    throw new Error("The normalization lane must have non-zero sample and control signals.");
  }

  return sampleRows.map((sampleRow) => {
    const controlRow = controlMap.get(sampleRow.lane);
    if (!controlRow) throw new Error(`No matching control lane found for "${sampleRow.lane}".`);

    const samplePercent = (sampleRow.signal / baselineSample.signal) * 100;
    const controlPercent = (controlRow.signal / baselineControl.signal) * 100;
    if (controlPercent === 0) throw new Error(`Control lane "${sampleRow.lane}" has a zero normalized signal.`);

    return {
      lane: sampleRow.lane,
      displayLane: sampleRow.displayLane,
      sampleSignal: sampleRow.signal,
      controlSignal: controlRow.signal,
      samplePercent,
      controlPercent,
      foldChange: samplePercent / controlPercent,
    };
  });
}

function renderSampleTabs() {
  els.sampleTabsPanel.hidden = state.sharedAnalyses.length <= 1;
  els.sampleTabs.innerHTML = state.sharedAnalyses
    .map(
      (analysis, index) => `
        <button class="tab-button" type="button" data-sample-tab="${index}" aria-selected="${index === state.activeSampleIndex}">
          ${escapeHtml(analysis.name)}
        </button>
      `,
    )
    .join("");
}

function selectSampleTab(event) {
  const button = event.target.closest("[data-sample-tab]");
  if (!button) return;
  state.activeSampleIndex = Number(button.dataset.sampleTab);
  renderSampleTabs();
  renderActiveSharedAnalysis();
}

function activeAnalysis() {
  return state.sharedAnalyses[state.activeSampleIndex];
}

function renderActiveSharedAnalysis() {
  const analysis = activeAnalysis();
  if (!analysis) return;
  state.chartTitle = analysis.title;
  els.chartTitle.textContent = analysis.title;
  drawBarChart(els.foldChart, analysis.results, analysis.title);
  renderResultTable(analysis.results);
  renderLabelEditor(analysis.results);
  renderGroupedGraphs();
  els.downloadChartButton.disabled = false;
  els.downloadCsvButton.disabled = false;
  els.labelPanel.hidden = false;
}

function drawEmptyChart(canvas) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#f4f7f8";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#64707d";
  ctx.font = "700 24px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText("Your chart will appear here", canvas.width / 2, canvas.height / 2);
}

function drawBarChart(canvas, rows, title) {
  const ctx = canvas.getContext("2d");
  const padding = { top: 42, right: 32, bottom: 92, left: 72 };
  const plotWidth = canvas.width - padding.left - padding.right;
  const plotHeight = canvas.height - padding.top - padding.bottom;
  const maxValue = Math.max(1.2, ...rows.map((row) => row.foldChange)) * 1.16;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGrid(ctx, canvas, padding, plotHeight, maxValue);

  const band = plotWidth / Math.max(rows.length, 1);
  const barWidth = Math.min(56, band * 0.56);
  rows.forEach((row, index) => {
    const x = padding.left + band * index + (band - barWidth) / 2;
    const barHeight = (row.foldChange / maxValue) * plotHeight;
    const y = padding.top + plotHeight - barHeight;
    ctx.fillStyle = "#2563eb";
    ctx.fillRect(x, y, barWidth, barHeight);
    drawValueLabel(ctx, row.foldChange, x + barWidth / 2, y - 12);
    drawAngledLabel(ctx, row.displayLane || row.lane, x + barWidth / 2, padding.top + plotHeight + 18);
  });

  drawAxes(ctx, canvas, padding, plotHeight);
  drawTitle(ctx, canvas, title);
}

function drawGrid(ctx, canvas, padding, plotHeight, maxValue) {
  ctx.strokeStyle = "#d9e0e6";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#64707d";
  ctx.font = "600 14px Inter, system-ui, sans-serif";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let index = 0; index <= 5; index += 1) {
    const value = (maxValue / 5) * index;
    const y = padding.top + plotHeight - (value / maxValue) * plotHeight;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(canvas.width - padding.right, y);
    ctx.stroke();
    ctx.fillText(value.toFixed(1), padding.left - 10, y);
  }
}

function drawAxes(ctx, canvas, padding, plotHeight) {
  ctx.strokeStyle = "#1f2933";
  ctx.beginPath();
  ctx.moveTo(padding.left, padding.top);
  ctx.lineTo(padding.left, padding.top + plotHeight);
  ctx.lineTo(canvas.width - padding.right, padding.top + plotHeight);
  ctx.stroke();
}

function drawTitle(ctx, canvas, title) {
  ctx.fillStyle = "#1f2933";
  ctx.font = "800 20px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.fillText(title, canvas.width / 2, 8);
}

function drawValueLabel(ctx, value, x, y) {
  ctx.fillStyle = "#1f2933";
  ctx.font = "700 13px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(value.toFixed(2), x, y);
}

function drawAngledLabel(ctx, text, x, y) {
  ctx.save();
  ctx.translate(x, y);
  ctx.rotate(-Math.PI / 5);
  ctx.textAlign = "right";
  ctx.fillStyle = "#47515c";
  ctx.font = "700 13px Inter, system-ui, sans-serif";
  ctx.fillText(text, 0, 0);
  ctx.restore();
}

function renderResultTable(rows) {
  els.resultTableBody.innerHTML = rows
    .map(
      (row) => `
        <tr>
          <td>${escapeHtml(row.displayLane || row.lane)}</td>
          <td>${formatNumber(row.samplePercent)}</td>
          <td>${formatNumber(row.controlPercent)}</td>
          <td><strong>${formatNumber(row.foldChange)}</strong></td>
        </tr>
      `,
    )
    .join("");
}

function renderLabelEditor(rows) {
  els.labelEditor.innerHTML = rows
    .map(
      (row, index) => `
        <label>
          ${escapeHtml(row.lane)}
          <input type="text" value="${escapeHtml(row.displayLane || row.lane)}" data-label-index="${index}" />
        </label>
      `,
    )
    .join("");
}

function updateSharedLaneLabel(event) {
  const input = event.target.closest("[data-label-index]");
  const analysis = activeAnalysis();
  if (!input || !analysis) return;
  const index = Number(input.dataset.labelIndex);
  analysis.results[index].displayLane = input.value || analysis.results[index].lane;//change made ***********
  drawBarChart(els.foldChart, analysis.results, analysis.title); //change made ***********
  renderResultTable(analysis.results);//change made ***********
  renderGroupedGraphs();//change made ***********
}

function renderGroupedGraphs() {
  const analysis = activeAnalysis();
  const shouldShow = els.enableGroupedGraphs.checked && analysis;
  els.groupPanel.hidden = !shouldShow;
  if (!shouldShow) return;
  const groups = buildGroupedRows(analysis);
  renderCustomGroupingPanel(analysis);
  renderGroupSummary(groups);
  renderMiniCharts(groups);
}

function buildGroupedRows(analysis) {
  const mode = els.groupMode.value;
  const size = clampInteger(Number(els.groupSize.value), 2, 24, 2);
  if (mode === "blocks") return buildBlockGroups(analysis.results, size);
  if (mode === "custom") return buildCustomGroups(analysis);
  return buildInterleavedGroups(analysis.results, size);
}

function buildInterleavedGroups(rows, size) {
  return Array.from({ length: size }, (_, offset) => {
    const groupRows = rows.filter((_, index) => index % size === offset);
    return {
      name: `Group ${offset + 1}: ${groupRows.map((row) => row.displayLane || row.lane).join(", ")}`,
      rows: groupRows,
    };
  }).filter((group) => group.rows.length);
}

function buildBlockGroups(rows, size) {
  const groups = [];
  for (let index = 0; index < rows.length; index += size) {
    const groupRows = rows.slice(index, index + size);
    groups.push({ name: `Group ${groups.length + 1}: lanes ${index + 1}-${index + groupRows.length}`, rows: groupRows });
  }
  return groups;
}

function buildCustomGroups(analysis) {
  return analysis.customGroups
    .map((group) => ({
      name: group.name.trim() || "Custom group",
      rows: group.indices.map((index) => analysis.results[index]).filter(Boolean),
    }))
    .filter((group) => group.rows.length);
}

function renderGroupSummary(groups) {
  els.groupSummary.innerHTML = groups
    .map((group) => {
      const baseline = group.rows[0]?.foldChange || 1;
      const items = group.rows
        .map((row) => `<li>${escapeHtml(row.displayLane || row.lane)}: ${formatNumber(row.foldChange / baseline)}x</li>`)
        .join("");
      return `<div class="group-box"><h3>${escapeHtml(group.name)}</h3><ul>${items}</ul></div>`;
    })
    .join("");
}

function renderMiniCharts(groups) {
  els.blockGraphs.innerHTML = groups
    .map(
      (group, index) => `
        <div class="mini-chart">
          <h3>${escapeHtml(group.name)}</h3>
          <button class="ghost-button" type="button" data-download-canvas="groupChart${index}" data-download-name="${escapeHtml(filenameSafe(group.name))}.jpg">Export JPG</button>
          <canvas id="groupChart${index}" width="640" height="360" aria-label="Grouped graph ${index + 1}"></canvas>
        </div>
      `,
    )
    .join("");
  groups.forEach((group, index) => {
    const baseline = group.rows[0]?.foldChange || 1;
    drawBarChart(
      document.querySelector(`#groupChart${index}`),
      group.rows.map((row) => ({ ...row, foldChange: row.foldChange / baseline })),
      group.name,
    );
  });
}

function renderCustomGroupingPanel(analysis) {
  const isCustom = els.groupMode.value === "custom" && els.enableGroupedGraphs.checked;
  els.customGroupingPanel.hidden = !isCustom;
  if (!isCustom) return;
  if (!analysis.customGroups.length) analysis.customGroups = createDefaultCustomGroups(analysis.results.length);
  els.customGroups.innerHTML = analysis.customGroups
    .map(
      (group, groupIndex) => `
        <div class="custom-group-card">
          <input type="text" value="${escapeHtml(group.name)}" data-custom-name="${groupIndex}" />
          <div class="lane-picks">
            ${analysis.results
              .map(
                (row, laneIndex) => `
                  <label>
                    <input type="checkbox" data-custom-group="${groupIndex}" data-custom-lane="${laneIndex}" ${group.indices.includes(laneIndex) ? "checked" : ""} />
                    ${escapeHtml(row.displayLane || row.lane)}
                  </label>
                `,
              )
              .join("")}
          </div>
        </div>
      `,
    )
    .join("");
}

function updateCustomGroups(event) {
  const analysis = activeAnalysis();
  if (!analysis) return;
  const nameInput = event.target.closest("[data-custom-name]");
  if (nameInput) {
    const groupIndex = Number(nameInput.dataset.customName);
    if (analysis.customGroups[groupIndex]) analysis.customGroups[groupIndex].name = nameInput.value;
    renderGroupSummary(buildGroupedRows(analysis));
    renderMiniCharts(buildGroupedRows(analysis));
    return;
  }

  const checkbox = event.target.closest("[data-custom-group]");
  if (!checkbox) return;
  const group = analysis.customGroups[Number(checkbox.dataset.customGroup)];
  const laneIndex = Number(checkbox.dataset.customLane);
  if (!group) return;
  if (checkbox.checked && !group.indices.includes(laneIndex)) group.indices.push(laneIndex);
  if (!checkbox.checked) group.indices = group.indices.filter((index) => index !== laneIndex);
  group.indices.sort((a, b) => a - b);
  renderGroupedGraphs();
}

function addCustomGroup() {
  const analysis = activeAnalysis();
  if (!analysis) return;
  analysis.customGroups.push({ name: `Custom group ${analysis.customGroups.length + 1}`, indices: [] });
  renderGroupedGraphs();
}

function renderComparisonLabelEditor() {
  els.comparisonLabelEditor.innerHTML = state.pairedAnalyses
    .map(
      (analysis, pairIndex) => `
        <div class="comparison-label-card">
          <h3>${escapeHtml(analysis.name)}</h3>
          <div class="comparison-label-grid">
            ${analysis.results
              .map(
                (row, laneIndex) => `
                  <label>
                    ${escapeHtml(row.lane)}
                    <input type="text" value="${escapeHtml(row.displayLane || row.lane)}" data-pair-label="${pairIndex}" data-pair-lane="${laneIndex}" />
                  </label>
                `,
              )
              .join("")}
          </div>
        </div>
      `,
    )
    .join("");
}

function updateComparisonLaneLabel(event) {
  const input = event.target.closest("[data-pair-label]");
  if (!input) return;
  const analysis = state.pairedAnalyses[Number(input.dataset.pairLabel)];
  const row = analysis?.results[Number(input.dataset.pairLane)];
  if (!row) return;
  row.displayLane = input.value.trim() || row.lane;
  renderComparisonChart();
}

function renderComparisonChart() {
  if (!state.pairedAnalyses.length) return;
  const commonLabels = getCommonComparisonLabels();
  const rows = commonLabels.map((label) => ({
    label,
    values: state.pairedAnalyses.map((analysis) => {
      const row = analysis.results.find((candidate) => (candidate.displayLane || candidate.lane) === label);
      return row.foldChange;
    }),
  }));

  els.comparisonChartTitle.textContent = commonLabels.length
    ? "Common lane comparison"
    : "No shared lane labels found";

  if (els.comparisonChartType.value === "points") {
    drawAveragePointChart(els.comparisonChart, rows, state.pairedAnalyses.map((analysis) => analysis.name));
  } else {
    drawGroupedComparisonChart(els.comparisonChart, rows, state.pairedAnalyses.map((analysis) => analysis.name));
  }

  renderComparisonGroupedGraphs(rows);
}

function getCommonComparisonLabels() {
  const labelSets = state.pairedAnalyses.map((analysis) => new Set(analysis.results.map((row) => row.displayLane || row.lane)));
  if (!labelSets.length) return [];
  return [...labelSets[0]].filter((label) => labelSets.every((set) => set.has(label)));
}

function renderComparisonGroupedGraphs(rows) {
  const shouldShow = els.enableGroupedGraphs.checked && state.mode === "comparison" && rows.length > 0;
  els.comparisonGroupPanel.hidden = !shouldShow;
  if (!shouldShow) return;

  const groups = buildComparisonGroups(rows);
  renderComparisonCustomGroupingPanel(rows);
  renderComparisonGroupSummary(groups);
  renderComparisonMiniCharts(groups);
}

function buildComparisonGroups(rows) {
  const mode = els.groupMode.value;
  const size = clampInteger(Number(els.groupSize.value), 2, 24, 2);
  if (mode === "blocks") return buildComparisonBlockGroups(rows, size);
  if (mode === "custom") return buildComparisonCustomGroups(rows);
  return buildComparisonInterleavedGroups(rows, size);
}

function buildComparisonInterleavedGroups(rows, size) {
  return Array.from({ length: size }, (_, offset) => {
    const groupRows = rows.filter((_, index) => index % size === offset);
    return {
      name: `Group ${offset + 1}: ${groupRows.map((row) => row.label).join(", ")}`,
      rows: groupRows,
    };
  }).filter((group) => group.rows.length);
}

function buildComparisonBlockGroups(rows, size) {
  const groups = [];
  for (let index = 0; index < rows.length; index += size) {
    const groupRows = rows.slice(index, index + size);
    groups.push({ name: `Group ${groups.length + 1}: lanes ${index + 1}-${index + groupRows.length}`, rows: groupRows });
  }
  return groups;
}

function buildComparisonCustomGroups(rows) {
  return state.comparisonCustomGroups
    .map((group) => ({
      name: group.name.trim() || "Custom group",
      rows: group.indices.map((index) => rows[index]).filter(Boolean),
    }))
    .filter((group) => group.rows.length);
}

function renderComparisonGroupSummary(groups) {
  els.comparisonGroupSummary.innerHTML = groups
    .map((group) => {
      const items = group.rows.map((row) => `<li>${escapeHtml(row.label)}</li>`).join("");
      return `<div class="group-box"><h3>${escapeHtml(group.name)}</h3><ul>${items}</ul></div>`;
    })
    .join("");
}

function renderComparisonMiniCharts(groups) {
  const seriesNames = state.pairedAnalyses.map((analysis) => analysis.name);
  els.comparisonGroupedCharts.innerHTML = groups
    .map(
      (group, index) => `
        <div class="mini-chart">
          <h3>${escapeHtml(group.name)}</h3>
          <button class="ghost-button" type="button" data-download-canvas="comparisonGroupChart${index}" data-download-name="${escapeHtml(filenameSafe(group.name))}.jpg">Export JPG</button>
          <canvas id="comparisonGroupChart${index}" width="720" height="420" aria-label="Grouped comparison graph ${index + 1}"></canvas>
        </div>
      `,
    )
    .join("");

  groups.forEach((group, index) => {
    const canvas = document.querySelector(`#comparisonGroupChart${index}`);
    if (els.comparisonChartType.value === "points") {
      drawAveragePointChart(canvas, group.rows, seriesNames, group.name);
    } else {
      drawGroupedComparisonChart(canvas, group.rows, seriesNames, group.name);
    }
  });
}

function renderComparisonCustomGroupingPanel(rows) {
  const isCustom = els.groupMode.value === "custom" && els.enableGroupedGraphs.checked && state.mode === "comparison";
  els.comparisonCustomGroupingPanel.hidden = !isCustom;
  if (!isCustom) return;
  if (!state.comparisonCustomGroups.length) state.comparisonCustomGroups = createDefaultCustomGroups(rows.length);

  els.comparisonCustomGroups.innerHTML = state.comparisonCustomGroups
    .map(
      (group, groupIndex) => `
        <div class="custom-group-card">
          <input type="text" value="${escapeHtml(group.name)}" data-comparison-custom-name="${groupIndex}" />
          <div class="lane-picks">
            ${rows
              .map(
                (row, laneIndex) => `
                  <label>
                    <input type="checkbox" data-comparison-custom-group="${groupIndex}" data-comparison-custom-lane="${laneIndex}" ${group.indices.includes(laneIndex) ? "checked" : ""} />
                    ${escapeHtml(row.label)}
                  </label>
                `,
              )
              .join("")}
          </div>
        </div>
      `,
    )
    .join("");
}

function updateComparisonCustomGroups(event) {
  const nameInput = event.target.closest("[data-comparison-custom-name]");
  if (nameInput) {
    const groupIndex = Number(nameInput.dataset.comparisonCustomName);
    if (state.comparisonCustomGroups[groupIndex]) state.comparisonCustomGroups[groupIndex].name = nameInput.value;
    renderComparisonChart();
    return;
  }

  const checkbox = event.target.closest("[data-comparison-custom-group]");
  if (!checkbox) return;
  const group = state.comparisonCustomGroups[Number(checkbox.dataset.comparisonCustomGroup)];
  const laneIndex = Number(checkbox.dataset.comparisonCustomLane);
  if (!group) return;
  if (checkbox.checked && !group.indices.includes(laneIndex)) group.indices.push(laneIndex);
  if (!checkbox.checked) group.indices = group.indices.filter((index) => index !== laneIndex);
  group.indices.sort((a, b) => a - b);
  renderComparisonChart();
}

function addComparisonCustomGroup() {
  state.comparisonCustomGroups.push({ name: `Custom group ${state.comparisonCustomGroups.length + 1}`, indices: [] });
  renderComparisonChart();
}

function drawGroupedComparisonChart(canvas, rows, seriesNames, title = "Grouped bar comparison") {
  const ctx = canvas.getContext("2d");
  const padding = { top: 48, right: 32, bottom: 110, left: 72 };
  const plotWidth = canvas.width - padding.left - padding.right;
  const plotHeight = canvas.height - padding.top - padding.bottom;
  const allValues = rows.flatMap((row) => row.values);
  const maxValue = Math.max(1.2, ...allValues) * 1.16;
  const colors = ["#2563eb", "#b45309", "#0f766e", "#7c3aed", "#be123c", "#475569"];

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGrid(ctx, canvas, padding, plotHeight, maxValue);

  const band = plotWidth / Math.max(rows.length, 1);
  const groupWidth = band * 0.7;
  const barWidth = groupWidth / Math.max(seriesNames.length, 1);
  rows.forEach((row, rowIndex) => {
    const averageValue = average(row.values);
    const highestValue = Math.max(...row.values);
    row.values.forEach((value, seriesIndex) => {
      const x = padding.left + band * rowIndex + (band - groupWidth) / 2 + barWidth * seriesIndex;
      const h = (value / maxValue) * plotHeight;
      const y = padding.top + plotHeight - h;
      ctx.fillStyle = colors[seriesIndex % colors.length];
      ctx.fillRect(x, y, Math.max(8, barWidth - 3), h);
    });
    const labelX = padding.left + band * rowIndex + band / 2;
    const labelY = padding.top + plotHeight - (highestValue / maxValue) * plotHeight - 14;
    drawAverageLabel(ctx, averageValue, labelX, labelY);
    drawAngledLabel(ctx, row.label, padding.left + band * rowIndex + band / 2, padding.top + plotHeight + 18);
  });

  drawLegend(ctx, seriesNames, colors, padding.left, 18);
  drawAxes(ctx, canvas, padding, plotHeight);
  drawTitle(ctx, canvas, title);
}

function drawAverageLabel(ctx, value, x, y) {
  ctx.fillStyle = "#1f2933";
  ctx.font = "800 13px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.fillText(`Avg ${value.toFixed(2)}`, x, y);
}

function drawAveragePointChart(canvas, rows, seriesNames, title = "Average with individual points") {
  const ctx = canvas.getContext("2d");
  const padding = { top: 48, right: 32, bottom: 110, left: 72 };
  const plotWidth = canvas.width - padding.left - padding.right;
  const plotHeight = canvas.height - padding.top - padding.bottom;
  const averages = rows.map((row) => average(row.values));
  const maxValue = Math.max(1.2, ...rows.flatMap((row) => row.values), ...averages) * 1.16;
  const colors = ["#2563eb", "#b45309", "#0f766e", "#7c3aed", "#be123c", "#475569"];

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  drawGrid(ctx, canvas, padding, plotHeight, maxValue);

  const band = plotWidth / Math.max(rows.length, 1);
  const barWidth = Math.min(62, band * 0.44);
  rows.forEach((row, index) => {
    const x = padding.left + band * index + (band - barWidth) / 2;
    const avg = averages[index];
    const h = (avg / maxValue) * plotHeight;
    const y = padding.top + plotHeight - h;
    ctx.fillStyle = "#9db7ff";
    ctx.fillRect(x, y, barWidth, h);
    row.values.forEach((value, valueIndex) => {
      const px = x + (barWidth / Math.max(row.values.length - 1, 1)) * valueIndex;
      const py = padding.top + plotHeight - (value / maxValue) * plotHeight;
      ctx.fillStyle = colors[valueIndex % colors.length];
      ctx.beginPath();
      ctx.arc(px, py, 6, 0, Math.PI * 2);
      ctx.fill();
    });
    drawValueLabel(ctx, avg, x + barWidth / 2, y - 12);
    drawAngledLabel(ctx, row.label, padding.left + band * index + band / 2, padding.top + plotHeight + 18);
  });

  drawLegend(ctx, seriesNames, colors, padding.left, 18);
  drawAxes(ctx, canvas, padding, plotHeight);
  drawTitle(ctx, canvas, title);
}

function drawLegend(ctx, names, colors, x, y) {
  names.forEach((name, index) => {
    const offset = index * 150;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(x + offset, y, 14, 14);
    ctx.fillStyle = "#1f2933";
    ctx.font = "700 13px Inter, system-ui, sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(name, x + offset + 20, y + 12);
  });
}

function average(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function downloadSharedCsv() {
  const analysis = activeAnalysis();
  if (!analysis) return;
  const lines = [
    "Lane,Sample %,Control %,Fold change",
    ...analysis.results.map((row) =>
      [csvCell(row.displayLane || row.lane), formatNumber(row.samplePercent), formatNumber(row.controlPercent), formatNumber(row.foldChange)].join(","),
    ),
  ];
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${analysis.name.replace(/\W+/g, "-").toLowerCase()}-fold-change.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

function loadExampleData() {
  if (state.mode === "comparison") {
    loadComparisonExample();
  } else {
    loadSharedExample();
  }
}

function loadSharedExample() {
  els.sampleCount.value = "2";
  renderSharedSampleInputs(2);
  const sampleA = [
    { Name: "A1", Signal: 18200 },
    { Name: "A2", Signal: 22150 },
    { Name: "A3", Signal: 15400 },
    { Name: "A4", Signal: 30900 },
  ];
  const sampleB = [
    { Name: "A1", Signal: 20200 },
    { Name: "A2", Signal: 28400 },
    { Name: "A3", Signal: 19800 },
    { Name: "A4", Signal: 34500 },
  ];
  const control = [
    { Name: "A1", Signal: 16100 },
    { Name: "A2", Signal: 16850 },
    { Name: "A3", Signal: 13900 },
    { Name: "A4", Signal: 20250 },
  ];

  setExampleDataset(state.sharedSamples[0], sharedSampleControls[0], sampleA, "Example sample A");
  setExampleDataset(state.sharedSamples[1], sharedSampleControls[1], sampleB, "Example sample B");
  sharedSampleControls[0].protein.value = "pERK";
  sharedSampleControls[1].protein.value = "pAKT";
  setExampleDataset(state.sharedControl, sharedControlControls(), control, "Example loading control");
  els.controlProtein.value = "GAPDH";
  refreshNormalizationLanes();
  runSharedAnalysis();
}

function loadComparisonExample() {
  els.pairCount.value = "2";
  renderPairInputs(2);
  const sampleA = [
    { Name: "Control", Signal: 18200 },
    { Name: "Drug", Signal: 22150 },
    { Name: "Washout", Signal: 15400 },
  ];
  const controlA = [
    { Name: "Control", Signal: 16100 },
    { Name: "Drug", Signal: 16850 },
    { Name: "Washout", Signal: 13900 },
  ];
  const sampleB = [
    { Name: "Control", Signal: 20200 },
    { Name: "Drug", Signal: 28400 },
    { Name: "Extra lane", Signal: 19800 },
  ];
  const controlB = [
    { Name: "Control", Signal: 17500 },
    { Name: "Drug", Signal: 18900 },
    { Name: "Extra lane", Signal: 16200 },
  ];

  setExampleDataset(state.pairedSets[0].sample, pairControls[0].sample, sampleA, "Pair 1 sample");
  setExampleDataset(state.pairedSets[0].control, pairControls[0].control, controlA, "Pair 1 control");
  setExampleDataset(state.pairedSets[1].sample, pairControls[1].sample, sampleB, "Pair 2 sample");
  setExampleDataset(state.pairedSets[1].control, pairControls[1].control, controlB, "Pair 2 control");
  pairControls[0].sample.label.value = "Sample set 1";
  pairControls[1].sample.label.value = "Sample set 2";
  refreshNormalizationLanes();
  runComparisonAnalysis();
}

function setExampleDataset(dataset, controls, rows, label) {
  dataset.fileLabel = label;
  dataset.workbook = {
    sheetNames: ["Data"],
    getRows() {
      return rows;
    },
  };
  controls.fileName.textContent = label;
  populateSheetSelect(controls.sheet, dataset.workbook);
  selectDatasetSheet(dataset, controls, "Data");
}

function createDefaultCustomGroups(count) {
  return [
    { name: "Odd lanes", indices: Array.from({ length: count }, (_, index) => index).filter((index) => index % 2 === 0) },
    { name: "Even lanes", indices: Array.from({ length: count }, (_, index) => index).filter((index) => index % 2 === 1) },
  ].filter((group) => group.indices.length);
}

function showSharedError(message) {
  els.chartTitle.innerHTML = `<span class="error-text">${escapeHtml(message)}</span>`;
  drawEmptyChart(els.foldChart);
  els.downloadChartButton.disabled = true;
  els.downloadCsvButton.disabled = true;
  els.labelPanel.hidden = true;
  els.groupPanel.hidden = true;
}

function downloadGeneratedChart(event) {
  const button = event.target.closest("[data-download-canvas]");
  if (!button) return;
  const canvas = document.querySelector(`#${CSS.escape(button.dataset.downloadCanvas)}`);
  if (!canvas) return;
  downloadCanvasJpeg(canvas, button.dataset.downloadName || "western-blot-chart.jpg");
}

function downloadCanvasJpeg(canvas, filename) {
  const safeName = filenameSafe(filename);
  const link = document.createElement("a");
  link.href = canvas.toDataURL("image/jpeg", 0.95);
  link.download = safeName.endsWith(".jpg") || safeName.endsWith(".jpeg") ? safeName : `${safeName}.jpg`;
  link.click();
}

function currentSharedChartFilename() {
  const analysis = activeAnalysis();
  return `${filenameSafe(analysis?.name || "western-blot")}-fold-change.jpg`;
}

function normalizeLaneName(value, index) {
  const text = String(value ?? "").trim();
  return text || `Lane ${index + 1}`;
}

function parseSignal(value) {
  if (typeof value === "number") return value;
  const cleaned = String(value ?? "").replace(/,/g, "").trim();
  const number = Number(cleaned);
  return Number.isFinite(number) ? number : NaN;
}

function clampInteger(value, min, max, fallback) {
  if (!Number.isInteger(value)) return fallback;
  return Math.min(max, Math.max(min, value));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function formatNumber(value) {
  return Number(value).toLocaleString(undefined, {
    maximumFractionDigits: 3,
    minimumFractionDigits: 0,
  });
}

function csvCell(value) {
  const text = String(value ?? "");
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function filenameSafe(value) {
  const cleaned = String(value ?? "western-blot-chart")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return cleaned || "western-blot-chart";
}
