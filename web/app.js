/* 《异环》第三方助手 · 本地看板前端（零构建、零依赖） */
const $ = (id) => document.getElementById(id);
let DATA = null;
let authSession = null;
let GACHA = null;
let gachaVersion = -1;
const gachaOpen = {};

/* ------------------------------------------------------------ 账号显示自定义 */
/* 纯显示层：只存浏览器 localStorage，不写 SQLite、不改官方数据、不影响任务队列。 */
const DISPLAY_KEY = 'nte.display.v1';
let DISPLAY = {accounts: {}};
let displayEditId = null;
let gachaFilterId = null;

function loadDisplay() {
  try {
    const raw = JSON.parse(localStorage.getItem(DISPLAY_KEY) || '{}');
    DISPLAY = {accounts: (raw && raw.accounts && typeof raw.accounts === 'object') ? raw.accounts : {}};
  } catch (e) { DISPLAY = {accounts: {}}; }
}
function saveDisplay() {
  try { localStorage.setItem(DISPLAY_KEY, JSON.stringify(DISPLAY)); }
  catch (e) { toast('显示设置保存失败：浏览器禁用了本地存储', false); }
}
function displayCfg(id) { return DISPLAY.accounts[String(id)] || {}; }
function originalTitle(a) { return a.remark || '未命名账号'; }
function originalSub(a) {
  return [a.phone_mask || '', a.role_name || ('角色 ' + a.role_id), a.credential_type || '']
    .filter(Boolean).join(' · ');
}
function displayTitle(a) {
  const c = displayCfg(a.id);
  if (c.titleMode === 'custom' && c.title) return c.title;
  if (c.titleMode === 'role') return a.role_name || originalTitle(a);
  if (c.titleMode === 'phone') return a.phone_mask || originalTitle(a);
  return originalTitle(a);
}
function displaySub(a) {
  const c = displayCfg(a.id);
  if (c.subCustom) return c.subCustom;
  const h = c.hide || {};
  const parts = [
    h.phone ? '' : (a.phone_mask || ''),
    h.role ? '' : (a.role_name || ('角色 ' + a.role_id)),
    h.credential ? '' : (a.credential_type || ''),
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : '副标题字段已隐藏（可在 ✎ 里还原）';
}
function isDisplayCustomized(id) {
  const c = displayCfg(id), h = c.hide || {};
  return Boolean((c.titleMode && c.titleMode !== 'original') || c.title || c.subCustom
    || h.phone || h.role || h.credential);
}

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function num(v) {
  if (v === null || v === undefined || v === '') return '--';
  return Number.isInteger(v) ? v : (typeof v === 'number' ? v.toFixed(1) : esc(v));
}
function hhmm(ts) {
  if (!ts) return '--:--';
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', {hour12:false});
}
async function api(path, body) {
  const opt = body === undefined
    ? {method:'GET'}
    : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const res = await fetch(path, opt);
  const json = await res.json();
  if (!json.ok) {
    const e = json.error || {message:'请求失败'};
    throw new Error(e.message || e.kind || '请求失败');
  }
  return json.data;
}
function toast(msg, ok) {
  const el = $('toolbarHint');
  el.textContent = msg;
  el.style.color = ok ? 'var(--ok)' : 'var(--danger)';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.textContent = ''; }, 6000);
}

function renderChips(d) {
  const s = d.settings;
  $('chips').innerHTML = [
    `<span class="chip">适配器 ${esc(d.adapter.version)} · ${esc(d.adapter.authProtocol)}</span>`,
    s.writeActionsEnabled
      ? '<span class="chip warn">写操作已开启</span>'
      : '<span class="chip good">写操作关闭（安全默认）</span>',
    s.automationEnabled
      ? '<span class="chip">自动化已开启</span>'
      : '<span class="chip">自动化关闭</span>',
    (d.weekly && d.weekly.visible)
      ? '<span class="chip">周本次数已启用 · 真值优先</span>'
      : '<span class="chip">周本次数已隐藏</span>',
  ].join('');
}

function renderSummary(d) {
  const s = d.summary;
  const cards = [
    ['账号总数', s.total, ''],
    ['今日已签', s.signedToday, ''],
    ['今日未签', s.unsignedToday, s.unsignedToday ? 'alert' : ''],
    ['本性像素已满', s.staminaFull, s.staminaFull ? 'alert' : ''],
    ['待重登', s.reauth, s.reauth ? 'bad' : ''],
    ['数据待刷新', s.staleData, s.staleData ? 'alert' : ''],
  ];
  $('summary').innerHTML = cards.map(([l, n, cls]) =>
    `<div class="stat ${cls}"><div class="n">${n}</div><div class="l">${l}</div></div>`).join('');
}

