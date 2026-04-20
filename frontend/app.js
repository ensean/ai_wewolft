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
  $('toggle-history-btn').addEventListener('click', toggleHistory);
  renderPlayerRows();
});

// ---- History panel ----
async function toggleHistory() {
  const list = $('history-list');
  const btn  = $('toggle-history-btn');
  if (list.style.display !== 'none') {
    list.style.display = 'none';
    btn.textContent = '展开';
    return;
  }
  list.style.display = 'block';
  btn.textContent = '收起';
  list.innerHTML = '<div style="color:#888">加载中…</div>';
  try {
    const res = await fetch('/api/games/history');
    const data = await res.json();
    const games = data.games || [];
    if (!games.length) {
      list.innerHTML = '<div style="color:#666;font-size:0.85rem">还没有已结束的游戏</div>';
      return;
    }
    list.innerHTML = games.map(g => {
      const winner = g.winner === 'werewolves' ? '🐺 狼人胜' :
                     g.winner === 'villagers'  ? '👨 好人胜' : '— 未完成';
      const t = new Date(g.ended_at).toLocaleString('zh-CN', {hour12:false});
      return `
        <div class="history-row">
          <div class="history-main">
            <div class="history-title">${winner} · ${g.total_rounds}轮 · ${g.player_count}人</div>
            <div class="history-sub">${g.game_id} · ${t}</div>
          </div>
          <button class="btn-sm" onclick="replayGame('${g.game_id}')">▶ 回放</button>
        </div>`;
    }).join('');
  } catch (e) {
    list.innerHTML = `<div style="color:#ef4444">加载失败：${e.message}</div>`;
  }
}

