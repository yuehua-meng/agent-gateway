// Cards, SVG trends and attempt timelines adapted from TextAgent observe.js.
const $ = id => document.getElementById(id);
const state = {key: '', page: 'overview', offset: 0, analytics: null, detail: null, selected: null, revision: 0, controller: null};
const names = {succeeded: '成功', failed: '失败', running: '运行中', unknown: '待核对'};
const num = value => Number(value || 0).toLocaleString('zh-CN');
const duration = value => value == null ? '—' : value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(2)} s`;
const date = value => value == null ? '—' : new Date(value * 1000).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false});
const tokens = u => u.unknown_usage_calls && !u.known_token_calls && !(u.input_tokens + u.output_tokens)
  ? '未知' : `${num(u.input_tokens + u.output_tokens)}${u.unknown_usage_calls ? '（部分缺失）' : ''}`;
const money = costs => Object.entries(costs).map(([currency, cost]) => `${currency} ${cost.toFixed(6)}`).join(' / ') || '—';
const detailTokens = (u, key) => !u[key + '_known_calls'] ? '未知 / 未返回' : `${num(u[key + '_tokens'])}${u[key + '_unknown_calls'] ? '（部分未返回）' : ''}`;
function el(tag, cls = '', text) { const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function notice(text = '') { $('notice').textContent = text; $('notice').hidden = !text; }
function empty(text = '暂无匹配记录') { const n = el('div', 'empty'); n.append(el('span', 'empty-icon', '◎'), el('h3', '', text), el('p', '', '调整筛选范围，或让项目通过网关发起模型请求。')); return n; }
function badge(value) { return el('span', `badge ${value === 'succeeded' || value === 'ok' ? 'success' : value === 'failed' ? 'failed' : value === 'running' ? 'running' : 'unknown'}`, names[value] || value); }
function metric(title, value, note) { const n = el('article', 'metric'); n.append(el('span', '', title), el('strong', '', value), el('small', '', note)); return n; }
function table(root, headers, rows) {
  if (!rows.length) { root.replaceChildren(empty()); return; }
  const t = el('table'), head = t.createTHead().insertRow(); headers.forEach(x => head.append(el('th', '', x)));
  const body = t.createTBody(); rows.forEach(row => { const tr = body.insertRow(); row.forEach(value => { const td = tr.insertCell(); if (value instanceof Node) td.append(value); else td.textContent = value ?? '—'; }); }); root.replaceChildren(t);
}
function breakdown(root, rows) { const box = el('div', 'breakdown'); rows.forEach(([k, v]) => { const row = el('div', 'breakdown-row'); row.append(el('span', '', k), el('strong', '', v)); box.append(row); }); root.replaceChildren(box); }
function kv(k, v) { const n = el('div', 'kv'); n.append(el('label', '', k), el('div', '', v == null || v === '' ? '—' : String(v))); return n; }
async function api(path, signal, body) {
  const r = await fetch(path, {signal, cache: 'no-store', method: body ? 'POST' : 'GET', headers: {Authorization: `Bearer ${state.key}`, ...(body ? {'Content-Type': 'application/json'} : {})}, ...(body ? {body: JSON.stringify(body)} : {})});
  const data = await r.json(); if (!r.ok) throw new Error(data.error?.message || `请求失败 (${r.status})`); return data;
}
function filters() { return new URLSearchParams({days: $('days').value, project: $('project').value, alias: $('alias').value}); }
function optionValues(id, values) {
  const select = $(id), current = select.value, first = select.options[0].cloneNode(true); select.replaceChildren(first);
  [...new Set([...values, ...(current ? [current] : [])])].sort().forEach(value => { const o = el('option', '', value); o.value = value; select.append(o); }); select.value = current;
}
function navigate(page, id = null) { location.hash = id ? `detail/${encodeURIComponent(id)}` : page; }
function route() {
  const hash = location.hash.slice(1); state.detailId = null;
  if (hash.startsWith('detail/')) { state.page = 'detail'; try { state.detailId = decodeURIComponent(hash.slice(7)); } catch { state.page = 'requests'; } }
  else state.page = ['overview', 'requests', 'usage'].includes(hash) ? hash : 'overview';
  const title = {overview: '运行总览', requests: '调用记录', usage: 'Token 与费用', detail: '请求详情'}[state.page];
  $('breadcrumb').textContent = title; $('page-title').textContent = state.page === 'overview' ? '每一次模型调用，都清晰可见。' : title;
  document.querySelectorAll('.page').forEach(n => n.hidden = n.id !== `page-${state.page}`);
  document.querySelectorAll('[data-page]').forEach(n => n.classList.toggle('active', n.dataset.page === (state.page === 'detail' ? 'requests' : state.page)));
  $('filters').hidden = state.page === 'detail';
  if (state.page === 'detail') { state.selected = null; ['trace-summary', 'span-tree', 'span-detail'].forEach(id => $(id).replaceChildren(el('p', 'loading', '加载中…'))); }
  refresh();
}
async function refresh() {
  if (!state.key) return;
  state.controller?.abort(); const controller = new AbortController(); state.controller = controller;
  const revision = ++state.revision, signal = controller.signal, page = state.page;
  $('connection-status').textContent = '更新中…';
  try {
    const params = filters(); let path = `admin/observe/analytics?${params}`;
    if (page === 'requests') { params.set('state', $('request-state').value); params.set('query', $('query').value.trim()); params.set('offset', state.offset); path = `admin/observe/requests?${params}`; }
    if (page === 'detail') path = `admin/observe/requests/${encodeURIComponent(state.detailId)}`;
    const [status, data] = await Promise.all([api('admin/status', signal), api(path, signal)]);
    if (revision !== state.revision || !state.key) return;
    $('login').hidden = true; $('dashboard').hidden = false; $('logout').hidden = false; $('refresh').disabled = false;
    $('mode').textContent = status.mode === 'demo' ? '演示模式' : '真实调用'; $('demo-banner').hidden = status.mode !== 'demo';
    optionValues('project', [...Object.keys(status.projects), ...(data.groups?.project || []).map(x => x.name)]);
    optionValues('alias', Object.values(status.projects).flatMap(p => p.models));
    if (page === 'overview') { state.analytics = data; renderOverview(data); renderResources(status); }
    else if (page === 'usage') { state.analytics = data; renderUsage(data); }
    else if (page === 'requests') {
      if (state.offset && state.offset >= data.total) { state.offset = 0; return refresh(); }
      renderRequests($('requests-table'), data.items); $('page-info').textContent = `共 ${num(data.total)} 条 · 第 ${Math.floor(state.offset / 20) + 1} 页`;
      $('prev-page').disabled = state.offset === 0; $('next-page').disabled = state.offset + 20 >= data.total;
    } else renderDetail(data);
    notice(); $('connection-status').textContent = '已连接 · 15 秒刷新'; $('updated-at').textContent = `更新于 ${date(Date.now() / 1000)}`;
  } catch (error) {
    if (error.name === 'AbortError' || revision !== state.revision) return;
    $('connection-status').textContent = '更新失败'; notice(`${error.message}。已有内容可能不是最新数据。`);
  } finally { if (revision === state.revision) state.controller = null; }
}
function renderOverview(d) {
  $('overview-metrics').replaceChildren(metric('网关请求', num(d.requests), `${num(d.running)} 个运行中`), metric('请求成功率', d.success_rate == null ? '—' : `${d.success_rate}%`, '成功 /（成功 + 失败）'), metric('P95 请求耗时', duration(d.p95_ms), '成功与失败请求 · 含排队及重试'), metric('已知 Token', num(d.usage.input_tokens + d.usage.output_tokens), `${d.usage.unknown_usage_calls} 次用量缺失`), metric('估算费用', money(d.usage.costs), `${d.usage.unpriced_calls} 次尚未计价`));
  chart($('trend-chart'), d.trend);
  breakdown($('health-list'), [['失败请求', `${d.failed} 次`], ['结果待核对', `${d.unknown} 次`], ['用量缺失（文本尝试）', `${d.usage.unknown_usage_calls} 次`], ['未计价尝试', `${d.usage.unpriced_calls} 次`], ['额外重试 / 切换', `${d.retries} 次`]]);
  renderRequests($('recent-table'), d.recent);
}
function svgNode(tag, attrs, text) { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); Object.entries(attrs || {}).forEach(([k,v]) => n.setAttribute(k, v)); if (text !== undefined) n.textContent = text; return n; }
function chart(root, data) {
  if (!data.some(d => d.requests)) { root.replaceChildren(empty('这个时间范围内还没有请求')); return; }
  const svg = svgNode('svg', {viewBox: '0 0 540 180', role: 'img', 'aria-label': '每日请求与失败趋势'}), max = Math.max(1, ...data.map(d => d.requests));
  const x = i => 32 + i / Math.max(1, data.length - 1) * 480, y = v => 142 - v / max * 118;
  for (let i=0; i<=3; i++) { const py = 142-i*118/3; svg.append(svgNode('line', {x1:32,y1:py,x2:512,y2:py,class:'gridline'}), svgNode('text', {x:24,y:py+3,'text-anchor':'end'}, Math.round(max*i/3))); }
  const points = data.map((d,i) => `${x(i)},${y(d.requests)}`).join(' ');
  svg.append(svgNode('polygon', {points:`32,142 ${points} 512,142`,class:'area'}), svgNode('polyline', {points,class:'trend-line'}), svgNode('polyline', {points:data.map((d,i)=>`${x(i)},${y(d.failed)}`).join(' '),class:'failed-line'}));
  data.forEach((d,i) => { const dot = svgNode('circle', {cx:x(i),cy:y(d.requests),r:3,fill:'#739754'}); dot.append(svgNode('title', {}, `${d.date}：${d.requests} 次请求，${d.failed} 次失败`)); svg.append(dot); if (i===0 || i===data.length-1 || i%Math.ceil(data.length/5)===0) svg.append(svgNode('text', {x:x(i),y:167,'text-anchor':'middle'}, d.date.slice(5))); }); root.replaceChildren(svg);
}
function renderRequests(root, requests) {
  table(root, ['开始时间 / 请求','项目 / 逻辑模型','状态','网关耗时','尝试','已知 Token','估算费用'], requests.map(op => {
    const link = el('button','text-btn request-link',date(op.created)); link.append(el('small','cell-sub mono',op.id)); link.onclick = () => navigate('detail',op.id);
    const project = el('div','',op.project); project.append(el('small','cell-sub',op.alias));
    return [link,project,badge(op.state),duration(op.duration_ms),op.attempt_count,tokens(op.usage),money(op.usage.costs)];
  }));
}
function renderUsage(d) {
  const u = d.usage;
  $('usage-metrics').replaceChildren(metric('上游尝试',num(u.calls),`${d.retries} 次额外重试 / 切换`),metric('已知输入 Token',num(u.input_tokens),'仅累加已返回字段'),metric('已知输出 Token',num(u.output_tokens),'仅累加已返回字段'),metric('用量缺失',num(u.unknown_usage_calls),'已结束的文本尝试'),metric('估算费用',money(u.costs),`${u.unpriced_calls} 次未计价`));
  breakdown($('cost-breakdown'), [...Object.entries(u.costs).map(([k,v])=>[`${k} · 价格表估算`,v.toFixed(6)]),['未计价尝试',`${u.unpriced_calls} 次`],['明确未受理（费用记 0）',`${u.not_accepted_calls} 次`],['计费说明','请以供应商最终账单为准']]);
  breakdown($('token-breakdown'),[['输入 Token',num(u.input_tokens)],['输出 Token',num(u.output_tokens)],['其中缓存输入（已包含）',detailTokens(u,'cached')],['缓存明细未返回',`${u.cached_unknown_calls} 次`],['其中推理输出（已包含）',detailTokens(u,'reasoning')],['推理明细未返回',`${u.reasoning_unknown_calls} 次`],['输入与输出均已返回',`${u.known_token_calls} 次`],['用量缺失文本尝试',`${u.unknown_usage_calls} 次`],['运行中尝试',`${u.pending_calls} 次`],['图片尝试（不计 Token）',`${u.image_calls} 次`]]); renderGroups();
}
function renderGroups() { if (!state.analytics) return; table($('usage-table'),['归属','尝试次数','输入 Token','输出 Token','其中缓存输入','其中推理输出','用量缺失','未计价','估算费用'],state.analytics.groups[$('group-by').value].map(g=>[g.name,num(g.calls),num(g.input_tokens),num(g.output_tokens),detailTokens(g,'cached'),detailTokens(g,'reasoning'),`${g.unknown_usage_calls} 次`,`${g.unpriced_calls} 次`,money(g.costs)])); }
function renderResources(d) {
  const c = d.capacity;
  $('active-global').textContent = c ? `文本 ${c.text_active} / ${c.text_limit} · 图片 ${c.image_active} / ${c.image_limit} · 图片等待 ${c.image_waiting} / ${c.image_queue_limit}` : `全局处理中 ${d.active_requests}`;
  table($('projects-table'),['项目','今日尝试 / 上限','允许的逻辑模型'],Object.entries(d.projects).map(([name,p])=>[name,`${d.daily.find(x=>x.project===name)?.calls || 0} / ${p.daily_limit}`,p.models.join('、')]));
  const blocks = d.blocks.filter(b=>b.until>=0);
  table($('deployments-table'),['候选','当前配置模型','账户 / 配额组','可用状态'],Object.entries(d.deployments).map(([name,p])=>[name,p.model,`${p.account} / ${p.quota_group}`,blocks.filter(b=>p.scopes.includes(b.scope)).map(b=>b.until===0 ? `停用 · ${b.reason}` : b.until>Date.now()/1000 ? `冷却至 ${date(b.until)}` : '等待下一次探测').join('；') || '可尝试']));
  $('blocks').replaceChildren(...blocks.map(b=> { const row=el('div','block'), button=el('button','btn secondary','验证后恢复'); row.append(el('span','',`${b.scope} · ${b.reason}`),button); button.onclick=async()=> { if (!confirm('请确认已充值、替换 Key 或修复权限。恢复后下一次请求会验证该候选。')) return; button.disabled=true; try { await api('admin/reset-block',undefined,{scope:b.scope}); refresh(); } catch(e) { notice(e.message); button.disabled=false; } }; return row; }));
}
function renderDetail(data) {
  state.detail = data; const op=data.operation, attempts=data.attempts, top=el('div','trace-top'), heading=el('div');
  heading.append(el('h2','',`${op.project} / ${op.alias}`),el('p','mono',op.id)); top.append(heading,badge(op.state));
  const facts=el('div','kv-grid'); [['任务标识',op.task_id],['开始时间',date(op.created)],['网关耗时',duration(op.duration_ms)],['请求类型',op.kind==='chat'?'文本 / 多模态':'图片'],['上游尝试',attempts.length],['最终实际模型',op.served_model],['备用候选',op.fallback?'已使用':'未使用'],['结果缓存',op.result_expired?'已过期（原调用成功）':'按现有保留策略'],['已知 Token',tokens(op.usage)],['估算费用',money(op.usage.costs)],['HTTP 结果',op.http_status],['请求错误码',op.error_code]].forEach(([k,v])=>facts.append(kv(k,v))); $('trace-summary').replaceChildren(top,facts);
  if (!attempts.length) { $('span-tree').replaceChildren(empty('没有上游尝试记录（可能尚未发起或记录已清理）')); $('span-detail').replaceChildren(); return; }
  if (!attempts.some(a=>a.id===state.selected)) state.selected=attempts.find(a=>!['ok','running'].includes(a.code))?.id || attempts[0].id;
  const end=Math.max(op.created+op.duration_ms/1000,...attempts.map(a=>a.started+(a.elapsed_ms??Math.max(0,(Date.now()/1000-a.started)*1000))/1000)), range=Math.max(.001,end-op.created);
  $('span-tree').replaceChildren(...attempts.map((a,i)=> { const button=el('button',`span-row kind-llm${state.selected===a.id?' selected':''}`),label=el('div','span-label'); label.append(el('strong','',a.deployment),el('small','',i===0?'首次尝试':a.deployment!==attempts[i-1].deployment?`第 ${i+1} 次 · 切换候选`:`第 ${i+1} 次 · 同候选重试`));
    const timeline=el('div','span-timeline'),svg=svgNode('svg',{viewBox:'0 0 100 15','aria-hidden':'true'}),x=Math.max(0,(a.started-op.created)/range*100),width=Math.max(1,Math.min(100-x,(a.elapsed_ms??Math.max(0,(Date.now()/1000-a.started)*1000))/1000/range*100)); svg.append(svgNode('rect',{x:0,y:5,width:100,height:5,rx:2,class:'track'}),svgNode('rect',{x,y:5,width,height:5,rx:2,class:'bar'}));timeline.append(svg);button.append(label,timeline,el('span','elapsed',duration(a.elapsed_ms)),badge(a.code==='ok'?'succeeded':a.code==='running'?'running':a.code==='RESULT_UNKNOWN'?'unknown':'failed'));button.onclick=()=>{state.selected=a.id;renderDetail(state.detail);};return button; }));
  const a=attempts.find(a=>a.id===state.selected), fields=el('div','span-fields');
  [['尝试 ID',a.id],['结果码',a.code],['候选',a.deployment],['发生时实际模型',a.actual_model || '历史记录未保存'],['开始时间',date(a.started)],['耗时',duration(a.elapsed_ms)],['输入 Token',a.prompt_tokens??'未知'],['输出 Token',a.completion_tokens??'未知'],['其中缓存输入（已包含）',a.cached_tokens??'未知 / 未返回'],['其中推理输出（已包含）',a.reasoning_tokens??'未知 / 未返回'],['费用',a.estimated_cost==null?'未知':`${a.currency} ${a.estimated_cost.toFixed(6)}`],['费用状态',({estimated:'价格表估算',unknown:'待核对',not_accepted:'明确未受理'})[a.cost_status] || a.cost_status]].forEach(([k,v])=>fields.append(kv(k,v))); $('span-detail').replaceChildren(el('h3','',a.deployment),fields);
}
$('login-form').onsubmit=event=>{event.preventDefault(); state.key=$('key').value.trim(); $('key').value=''; refresh();};
$('logout').onclick=()=>{state.key='';state.revision++;state.controller?.abort();state.controller=null;state.analytics=null;state.detail=null;$('dashboard').hidden=true;$('login').hidden=false;$('logout').hidden=true;$('refresh').disabled=true;$('mode').textContent='未连接';$('connection-status').textContent='已断开';$('updated-at').textContent='等待数据';notice();};
document.querySelectorAll('[data-page]').forEach(n=>n.onclick=()=>navigate(n.dataset.page));
['days','project','alias','request-state'].forEach(id=>$(id).onchange=()=>{state.offset=0;refresh();});
let searchTimer; $('query').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.offset=0;refresh();},300);};
$('prev-page').onclick=()=>{state.offset=Math.max(0,state.offset-20);refresh();};$('next-page').onclick=()=>{state.offset+=20;refresh();};
$('group-by').onchange=renderGroups;$('refresh').onclick=refresh;$('view-all').onclick=()=>navigate('requests');$('back-requests').onclick=()=>navigate('requests');
window.addEventListener('hashchange',route);setInterval(()=>{if(!document.hidden && !state.controller) refresh();},15000);route();