const PREFIX = {stamina:'stamina', cityStamina:'city', activity:'activity', weeklyCopies:'weekly'};
function metricHtml(spec, snap, weekly) {
  const p = PREFIX[spec.key] || spec.key;
  const v = snap ? snap[p + '_value'] : null;
  const m = snap ? snap[p + '_max'] : null;
  const src = snap ? snap[p + '_max_source'] : null;
  const vSrc = snap ? snap[p + '_value_source'] : null;
  // 周本次数是条件性字段：官方没下发就显示「—」，绝不补 H5 的 3/3 兜底常量
  const isWeekly = spec.key === 'weeklyCopies';
  const unavailable = isWeekly && (v === null || v === undefined || vSrc === 'unavailable');
  const pct = (!unavailable && m && v !== null && v !== undefined)
    ? Math.min(100, Math.max(0, v / m * 100)) : 0;
  const full = !unavailable && m && v !== null && v >= m;
  let badges = '';
  const notDone = unavailable && snap && snap.weekly_state === 'pending_sync';
  if (unavailable) {
    // 周本次数是存档字段（11-09 实测：账号 2 退出游戏+重新授权后 22 键 → 23 键、值 2，
    // 与官方 App 一致；同期没进过游戏的账号 1 仍无该键）。缺失不代表 0 次，也不补 3/3。
    const hint = (weekly && weekly.missingHint) || '';
    const text = notDone ? '待同步 · 退出游戏后刷新'
      : ((weekly && weekly.missingBadge) || '未同步');
    badges = `<span class="badge"${hint ? ` title="${esc(hint)}"` : ''}>${esc(text)}</span>`;
  } else {
    if (isWeekly) badges += '<span class="badge api">接口真值</span>';
    // 都市活力：周一 05:00 归满。工具默认就按满显示，不等服务端回填；
    // 只要接口给出本周真实值就用真实值覆盖。角标只是说明当前这个数还没被服务端确认。
    if (p === 'city' && snap && snap.city_assumed) {
      badges += '<span class="badge" title="周一 05:00 活力归满，工具默认显示满。服务端要等你在游戏里花掉第一点才重算：没花完的号会回显上周残留值、花光的号返回 0，这两种旧数都不会被当成真实余量。进游戏花一点后刷新，即为接口真值。">周重置 · 按满显示</span>';
    }
    if (src === 'estimated') badges += '<span class="badge">推算</span>';
    else if (src === 'frontend_default') badges += '<span class="badge">前端缺省</span>';
    else if (src === 'frontend_constant') badges += '<span class="badge const">分母常量</span>';
  }
  const valHtml = unavailable
    ? `<span class="na">${esc((weekly && weekly.missingLabel) || '—')}</span>`
    : `${num(v)}<span class="den"> / ${num(m)}</span>`;
  const label = `<span class="metric-label"${spec.hint ? ` title="${esc(spec.hint)}"` : ''}>${esc(spec.label)}</span>`;
  return `<div class="metric">
    <div class="metric-top">
      ${label}
      <span class="metric-val">${valHtml}${badges}</span>
    </div>
    <div class="bar ${full ? 'full' : ''}"><i style="width:${pct}%"></i></div>
  </div>`;
}

function isPremium(name) { return String(name || '').indexOf('环石') >= 0; }

function signinReward(a) {
  const s = a.signin_today;
  if (!s || !s.reward_json) return '';
  try {
    const r = JSON.parse(s.reward_json);
    if (!r || !r.name) return '';
    const n = `${esc(r.name)}×${num(r.num)}`;
    return ' · +' + (isPremium(r.name) ? `<b class="rw-premium">${n}</b>` : n);
  } catch (e) {
    return '';
  }
}

function renderAccounts(d) {
  const list = d.accounts;
  if (!list.length) {
    $('accounts').innerHTML = `<div class="empty"><b>还没有账号</b>
      点上方「导入现有 TOKEN.txt」免短信复用已验证凭证，或用「手机号 + 验证码授权」新增账号。<br>
      看板展示本性像素、都市活力、活跃度、周本次数四项；官方未下发周本字段时显示「—」。</div>`;
    return;
  }
  $('accounts').innerHTML = list.map(a => {
    const signed = a.signin_today && ['success','already'].includes(a.signin_today.result);
    const writeOn = d.settings.writeActionsEnabled;
    const canSign = writeOn && !signed && a.enabled && a.status !== 'reauth';
    const signTitle = !writeOn ? '写操作开关未开启，不会提交任何签到请求'
      : signed ? '今日已签到，幂等跳过' : '提交一次异环游戏内每日签到（发游戏奖励）';
    const rewardText = signinReward(a);
    // 官方 H5 原文：「（签到奖励可能存在延迟，请前往游戏内邮箱领取奖励邮件）」
    const mailHint = signed && rewardText
      ? '<span class="mail-hint" title="官方说明：签到奖励可能存在延迟，请前往游戏内邮箱领取奖励邮件。不进背包是正常的，去邮箱领附件。若邮箱里没有，打开「签到详情」点「补投递」触发一次投递，再完全退出游戏重进查看（邮件不会实时推送）。">走游戏邮箱</span>'
      : '';
    const stateText = {ok:'正常', reauth:'待重登', error:'异常', disabled:'停用'}[a.status] || a.status;
    const last = a.last_refresh_at ? hhmm(a.last_refresh_at) : '未刷新';
    const customFlag = isDisplayCustomized(a.id) ? '<span class="display-flag">自定义</span>' : '';
    const info = a.signin_info || null;
    const coinText = info
      ? ` · 呗果 <b>${num(info.coin)}</b> · 可补签 ${num(info.resignLeft)} 次`
      : '';
    const canResign = writeOn && !!info && info.canResign && a.enabled && a.status !== 'reauth';
    const resignTitle = !writeOn ? '写操作开关未开启，不会提交任何请求'
      : !info ? '还没有读取签到详情：先点「签到详情」做一次只读查询'
      : !info.todaySign ? '官方要求先完成今日签到才能补签'
      : info.resignLeft <= 0 ? '本月补签次数已用完（每月 1 日 00:00 重置）'
      : info.coin < info.cost ? `呗果不足：需 ${info.cost}，当前 ${info.coin}`
      : `补签消耗 ${info.cost} 呗果（剩余 ${info.resignLeft} 次），奖励走游戏邮箱`;
    return `<div class="card" data-id="${a.id}">
      <div class="card-head">
        <div><div class="card-title">${esc(displayTitle(a))}${customFlag}
            <button class="title-edit" data-act="display" title="自定义显示 / 还原原显示">✎</button></div>
          <div class="card-sub">${esc(displaySub(a))}</div></div>
        <span class="state ${esc(a.status)}">${esc(stateText)}</span>
      </div>
      <div class="metrics">${d.metrics.map(m => metricHtml(m, a.snapshot, d.weekly)).join('')}</div>
      <div class="card-foot">
        <span class="meta">${signed ? '今日已签' : '今日未签'}${rewardText}${mailHint}${coinText} · 刷新 ${last}</span>
        <span class="card-actions">
          <button class="btn" data-act="refresh">刷新数据</button>
          <button class="btn" data-act="sign" ${canSign ? '' : 'disabled'} title="${signTitle}">${signed ? '今日已签' : '签到'}</button>
          <button class="btn ghost" data-act="resign" ${canResign ? '' : 'disabled'} title="${esc(resignTitle)}">补签</button>
          <button class="btn ghost" data-act="signinfo" title="只读查询：呗果余额 / 补签次数 / 本月奖励表 / 最近到账">签到详情</button>
          <button class="btn ghost" data-act="rename">备注</button>
          <button class="btn ghost" data-act="delete">删除</button>
          <button class="btn ghost" data-act="gacha" title="只查看该账号本地已保存的抽卡快照，不刷新、不入队、不发官方请求">抽卡统计</button>
        </span>
      </div>
      ${a.last_error ? `<div class="err">${esc(a.last_error)}</div>` : ''}
    </div>`;
  }).join('');
}