async function replayGame(game_id) {
  // Switch to game view and replay via SSE
  hide('setup-screen');
  show('game-screen');
  $('log-panel').innerHTML = '';
  $('player-list').innerHTML = '';
  players = [];
  $('phase-info').textContent = `回放 · ${game_id}`;

  if (eventSource) { eventSource.close(); }
  eventSource = new EventSource(`/api/games/${game_id}/replay?speed=4`);
  eventSource.onmessage = (e) => {
    try { handleEvent(JSON.parse(e.data)); } catch {}
  };
  eventSource.onerror = () => {
    appendLog(null, '回放结束', 'system');
    eventSource.close();
  };
}

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
        <td style="text-align:center">
          <input type="radio" name="human-player" class="p-human" value="${i}">
        </td>
      </tr>`;
  }
  tbody.innerHTML = html;

  // Toggle model/key inputs when human radio changes
  tbody.querySelectorAll('.p-human').forEach(radio => {
    radio.addEventListener('change', () => {
      tbody.querySelectorAll('tr').forEach(tr => {
        const isHuman = tr.querySelector('.p-human')?.checked;
        tr.querySelectorAll('.p-model, .p-apikey').forEach(el => {
          el.disabled = isHuman;
          el.style.opacity = isHuman ? '0.3' : '1';
        });
      });
    });
  });
}

// ---- Start game ----
async function startGame() {
  const btn = $('start-btn');
  btn.disabled = true;
  btn.textContent = '启动中…';

  const region = $('aws-region').value.trim() || 'us-east-1';
  const accessKey = $('aws-key').value.trim() || null;
  const secretKey = $('aws-secret').value.trim() || null;

  const nameInputs   = document.querySelectorAll('.p-name');
  const modelInputs  = document.querySelectorAll('.p-model');
  const apikeyInputs = document.querySelectorAll('.p-apikey');
  const humanInputs  = document.querySelectorAll('.p-human');

  const playerConfigs = [];
  for (let i = 0; i < nameInputs.length; i++) {
    const isHuman = humanInputs[i]?.checked || false;
    playerConfigs.push({
      name: nameInputs[i].value.trim() || `玩家${i+1}`,
      model_id: isHuman ? '' : modelInputs[i].value,
      is_human: isHuman,
      aws_access_key_id: isHuman ? null : accessKey,
      aws_secret_access_key: isHuman ? null : secretKey,
      api_key: isHuman ? null : (apikeyInputs[i]?.value.trim() || null),
    });
  }

  try {
    const res = await fetch('/api/games', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        player_configs: playerConfigs,
        aws_region: region,
        quick_model_id: $('quick-model').value.trim() || null,
        enable_sheriff: $('enable-sheriff').checked,
      }),
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
    case 'last_words':            onLastWords(ev); break;
    case 'vote_tally':            onVoteTally(ev); break;
    case 'sheriff_campaign_start': onSheriffStart(ev); break;
    case 'sheriff_candidates':     onSheriffCandidates(ev); break;
    case 'sheriff_campaign':       onSheriffCampaign(ev); break;
    case 'sheriff_vote':           onSheriffVote(ev); break;
    case 'sheriff_elected':        onSheriffElected(ev); break;
    case 'sheriff_badge_handoff':  onBadgeHandoff(ev); break;
    case 'human_role_reveal':     onHumanRoleReveal(ev); break;
    case 'human_input_required':  onHumanInputRequired(ev); break;
    case 'human_input_done':      onHumanInputDone(ev); break;
    case 'game_end':              onGameEnd(ev); break;
    case 'error':         onError(ev); break;
  }
}

// ---- Event handlers ----

// model_id lookup: populated by game_start
const playerModels = {};  // id -> model_id

// Human player state
let humanPlayerId = null;
let humanRole = null;
let humanTimerInterval = null;
let sheriffId = null;  // current sheriff player_id

function onGameStart(ev) {
  const { player_count, players: plist } = ev.data;
  (plist || []).forEach(p => { playerModels[p.id] = p.model_id; });
  appendLog(null, `🎮 游戏开始，共 ${player_count} 名玩家`, 'phase');
  $('phase-info').textContent = `游戏开始 · ${player_count}人`;
}

function onRoleAssign(ev) {
  const { assignments, role_counts } = ev.data;
  players = assignments.map(a => ({
    ...a,
    is_alive: true,
    role: null,
    role_label: null,
    model_id: playerModels[a.id] || '',
  }));
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

function onLastWords(ev) {
  const { player_name, role_label, content, cause } = ev.data;
  const causeIcon = {
    wolf_kill: '🌙',
    voted_out: '🗳️',
    hunter_shot: '🔫',
    witch_poison: '☠️',
  }[cause] || '💀';
  const entry = document.createElement('div');
  entry.className = 'log-entry last-words';
  entry.innerHTML =
    `<div class="lw-header">${causeIcon} <strong>${esc(player_name)}</strong> 的遗言 <span class="lw-role">[${esc(role_label || '')}]</span></div>` +
    `<div class="lw-content">“${esc(content)}”</div>`;
  $('log-panel').appendChild(entry);
  scrollLog();
}

// ---- Sheriff election events ----
function onSheriffStart(ev) {
  appendLog(null, ev.data.message || '💂 警长竞选开始', 'phase');
}

function onSheriffCandidates(ev) {
  const names = ev.data.candidates.map(c => c.name).join('、');
  appendLog(null, `💂 上警名单：${names}`, 'system');
}

function onSheriffCampaign(ev) {
  const { player_name, content } = ev.data;
  const entry = document.createElement('div');
  entry.className = 'log-entry speech sheriff-campaign';
  entry.innerHTML =
    `<span class="speaker">💂 ${esc(player_name)}（竞选）</span>` +
    `<span class="speech-content">${esc(content)}</span>`;
  $('log-panel').appendChild(entry);
  scrollLog();
}

function onSheriffVote(ev) {
  const { voter_name, target_name } = ev.data;
  appendLog(null, `🗳️ ${voter_name} 支持 ${target_name} 当警长`, 'vote');
}

function onSheriffElected(ev) {
  const { sheriff_id, sheriff_name, vote_count, uncontested } = ev.data;
  sheriffId = sheriff_id;
  const desc = uncontested ? '独自上警自动当选'
                           : `${vote_count} 票当选`;
  const entry = document.createElement('div');
  entry.className = 'log-entry phase sheriff-elected';
  entry.textContent = `👮 ${sheriff_name} 当选警长！（${desc}）`;
  $('log-panel').appendChild(entry);
  scrollLog();
  renderSidebar();
}

function onBadgeHandoff(ev) {
  const { from_name, to_name, action } = ev.data;
  if (action === 'pass') {
    sheriffId = ev.data.to_id;
    appendLog(null, `👮 ${from_name} 将警徽移交给 ${to_name}`, 'phase');
  } else {
    sheriffId = null;
    appendLog(null, `💥 ${from_name} 撕毁警徽，本局不再有警长`, 'phase');
  }
  renderSidebar();
}

// ---- Vote tally: live-updating bar chart ----
function onVoteTally(ev) {
  const { items, voted, total, final } = ev.data;
  // Keep only one "live" tally element per round, update in place
  let el = document.getElementById('tally-live');
  if (!el) {
    el = document.createElement('div');
    el.id = 'tally-live';
    el.className = 'log-entry tally';
    $('log-panel').appendChild(el);
  }
  const maxVotes = Math.max(1, ...items.map(i => i.votes));
  const bars = items.map(i => {
    const pct = Math.round(100 * i.votes / maxVotes);
    return `
      <div class="tally-row">
        <div class="tally-name">${esc(i.target_name)}</div>
        <div class="tally-bar-wrap"><div class="tally-bar" style="width:${pct}%"></div></div>
        <div class="tally-count">${i.votes}</div>
      </div>`;
  }).join('');
  el.innerHTML =
    `<div class="tally-header">🗳️ 投票进度 ${voted}/${total}${final ? ' · 最终票型' : ''}</div>` +
    `<div class="tally-bars">${bars}</div>`;
  if (final) {
    // Freeze by removing the live id so next round makes a new one
    el.id = '';
    el.classList.add('tally-final');
  }
  scrollLog();
}

function onGameEnd(ev) {
  const { winner_label, reason, all_roles, total_rounds, usage } = ev.data;

  // Reveal all roles in sidebar
  players = all_roles.map(r => ({
    id: r.id, name: r.name, is_alive: r.alive, role: r.role, role_label: r.role_label,
    model_id: playerModels[r.id] || '',
  }));
  renderSidebar(true);

  $('phase-info').textContent = `游戏结束 · 共 ${total_rounds} 轮`;

  // Build roles grid
  const chipsHtml = all_roles.map(r =>
    `<span class="role-chip ${r.role} ${r.alive ? '' : 'dead'}">${r.name} ${r.role_label}</span>`
  ).join('');

  const usageHtml = usage && usage.input_tokens > 0
    ? `<div class="usage-stats">
         📊 Token 用量：输入 ${usage.input_tokens.toLocaleString()} / 输出 ${usage.output_tokens.toLocaleString()}
         ${usage.cache_read_tokens > 0
           ? `· 缓存命中 ${usage.cache_read_tokens.toLocaleString()} (${usage.cache_hit_pct}%)`
           : ''}
       </div>`
    : '';

  const entry = document.createElement('div');
  entry.className = 'log-entry game-end';
  entry.innerHTML = `
    <div class="winner">🏆 ${esc(winner_label)}获胜！</div>
    <div class="reason">${esc(reason)}</div>
    <div class="roles-grid">${chipsHtml}</div>
    ${usageHtml}
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
  list.innerHTML = players.map(p => {
    const modelShort = shortModel(p.model_id || '');
    const sheriffBadge = p.id === sheriffId ? '<span class="sheriff-badge" title="警长">👮</span>' : '';
    return `
    <div class="player-card ${p.is_alive ? '' : 'dead'} ${p.id === humanPlayerId ? 'human-me' : ''}">
      <div class="dot"></div>
      <div class="player-info">
        <div class="player-name-row">
          <span class="pid">${p.id}</span>
          <span class="pname">${esc(p.name)}</span>
          ${sheriffBadge}
          ${(showRoles || p.role) && p.role_label
            ? `<span class="role-badge">${esc(p.role_label)}</span>`
            : ''}
        </div>
        ${modelShort ? `<div class="player-model">${esc(modelShort)}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

// ===========================================================================
// Human player handlers
// ===========================================================================

function onHumanRoleReveal(ev) {
  const { player_id, role, role_label, wolf_allies } = ev.data;
  humanPlayerId = player_id;
  humanRole = role;
  // Show role badge persistently
  const roleClass = { werewolf:'🐺', villager:'👨', seer:'🔮', witch:'🧪', hunter:'🔫' }[role] || '🎭';
  $('hp-role-badge').textContent = `${roleClass} 你的身份：${role_label}`;
  $('hp-role-badge').className = `hp-role hp-role-${role}`;
  if (wolf_allies && wolf_allies.length) {
    $('hp-private-info').textContent = `狼队友：${wolf_allies.map(a => a.name).join('、')}`;
  }
  $('human-panel').classList.add('active');
}

function onHumanInputRequired(ev) {
  if (ev.data.player_id !== humanPlayerId) return;
  const { action_type, prompt, candidates, kill_target,
          save_available, poison_available, timeout } = ev.data;

  $('hp-prompt').textContent = prompt || '';
  $('hp-controls').innerHTML = '';

  if (['speak', 'last_words', 'werewolf_discuss', 'sheriff_campaign'].includes(action_type)) {
    // Text input
    const ta = document.createElement('textarea');
    ta.className = 'hp-textarea';
    ta.placeholder = action_type === 'sheriff_campaign'
      ? '说服大家投你当警长…' : '输入你的发言…';
    ta.rows = 3;
    const btn = document.createElement('button');
    btn.className = 'btn-primary hp-submit';
    btn.textContent = '提交';
    btn.onclick = () => submitHumanInput(ta.value.trim() || '（沉默）');
    $('hp-controls').append(ta, btn);
    setTimeout(() => ta.focus(), 100);

  } else if (action_type === 'run_for_sheriff') {
    $('hp-controls').appendChild(_actionBtn('💂 上警', 'save', () => submitHumanInput('yes')));
    $('hp-controls').appendChild(_actionBtn('不上警', 'skip', () => submitHumanInput('no')));

  } else if (action_type === 'badge_decision') {
    if (candidates?.length) {
      const div = document.createElement('div');
      div.innerHTML = '<div class="hp-section-label">👮 移交警徽给：</div>';
      candidates.forEach(c => {
        div.appendChild(_actionBtn(c.name, 'save', () => submitHumanInput(`pass:${c.id}`)));
      });
      $('hp-controls').appendChild(div);
    }
    $('hp-controls').appendChild(_actionBtn('💥 撕毁警徽', 'skip', () => submitHumanInput('destroy')));

  } else if (action_type === 'witch_decide') {
    // Save button
    if (save_available && kill_target) {
      const btn = _actionBtn(`💊 救 ${kill_target.name}`, 'save', () => submitHumanInput('save'));
      $('hp-controls').appendChild(btn);
    }
    // Poison buttons
    if (poison_available && candidates?.length) {
      const div = document.createElement('div');
      div.innerHTML = '<div class="hp-section-label">☠️ 毒死：</div>';
      candidates.forEach(c => {
        div.appendChild(_actionBtn(c.name, 'poison', () => submitHumanInput(`poison:${c.id}`)));
      });
      $('hp-controls').appendChild(div);
    }
    // Skip
    $('hp-controls').appendChild(_actionBtn('跳过', 'skip', () => submitHumanInput('skip')));

  } else if (action_type === 'hunter_shoot') {
    if (candidates?.length) {
      candidates.forEach(c => {
        $('hp-controls').appendChild(_actionBtn(`🔫 ${c.name}`, 'shoot', () => submitHumanInput(String(c.id))));
      });
    }
    $('hp-controls').appendChild(_actionBtn('不开枪', 'skip', () => submitHumanInput('skip')));

  } else if (candidates?.length) {
    // Generic player-choice (vote, seer_check, werewolf_vote_kill)
    candidates.forEach(c => {
      $('hp-controls').appendChild(_actionBtn(c.name, 'pick', () => submitHumanInput(String(c.id))));
    });
  }

  startTimer(timeout || 180);
}

function onHumanInputDone(ev) {
  if (ev.data.player_id !== humanPlayerId) return;
  $('hp-controls').innerHTML = '<div style="color:#22c55e;padding:0.5rem">✓ 已提交</div>';
  $('hp-prompt').textContent = '';
  clearTimer();
}

function _actionBtn(label, cls, onclick) {
  const btn = document.createElement('button');
  btn.className = `hp-action-btn hp-action-${cls}`;
  btn.textContent = label;
  btn.onclick = onclick;
  return btn;
}

async function submitHumanInput(value) {
  if (!humanPlayerId) return;
  // Disable all controls immediately to prevent double-submit
  $('hp-controls').querySelectorAll('button,textarea').forEach(el => el.disabled = true);
  try {
    await fetch('/api/games/input', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ player_id: humanPlayerId, value }),
    });
  } catch (e) {
    console.error('submitHumanInput failed:', e);
  }
}

