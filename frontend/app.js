'use strict';

// ---- State ----
let eventSource = null;
let players = [];  // [{id, name, is_alive, role, role_label}]

// ---- DOM helpers ----
const $ = id => document.getElementById(id);
const show = id => { $(id).classList.add('active'); };
const hide = id => { $(id).classList.remove('active'); };

// ---- Init ----
// Bedrock models (loaded live from AWS), other models (static)
window._bedrockModels = [];
window._otherModels   = [];

window.addEventListener('DOMContentLoaded', async () => {
  await loadModels();
  $('player-count').addEventListener('change', renderPlayerRows);
  $('start-btn').addEventListener('click', startGame);
  $('load-bedrock-btn').addEventListener('click', loadBedrockModels);
  renderPlayerRows();
});

// ---- Load static (non-Bedrock) models ----
async function loadModels() {
  try {
    const res = await fetch('/api/models');
    const data = await res.json();
    window._otherModels = data.models || [];
  } catch {
    window._otherModels = [
      { id: 'moonshot-v1-8k',  label: 'moonshot-v1-8k',  group: 'Kimi' },
      { id: 'deepseek-chat',   label: 'deepseek-chat',    group: 'DeepSeek' },
      { id: 'glm-4-flash',     label: 'glm-4-flash',      group: 'GLM' },
      { id: 'MiniMax-Text-01', label: 'MiniMax-Text-01',  group: 'MiniMax' },
    ];
  }
}

// ---- Load live Bedrock models from AWS ----
async function loadBedrockModels() {
  const btn = $('load-bedrock-btn');
  btn.disabled = true;
  btn.textContent = '加载中…';

  try {
    const res = await fetch('/api/bedrock-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        region: $('aws-region').value.trim() || 'us-east-1',
        aws_access_key_id: $('aws-key').value.trim() || null,
        aws_secret_access_key: $('aws-secret').value.trim() || null,
      }),
    });
    if (!res.ok) {
      const err = await res.json();
      alert('获取失败: ' + (err.detail || '未知错误'));
      return;
    }
    const data = await res.json();
    window._bedrockModels = data.models || [];
    btn.textContent = `✓ 已加载 ${window._bedrockModels.length} 个模型`;
    // Refresh dropdowns while preserving currently selected values
    refreshModelDropdowns();
  } catch (e) {
    alert('网络错误: ' + e.message);
    btn.textContent = '🔄 加载 Bedrock 模型';
  } finally {
    btn.disabled = false;
  }
}

// ---- Refresh model dropdowns in all player rows (preserve selection) ----
function refreshModelDropdowns() {
  const selects = document.querySelectorAll('.p-model');
  const optHtml = buildModelOptHtml();
  selects.forEach(sel => {
    const prev = sel.value;
    sel.innerHTML = optHtml;
    // Restore previous selection if still available
    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
  });
}

// ---- Build model <optgroup> HTML from both sources ----
function buildModelOptHtml() {
  const allModels = [...(window._bedrockModels || []), ...(window._otherModels || [])];
  const groups = {};
  allModels.forEach(m => {
    const g = m.group || '其他';
    if (!groups[g]) groups[g] = [];
    groups[g].push(m);
  });
  // Bedrock groups first, then others
  const bedrockGroups = Object.keys(groups).filter(g => !['Kimi','DeepSeek','MiniMax','GLM'].includes(g));
  const otherGroups   = Object.keys(groups).filter(g =>  ['Kimi','DeepSeek','MiniMax','GLM'].includes(g));
  return [...bedrockGroups, ...otherGroups]
    .map(g =>
      `<optgroup label="${g}">${groups[g].map(m => `<option value="${m.id}">${m.label}</option>`).join('')}</optgroup>`
    ).join('');
}

// ---- Render player config rows ----
function renderPlayerRows() {
  const n = parseInt($('player-count').value) || 6;
  const tbody = $('players-tbody');
  const defaultNames = ['张三', '李四', '王五', '赵六', '孙七', '周八', '吴九', '郑十', '冯十一', '陈十二'];
  const modelOpts = buildModelOptHtml();

  let html = '';
  for (let i = 1; i <= n; i++) {
    html += `
      <tr>
        <td style="color:#555;text-align:center">${i}</td>
        <td><input type="text" class="p-name" placeholder="玩家${i}" value="${defaultNames[i-1] || '玩家'+i}"></td>
        <td>
          <select class="p-model">
            ${modelOpts || '<option value="">（先点击加载 Bedrock 模型）</option>'}
          </select>
        </td>
        <td><input type="password" class="p-apikey" placeholder="sk-…（Kimi/DeepSeek/MiniMax/GLM）"></td>
      </tr>`;
  }
  tbody.innerHTML = html;
}

