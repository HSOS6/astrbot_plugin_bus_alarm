/**
 * 公交赶车闹钟 —— Dashboard 插件页面。
 *
 * 通过 AstrBot 注入的 bridge（window.AstrBotPluginView）调用插件后端接口，
 * 接口列表见 webui.py；与 QQ 指令共享同一份订阅数据。
 */

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const state = {
  city: "",
  alarms: [],
  sessions: [],
  calendar: null,
  editId: "",
};

const $ = (selector) => document.querySelector(selector);

/**
 * 校验 bridge 返回的响应：后端返回 `{"status":"error"}` 时抛出异常，
 * 否则原样返回业务数据（AstrBot 已按约定解包，业务数据本身可能带 ok 字段）。
 */
function unwrap(res) {
  if (res && res.status === "error") {
    throw new Error(res.message || "请求失败");
  }
  return res;
}

/** 调用 GET 接口。 */
async function api(endpoint, params) {
  return unwrap(await bridge.apiGet(endpoint, params || {}));
}

/** 调用 POST 接口。 */
async function post(endpoint, body) {
  return unwrap(await bridge.apiPost(endpoint, body || {}));
}

let toastTimer = 0;

function toast(message, isError) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.toggle("error", Boolean(isError));
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, isError ? 5000 : 2600);
}

/** 统一的按钮动作包装：禁用按钮、捕获异常、显示提示。 */
async function guard(button, action) {
  const label = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = "处理中…";
  }
  try {
    await action();
  } catch (error) {
    toast(error.message || String(error), true);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

function text(node, value) {
  if (node) {
    node.textContent = value;
  }
}

function chip(value, extraClass) {
  const span = document.createElement("span");
  span.className = "chip static" + (extraClass ? " " + extraClass : "");
  span.textContent = value;
  return span;
}

/* ---------------- 订阅列表 ---------------- */

function renderStats(stats, defaultCity) {
  const box = $("#stats");
  box.textContent = "";
  const push = (value) => box.appendChild(chip(value));
  push(`默认城市 ${defaultCity}`);
  push(`订阅 ${stats.total} 条`);
  push(`启用 ${stats.enabled} 条`);
  if (stats.cities.length) {
    push(`涉及城市 ${stats.cities.join("、")}`);
  }
}

function renderAlarms() {
  const list = $("#alarm-list");
  list.textContent = "";
  if (!state.alarms.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "还没有订阅，去「新建 / 编辑」加一条吧。";
    list.appendChild(empty);
    return;
  }

  for (const item of state.alarms) {
    const card = document.createElement("div");
    card.className = "alarm" + (item.enabled ? "" : " paused");

    const head = document.createElement("div");
    head.className = "alarm-head";
    const time = document.createElement("span");
    time.className = "alarm-time";
    time.textContent = item.time;
    head.appendChild(time);

    const badges = document.createElement("span");
    badges.className = "row gap";
    const stateBadge = document.createElement("span");
    stateBadge.className = "badge " + (item.enabled ? "ok" : "off");
    stateBadge.textContent = item.enabled ? "已启用" : "已暂停";
    badges.appendChild(stateBadge);
    if (item.dueToday) {
      const dueBadge = document.createElement("span");
      dueBadge.className = "badge today";
      dueBadge.textContent = "今天生效";
      badges.appendChild(dueBadge);
    }
    head.appendChild(badges);
    card.appendChild(head);

    const line = document.createElement("p");
    line.className = "alarm-line";
    line.textContent = `[${item.city}] ${item.lineName}（${item.directionLabel} · 往 ${
      item.directionTo || "?"
    }）`;
    card.appendChild(line);

    const meta = document.createElement("p");
    meta.className = "alarm-meta";
    meta.textContent = `${item.repeatText} · 候车：${item.stationName}（第 ${
      item.stationOrder
    } 站）· 显示 ${item.topN} 辆 · [${item.id}]`;
    card.appendChild(meta);

    const target = document.createElement("p");
    target.className = "alarm-meta";
    const owner = item.ownerName || item.ownerId || "";
    target.textContent =
      `推送至 ${item.umo || "（未设置）"}` +
      (owner ? ` · 创建者 ${owner}` : " · 创建者（同会话共管）");
    card.appendChild(target);

    const actions = document.createElement("div");
    actions.className = "alarm-actions";

    const toggle = document.createElement("button");
    toggle.className = "ghost small";
    toggle.textContent = item.enabled ? "暂停" : "启用";
    toggle.addEventListener("click", () =>
      guard(toggle, async () => {
        await post("alarm/toggle", { id: item.id });
        toast(`${item.time} 已${item.enabled ? "暂停" : "启用"}`);
        await reload();
      })
    );
    actions.appendChild(toggle);

    const now = document.createElement("button");
    now.className = "ghost small";
    now.textContent = "立即查询";
    now.addEventListener("click", () =>
      guard(now, async () => {
        $("#rt-picker").value = item.id;
        switchTab("realtime");
        await queryRealtime();
      })
    );
    actions.appendChild(now);

    const push = document.createElement("button");
    push.className = "ghost small";
    push.textContent = "试推送";
    push.addEventListener("click", () =>
      guard(push, async () => {
        const res = await post("alarm/push", { id: item.id });
        toast(res.sent ? "已推送到目标会话" : `推送失败：${res.error || "未知原因"}`, !res.sent);
      })
    );
    actions.appendChild(push);

    const edit = document.createElement("button");
    edit.className = "ghost small";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => fillEditor(item));
    actions.appendChild(edit);

    const del = document.createElement("button");
    del.className = "ghost small";
    del.textContent = "删除";
    del.addEventListener("click", () =>
      guard(del, async () => {
        if (!confirm(`确定删除 ${item.time} 的这条订阅？`)) {
          return;
        }
        await post("alarm/delete", { id: item.id });
        if (state.editId === item.id) {
          clearEditor();
        }
        toast("已删除");
        await reload();
      })
    );
    actions.appendChild(del);

    card.appendChild(actions);
    list.appendChild(card);
  }
}

