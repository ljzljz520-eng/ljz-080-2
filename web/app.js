const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const labels = {
  air_conditioner: '空调', water_heater: '热水器', solar_pv: '光伏', water_tank: '蓄水',
};
const statusText = {
  ready: '已就绪', precooling: '预冷中', not_ready: '未预冷', scheduled: '等待中',
  overdue: '已到店未处理', checked_in: '在住', no_ac: '缺少空调', offline: '设备离线',
  waiting: '等待预冷', arrival_passed: '已到入住时间', normal: '正常', low: '偏低',
  critical: '告急', unknown: '未知', good: '良好',
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}

function toast(message, isError = false) {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast${isError ? ' error' : ''}`;
  el.hidden = false;
  setTimeout(() => { el.hidden = true; }, 2800);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, s => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[s]));
}
const yuan = value => `¥${Number(value || 0).toFixed(2)}`;
const badge = value => `<span class="badge ${esc(value)}">${esc(statusText[value] || value)}</span>`;

function setClock() {
  $('#clock').textContent = new Date().toLocaleString('zh-CN', { hour12: false });
}

async function loadBoard() {
  const [overview, arrivals, anomalies, tanks] = await Promise.all([
    api('/api/board/overview'),
    api('/api/board/arrivals?hours=24'),
    api('/api/anomalies?active=1'),
    api('/api/board/tanks'),
  ]);
  $('#kpis').innerHTML = [
    ['24小时到店', overview.arrivals_24h, '间/组订单', ''],
    ['未预冷', overview.precool.not_ready + overview.precool.overdue, '需立即处理', overview.precool.not_ready ? 'alert' : 'good'],
    ['设备异常', overview.open_anomalies, `${overview.critical_anomalies} 条严重`, overview.critical_anomalies ? 'alert' : 'good'],
    ['蓄水告警', overview.tank_alerts, '栋房源', overview.tank_alerts ? 'warn' : 'good'],
    ['在线设备', overview.devices, '台已接入', ''],
  ].map(([label, value, sub, cls]) => `<div class="kpi ${cls}"><div class="label">${label}</div><div class="value">${value}</div><div class="meta">${sub}</div></div>`).join('');

  $('#arrivals').innerHTML = arrivals.map(a => `
    <div class="item">
      <div class="item-title"><span>${esc(a.house_name)} · ${esc(a.room_name)} <span class="meta">${esc(a.guest_name)} / ${a.guest_count}人</span></span>${badge(a.precool_status)}</div>
      <div class="meta">入住：${new Date(a.check_in_at).toLocaleString('zh-CN', { hour12: false })}（${a.minutes_to_arrival}分钟）；目标 ${a.target_temperature}℃</div>
      <div class="meta">${a.air_conditioners.map(ac => `${esc(ac.device_name)}：${esc(statusText[ac.status] || ac.status)} ${ac.room_temperature ? ac.room_temperature + '℃' : ''} ${ac.power_kw ? ac.power_kw + 'kW' : ''}`).join(' · ') || '未绑定空调'}</div>
      ${['not_ready', 'precooling'].includes(a.precool_status) ? `<button class="btn small" onclick="precool(${a.booking_id})">${a.precool_status === 'precooling' ? '继续预冷/确认指令' : '立即预冷'}</button>` : ''}
    </div>`).join('') || '<p class="meta">24小时内暂无到店订单</p>';

  $('#anomalies').innerHTML = anomalies.map(x => `
    <div class="item">
      <div class="item-title"><span>${esc(x.house_name)} · ${esc(x.room_name || '整栋')} · ${esc(x.device_name)}</span>${badge(x.severity)}</div>
      <div class="meta">${esc(x.message)}</div>
      <div class="meta">最近：${new Date(x.last_seen).toLocaleString('zh-CN', { hour12: false })}</div>
      <button class="btn small ghost" onclick="ack(${x.id})">已知晓并关闭</button>
    </div>`).join('') || '<p class="item meta">暂无未处理异常</p>';

  $('#tanks').innerHTML = tanks.map(t => `
    <div class="tank">
      <div class="item-title"><span>${esc(t.house_name)} · ${esc(t.device_name)}</span>${badge(t.state)}</div>
      <div class="tank-track"><div class="tank-fill ${t.state === 'normal' ? '' : 'low'}" style="width:${t.level_percent || 0}%"></div></div>
      <div class="meta">液位 ${t.level_percent ?? '--'}% · 可用 ${t.available_liters ?? '--'}L / 容量 ${t.capacity_liters || '--'}L</div>
    </div>`).join('') || '<p class="meta">暂无蓄水设备</p>';
}

async function precool(bookingId) {
  try {
    const result = await api(`/api/bookings/${bookingId}/precool`, { method: 'POST', body: {} });
    toast(result.message || '预冷指令已下发');
    await loadBoard();
  } catch (err) { toast(err.message, true); }
}
window.precool = precool;

async function ack(anomalyId) {
  try {
    await api(`/api/anomalies/${anomalyId}/ack`, { method: 'POST', body: { note: '管家已处理' } });
    toast('异常已关闭');
    await loadBoard();
  } catch (err) { toast(err.message, true); }
}
window.ack = ack;

function latestSummary(device) {
  const r = device.latest || {};
  return [
    r.power_kw != null ? `功率 ${r.power_kw}kW` : '',
    r.energy_kwh != null ? `累计 ${r.energy_kwh}kWh` : '',
    r.room_temperature != null ? `室温 ${r.room_temperature}℃` : '',
    r.set_temperature != null ? `设定 ${r.set_temperature}℃` : '',
    r.water_temperature != null ? `水温 ${r.water_temperature}℃` : '',
    r.tank_level_percent != null ? `液位 ${r.tank_level_percent}%` : '',
    r.solar_generation_kw != null ? `发电 ${r.solar_generation_kw}kW` : '',
    r.water_liters != null ? `累计水 ${r.water_liters}L` : '',
  ].filter(Boolean).join(' · ') || '暂无遥测';
}

async function loadDevices() {
  const [houses, devices] = await Promise.all([api('/api/houses'), api('/api/devices')]);
  const filter = $('#deviceHouseFilter');
  const current = filter.value;
  filter.innerHTML = '<option value="">全部房源</option>' + houses.map(h => `<option value="${h.id}">${esc(h.name)}</option>`).join('');
  filter.value = current;
  const filtered = filter.value ? devices.filter(d => d.house_id === Number(filter.value)) : devices;
  $('#devicesBody').innerHTML = filtered.map(d => `
    <tr>
      <td>${esc(d.house_name)}<br><span class="meta">${esc(d.room_name || '整栋共享')}</span></td>
      <td><strong>${esc(d.name)}</strong><br><span class="meta">ID ${d.id}</span></td>
      <td>${labels[d.type]}</td>
      <td><span class="dot ${d.online ? 'on' : 'off'}"></span>${d.online ? '在线' : '离线'}</td>
      <td class="meta">${esc(latestSummary(d))}</td>
      <td class="meta">${d.latest ? new Date(d.latest.ts).toLocaleString('zh-CN', { hour12: false }) : '--'}</td>
    </tr>`).join('');
}

function renderBills(bills) {
  $('#bills').innerHTML = bills.map(b => `
    <div class="card bill-card" onclick="showBill(${b.id})">
      <div class="item-title"><span>${esc(b.house_name)} · ${esc(b.room_name)}</span><span class="badge good">${esc(b.guest_name)}</span></div>
      <p class="meta">${new Date(b.check_in_at).toLocaleDateString()} - ${new Date(b.check_out_at).toLocaleDateString()}</p>
      <div class="money">${yuan(b.total_fee)}</div>
      <p class="meta">电 ${b.electricity_kwh}kWh · 光伏抵扣 ${b.pv_self_consumed_kwh}kWh<br>水 ${b.water_liters}L · 已省 ${yuan(b.saved_fee)}</p>
    </div>`).join('') || '<p class="meta">暂无账单，可在上方输入已离店订单ID生成</p>';
}

async function loadBills() {
  renderBills(await api('/api/bills'));
}

async function checkoutNow() {
  const id = Number($('#checkoutBookingId').value);
  if (!id) return toast('请输入订单ID', true);
  try {
    await api(`/api/bookings/${id}/checkout`, { method: 'POST', body: {} });
    toast('账单已生成，点击卡片查看节能建议');
    await loadBills();
  } catch (err) { toast(err.message, true); }
}

async function showBill(id) {
  const b = await api(`/api/bills/${id}`);
  $('#billDetail').innerHTML = `
    <p class="eyebrow">Energy Bill</p><h2>${esc(b.house_name)} · ${esc(b.room_name)}</h2>
    <p class="meta">${esc(b.guest_name)} · ${new Date(b.check_in_at).toLocaleString()} 至 ${new Date(b.check_out_at).toLocaleString()}</p>
    <div class="kpi-grid compact">
      <div class="kpi"><div class="label">电费</div><div class="value">${yuan(b.electricity_fee)}</div></div>
      <div class="kpi"><div class="label">水费</div><div class="value">${yuan(b.water_fee)}</div></div>
      <div class="kpi good"><div class="label">总计</div><div class="value">${yuan(b.total_fee)}</div></div>
      <div class="kpi warn"><div class="label">光伏已省</div><div class="value">${yuan(b.saved_fee)}</div></div>
    </div>
    <p>用电 ${b.electricity_kwh}kWh，其中自发自用 ${b.pv_self_consumed_kwh}kWh；用水 ${b.water_liters}L。</p>
    <h3>节能建议</h3>${(b.recommendations || []).map(r => `<div class="reco"><strong>${esc(r.title)}</strong><p class="meta">${esc(r.detail)}</p></div>`).join('')}
    <h3>分设备用量</h3><div class="table-wrap"><table><thead><tr><th>设备</th><th>类型</th><th>电kWh</th><th>水L</th><th>光伏kWh</th><th>点位数</th></tr></thead>
    <tbody>${(b.details || []).map(d => `<tr><td>${esc(d.device_name)}</td><td>${labels[d.type]}</td><td>${d.electricity_kwh}</td><td>${d.water_liters}</td><td>${d.solar_generated_kwh}</td><td>${d.reading_count}</td></tr>`).join('')}</tbody></table></div>`;
  $('#billModal').hidden = false;
}
window.showBill = showBill;

async function loadReport() {
  const now = new Date();
  $('#reportYear').value = $('#reportYear').value || now.getFullYear();
  $('#reportMonth').value = $('#reportMonth').value || (now.getMonth() + 1);
  const report = await api(`/api/reports/monthly?year=${$('#reportYear').value}&month=${$('#reportMonth').value}`);
  $('#reportTotals').innerHTML = [
    ['账单数', report.totals.bills], ['总用电', `${report.totals.electricity_kwh}kWh`],
    ['总用水', `${report.totals.water_liters}L`], ['光伏自用', `${report.totals.pv_self_consumed_kwh}kWh`],
  ].map(([label, value]) => `<div class="kpi"><div class="label">${label}</div><div class="value">${value}</div></div>`).join('');
  $('#houseReport').innerHTML = `<div class="card"><h2>房源排行（按人均晚用电）</h2>${report.houses.map((h, i) => `
    <div class="item"><div class="item-title"><span>${i + 1}. ${esc(h.house_name)}</span><span class="badge ${i === 0 ? 'high' : 'good'}">${h.kwh_per_guest_night} kWh/人晚</span></div>
    <div class="meta">用电 ${h.electricity_kwh}kWh · 用水 ${h.water_liters}L（${h.liters_per_guest_night}L/人晚）<br>账单 ${h.bills} · 光伏 ${h.pv_self_consumed_kwh}kWh · 费用 ${yuan(h.total_fee)} · 环比 ${h.month_over_month_change_percent ?? '--'}%</div></div>`).join('') || '<p class="meta">本月暂无离店账单</p>'}</div>`;
  $('#roomReport').innerHTML = `<div class="card"><h2>房间明细</h2>${report.rooms.map(r => `
    <div class="item"><div class="item-title"><span>${esc(r.house_name)} · ${esc(r.room_name)}</span><span>${r.kwh_per_guest_night}kWh/人晚</span></div>
    <div class="meta">电 ${r.electricity_kwh}kWh · 水 ${r.water_liters}L · 账单 ${r.bills}</div></div>`).join('') || '<p class="meta">暂无房间数据</p>'}</div>`;
}

async function loadOnboarding() {
  const houses = await api('/api/houses');
  $$('#roomForm select[name=house_id],#deviceForm select[name=house_id]').forEach(select => {
    select.innerHTML = houses.map(h => `<option value="${h.id}">${esc(h.name)}</option>`).join('');
  });
  syncDeviceRooms();
}

async function syncDeviceRooms() {
  const houseId = $('#deviceForm select[name=house_id]').value;
  if (!houseId) return;
  const rooms = await api(`/api/rooms?house_id=${houseId}`);
  const roomSelect = $('#deviceForm select[name=room_id]');
  const value = roomSelect.value;
  roomSelect.innerHTML = '<option value="">整栋共享设备（光伏/蓄水）</option>' + rooms.map(r => `<option value="${r.id}">${esc(r.name)}</option>`).join('');
  roomSelect.value = value;
}

function formJson(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function bindForms() {
  $('#houseForm').addEventListener('submit', async e => {
    e.preventDefault();
    try { await api('/api/houses', { method: 'POST', body: formJson(e.target) }); $('.form-result', e.target).textContent = '房源已创建'; e.target.reset(); await loadOnboarding(); toast('房源已创建'); }
    catch (err) { toast(err.message, true); }
  });
  $('#roomForm').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const data = formJson(e.target);
      await api(`/api/houses/${data.house_id}/rooms`, { method: 'POST', body: data });
      $('.form-result', e.target).textContent = '房间已创建'; toast('房间已创建');
    } catch (err) { toast(err.message, true); }
  });
  $('#deviceForm').addEventListener('submit', async e => {
    e.preventDefault();
    try {
      const data = formJson(e.target);
      if (!data.room_id) delete data.room_id;
      if (data.type === 'solar_pv') { data.pv_capacity_kwp = data.capacity_liters; delete data.capacity_liters; }
      if (!data.rated_power_kw) delete data.rated_power_kw;
      if (!data.capacity_liters) delete data.capacity_liters;
      await api('/api/devices', { method: 'POST', body: data });
      $('.form-result', e.target).textContent = '设备已绑定'; toast('设备已绑定'); await loadDevices();
    } catch (err) { toast(err.message, true); }
  });
  $('#deviceForm select[name=house_id]').addEventListener('change', syncDeviceRooms);
  $('#deviceForm select[name=type]').addEventListener('change', e => {
    const isShared = ['solar_pv', 'water_tank'].includes(e.target.value);
    $('#deviceForm select[name=room_id]').disabled = isShared;
  });
}

$$('.tab').forEach(tab => tab.addEventListener('click', async () => {
  $$('.tab').forEach(t => t.classList.remove('active'));
  $$('.tab-panel').forEach(p => p.classList.remove('active'));
  tab.classList.add('active');
  $(`#tab-${tab.dataset.tab}`).classList.add('active');
  try {
    if (tab.dataset.tab === 'board') await loadBoard();
    if (tab.dataset.tab === 'devices') await loadDevices();
    if (tab.dataset.tab === 'bills') await loadBills();
    if (tab.dataset.tab === 'reports') await loadReport();
    if (tab.dataset.tab === 'onboarding') await loadOnboarding();
  } catch (err) { toast(err.message, true); }
}));

$('#refreshBtn').addEventListener('click', () => loadBoard().then(() => toast('看板已刷新')).catch(err => toast(err.message, true)));
$('#checkoutBtn').addEventListener('click', checkoutNow);
$('#reportBtn').addEventListener('click', () => loadReport().catch(err => toast(err.message, true)));
$('#closeBill').addEventListener('click', () => { $('#billModal').hidden = true; });
$('#billModal').addEventListener('click', e => { if (e.target.id === 'billModal') e.currentTarget.hidden = true; });
$('#deviceHouseFilter').addEventListener('change', loadDevices);

setClock();
setInterval(setClock, 1000);
bindForms();
loadBoard().catch(err => toast(err.message, true));
loadOnboarding().catch(() => {});