// ---- Start game ----
async function startGame() {
  const btn = $('start-btn');
  btn.disabled = true;
  btn.textContent = '启动中…';

  const region = $('aws-region').value.trim() || 'us-east-1';
  const accessKey = $('aws-key').value.trim() || null;
  const secretKey = $('aws-secret').value.trim() || null;

  const nameInputs  = document.querySelectorAll('.p-name');
  const modelInputs = document.querySelectorAll('.p-model');
  const apikeyInputs = document.querySelectorAll('.p-apikey');

  const playerConfigs = [];
  for (let i = 0; i < nameInputs.length; i++) {
    playerConfigs.push({
      name: nameInputs[i].value.trim() || `玩家${i+1}`,
      model_id: modelInputs[i].value,
      aws_access_key_id: accessKey,
      aws_secret_access_key: secretKey,
      api_key: apikeyInputs[i]?.value.trim() || null,
    });
  }

  try {
    const res = await fetch('/api/games', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ player_configs: playerConfigs, aws_region: region }),
    });
    if (!res.ok) {
      const err = await res.json();
      alert('启动失败: ' + (err.detail || '未知错误'));
      btn.disabled = false;
      btn.textContent = '开始游戏';
      return;
    }

    // Switch to game screen
    hide('setup-screen');
    show('game-screen');
    $('log-panel').innerHTML = '';
    $('player-list').innerHTML = '';
    $('phase-info').textContent = '游戏初始化…';

    // Connect SSE
    connectSSE();

  } catch (e) {
    alert('网络错误: ' + e.message);
    btn.disabled = false;
    btn.textContent = '开始游戏';
  }
}

// ---- SSE connection ----
function connectSSE() {
  if (eventSource) { eventSource.close(); }
  eventSource = new EventSource('/api/games/events');

  eventSource.onmessage = (e) => {
    try {
      const event = JSON.parse(e.data);
      handleEvent(event);
    } catch {}
  };

  eventSource.onerror = () => {
    appendLog('系统', '连接中断，游戏可能已结束。', 'system');
    eventSource.close();
  };
}

// ---- Event dispatch ----
function handleEvent(ev) {
  switch (ev.type) {
    case 'game_start':    onGameStart(ev); break;
    case 'role_assign':   onRoleAssign(ev); break;
    case 'phase_start':   onPhaseStart(ev); break;
    case 'system':        onSystem(ev); break;
    case 'speech':        onSpeech(ev); break;
    case 'vote':          onVote(ev); break;
    case 'death':         onDeath(ev); break;
    case 'wolf_discuss':  onWolfDiscuss(ev); break;
    case 'seer_result':   onSeerResult(ev); break;
    case 'witch_action':  onWitchAction(ev); break;
    case 'hunter_shot':   onHunterShot(ev); break;
    case 'game_end':      onGameEnd(ev); break;
    case 'error':         onError(ev); break;
  }
}

// ---- Event handlers ----

function onGameStart(ev) {
  const { player_count } = ev.data;
  appendLog(null, `🎮 游戏开始，共 ${player_count} 名玩家`, 'phase');
  $('phase-info').textContent = `游戏开始 · ${player_count}人`;
}

function onRoleAssign(ev) {
  const { assignments, role_counts } = ev.data;
  players = assignments.map(a => ({ ...a, is_alive: true, role: null, role_label: null }));
  renderSidebar();

  const counts = Object.entries(role_counts)
    .map(([r, n]) => `${roleLabel(r)}×${n}`)
    .join('  ');
  appendLog(null, `🃏 角色分配完成：${counts}`, 'system');
}

function onPhaseStart(ev) {
  const { phase, message, round } = ev.data;
  if (message) {
    const icon = phase === 'night' ? '🌙' : '☀️';
    appendLog(null, message || `${icon} 第${ev.round}轮 ${phase === 'night' ? '夜晚' : '白天'}`, 'phase');
  }
  const label = phase === 'night' ? '🌙 夜晚' : '☀️ 白天';
  $('phase-info').textContent = `第 ${ev.round} 轮 · ${label}`;
}

function onSystem(ev) {
  const msg = ev.data.message || '';
  appendLog(null, msg, 'system');
  if (msg.includes('夜')) $('phase-info').textContent = `第 ${ev.round} 轮 · 🌙 夜晚`;
  if (msg.includes('天亮')) $('phase-info').textContent = `第 ${ev.round} 轮 · ☀️ 白天`;
  // Show thinking placeholders when discussion starts
  if (msg.includes('发言顺序')) {
    // parse names from "发言顺序：A→B→C"
    const match = msg.match(/发言顺序：(.+)/);
    if (match) {
      match[1].split('→').forEach(name => {
        const p = players.find(x => x.name === name.trim());
        if (p) showThinking(p.id, p.name);
      });
    }
  }
}

function onSpeech(ev) {
  const { player_id, player_name, content } = ev.data;
  // Remove any "thinking…" placeholder for this player
  const placeholder = document.getElementById(`thinking-${player_id}`);
  if (placeholder) placeholder.remove();

  const text = (content || '').trim() || '（沉默）';
  const entry = document.createElement('div');
  entry.className = 'log-entry speech';
  entry.innerHTML =
    `<span class="speaker">💬 ${esc(player_name)}</span>` +
    `<span class="speech-content">${esc(text)}</span>`;
  $('log-panel').appendChild(entry);
  scrollLog();
}