/* ---------------- 表单 ---------------- */

function fillEditor(item) {
  state.editId = item.id;
  $("#f-city").value = item.city;
  $("#f-line").value = item.lineName;
  $("#f-direction").value = String(item.direction);
  $("#f-station").value = item.stationName;
  $("#f-time").value = item.time;
  $("#f-topn").value = String(item.topN);
  $("#f-umo").value = item.umo;
  $("#f-owner-id").value = item.ownerId || "";
  $("#f-owner-name").value = item.ownerName || "";

  const presetValues = [...$("#f-repeat").options].map((option) => option.value);
  const preset = presetValues.includes(item.repeatRaw) && item.repeatRaw !== "custom";
  $("#f-repeat").value = preset ? item.repeatRaw : "custom";
  $("#f-repeat-custom").value = preset ? "" : item.repeatRaw;
  $("#f-repeat-custom").style.display = preset ? "none" : "";

  text($("#editor-title"), `编辑订阅 [${item.id}]`);
  text($("#editor-msg"), "");
  switchTab("editor");
  loadRoute();
}

function clearEditor() {
  state.editId = "";
  $("#f-line").value = "";
  $("#f-station").value = "";
  $("#f-repeat-custom").value = "";
  $("#f-repeat-custom").style.display = "none";
  $("#f-repeat").value = "每天";
  $("#f-time").value = "07:30";
  $("#f-topn").value = "3";
  $("#f-umo").value = "";
  $("#f-owner-id").value = "";
  $("#f-owner-name").value = "";
  $("#route-card").hidden = true;
  text($("#editor-title"), "新建订阅");
  text($("#editor-msg"), "");
  text($("#station-hint"), "");
}

function repeatValue() {
  const preset = $("#f-repeat").value;
  if (preset !== "custom") {
    return preset;
  }
  const custom = $("#f-repeat-custom").value.trim();
  return custom || "每天";
}

function formPayload() {
  const umo = $("#f-umo").value.trim() || state.sessions[0] || "";
  return {
    umo,
    city: $("#f-city").value.trim() || state.city,
    line_name: $("#f-line").value.trim(),
    direction: $("#f-direction").value,
    station: $("#f-station").value.trim(),
    time: $("#f-time").value,
    repeat: repeatValue(),
    top_n: $("#f-topn").value,
    owner_id: $("#f-owner-id").value.trim(),
    owner_name: $("#f-owner-name").value.trim(),
  };
}