function renderJobs(d) {
  const q = d.queue;
  $('queueTag').textContent = `队列 ${q.size} · 持久化 ${JSON.stringify(q.persisted || {})}`;
  if (!q.jobs.length) { $('jobs').innerHTML = '<div class="row"><span class="m">暂无任务</span></div>'; return; }
  const kindLabel = {refresh:'刷新数据', signin:'游戏签到', gacha:'抽卡统计',
                     signinfo:'签到详情(只读)', resign:'补签', deliver:'补投递'};
  $('jobs').innerHTML = q.jobs.map(j =>
    `<div class="row"><span class="t">${hhmm(j.created_at)}</span>
      <span class="m">账号 ${j.account_id} · ${esc(kindLabel[j.kind] || j.kind)}
      <span class="status-${esc(j.status)}">[${esc(j.status)}]</span> ${esc(j.message)}</span></div>`).join('');
}
function renderLogs(d) {
  if (!d.logs.length) { $('logs').innerHTML = '<div class="row"><span class="m">暂无日志</span></div>'; return; }
  $('logs').innerHTML = d.logs.map(l =>
    `<div class="row"><span class="t">${hhmm(l.ts)}</span>
      <span class="m">${esc(l.level)} ${esc(l.action)} · ${esc(l.message)}</span></div>`).join('');
}
function render(d) {
  DATA = d;
  renderChips(d); renderSummary(d); renderAccounts(d); renderJobs(d); renderLogs(d);
  // 签到详情弹窗开着时，跟着 3 秒轮询一起刷新（补签 / 签到结果会即时反映到呗果与次数）
  if (signEditId !== null && !$('signModal').hidden) renderSignModal();
  $('dataDir').textContent = 'data/';
  $('btnSignAll').disabled = !d.settings.writeActionsEnabled;
  $('btnSignAll').title = d.settings.writeActionsEnabled ? '串行执行当日未签账号' : '写操作开关未开启';
  $('adapterInfo').textContent = `${d.adapter.version} · ${d.adapter.authProtocol} · ${d.adapter.credentialMode}（可选版本：${d.adapter.available.join(', ')}）`;
  // state 里只带轻量版本戳；抽卡明细较大，仅在版本变化时重拉
  if (d.gacha && d.gacha.version !== gachaVersion) {
    gachaVersion = d.gacha.version;
    loadGacha();
  }
}
async function load() {
  try { render(await api('/api/state')); }
  catch (e) { toast('读取状态失败：' + e.message, false); }
}