function showThinking(player_id, player_name) {
  const existing = document.getElementById(`thinking-${player_id}`);
  if (existing) return;
  const el = document.createElement('div');
  el.className = 'log-entry thinking';
  el.id = `thinking-${player_id}`;
  el.textContent = `⏳ ${player_name} 思考中…`;
  $('log-panel').appendChild(el);
  scrollLog();
}

function onVote(ev) {
  const { voter_name, target_name } = ev.data;
  appendLog(null, `🗳️ ${voter_name} 投票驱逐 ${target_name}`, 'vote');
}

function onDeath(ev) {
  const { player_name, cause, role_revealed } = ev.data;
  const causeText = {
    wolf_kill: '被狼人击杀',
    witch_poison: '被女巫毒死',
    voted_out: '被投票驱逐',
    hunter_shot: '被猎人击毙',
  }[cause] || cause;
  const roleText = roleLabel(role_revealed);
  appendLog(null, `💀 ${player_name} ${causeText}，身份揭晓：${roleText}`, 'death');

  // Update sidebar
  const p = players.find(x => x.name === player_name);
  if (p) {
    p.is_alive = false;
    p.role = role_revealed;
    p.role_label = roleText;
  }
  renderSidebar();
}

function onWolfDiscuss(ev) {
  const { player_name, content } = ev.data;
  appendLog(null, `🐺 [狼人私聊] ${player_name}：${content}`, 'private');
}

function onSeerResult(ev) {
  const { seer_name, target_name, result } = ev.data;
  appendLog(null, `🔮 [预言家私信] ${seer_name} 查验了 ${target_name}，结果：${result}`, 'private');
}

function onWitchAction(ev) {
  const { witch_name, action, target_name } = ev.data;
  const desc = {
    save: `对 ${target_name} 使用了解药`,
    poison: `对 ${target_name} 使用了毒药`,
    skip: '选择不使用任何道具',
  }[action] || action;
  appendLog(null, `🧪 [女巫私信] ${witch_name} ${desc}`, 'private');
}

function onHunterShot(ev) {
  const { hunter_name, target_name, role_revealed } = ev.data;
  appendLog(null, `🔫 猎人 ${hunter_name} 开枪击中 ${target_name}（${roleLabel(role_revealed)}）！`, 'hunter');
  const p = players.find(x => x.name === target_name);
  if (p) { p.is_alive = false; p.role = role_revealed; p.role_label = roleLabel(role_revealed); }
  renderSidebar();
}

function onGameEnd(ev) {
  const { winner_label, reason, all_roles, total_rounds } = ev.data;

  // Reveal all roles in sidebar
  players = all_roles.map(r => ({
    id: r.id, name: r.name, is_alive: r.alive, role: r.role, role_label: r.role_label,
  }));
  renderSidebar(true);

  $('phase-info').textContent = `游戏结束 · 共 ${total_rounds} 轮`;

  // Build roles grid
  const chipsHtml = all_roles.map(r =>
    `<span class="role-chip ${r.role} ${r.alive ? '' : 'dead'}">${r.name} ${r.role_label}</span>`
  ).join('');

  const entry = document.createElement('div');
  entry.className = 'log-entry game-end';
  entry.innerHTML = `
    <div class="winner">🏆 ${esc(winner_label)}获胜！</div>
    <div class="reason">${esc(reason)}</div>
    <div class="roles-grid">${chipsHtml}</div>
    <button class="new-game-btn" onclick="resetToSetup()">再来一局</button>
  `;
  $('log-panel').appendChild(entry);
  scrollLog();

  if (eventSource) { eventSource.close(); eventSource = null; }
}

function onError(ev) {
  appendLog(null, `❌ 错误：${ev.data.message}`, 'system');
}

// ---- Sidebar ----
function renderSidebar(showRoles = false) {
  const list = $('player-list');
  list.innerHTML = players.map(p => `
    <div class="player-card ${p.is_alive ? '' : 'dead'}">
      <div class="dot"></div>
      <span class="pid">${p.id}</span>
      <span>${esc(p.name)}</span>
      ${(showRoles || p.role) && p.role_label
        ? `<span class="role-badge">${esc(p.role_label)}</span>`
        : ''}
    </div>
  `).join('');
}

// ---- Utils ----
function appendLog(label, text, cls) {
  const entry = document.createElement('div');
  entry.className = `log-entry ${cls || 'system'}`;
  entry.textContent = label ? `${label}: ${text}` : text;
  $('log-panel').appendChild(entry);
  scrollLog();
}

function scrollLog() {
  const panel = $('log-panel');
  panel.scrollTop = panel.scrollHeight;
}

function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function roleLabel(role) {
  const map = {
    werewolf: '狼人', villager: '村民', seer: '预言家',
    witch: '女巫', hunter: '猎人',
  };
  return map[role] || role;
}

function resetToSetup() {
  if (eventSource) { eventSource.close(); eventSource = null; }
  hide('game-screen');
  show('setup-screen');
  const btn = $('start-btn');
  btn.disabled = false;
  btn.textContent = '开始游戏';
}