async function saveAlarm(button) {
  const payload = formPayload();
  if (!payload.line_name || !payload.station) {
    throw new Error("线路和候车站都要填");
  }
  if (!payload.umo) {
    throw new Error("请填写推送目标会话（可以先在已有订阅里找一条复制过来）");
  }
  if (state.editId) {
    await post("alarm/update", { id: state.editId, ...payload });
    toast("已保存修改");
  } else {
    await post("alarm/create", payload);
    toast("订阅已创建");
    clearEditor();
  }
  await reload();
}

async function loadRoute() {
  const city = $("#f-city").value.trim() || state.city;
  const line = $("#f-line").value.trim();
  const direction = Number($("#f-direction").value);
  if (!line) {
    $("#route-card").hidden = true;
    return;
  }
  try {
    const data = await api("route", { city, line_name: line, direction });
    $("#route-card").hidden = false;
    text(
      $("#route-title"),
      `[${data.city}] ${data.line_name}（${data.directionLabel}）${data.from_station} → ${data.to_station} · 首班 ${data.first_time || "?"} / 末班 ${data.last_time || "?"}`
    );
    text($("#station-hint"), `共 ${data.stations.length} 站，点站点可直接填入`);
    const box = $("#route-stations");
    box.textContent = "";
    for (const station of data.stations) {
      const node = document.createElement("span");
      node.className = "chip" + (station.running ? "" : " hot");
      node.textContent = `${station.order}. ${station.name}`;
      node.addEventListener("click", () => {
        $("#f-station").value = station.name;
        toast(`已填入第 ${station.order} 站：${station.name}`);
      });
      box.appendChild(node);
    }
  } catch (error) {
    $("#route-card").hidden = true;
    text($("#station-hint"), error.message);
  }
}

/* ---------------- 实时到站 ---------------- */

function renderArrivals(items, meta) {
  const box = $("#rt-arrivals");
  box.textContent = "";
  text($("#rt-meta"), meta || "");
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "暂时没有查到车辆";
    box.appendChild(empty);
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "arrival";

    const left = document.createElement("div");
    const plate = document.createElement("div");
    plate.className = "plate";
    plate.textContent = item.plate;
    const where = document.createElement("div");
    where.className = "alarm-meta";
    where.textContent = `${item.currentStation || "—"} · 距离 ${item.distanceM} 米${
      item.stopped ? " · 停靠中" : ""
    }`;
    left.appendChild(plate);
    left.appendChild(where);

    const right = document.createElement("div");
    right.style.textAlign = "right";
    const big = document.createElement("div");
    big.className = "big";
    big.textContent = item.stopsText;
    const eta = document.createElement("div");
    eta.className = "alarm-meta";
    eta.textContent = `约 ${item.etaText}`;
    right.appendChild(big);
    right.appendChild(eta);

    row.appendChild(left);
    row.appendChild(right);
    box.appendChild(row);
  }
}

async function queryRealtime() {
  const id = $("#rt-picker").value;
  const target = state.alarms.find((item) => item.id === id);
  if (!target) {
    throw new Error("请先选择一条订阅");
  }
  const data = await api("realtime", {
    city: target.city,
    line_name: target.lineName,
    direction: target.direction,
    direction_to: target.directionTo,
    station_order: target.stationOrder,
    station_name: target.stationName,
    top_n: target.topN,
  });
  showRealtime(data, target.stationName, target.lineName);
}

/** 把一次实时查询结果渲染到「实时到站」标签。 */
function showRealtime(data, stationName, lineName) {
  const meta = `[${data.city}] ${lineName} · 候车：${stationName} · ${
    data.dataTime ? "数据时间 " + data.dataTime : "无数据时间"
  }${data.planTime ? " · " + data.planTime : ""}`;
  switchTab("realtime");
  renderArrivals(data.arrivals, data.ok ? meta : `${meta} · ${data.reason || "查询失败"}`);
}

/* ---------------- 线路地图 ---------------- */