/* ------------------------------------------------------------ 抽卡统计 */
function fmtTime(ts) {
  if (!ts) return '--';
  return new Date(ts * 1000).toLocaleString('zh-CN',
    {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false});
}
function signed(n) { return (n > 0 ? '+' : '') + n; }
function itemLabel(row) {
  const id = esc(row.itemId || '?');
  if (row.name) {
    const src = row.nameSource === 'user' ? '本地别名' : '官方字典';
    return `<span class="item-name" data-item="${id}" title="点击设置显示名称">${esc(row.name)}</span>` +
           `<span class="item-id">${id} · ${src}</span>`;
  }
  return `<span class="item-name name-missing" data-item="${id}" title="官方字典未覆盖，点击自定义名称">${id}</span>` +
         `<span class="item-id">未下发名称 · 点击设置别名</span>`;
}
function poolHtml(pool, key) {
  const items = (pool.items || []).map(x => `<tr>
    <td>${itemLabel(x)}</td><td>${num(x.times)}</td>
    <td class="strong">${num(x.totalDraws)}</td><td>${num(x.avgDraws)}</td>
    <td>${num(x.maxDraws)}</td><td>${esc(x.lastTime || '--')}</td>
  </tr>`).join('') || '<tr><td colspan="6">该池暂无出货明细</td></tr>';
  const details = (pool.details || []).map(x => {
    const pct = (x.pityPct === null || x.pityPct === undefined) ? 0 : x.pityPct;
    const hot = x.nearPity ? ' hot' : '';
    return `<tr>
      <td>${esc(x.time || '--')}</td><td>${itemLabel(x)}</td>
      <td class="strong">${num(x.draws)}</td><td>${num(x.cumulative)}</td>
      <td><span class="bar-mini${hot}"><i style="width:${Math.min(100, pct)}%"></i></span>${num(pct)}%${x.nearPity ? '<span class="pill near">接近保底</span>' : ''}</td>
      <td>${esc(x.luckyType === null || x.luckyType === undefined ? '--' : x.luckyType)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6">暂无明细</td></tr>';
  const sub = pool.drawConsistent
    ? `明细抽数合计 <b>${num(pool.drawSum)}</b> = 官方累计 <b>${num(pool.drawCount)}</b> 抽，口径闭合`
    : `⚠ 明细合计 ${num(pool.drawSum)} / 官方累计 ${num(pool.drawCount)} 抽（覆盖 ${Math.round((pool.coverage || 0) * 100)}%）：早期明细可能已被官方滚动清理，累计抽数只覆盖现存明细`;
  return `<div class="pool">
    <div class="pool-title"><span>${esc(pool.tab)}</span>
      <span class="tag">保底 ${num(pool.pityMax)} 抽 · 超过 ${esc(pool.playerOver || '--')} 玩家</span></div>
    <div class="pool-kpis">
      <div class="kpi"><b>${num(pool.drawCount)}</b><span>累计抽数</span></div>
      <div class="kpi"><b>${num(pool.rareCount)}</b><span>稀有出货</span></div>
      <div class="kpi"><b>${num(pool.average)}</b><span>平均出货</span></div>
      <div class="kpi"><b>${num(pool.detailCount)}</b><span>可见明细</span></div>
    </div>
    <div class="pool-sub ${pool.drawConsistent ? '' : 'warn'}">${sub}</div>
    <div class="table-wrap"><table class="gtable">
      <thead><tr><th>角色 / 弧盘</th><th>出货次数</th><th>获取花费抽数</th><th>平均</th><th>最非</th><th>最近出货</th></tr></thead>
      <tbody>${items}</tbody>
    </table></div>
    <details class="pool-details" data-key="${esc(key)}" ${gachaOpen[key] ? 'open' : ''}>
      <summary>逐次出货明细（${num(pool.detailCount)} 条）· 本次抽数 / 累计抽数 / 保底进度</summary>
      <div class="table-wrap"><table class="gtable">
        <thead><tr><th>出货时间</th><th>角色 / 弧盘</th><th>本次抽数</th><th>累计抽数</th><th>保底进度</th><th>类型</th></tr></thead>
        <tbody>${details}</tbody>
      </table></div>
    </details>
  </div>`;
}
function gachaDisplayTitle(a) {
  const base = ((DATA && DATA.accounts) || []).find(x => x.id === a.accountId);
  if (base) return displayTitle(base);
  const c = displayCfg(a.accountId);
  if (c.titleMode === 'custom' && c.title) return c.title;
  if (c.titleMode === 'role') return a.roleName || a.remark || '未命名账号';
  if (c.titleMode === 'phone') return a.phoneMask || a.remark || '未命名账号';
  return a.remark || '未命名账号';
}
function gachaAccountHtml(a) {
  const d = a.data;
  const main = gachaDisplayTitle(a);
  const role = a.roleName || (d && d.roleName) || '';
  const title = esc(main) + (role && role !== main ? ' · ' + esc(role) : '');
  const h = displayCfg(a.accountId).hide || {};
  const sub = [
    h.phone ? '' : (a.phoneMask || ''),
    (d && d.luckTitle) ? '欧气评价：' + d.luckTitle : '尚无抽卡快照',
    '抓取 ' + (a.fetchedAt ? fmtTime(a.fetchedAt) : '未抓取'),
  ].filter(Boolean).map(x => esc(x)).join(' · ');
  const head = `<div class="gacha-acct-head">
    <div>
      <div class="gacha-acct-title">${title}<span class="lv">Lv${num(d ? d.level : '--')}</span></div>
      <div class="card-sub">${sub}</div>
    </div>
    <div class="gacha-acct-stats">
      ${d ? `<span><b class="big">${num(d.totalDraw)}</b> <span class="unit">总抽数</span></span>
             <span><b class="big">${num(d.totalRare)}</b> <span class="unit">稀有出货</span></span>
             <span><b class="big">${num(d.totalDetail)}</b> <span class="unit">出货明细</span></span>` : ''}
      <button class="btn" data-gact="refresh" data-id="${a.accountId}">抓取此账号</button>
    </div>
  </div>`;
  if (!d) {
    return `<div class="gacha-account">${head}<div class="gacha-note" style="margin:12px 0 0">该账号还没有抽卡快照，点「抓取此账号」做一次只读抓取。</div></div>`;
  }
  const diff = (a.diff && a.diff.hasPrevious)
    ? `较上次抓取（${fmtTime(a.diff.previousAt)}）：<span class="up">${signed(a.diff.drawDelta)} 抽 / ${signed(a.diff.rareDelta)} 金</span>`
    : '首次抓取，暂无历史对比';
  const hist = (a.history || []).slice(0, 6).map(h =>
    `<span class="hchip">${fmtTime(h.ts)} · ${num(h.total_draw)}抽/${num(h.total_rare)}金</span>`).join('');
  const pools = (d.pools || []).map(p => poolHtml(p, `${a.accountId}:${p.tab}`)).join('');
  return `<div class="gacha-account">
    ${head}
    <div class="gacha-diff">${diff} · 快照按抓取时间留存于本地，可回看抽数增长</div>
    <div class="pool-grid">${pools}</div>
    <div class="gacha-history"><span>历史快照：</span>${hist || '<span>暂无</span>'}</div>
  </div>`;
}
function renderGacha(g) {
  GACHA = g;
  const n = g.notes || {};
  $('gachaNotes').innerHTML = [
    ['抽数口径', n.draws], ['出货时间', n.time], ['明细覆盖', n.coverage],
    ['物品名称', n.names], ['类型标记', n.luckyType], ['只读说明', n.readonly],
  ].filter(x => x[1]).map(([k, v]) => `<div><b>${esc(k)}：</b>${esc(v)}</div>`).join('');
  const all = g.accounts || [];
  // 筛选目标账号被删除时自动退出单账号视图
  if (gachaFilterId !== null && !all.some(a => a.accountId === gachaFilterId)) gachaFilterId = null;
  const list = gachaFilterId === null ? all : all.filter(a => a.accountId === gachaFilterId);
  const withData = all.filter(a => a.hasData).length;
  const suffix = gachaFilterId === null ? '' : ' · 单账号只读查看';
  $('gachaMeta').textContent = `${withData}/${all.length} 个账号有快照 · ${g.endpoint || ''} · 只读 GET${suffix}`;
  $('gachaFilterBar').hidden = gachaFilterId === null;
  $('btnGachaShowAll').hidden = gachaFilterId === null;
  if (gachaFilterId !== null) {
    const t = all.find(a => a.accountId === gachaFilterId);
    const name = t ? gachaDisplayTitle(t) : `账号 ${gachaFilterId}`;
    $('gachaFilterText').textContent =
      `仅显示「${name}」的本地抽卡快照（抓取于 ${t && t.fetchedAt ? fmtTime(t.fetchedAt) : '无快照'}）`;
  }
  $('gachaEmpty').hidden = list.length > 0;
  $('gachaEmpty').textContent = (gachaFilterId !== null && !list.length)
    ? '该账号暂无抽卡快照。此按钮只查看本地已保存数据，不会自动抓取；如需更新请用「抓取全部账号」。'
    : '还没有抽卡快照，点「抓取全部账号」开始一次只读抓取。';
  $('gachaList').innerHTML = list.map(gachaAccountHtml).join('');
}
async function loadGacha() {
  try { renderGacha(await api('/api/gacha')); }
  catch (e) {
    $('gachaMeta').textContent = '读取失败';
    toast('读取抽卡统计失败：' + e.message, false);
  }
}
async function showAccountGacha(id) {
  // 只查看本地已保存快照：最多读一次本地 /api/gacha，不入队、不刷新、不发官方请求
  if (!GACHA) await loadGacha();
  gachaFilterId = id;
  if ($('gachaBody').hidden) {
    $('gachaBody').hidden = false;
    $('btnGachaToggle').textContent = '收起';
  }
  renderGacha(GACHA || {accounts: [], notes: {}});
  const a = ((GACHA && GACHA.accounts) || []).find(x => x.accountId === id);
  const name = a ? gachaDisplayTitle(a) : `账号 ${id}`;
  toast(`「${name}」：只查看本地抽卡快照，未刷新、未发起任何官方请求`, true);
  if ($('gachaSection').scrollIntoView) {
    $('gachaSection').scrollIntoView({behavior: 'smooth', block: 'start'});
  }
}
$('btnGachaShowAll').onclick = () => {
  gachaFilterId = null;
  if (GACHA) renderGacha(GACHA);
  toast('已显示全部账号的抽卡快照（仍未发起刷新）', true);
};
async function refreshGachaAll() {
  try {
    const r = await api('/api/gacha/refresh-all', {});
    toast(r.queued
      ? `已排队 ${r.queued} 个账号的只读抽卡抓取（串行 + 限速），完成后看板自动刷新`
      : '没有启用中的账号，未发送任何请求', true);
  } catch (e) { toast(e.message, false); }
}
$('btnGachaRefresh').onclick = refreshGachaAll;
$('btnGachaRefreshAll').onclick = refreshGachaAll;
$('btnGachaToggle').onclick = () => {
  const box = $('gachaBody');
  box.hidden = !box.hidden;
  $('btnGachaToggle').textContent = box.hidden ? '展开' : '收起';
};
$('gachaList').addEventListener('toggle', (ev) => {
  const el = ev.target;
  if (el.matches && el.matches('details.pool-details')) gachaOpen[el.dataset.key] = el.open;
}, true);
$('gachaList').addEventListener('click', async (ev) => {
  const refresh = ev.target.closest('button[data-gact="refresh"]');
  if (refresh) {
    try {
      await api('/api/gacha/refresh', {accountId:Number(refresh.dataset.id)});
      toast('该账号抽卡抓取已排队，完成后看板自动刷新', true);
    } catch (e) { toast(e.message, false); }
    return;
  }
  const label = ev.target.closest('.item-name');
  if (!label) return;
  const id = label.dataset.item;
  const cur = (GACHA && GACHA.names && GACHA.names[id]) || '';
  const name = prompt('给物品 ' + id + ' 设置显示名称\n留空则恢复官方名称或纯 ID（别名只存本地）', cur);
  if (name === null) return;
  try {
    await api('/api/gacha/alias', {itemId:id, name:name.trim()});
    await loadGacha();
    toast('名称已保存到本地', true);
  } catch (e) { toast(e.message, false); }
});

/* ------------------------------------------------------------ 工具栏 */
$('btnRefreshAll').onclick = async () => {
  try { const r = await api('/api/refresh-all', {}); toast(`已排队刷新 ${r.queued} 个账号（共 ${r.eligible} 个待刷新）`, true); }
  catch (e) { toast(e.message, false); }
};
$('btnSignAll').onclick = async () => {
  try {
    const r = await api('/api/signin-all', {});
    toast(r.queued
      ? `已排队签到 ${r.queued} 个账号（串行 + 限速，结果看任务队列 / 今日已签）`
      : '今日没有待签账号：已全部签到或已记录，未提交任何请求', true);
  } catch (e) { toast(e.message, false); }
};
$('btnImport').onclick = async () => {
  try {
    const r = await api('/api/auth/import', {});
    toast(`已加密导入 ${r.count} 个账号，点「刷新数据」拉取四项数值`, true);
    await load();
  } catch (e) { toast(e.message, false); }
};
$('accounts').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('button[data-act]');
  if (!btn) return;
  const id = Number(btn.closest('.card').dataset.id);
  const act = btn.dataset.act;
  if (act === 'display') { openDisplayModal(id); return; }
  if (act === 'gacha') { showAccountGacha(id).catch(e => toast(e.message, false)); return; }
  if (act === 'signinfo') { openSignModal(id); return; }
  if (act === 'resign') {
    const acc = ((DATA && DATA.accounts) || []).find(x => x.id === id);
    const info = acc && acc.signin_info;
    if (!info) { toast('还没有签到详情：先点「签到详情」读一次', false); return; }
    signEditId = id;
    renderSignModal();
    openModal('signModal');
    $('btnResign').click();
    return;
  }
  try {
    if (act === 'refresh') { await api('/api/refresh', {accountId:id}); toast('只读刷新任务已排队', true); }
    if (act === 'sign') { await api('/api/signin', {accountId:id}); toast('签到任务已排队，结果看任务队列 / 今日已签', true); }
    if (act === 'rename') {
      const remark = prompt('账号备注名', '');
      if (remark !== null) await api('/api/account/update', {accountId:id, fields:{remark:remark.slice(0,30)}});
    }
    if (act === 'delete') {
      if (confirm('删除该账号及其加密凭证？此操作不可撤销。')) {
        await api('/api/account/delete', {accountId:id}); toast('账号已删除', true);
      }
    }
    await load();
  } catch (e) { toast(e.message, false); }
});

/* ------------------------------------------------------------ 自定义显示弹窗 */
function openDisplayModal(id) {
  const a = ((DATA && DATA.accounts) || []).find(x => x.id === id);
  if (!a) { toast('账号数据未就绪，请稍后再试', false); return; }
  displayEditId = id;
  const c = displayCfg(id), h = c.hide || {};
  $('displayOriginal').innerHTML =
    `原显示：标题 <b>${esc(originalTitle(a))}</b> · 副标题 <b>${esc(originalSub(a))}</b>`;
  $('displayTitleMode').value = c.titleMode || 'original';
  $('displayTitleText').value = c.title || '';
  $('displaySubText').value = c.subCustom || '';
  $('displayShowPhone').checked = !h.phone;
  $('displayShowRole').checked = !h.role;
  $('displayShowCredential').checked = !h.credential;
  $('displayMsg').textContent = '';
  openModal('displayModal');
}
function applyDisplayChange(msg) {
  saveDisplay();
  closeModal('displayModal');
  if (DATA) renderAccounts(DATA);
  if (GACHA) renderGacha(GACHA);
  toast(msg, true);
}
$('btnDisplaySave').onclick = () => {
  const mode = $('displayTitleMode').value;
  const title = $('displayTitleText').value.trim().slice(0, 30);
  if (mode === 'custom' && !title) {
    $('displayMsg').textContent = '选了「自定义文字」就必须填写标题（或选「原显示」）';
    return;
  }
  DISPLAY.accounts[String(displayEditId)] = {
    titleMode: mode,
    title: mode === 'custom' ? title : '',
    subCustom: $('displaySubText').value.trim().slice(0, 60),
    hide: {
      phone: !$('displayShowPhone').checked,
      role: !$('displayShowRole').checked,
      credential: !$('displayShowCredential').checked,
    },
  };
  applyDisplayChange('显示已自定义（只存本机浏览器，不写数据库）');
};
$('btnDisplayRestore').onclick = () => {
  delete DISPLAY.accounts[String(displayEditId)];
  applyDisplayChange('该账号已还原原始显示');
};
$('btnDisplayRestoreAll').onclick = () => {
  if (!confirm('还原所有账号的原始显示？（只清本机浏览器显示设置，不改动任何数据）')) return;
  DISPLAY = {accounts: {}};
  applyDisplayChange('所有账号已还原原始显示');
};

/* ------------------------------------------------------------ 签到详情 / 补签 */
let signEditId = null;
let signFetchAt = 0;

function rewardCell(r) {
  const cls = ['rw'];
  if (r.signed) cls.push('signed');
  if (r.today) cls.push('today');
  if (r.premium) cls.push('premium');
  return `<div class="${cls.join(' ')}" title="第 ${r.index} 天 · ${esc(r.name)} ×${num(r.num)}${r.signed ? ' · 已签' : ''}">
    <div class="rw-day">第${r.index}天${r.today ? ' · 今天' : ''}</div>
    <div class="rw-name">${esc(r.name)}</div>
    <div class="rw-num">×${num(r.num)}</div>
    ${r.signed ? '<span class="rw-check">✓</span>' : ''}
  </div>`;
}

function renderSignModal() {
  const a = ((DATA && DATA.accounts) || []).find(x => x.id === signEditId);
  if (!a) { closeModal('signModal'); return; }
  $('signTitle').textContent = `签到详情 · ${displayTitle(a)}`;
  const info = a.signin_info;
  const body = $('signBody');
  const foot = $('btnResign');
  if (!info) {
    body.innerHTML = '<div class="empty">该账号还没有签到详情缓存。<br>点右下「重新读取」做一次只读查询（呗果余额 / 补签次数 / 本月奖励表 / 最近到账）。</div>';
    foot.disabled = true;
    foot.textContent = '补签';
    foot.title = '先读取签到详情';
    return;
  }
  const kpis = [
    ['呗果余额', num(info.coin), `补签消耗 ${num(info.cost)} / 次`],
    ['剩余补签', num(info.resignLeft) + ' 次', `本月已用 ${num(info.resignCnt)}/${num(info.resignLimit)}`],
    ['本月已签', num(info.days) + ' 天', `${num(info.month)} 月 · 第 ${num(info.day)} 天`],
    ['今日', info.todaySign ? '已签' : '未签', info.todaySign ? '奖励走游戏邮箱' : '点卡片「签到」'],
  ].map(([l, v, s]) => `<div class="sign-kpi"><div class="k-l">${esc(l)}</div>
      <div class="k-v">${esc(v)}</div><div class="k-s">${esc(s)}</div></div>`).join('');
  const grid = (info.rewards || []).map(rewardCell).join('')
    || '<div class="empty">官方未返回奖励表</div>';
  const recs = (info.records || []).slice(0, 10).map(r =>
    `<div class="sign-rec"><span class="t">${fmtTime(r.at)}</span>
      <span class="n${isPremium(r.name) ? ' rw-premium' : ''}">${esc(r.name)} ×${num(r.num)}</span>
      <span class="s">已发放 · 去游戏邮箱领</span></div>`).join('')
    || '<div class="empty">暂无到账记录</div>';
  body.innerHTML = `<div class="sign-kpis">${kpis}</div>
    <div class="sign-section-title">本月累计签到奖励
      <span class="muted">环石已加粗 · 勾选 = 已签 · 高亮 = 今天</span></div>
    <div class="reward-grid">${grid}</div>
    <div class="sign-section-title">最近到账（官方 reward_records）</div>
    <div class="sign-recs">${recs}</div>
    <p class="muted small">详情读取于 ${a.signin_info_at ? fmtTime(a.signin_info_at) : '--'} ·
      只读 GET，不改任何官方数据。奖励由官方经<b>游戏内邮箱</b>发放，可能有延迟。</p>`;
  const writeOn = !!(DATA && DATA.settings.writeActionsEnabled);
  foot.disabled = !(writeOn && info.canResign);
  foot.textContent = `补签（消耗 ${num(info.cost)} 呗果）`;
  foot.title = !writeOn ? '写操作开关未开启，不会提交任何请求'
    : !info.todaySign ? '官方要求先完成今日签到才能补签'
    : info.resignLeft <= 0 ? '本月补签次数已用完（每月 1 日 00:00 重置）'
    : info.coin < info.cost ? `呗果不足：需 ${info.cost}，当前 ${info.coin}`
    : '正式提交补签写请求';
}

function openSignModal(id) {
  signEditId = id;
  renderSignModal();
  openModal('signModal');
  const a = ((DATA && DATA.accounts) || []).find(x => x.id === id);
  const at = (a && a.signin_info_at) || 0;
  if (!a || !a.signin_info || Date.now() / 1000 - at > 60) reloadSignInfo(id, true);
}

async function reloadSignInfo(id, silent) {
  if (Date.now() - signFetchAt < 4000) return;   // 防连点：同一时刻只发一次
  signFetchAt = Date.now();
  try {
    await api('/api/signinfo', {accountId:id});
    if (!silent) toast('已排队只读查询：呗果 / 补签次数 / 本月奖励表，完成后自动刷新', true);
  } catch (e) { toast(e.message, false); }
}

$('btnSignInfoReload').onclick = () => { if (signEditId !== null) reloadSignInfo(signEditId, false); };
$('btnResign').onclick = async () => {
  const a = ((DATA && DATA.accounts) || []).find(x => x.id === signEditId);
  const info = a && a.signin_info;
  if (!a || !info) return;
  const ok = confirm(
    `确认给「${displayTitle(a)}」补签？\n\n` +
    `· 消耗 ${info.cost} 呗果（当前 ${info.coin}，补后剩 ${info.coin - info.cost}）\n` +
    `· 本月剩余补签 ${info.resignLeft} 次（补后剩 ${Math.max(0, info.resignLeft - 1)} 次）\n` +
    `· 官方向「今日签到后下一日」发放奖励，同样是游戏内邮箱\n\n` +
    '这会真正提交官方写请求，确认继续？');
  if (!ok) return;
  try {
    await api('/api/resign', {accountId:a.id});
    toast('补签任务已排队：成功后看板会自动回填呗果、剩余次数与累计天数', true);
  } catch (e) { toast(e.message, false); }
};
$('btnDeliver').onclick = async () => {
  const a = ((DATA && DATA.accounts) || []).find(x => x.id === signEditId);
  if (!a) return;
  const today = a.signin_today || {};
  const signed = ['success', 'already'].includes(today.result)
    || !!(a.signin_info && a.signin_info.todaySign);
  // 今日未签也可能有历史待发放奖励（昨天签了但没投递），这里只提示不硬拦；
  // 真正的合法性由后端再校验一次，避免用户在这条路上彻底无路可走。
  const ok = confirm(
    `给「${displayTitle(a)}」补一次奖励投递？\n\n` +
    (signed ? '' : '· 注意：今日尚未签到，仅对历史未投递的奖励有效\n') +
    '· 它会让游戏服重新把待发放奖励推进游戏邮箱\n' +
    '· 不改签到状态、不消耗呗果、不重复发奖\n' +
    '· 触发后请完全退出游戏再重新进入，邮箱才会刷新\n\n' +
    '继续？');
  if (!ok) return;
  try {
    await api('/api/deliver', {accountId:a.id});
    toast('已触发投递：请完全退出游戏后重进，在邮箱里看最新邮件', true);
  } catch (e) { toast(e.message, false); }
};
/* ------------------------------------------------------------ 授权弹窗 */
function openModal(id) { $(id).hidden = false; }
function closeModal(id) { $(id).hidden = true; }
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => closeModal(b.dataset.close));
document.querySelectorAll('.modal').forEach(m => m.addEventListener('click', e => {
  // 授权弹窗不响应「点外面关闭」：以前误点一下就把 sessionId 丢了，只能干等过期。
  // 现在会话虽然能续上，但少一次误触更省事。
  if (e.target === m && m.id !== 'authModal') m.hidden = true;
}));
/* 授权弹窗。会话 id 以前只存在这个变量里，误点弹窗外就丢了、服务端还占着会话，
   只能干等 10 分钟。现在本地存一份 + 服务端可查询，重新打开弹窗能直接续上。 */
const AUTH_KEY = 'nte.authPending';
let authTimer = null;

function loadAuthLocal() {
  try { return JSON.parse(localStorage.getItem(AUTH_KEY) || 'null'); } catch (e) { return null; }
}
function saveAuthLocal(v) {
  try { v ? localStorage.setItem(AUTH_KEY, JSON.stringify(v)) : localStorage.removeItem(AUTH_KEY); } catch (e) {}
}
function authCountdown(expiresIn, cooldownSec) {
  clearInterval(authTimer);
  let left = Math.max(0, Math.floor(expiresIn || 0));
  let cd = Math.max(0, Math.floor(cooldownSec || 0));
  const paint = () => {
    const btn = $('btnResendCode');
    if (btn) {
      btn.disabled = cd > 0;
      btn.textContent = cd > 0 ? `重新发送（${cd}s）` : '重新发送验证码';
    }
    const tip = $('authExpire');
    if (tip) tip.textContent = left > 0
      ? `会话剩余 ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}，关掉弹窗也不会丢`
      : '会话已过期，请重新发送验证码';
    if (left <= 0) {
      clearInterval(authTimer);
      authSession = null; saveAuthLocal(null);
      $('authStep1').hidden = false; $('authStep2').hidden = true;
      return false;
    }
    left -= 1; if (cd > 0) cd -= 1;
    return true;
  };
  if (paint()) authTimer = setInterval(paint, 1000);
}
function authShowStep2(mask, expiresIn, cooldownSec, resumed) {
  $('authMask').textContent = mask;
  $('authStep1').hidden = true; $('authStep2').hidden = false;
  const msg = $('authMsg');
  msg.className = 'modal-msg ok';
  msg.textContent = resumed
    ? '这个号码刚发过验证码，直接输入收到的码即可（没有收到就点重新发送）。'
    : '验证码已发送，请在 10 分钟内输入。';
  authCountdown(expiresIn || 600, cooldownSec || 0);
  setTimeout(() => $('authCode').focus(), 50);
}
$('btnAuth').onclick = async () => {
  $('authMsg').textContent = ''; $('authMsg').className = 'modal-msg';
  $('authCode').value = '';
  $('authStep1').hidden = false; $('authStep2').hidden = true;
  /* 本地只留一个「有未完成会话」的标记，不存手机号：续期靠 /api/auth/pending 回传的 phoneMask，
     没必要把完整号码留在浏览器里（同机换个系统用户就能看到）。 */
  const local = loadAuthLocal();
  openModal('authModal');
  try {
    const r = await api('/api/auth/pending', {});
    const p = r && r.pending;
    if (p) {                       // 进程里还有有效会话，直接续上
      authSession = p.sessionId;
      authShowStep2(p.phoneMask, p.expiresIn, p.cooldownSec, true);
    } else if (local) {
      saveAuthLocal(null);
    }
  } catch (e) { /* 查询失败就按全新流程走，不影响发码 */ }
};
async function authStart(force) {
  const msg = $('authMsg'); msg.className = 'modal-msg';
  const phone = ($('authPhone').value || '').replace(/\D/g, '');
  try {
    const r = await api('/api/auth/start', {phone, force: !!force});
    authSession = r.sessionId;
    saveAuthLocal({sessionId:r.sessionId});
    authShowStep2(r.phoneMask, r.expiresIn, r.cooldownSec, !!r.resumed);
  } catch (e) {
    msg.className = 'modal-msg';
    msg.textContent = e.message;
  }
}
$('btnSendCode').onclick = () => authStart(false);
$('btnResendCode').onclick = () => authStart(true);
$('btnVerify').onclick = async () => {
  const msg = $('authMsg'); msg.className = 'modal-msg';
  try {
    const r = await api('/api/auth/complete', {
      sessionId:authSession, code:$('authCode').value,
      skipCheck:$('authSkipCheck').checked, remark:$('authRemark').value,
    });
    clearInterval(authTimer); saveAuthLocal(null); authSession = null;
    msg.className = 'modal-msg ok';
    msg.textContent = `授权完成（${r.variant} / ${r.credentialType}），凭证已加密保存。`;
    setTimeout(() => { closeModal('authModal'); load(); }, 900);
  } catch (e) {
    msg.className = 'modal-msg';
    msg.textContent = e.message;
  }
};

/* ------------------------------------------------------------ 设置 */
$('btnSettings').onclick = () => {
  const s = DATA.settings;
  $('setWrite').checked = !!s.writeActionsEnabled;
  $('setAuto').checked = !!s.automationEnabled;
  $('setRefresh').value = s.refreshIntervalMin;
  $('setBatch').value = s.batchSize;
  $('setMin').value = s.minIntervalSec;
  $('setMax').value = s.maxIntervalSec;
  $('setStart').value = s.signinWindowStart;
  $('setEnd').value = s.signinWindowEnd;
  $('setWeekly').checked = !!s.weeklyCopiesVisible;
  $('setMsg').textContent = '';
  openModal('settingsModal');
};
$('btnSaveSettings').onclick = async () => {
  const values = {
    writeActionsEnabled:$('setWrite').checked,
    automationEnabled:$('setAuto').checked,
    refreshIntervalMin:Number($('setRefresh').value),
    batchSize:Number($('setBatch').value),
    minIntervalSec:Number($('setMin').value),
    maxIntervalSec:Number($('setMax').value),
    signinWindowStart:$('setStart').value,
    signinWindowEnd:$('setEnd').value,
    weeklyCopiesVisible:$('setWeekly').checked,
  };
  try {
    await api('/api/settings', {values});
    $('setMsg').className = 'modal-msg ok'; $('setMsg').textContent = '设置已保存';
    await load();
  } catch (e) { $('setMsg').className = 'modal-msg'; $('setMsg').textContent = e.message; }
};
$('btnStopAutomation').onclick = async () => {
  try {
    await api('/api/settings', {values:{automationEnabled:false, writeActionsEnabled:false}});
    $('setAuto').checked = false; $('setWrite').checked = false;
    $('setMsg').className = 'modal-msg ok';
    $('setMsg').textContent = '自动化与写操作已全部停用，队列保留但不会自动执行。';
    await load();
  } catch (e) { $('setMsg').textContent = e.message; }
};

loadDisplay();
load();
setInterval(load, 3000);

/* 开发自测钩子：?preview=sign1 直接打开 1 号账号的签到详情（仅本地调试用，不影响正常使用） */
(function () {
  const pv = new URLSearchParams(location.search).get('preview');
  if (!pv || pv.indexOf('sign') !== 0) return;
  const id = Number(pv.slice(4)) || 1;
  setTimeout(() => { if (DATA) openSignModal(id); }, 1500);
})();
