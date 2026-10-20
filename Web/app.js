"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="acc-token"]').content;
const state = { assets: [], selected: new Set(), page: 0, batchPage: 0, batches: [], left: null, right: null, comparison: null, mode: "side", zoom: 1, pan: [0, 0], running: false, calibration: false };
const labels = { success: "成功", partial: "部分成功", failed: "失败", incomplete: "部分视角无法评分", running: "运行中", cancelled: "已取消", interrupted: "已中断", no_baseline: "无历史基线", incompatible: "配置不兼容", small: "差异较小", review: "建议检查", changed: "差异明显" };
const viewNames = { front: "正面", front_right: "前斜面", right: "右侧", back: "背面", left: "左侧", back_right: "后斜面", top: "俯视", bottom: "仰视" };
function workerPhase(worker) {
  const names = { starting: "启动子编辑器", idle: "空闲", loading_world: "加载采集场景", loading_asset: "加载资产", settling_bounds: "更新包围盒", waiting_resources: "等待纹理及 Shader", cleaning: "清理资产", processing_images: "处理图片及发布结果" };
  const phase = worker.phase || "starting";
  const match = phase.match(/^(warming|capturing|writing)_(.+)$/);
  return (match ? ({ warming: "额外渲染", capturing: "采集", writing: "导出" }[match[1]] + " · " + (viewNames[match[2]] || match[2])) : names[phase] || phase) +
    (phase === "starting" && worker.startup_seconds != null ? ` · ${Math.round(worker.startup_seconds)}s` : "");
}
const text = (tag, value, className) => { const e = document.createElement(tag); e.textContent = value; if (className) e.className = className; return e; };
const button = (label, action) => { const e = text("button", label); e.onclick = () => safe(action); return e; };
const imageURL = (id, file) => "/files/" + ("batches/" + id.split("/")[0] + "/assets/" + id.split("/")[1] + "/" + file).split("/").map(encodeURIComponent).join("/");
const localTime = value => new Date(value).toLocaleString();
function notice(message, error = false) { $("notice").textContent = message; $("notice").hidden = !message; $("notice").classList.toggle("error", error); }
async function api(path, data) { const response = await fetch(path, data === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-ACC-Token": token }, body: JSON.stringify(data) }); const value = await response.json(); if (!response.ok) throw new Error(value.error || response.statusText); return value; }
async function safe(action) { try { await action(); } catch (error) { notice(error.message, true); } }
function tab(name) { document.querySelectorAll(".page").forEach(e => e.classList.toggle("active", e.id === name)); document.querySelectorAll("[data-tab]").forEach(e => e.classList.toggle("active", e.dataset.tab === name)); }
document.querySelectorAll("[data-tab]").forEach(e => e.onclick = () => tab(e.dataset.tab));
function filteredAssets() { const q = $("asset-search").value.toLowerCase(); return state.assets.filter(a => a.path.toLowerCase().includes(q)); }
function renderAssets() {
  const values = filteredAssets(); const pages = Math.max(1, Math.ceil(values.length / 50)); state.page = Math.min(state.page, pages - 1);
  $("asset-list").replaceChildren();
  for (const asset of values.slice(state.page * 50, (state.page + 1) * 50)) {
    const row = document.createElement("label"); row.className = "asset-row";
    const check = document.createElement("input"); check.type = "checkbox"; check.checked = state.selected.has(asset.path); check.onchange = () => { check.checked ? state.selected.add(asset.path) : state.selected.delete(asset.path); renderCount(); };
    const title = document.createElement("span"); title.append(text("strong", asset.name), text("small", asset.path)); row.append(check, title, text("span", asset.class)); $("asset-list").append(row);
  }
  $("asset-page").textContent = `${state.page + 1} / ${pages}`; $("asset-prev").disabled = state.page === 0; $("asset-next").disabled = state.page + 1 >= pages; renderCount();
}
function renderCount() { $("asset-count").textContent = `${state.selected.size} 已选 / ${state.assets.length} 个资产`; }
$("asset-search").oninput = () => { state.page = 0; renderAssets(); };
$("asset-prev").onclick = () => { state.page--; renderAssets(); }; $("asset-next").onclick = () => { state.page++; renderAssets(); };
$("select-all").onclick = () => { filteredAssets().forEach(a => state.selected.add(a.path)); renderAssets(); };
$("select-none").onclick = () => { state.selected.clear(); renderAssets(); };
async function loadAssets(action) { notice(""); const value = await api("/api/editor", { action, path: $("scan-path").value }); state.assets = value.assets; state.selected = new Set(action === "selection" ? state.assets.map(a => a.path) : []); state.page = 0; renderAssets(); }
$("selection").onclick = () => safe(() => loadAssets("selection")); $("scan").onclick = () => safe(() => loadAssets("scan"));
function profile() { return { size: Number($("size").value), columns: Number($("columns").value), fov: Number($("fov").value), padding: Number($("padding").value), orientation: Number($("orientation").value), warmup_frames: Number($("warmup_frames").value), worker_memory_gib: Number($("worker_memory_gib").value), scene: $("scene").value, hide_floor: $("hide_floor").checked, reframe: $("reframe").checked }; }
$("capture-form").onsubmit = event => { event.preventDefault(); safe(async () => { if (!state.selected.size) throw new Error("请选择需要采集的资产"); $("capture-start").disabled = true; try { const value = await api("/api/capture", { paths: [...state.selected], profile: profile(), concurrency: Number($("concurrency").value), label: $("label").value }); notice("采集任务已创建：" + value.batch_id); await refresh(); } finally { $("capture-start").disabled = state.running; } }); };
$("capture-cancel").onclick = () => safe(async () => { await api("/api/cancel", {}); notice("正在停止采集并保留已完成结果"); });
$("output-save").onclick = () => safe(async () => { await api("/api/output", { root: $("output-root").value }); await refresh(); });
$("reveal").onclick = () => safe(() => api("/api/reveal", {}));
function options(select, values, selected) { select.replaceChildren(...values.map(v => { const option = text("option", v.label); option.value = v.value; return option; })); if (values.some(v => v.value === selected)) select.value = selected; }
function refreshSelectors() {
  const choices = state.batches.map(b => ({ value: b.batch_id, label: `${b.label} · ${localTime(b.created_at)}` }));
  const oldLeft = $("left-batch").value, oldRight = $("right-batch").value;
  options($("left-batch"), choices, oldLeft || choices[1]?.value); options($("right-batch"), choices, oldRight || choices[0]?.value);
  assetOptions("left"); assetOptions("right");
}
function assetOptions(side, wanted) {
  const batch = state.batches.find(b => b.batch_id === $(side + "-batch").value), old = wanted || $(side + "-asset").value;
  const previous = state.batches.flatMap(b => b.assets).find(a => a.capture_id === old);
  const same = batch?.assets.find(a => a.capture_id === old || a.path === previous?.path);
  const selected = same?.capture_id ?? (previous ? "" : batch?.assets[0]?.capture_id || "");
  options($(side + "-asset"), [{ value: "", label: "未选择资产" }, ...(batch?.assets || []).map(a => ({ value: a.capture_id, label: `${a.name} · ${labels[a.status] || a.status} · ${a.path}` }))], selected);
}
async function pickBaseline() {
  if (!$("use-baseline").checked) return true;
  const rightID = $("right-asset").value; const asset = state.batches.flatMap(b => b.assets).find(a => a.capture_id === rightID);
  if (!asset?.baseline_id) { state.left = null; notice("此资产没有历史基线，可关闭基线选项手动选择。"); return false; }
  const batch = state.batches.find(b => b.batch_id === asset.baseline_id.split("/")[0]);
  if (!batch || !batch.assets.some(a => a.capture_id === asset.baseline_id && a.status === "success")) { state.left = null; notice("历史基线已删除，可手动选择其他记录。"); return false; }
  $("left-batch").value = batch.batch_id; assetOptions("left", asset.baseline_id);
  return true;
}
async function loadPair() {
  state.comparison = null;
  const hasLeft = await pickBaseline();
  const values = await Promise.all([hasLeft && $("left-asset").value ? api("/api/capture?id=" + encodeURIComponent($("left-asset").value)).catch(() => null) : null, $("right-asset").value ? api("/api/capture?id=" + encodeURIComponent($("right-asset").value)).catch(() => null) : null]);
  [state.left, state.right] = values; if ($("use-baseline").checked && !state.batches.flatMap(b => b.assets).find(a => a.capture_id === $("right-asset").value)?.baseline_id) state.left = null;
  const views = state.right?.views || state.left?.views || []; const old = $("view").value;
  options($("view"), [{ value: "sheet", label: "汇总拼图" }, ...views.map(v => ({ value: v.id, label: viewNames[v.id] || v.id }))], old);
  $("left-caption").textContent = state.left ? `${state.left.path} · ${localTime(state.left.finished_at)}` : "无左侧采集结果"; $("right-caption").textContent = state.right ? `${state.right.path} · ${localTime(state.right.finished_at)}` : "无右侧采集结果";
  $("scores").replaceChildren(); $("compare-info").replaceChildren();
  $("compare-summary").textContent = [state.left, state.right].filter(Boolean).map(c => { const b = state.batches.find(b => b.batch_id === c.capture_id.split("/")[0]); return `${b?.label || ""} · ${b?.profile.size || ""}px · FOV ${b?.profile.fov || ""}`; }).join(" / ");
  if (!state.left && state.right) $("compare-info").append(text("span", "仅右侧", "badge review"));
  if (state.left && !state.right) $("compare-info").append(text("span", "仅左侧", "badge review"));
  renderImages();
}
for (const side of ["left", "right"]) {
  $(side + "-batch").onchange = () => safe(async () => { if (side === "left") $("use-baseline").checked = false; assetOptions(side); await loadPair(); });
  $(side + "-asset").onchange = () => safe(async () => { if (side === "left") $("use-baseline").checked = false; if (side === "right" && !$("use-baseline").checked) { const counterpart = state.batches.find(b => b.batch_id === $("left-batch").value)?.assets.find(a => a.path === state.batches.flatMap(b => b.assets).find(a => a.capture_id === $("right-asset").value)?.path); $("left-asset").value = counterpart?.capture_id || ""; } await loadPair(); });
}
$("use-baseline").onchange = () => safe(loadPair);
$("run-compare").onclick = () => safe(async () => {
  if (!state.left || !state.right) throw new Error("请选择两侧完整采集记录");
  $("run-compare").disabled = true; notice("正在计算 SSIM…");
  try { state.comparison = await api("/api/compare", { left: state.left.capture_id, right: state.right.capture_id, thresholds: [Number($("threshold-small").value), Number($("threshold-review").value)] }); renderComparison(); notice(""); } finally { $("run-compare").disabled = false; }
});
function renderComparison() {
  const c = state.comparison; $("compare-info").replaceChildren(); $("scores").replaceChildren(); if (!c) return;
  $("compare-info").append(text("span", labels[c.category || c.status] || c.status, "badge " + (c.category || c.status)));
  if (c.status === "success") { $("compare-info").append(text("strong", `最低 SSIM ${c.score.toFixed(6)}`), text("span", `均值 ${c.mean.toFixed(6)} · 最差角度 ${viewNames[c.worst_view] || c.worst_view}`, "muted")); }
  else if (c.status === "incomplete") { $("compare-info").append(text("span", "无完整主分数；投影区域过小：" + c.invalid_views.map(id => viewNames[id] || id).join("、"), "muted")); }
  else { const names = { compatibility: "采集环境", framing_id: "相机构图", view_ids: "视角集合", profile: "采集配置", scene: "场景依赖版本", engine: "引擎版本", gpu: "GPU 或驱动", renderer: "渲染器" }; $("compare-info").append(text("span", "不兼容：" + [...c.reasons, ...(c.changed_settings || [])].map(k => names[k] || k).join("、") + (c.changed_profile_fields?.length ? "（" + c.changed_profile_fields.join("、") + "）" : ""), "muted")); }
  if (c.manual_pair) $("compare-info").append(text("span", "手动资产配对", "badge review"));
  for (const v of c.views) { const e = text("div", viewNames[v.id] || v.id, "score"); e.append(text("strong", v.score == null ? "ROI 过小" : v.score.toFixed(6))); e.title = `全图 ${v.full_score.toFixed(6)} · ROI ${v.roi.join(", ")}`; $("scores").append(e); }
  $("scores").title = `${c.parameters.library} ${c.parameters.version} · RGB mean · Gaussian σ=${c.parameters.sigma} · K1=${c.parameters.K1} · K2=${c.parameters.K2} · data_range=${c.parameters.data_range} · thresholds=${c.thresholds.join(",")}`;
  renderImages();
}
function viewFile(capture) { const view = $("view").value; return capture?.status === "success" ? imageURL(capture.capture_id, view === "sheet" ? "contact_sheet.png" : capture.views.find(v => v.id === view)?.file || "missing.png") : null; }
function setImage(id, url) { const image = $(id); image.hidden = !url; if (url) { image.src = url; image.onerror = () => { image.hidden = true; notice("图片缺失或已删除", true); }; } else image.removeAttribute("src"); }
function renderImages() {
  const left = viewFile(state.left), right = viewFile(state.right), mode = state.mode, c = state.comparison;
  const single = mode !== "side"; $("image-stage").classList.toggle("single", single); $("image-stage").classList.toggle("sheet", $("view").value === "sheet");
  setImage("left-image", left); setImage("right-image", right);
  $("image-stage").querySelector(".slider-stage").hidden = !single; $("split").hidden = mode !== "slider";
  $("slider-clip").hidden = mode !== "slider";
  if (mode === "slider") { setImage("slider-left", left); setImage("slider-right", right); }
  else if (mode === "diff" || mode === "heat") {
    const url = ["success", "incomplete"].includes(c?.status) && $("view").value !== "sheet" ? `/files/comparisons/${c.comparison_id}/${$("view").value}_${mode}.png` : null;
    setImage("slider-left", url); if (!url) notice("差异图需要有效对比结果和单个视角。");
  }
  updateTransform();
}
$("view").onchange = () => { state.zoom = 1; state.pan = [0, 0]; renderImages(); };
for (const id of ["left-image", "right-image"]) $(id).ondblclick = event => {
  if ($("view").value !== "sheet") return;
  const capture = id === "left-image" ? state.left : state.right, batch = state.batches.find(b => b.batch_id === capture?.capture_id.split("/")[0]); if (!batch) return;
  const rect = event.currentTarget.getBoundingClientRect(), scale = Math.min(rect.width / event.currentTarget.naturalWidth, rect.height / event.currentTarget.naturalHeight);
  const x = (event.clientX - rect.left - (rect.width - event.currentTarget.naturalWidth * scale) / 2) / scale;
  const y = (event.clientY - rect.top - (rect.height - event.currentTarget.naturalHeight * scale) / 2) / scale;
  if (x < 0 || y < 0) return;
  const view = capture.views[Math.floor(y / batch.profile.size) * batch.profile.columns + Math.floor(x / batch.profile.size)];
  if (view) { $("view").value = view.id; $("view").onchange(); }
};
document.querySelectorAll("[data-mode]").forEach(e => e.onclick = () => { state.mode = e.dataset.mode; document.querySelectorAll("[data-mode]").forEach(b => b.classList.toggle("active", b === e)); renderImages(); });
$("split").oninput = () => { $("slider-clip").style.clipPath = `inset(0 ${100 - Number($("split").value)}% 0 0)`; };
function updateTransform() { const transform = `translate(${state.pan[0]}px, ${state.pan[1]}px) scale(${state.zoom})`; for (const id of ["left-image", "right-image", "slider-left", "slider-right"]) $(id).style.transform = transform; $("zoom-label").textContent = `${Math.round(state.zoom * 100)}%`; }
$("zoom-in").onclick = () => { state.zoom = Math.min(8, state.zoom * 1.25); updateTransform(); }; $("zoom-out").onclick = () => { state.zoom = Math.max(.25, state.zoom / 1.25); updateTransform(); }; $("zoom-reset").onclick = () => { state.zoom = 1; state.pan = [0, 0]; updateTransform(); };
let dragging = null; $("image-stage").onpointerdown = event => { dragging = [event.clientX, event.clientY, ...state.pan]; $("image-stage").setPointerCapture(event.pointerId); }; $("image-stage").onpointermove = event => { if (dragging) { state.pan = [dragging[2] + event.clientX - dragging[0], dragging[3] + event.clientY - dragging[1]]; updateTransform(); } }; $("image-stage").onpointerup = $("image-stage").onpointercancel = () => { dragging = null; };
$("copy-link").onclick = () => safe(async () => { const params = new URLSearchParams({ left: $("left-asset").value, right: $("right-asset").value, view: $("view").value }); await navigator.clipboard.writeText(location.origin + "/?" + params); notice("对比链接已复制"); });
async function openAsset(asset) { tab("compare"); $("right-batch").value = asset.capture_id.split("/")[0]; assetOptions("right", asset.capture_id); $("use-baseline").checked = true; await loadPair(); if (state.left && state.right?.status === "success") $("run-compare").click(); }
function matchedAssets(batch) {
  const filter = $("result-filter").value, q = $("batch-search").value.toLowerCase();
  const values = batch.assets.filter(a => (!filter || (filter === "failed" ? a.status === "failed" : (a.comparison?.category || a.comparison?.status) === filter)) && (!q || batch.label.toLowerCase().includes(q) || a.path.toLowerCase().includes(q)));
  if ($("result-sort").value === "score") values.sort((a, b) => (a.comparison?.score ?? 2) - (b.comparison?.score ?? 2));
  return values;
}
function historyRows(batch, assets) {
  const details = document.createElement("details"), summary = text("summary", `${assets.length} 条记录`), content = text("div", ""); let page = 0;
  details.append(summary, content);
  const render = () => {
    content.replaceChildren();
    for (const asset of assets.slice(page * 50, (page + 1) * 50)) {
      const row = text("div", "", "history-row"); row.append(text("span", asset.path), text("span", `${labels[asset.comparison?.category || asset.comparison?.status || asset.status] || asset.status}${asset.comparison?.score == null ? "" : " · " + asset.comparison.score.toFixed(6)}`));
      row.tabIndex = 0; row.setAttribute("role", "button"); row.onclick = () => safe(() => openAsset(asset)); row.onkeydown = e => { if (e.key === "Enter") row.click(); }; if (asset.error) row.title = asset.error; content.append(row);
    }
    const pages = Math.max(1, Math.ceil(assets.length / 50)), nav = text("div", "", "pagination");
    const prev = button("←", () => { page--; render(); }), next = button("→", () => { page++; render(); }); prev.title = "上一页"; next.title = "下一页"; prev.disabled = page === 0; next.disabled = page + 1 >= pages;
    nav.append(prev, text("span", `${page + 1} / ${pages}`), next); content.append(nav);
  };
  details.ontoggle = () => { if (details.open && !content.childElementCount) render(); };
  return details;
}
function renderBatches() {
  const q = $("batch-search").value.toLowerCase(), filter = $("batch-status").value;
  const values = state.batches.filter(b => (!filter || b.status === filter) && matchedAssets(b).length);
  if ($("result-sort").value === "score") values.sort((a, b) => Math.min(...matchedAssets(a).map(a => a.comparison?.score ?? 2)) - Math.min(...matchedAssets(b).map(a => a.comparison?.score ?? 2)));
  $("batch-count").textContent = `${values.length} 个批次`; $("batch-list").replaceChildren();
  const pages = Math.max(1, Math.ceil(values.length / 20)); state.batchPage = Math.min(state.batchPage, pages - 1); $("batch-page").textContent = `${state.batchPage + 1} / ${pages}`; $("batch-prev").disabled = state.batchPage === 0; $("batch-next").disabled = state.batchPage + 1 >= pages;
  for (const batch of values.slice(state.batchPage * 20, (state.batchPage + 1) * 20)) {
    const section = document.createElement("article"); section.className = "batch"; const title = document.createElement("div");
    const counts = status => batch.assets.filter(a => a.status === status).length;
    title.append(text("h3", batch.label), text("p", `${localTime(batch.created_at)} · ${counts("success")}/${batch.assets.length} 成功 · ${counts("failed")} 失败 · ${counts("cancelled")} 取消 · ${labels[batch.status] || batch.status}`),
      text("p", `${batch.profile.size}px × ${batch.profile.views.length} 视角 · FOV ${batch.profile.fov} · ${batch.context.actual_concurrency ?? batch.context.requested_concurrency} 并发 · ${Math.round(batch.elapsed_seconds || 0)}s${batch.disk_bytes ? " · " + (batch.disk_bytes / 1024 ** 2).toFixed(1) + " MiB" : ""}`), text("p", batch.batch_id));
    if (batch.error) title.append(text("p", batch.error)); section.append(title);
    const commands = text("div", "", "commands"); commands.append(button("查看", () => openAsset(batch.assets[0])), button("定位", () => api("/api/reveal", { batch_id: batch.batch_id })));
    if (batch.status !== "running" && batch.assets.some(a => ["failed", "cancelled", "interrupted"].includes(a.status))) commands.append(button("重试失败项", async () => { await api("/api/retry", { batch_id: batch.batch_id, concurrency: Number($("concurrency").value) }); tab("capture"); await refresh(); }));
    const remove = button("删除", async () => { const size = await api("/api/batch/size?id=" + encodeURIComponent(batch.batch_id)); $("delete-description").textContent = `${batch.label}，${localTime(batch.created_at)}，${batch.assets.length} 个资产，${(size.bytes / 1024 ** 2).toFixed(1)} MiB。相关对比缓存也会删除。`; const dialog = $("delete-dialog"); dialog.showModal(); const choice = await new Promise(resolve => dialog.addEventListener("close", () => resolve(dialog.returnValue), { once: true })); if (choice === "confirm") { await api("/api/delete", { batch_id: batch.batch_id }); await refresh(); } }); remove.disabled = batch.status === "running"; commands.append(remove); section.append(commands);
    const previews = text("div", "", "batch-assets");
    const assets = matchedAssets(batch);
    for (const asset of assets.slice(0, 5)) { const item = text("div", "", "thumbnail"); if (asset.status === "success") { const image = document.createElement("img"); image.loading = "lazy"; image.src = imageURL(asset.capture_id, "contact_sheet.png"); image.alt = asset.name; item.append(image); } item.append(text("span", `${asset.name} · ${labels[asset.comparison?.category || asset.comparison?.status || asset.status] || asset.status}`)); if (asset.error) item.append(text("span", asset.error)); item.onclick = () => safe(() => openAsset(asset)); previews.append(item); }
    section.append(previews, historyRows(batch, assets)); $("batch-list").append(section);
  }
}
$("batch-search").oninput = () => { state.batchPage = 0; renderBatches(); }; for (const id of ["batch-status", "result-filter", "result-sort"]) $(id).onchange = () => { state.batchPage = 0; renderBatches(); };
$("batch-prev").onclick = () => { state.batchPage--; renderBatches(); }; $("batch-next").onclick = () => { state.batchPage++; renderBatches(); };
async function poll() {
  const value = await api("/api/progress?summary=1"); const wasRunning = state.running; state.running = value.running || Boolean(value.preparation); $("capture-start").disabled = state.running || state.calibration; $("capture-cancel").disabled = !state.running || value.cancelling;
  $("worker-list").replaceChildren();
  if (value.preparation) { const p = value.preparation; const names = { dependencies: "检查资产依赖", baselines: "查找历史基线", scene: "检查采集场景" }; $("progress").max = p.total || 1; $("progress").value = p.completed; $("progress-summary").textContent = `${value.cancelling ? "正在取消" : names[p.phase] || p.phase} · ${p.completed}/${p.total} · ${Math.round(p.elapsed_seconds)}s`; }
  else if (value.running) { $("progress").max = value.total; $("progress").value = value.completed; $("progress-summary").textContent = `${value.cancelling ? "正在取消 · " : value.phase === "finalizing" ? "正在收尾 · " : ""}${value.completed}/${value.total} · ${Math.round(value.elapsed_seconds || 0)}s · 并发 ${value.actual_concurrency} / 上限 ${value.concurrency_limit}`; for (const worker of value.workers || []) $("worker-list").append(text("div", `Worker ${worker.index + 1} · ${workerPhase(worker)} · ${worker.path || ""}`)); const reasons = { "RAM headroom": "内存余量限制", "VRAM headroom": "显存余量限制", "Disk headroom": "磁盘余量限制", "Waiting for worker measurement": "正在测量 Worker 占用", "Image processing backpressure": "正在处理采集图片" }; if (value.concurrency_reason) $("worker-list").append(text("div", reasons[value.concurrency_reason] || value.concurrency_reason, "muted")); }
  else if (wasRunning) { $("progress-summary").textContent = "任务结束"; notice("任务已结束，已完成结果已保留"); await refresh(); }
  const calibration = await api("/api/calibration"); const wasCalibrating = state.calibration; state.calibration = calibration.running; $("calibration-state").textContent = calibration.result?.error || calibration.phase || ""; $("calibrate").disabled = state.running || state.calibration; $("calibrate-cancel").hidden = !state.calibration; $("capture-start").disabled = state.running || state.calibration;
  if (wasCalibrating && !state.calibration && calibration.result?.recommended) $("concurrency").value = calibration.result.recommended;
  if (calibration.result) renderCalibration(calibration.result);
}
function renderCalibration(result) {
  $("calibration-results").replaceChildren();
  if (result.sample_count) $("calibration-results").append(text("p", `本次样本 ${result.sample_count} / 所选 ${result.selection_count} 个资产`, "muted"));
  for (const row of result.results || []) $("calibration-results").append(text("p", `${row.concurrency} 并发 · ${row.median_seconds.toFixed(1)}s · ${row.speedup.toFixed(2)}× · ${row.stable ? "稳定" : "不稳定"}`));
  if (result.resource_limit) $("calibration-results").append(text("p", result.resource_limit, "muted"));
  if (result.status === "success" && result.meets_speedup_target === false) $("calibration-results").append(text("p", "实测未达到 1.3× 参考提速目标", "muted"));
  if (result.recommended) {
    const changed = result.profile?.renderer !== state.renderer || Number(result.profile?.warmup_frames) !== Number($("warmup_frames").value);
    $("recommendation").textContent = changed ? `历史推荐 ${result.recommended} 并发 · 配置已变更` : `推荐 ${result.recommended} 并发`;
    $("recommendation").title = result.created_at;
  }
}
$("calibrate").onclick = () => safe(async () => { if (state.selected.size < 10) throw new Error("校准至少选择 10 个代表资产；正式性能验收使用至少 50 个。"); await api("/api/calibrate", { paths: [...state.selected], profile: profile(), max_workers: Number($("calibration-max").value) }); notice("已启动逐档并发校准"); });
$("calibrate-cancel").onclick = () => safe(() => api("/api/calibrate/cancel", {}));
async function refresh() { const value = await api("/api/batches"); state.batches = value.batches; refreshSelectors(); renderBatches(); }
$("refresh").onclick = () => safe(refresh);
async function init() {
  const value = await api("/api/state"); $("connection").textContent = value.editor.connected ? "UE 已连接" : "独立查看模式"; $("connection").classList.toggle("offline", !value.editor.connected);
  $("hardware").textContent = `${value.machine.cpu_physical} 核 · ${value.machine.memory_gib} GiB · ${value.machine.gpu.name}`;
  $("concurrency").value = value.settings.concurrency; $("output-root").value = value.root;
  for (const id of ["size", "columns", "fov", "padding", "orientation", "warmup_frames", "worker_memory_gib", "scene"]) $(id).value = value.settings.profile[id]; $("hide_floor").checked = value.settings.profile.hide_floor;
  state.renderer = value.settings.profile.renderer;
  if (value.calibration) renderCalibration(value.calibration);
  await refresh();
  const query = new URLSearchParams(location.search); if (query.has("right")) { tab("compare"); $("use-baseline").checked = false; for (const side of ["left", "right"]) { const id = query.get(side); if (id) { $(side + "-batch").value = id.split("/")[0]; assetOptions(side, id); } } await loadPair(); $("view").value = query.get("view") || "sheet"; renderImages(); }
  const schedulePoll = async () => { try { await safe(poll); } finally { setTimeout(schedulePoll, 1500); } }; await schedulePoll();
}
safe(init);