async function loadMap() {
  const id = $("#map-picker").value;
  const target = state.alarms.find((item) => item.id === id);
  if (!target) {
    throw new Error("请先选择一条订阅");
  }
  const data = await api("map", {
    city: target.city,
    line_name: target.lineName,
    direction: target.direction,
    direction_to: target.directionTo,
    station_order: target.stationOrder,
    station_name: target.stationName,
    top_n: target.topN,
  });
  const image = $("#map-image");
  image.src = data.image;
  image.hidden = false;
  $("#map-empty").hidden = true;
  text(
    $("#map-meta"),
    `[${data.city}] ${data.lineName} · 往 ${data.directionTo || "?"} · 候车：${
      data.stationName
    }（第 ${data.stationOrder} 站）· 在途 ${data.busCount} 辆 · 数据时间 ${
      data.dataTime || "—"
    }`
  );
}

/* ---------------- 节假日 ---------------- */

function renderCalendar(calendar) {
  state.calendar = calendar;
  text($("#cal-desc"), calendar.describe || "在线节假日数据已关闭，仅按周末判断");
  text($("#cal-today"), `今天 ${calendar.today} · ${calendar.todayState}`);

  const holidays = $("#cal-holidays");
  const workdays = $("#cal-workdays");
  holidays.textContent = "";
  workdays.textContent = "";
  for (const day of calendar.upcomingHolidays) {
    holidays.appendChild(chip(day, "hot"));
  }
  for (const day of calendar.upcomingWorkdays) {
    workdays.appendChild(chip(day));
  }
  if (!calendar.upcomingHolidays.length) {
    holidays.appendChild(chip("暂无数据"));
  }
  if (!calendar.upcomingWorkdays.length) {
    workdays.appendChild(chip("暂无数据"));
  }

  const manual = [];
  if (calendar.manualHolidays.length) {
    manual.push(`手填放假 ${calendar.manualHolidays.join("、")}`);
  }
  if (calendar.manualWorkdays.length) {
    manual.push(`手填补班 ${calendar.manualWorkdays.join("、")}`);
  }
  text(
    $("#cal-manual"),
    `${manual.length ? manual.join("；") : "没有手填补充日期"}。共收录 ${
      calendar.holidays.length
    } 个放假日、${calendar.workdays.length} 个补班日。`
  );
}

/* ---------------- 数据加载 ---------------- */