// Also update seer result in private info area
const _origSeerResult = onSeerResult;
// eslint-disable-next-line no-global-assign
function onSeerResult(ev) {
  _origSeerResult(ev);
  // If human is the seer, update private info area
  if (ev.data.seer_id === humanPlayerId) {
    const cur = $('hp-private-info').textContent;
    const add = `查验：${ev.data.target_name}=${ev.data.result}`;
    $('hp-private-info').textContent = cur ? `${cur} · ${add}` : add;
  }
}

// ---- Timer ----
function startTimer(seconds) {
  clearTimer();
  let remaining = seconds;
  const el = $('hp-timer');
  const update = () => {
    const m = Math.floor(remaining / 60);
    const s = remaining % 60;
    el.textContent = `⏱ ${m}:${s.toString().padStart(2,'0')}`;
    el.style.color = remaining <= 30 ? '#ef4444' : '#888';
    if (remaining-- <= 0) clearTimer();
  };
  update();
  humanTimerInterval = setInterval(update, 1000);
}
function clearTimer() {
  if (humanTimerInterval) { clearInterval(humanTimerInterval); humanTimerInterval = null; }
  if ($('hp-timer')) $('hp-timer').textContent = '';
}

function shortModel(model_id) {
  if (!model_id) return '';
  // Strip common prefixes for brevity
  return model_id
    .replace(/^(us\.|global\.)/, '')
    .replace(/^anthropic\./, '')
    .replace(/^amazon\./, '')
    .replace(/^meta\./, '')
    .replace(/-\d{8}-v\d:\d$/, '')  // remove date+version suffix
    .replace(/-v\d:\d$/, '')
    .replace(/-v\d$/, '');
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
  clearTimer();
  humanPlayerId = null;
  humanRole = null;
  sheriffId = null;
  $('human-panel').classList.remove('active');
  hide('game-screen');
  show('setup-screen');
  const btn = $('start-btn');
  btn.disabled = false;
  btn.textContent = '开始游戏';
}