/** 用当前订阅列表填充一个下拉框，尽量保留原先选中的那一条。 */
function fillAlarmPicker(selector) {
  const picker = $(selector);
  const previous = picker.value;
  picker.textContent = "";
  for (const item of state.alarms) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.time} · [${item.city}] ${item.lineName} · ${item.stationName}`;
    picker.appendChild(option);
  }
  if (previous && state.alarms.some((item) => item.id === previous)) {
    picker.value = previous;
  }
}

function renderSelectors() {
  fillAlarmPicker("#rt-picker");
  fillAlarmPicker("#map-picker");

  const umoPicker = $("#umo-picker");
  umoPicker.textContent = "";
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = state.sessions.length ? "选择已有会话" : "暂无已有会话";
  umoPicker.appendChild(placeholder);
  for (const umo of state.sessions) {
    const option = document.createElement("option");
    option.value = umo;
    option.textContent = umo;
    umoPicker.appendChild(option);
  }
}

async function reload() {
  const data = await api("overview");
  state.city = data.defaultCity;
  state.alarms = data.alarms;
  state.sessions = data.sessions;
  text($("#subtitle"), `插件 ${data.pluginName} · 默认城市 ${data.defaultCity}`);
  renderStats(data.stats, data.defaultCity);
  renderAlarms();
  renderSelectors();
  renderCalendar(data.calendar);
  text(
    $("#today-pill"),
    `${data.calendar.today} · ${data.calendar.todayState}`
  );
}

/* ---------------- 交互绑定 ---------------- */

function switchTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".panel")) {
    panel.classList.toggle("active", panel.dataset.panel === name);
  }
}

function bind() {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  }
  for (const link of document.querySelectorAll("[data-goto]")) {
    link.addEventListener("click", () => switchTab(link.dataset.goto));
  }

  $("#refresh-all").addEventListener("click", (event) =>
    guard(event.currentTarget, async () => {
      await reload();
      toast("已刷新");
    })
  );

  $("#btn-lines").addEventListener("click", (event) =>
    guard(event.currentTarget, async () => {
      const keyword = $("#f-line").value.trim();
      if (!keyword) {
        throw new Error("先输入线路关键词，例如 1路");
      }
      const data = await api("lines", {
        q: keyword,
        city: $("#f-city").value.trim() || state.city,
      });
      const list = $("#line-list");
      list.textContent = "";
      for (const line of data.lines) {
        const option = document.createElement("option");
        option.value = line.line_name;
        option.label = `方向${line.direction} ${line.from} → ${line.to}`;
        list.appendChild(option);
      }
      toast(`[${data.city}] 找到 ${data.total} 条线路，下拉可选`);
      await loadRoute();
    })
  );

  $("#btn-cities").addEventListener("click", (event) =>
    guard(event.currentTarget, async () => {
      const keyword = $("#f-city").value.trim();
      const data = await api("cities", { q: keyword });
      const list = $("#city-list");
      list.textContent = "";
      for (const city of data.cities) {
        const option = document.createElement("option");
        option.value = city;
        list.appendChild(option);
      }
      toast(`共 ${data.total} 个城市匹配，输入框下拉可选`);
    })
  );

  $("#f-line").addEventListener("change", () => loadRoute());
  $("#f-direction").addEventListener("change", () => loadRoute());

  $("#btn-save").addEventListener("click", (event) =>
    guard(event.currentTarget, () => saveAlarm())
  );
  $("#btn-cancel").addEventListener("click", () => clearEditor());

  $("#btn-preview").addEventListener("click", (event) =>
    guard(event.currentTarget, async () => {
      const payload = formPayload();
      if (!payload.line_name || !payload.station) {
        throw new Error("线路和候车站都要填");
      }
      const data = await api("realtime", {
        city: payload.city,
        line_name: payload.line_name,
        direction: payload.direction,
        station: payload.station,
        top_n: payload.top_n,
      });
      showRealtime(data, payload.station, payload.line_name);
      toast("试查完成");
    })
  );

  $("#btn-rt-query").addEventListener("click", (event) =>
    guard(event.currentTarget, () => queryRealtime())
  );
  $("#btn-map-load").addEventListener("click", (event) =>
    guard(event.currentTarget, () => loadMap())
  );
  $("#btn-rt-push").addEventListener("click", (event) =>
    guard(event.currentTarget, async () => {
      const id = $("#rt-picker").value;
      if (!id) {
        throw new Error("请先选择一条订阅");
      }
      const res = await post("alarm/push", { id });
      renderArrivals(res.arrivals, res.text);
      toast(res.sent ? "已推送到目标会话" : `推送失败：${res.error || "未知原因"}`, !res.sent);
    })
  );

  $("#btn-cal-refresh").addEventListener("click", (event) =>
    guard(event.currentTarget, async () => {
      const data = await post("calendar/refresh", {});
      renderCalendar(data.calendar);
      text($("#today-pill"), `${data.calendar.today} · ${data.calendar.todayState}`);
      toast("节假日数据已更新");
    })
  );

  $("#f-repeat").addEventListener("change", () => {
    const custom = $("#f-repeat-custom");
    custom.style.display = $("#f-repeat").value === "custom" ? "" : "none";
  });
  $("#f-repeat-custom").style.display = "none";
  $("#f-city").addEventListener("change", () => loadRoute());
  $("#umo-picker").addEventListener("change", () => {
    if ($("#umo-picker").value) {
      $("#f-umo").value = $("#umo-picker").value;
    }
  });
}

async function main() {
  if (!bridge || typeof bridge.ready !== "function") {
    text($("#subtitle"), "未检测到 AstrBot 页面桥接，请从 AstrBot WebUI 的插件卡片进入本页");
    return;
  }
  const context = await bridge.ready();
  bind();
  try {
    await reload();
  } catch (error) {
    toast(error.message || String(error), true);
    text($("#subtitle"), "加载失败，请检查插件是否已启用");
  }
  if (context && context.displayName) {
    document.title = context.displayName;
  }
}

main();
