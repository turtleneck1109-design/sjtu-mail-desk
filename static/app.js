'use strict';
const $ = (id) => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.get('token')) {
  sessionStorage.setItem('maildesk-token', fragment.get('token'));
  history.replaceState(null, '', '/');
}
const token = sessionStorage.getItem('maildesk-token') || '';
const labels = {pending:'待发送', sent:'已接受', invalid:'检查未通过', failed:'明确失败', partial:'部分接受', uncertain:'待核对', attempting:'提交中'};
let state = {items:[], history:[], job:null, demo:false};
let selected = new Set(), initialized = false, uploading = false, plan = null, detailId = null, activeView = 'drafts';
let listSignature = '', toastTimer;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const busy = () => ['running','stopping'].includes(state.job?.status);
const sizeText = (bytes) => bytes >= 1048576 ? (bytes/1048576).toFixed(1)+' MB' : bytes >= 1024 ? Math.ceil(bytes/1024)+' KB' : bytes+' B';
const badge = (status) => `<span class="badge ${esc(status)}">${esc(labels[status] || status)}</span>`;
const timeText = (value) => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '';
function toast(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 5000);
}
async function api(path, payload, raw = false) {
  const options = {headers:{'X-MailDesk-Token':token}, cache:'no-store'};
  if (payload !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
    options.body = raw ? payload : JSON.stringify(payload);
  }
  const response = await fetch('/api/'+path, options);
  if (!response.ok) {
    let message = '操作失败，请重试';
    try { message = (await response.json()).error || message; } catch {}
    throw new Error(message);
  }
  return response;
}
const json = async (path, payload) => (await api(path, payload)).json();
async function download(path, name) {
  try {
    const blob = await (await api(path)).blob();
    const url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = name; document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 15000);
  } catch (error) { toast(error.message); }
}
function filteredItems() {
  const query = $('search').value.trim().toLowerCase(), filter = $('filter').value;
  return state.items.filter(item => {
    const matches = !query || [item.subject,item.filename,...item.recipients].join(' ').toLowerCase().includes(query);
    return matches && (filter === 'all' || (filter === 'eligible' && item.eligible) ||
      (filter === 'sent' && item.status === 'sent') || (filter === 'attention' && ['invalid','failed','partial','uncertain'].includes(item.status)));
  });
}
function controls() {
  const picked = state.items.filter(i => selected.has(i.id));
  const eligible = picked.filter(i => i.eligible);
  $('selection-count').textContent = `已选择 ${picked.length} 封`;
  $('send-selected').textContent = state.demo ? `预览并模拟发送${eligible.length ? ' '+eligible.length+' 封' : '所选'}` : `预览并发送${eligible.length ? ' '+eligible.length+' 封' : '所选'}`;
  if (picked.some(i=>i.status==='invalid')) $('send-selected').textContent = '请先取消选择问题草稿';
  $('send-selected').disabled = busy() || uploading || !eligible.length || picked.some(i=>i.status==='invalid');
  $('test-send').disabled = busy() || uploading || picked.length !== 1 || picked[0]?.status === 'invalid';
  $('remove-selected').disabled = busy() || uploading || !picked.length;
  $('choose-files').disabled = busy() || uploading;
  for (const id of ['account','test-to','delay']) $(id).disabled = busy();
  const shown = filteredItems();
  const chosen = shown.filter(i => selected.has(i.id)).length;
  $('select-all').checked = shown.length > 0 && chosen === shown.length;
  $('select-all').indeterminate = chosen > 0 && chosen < shown.length;
  $('select-all').disabled = busy() || uploading || !shown.length;
}
function renderList(force = false) {
  const shown = filteredItems();
  const signature = JSON.stringify([shown, [...selected],busy(),uploading]);
  if (!force && signature === listSignature) { controls(); return; }
  listSignature = signature;
  $('list-count').textContent = shown.length;
  $('empty-state').hidden = shown.length > 0;
  $('empty-state').querySelector('h3').textContent = state.items.length ? '没有符合条件的草稿' : '你的第一批邮件，从这里开始';
  $('empty-state').querySelector('p').textContent = state.items.length ? '试试其他关键词或状态筛选。' : '导入已有 .eml 草稿，先检查收件人与附件。预览不会连接邮件服务器。';
  $('mail-rows').innerHTML = shown.map(item => {
    const to = item.recipients[0] || '无有效收件人';
    const extra = item.recipients.length > 1 ? ` 等 ${item.recipients.length} 个地址` : '';
    const files = item.attachments;
    const attachmentTitle = files.map(a => a.name).join('、');
    const attachmentLabel = files.length ? (files.length === 1 ? (files[0].name.split('.').pop().toUpperCase().slice(0,7)) : `${files.length} 个附件`) : '';
    return `<tr><td class="check-cell"><input type="checkbox" data-select="${item.id}" aria-label="选择 ${esc(item.subject)}" ${selected.has(item.id)?'checked':''} ${busy()||uploading?'disabled':''}></td><td><button class="subject-button" data-detail="${item.id}">${esc(item.subject)}</button><div class="mail-meta">${esc(to+extra)}<span class="file-label">${esc(item.filename)}${item.test_count ? ' · 已测试 '+item.test_count+' 次' : ''}</span></div></td><td>${files.length ? `<span class="attachment-chip" title="${esc(attachmentTitle)}">▧ ${esc(attachmentLabel)}</span><div class="mail-meta">${sizeText(files.reduce((sum,a)=>sum+a.size,0))}</div>` : '<span class="muted">无</span>'}</td><td>${badge(item.status)}</td><td class="right"><button class="text-button" data-detail="${item.id}">查看</button></td></tr>`;
  }).join('');
  controls();
}
function renderJob() {
  const job = state.job; $('job-panel').hidden = !job;
  if (!job) return;
  const progress = `${job.completed} / ${job.total} 封`;
  $('job-mode').textContent = (state.demo?'模拟 · ':'')+(job.scope.startsWith('test:')?'测试投递':'正式投递')+' · '+job.sender;
  $('job-title').textContent = {running:'正在逐封提交',stopping:'正在停止后续发送',stopped:'本批次已停止',finished:'本批次已完成',error:'本批次已停止，请核对记录'}[job.status] || job.status;
  $('job-progress').max = job.total; $('job-progress').value = job.completed;
  $('job-description').textContent = `${progress} · 已接受 ${job.sent} · 部分接受 ${job.partial} · 明确失败 ${job.failed} · 待核对 ${job.uncertain}${job.current ? ' · 当前：'+job.current : ''}${['stopped','error'].includes(job.status) ? ' · 未处理 '+(job.total-job.completed)+' 封' : ''}`;
  $('stop-job').hidden = !busy(); $('stop-job').disabled = job.status === 'stopping';
  $('job-events').innerHTML = job.events.slice(-6).reverse().map(e => `<div class="job-event">${badge(e.status)}<span>${esc(e.filename)} · ${esc(e.detail)}</span></div>`).join('');
}
function renderHistory() {
  $('history-empty').hidden = state.history.length > 0;
  $('history-rows').innerHTML = state.history.map(row => {
    let recipients = []; try { recipients = JSON.parse(row.recipients); } catch {}
    return `<tr><td><strong>${esc(row.subject)}</strong><div class="mail-meta">${esc(row.filename)}<br>${esc(timeText(row.started_at))}</div></td><td>${state.demo?'模拟 · ':''}${row.scope.startsWith('test:')?'测试':'正式'}<div class="mail-meta">${recipients.map(esc).join('<br>')}</div></td><td>${badge(row.status)}</td><td>${esc(row.detail)}<div class="mail-meta">${esc(row.message_id)}</div>${row.status==='uncertain' ? `<button class="text-button" data-retry="${esc(row.hash)}" data-scope="${esc(row.scope)}">人工核对与处理</button>` : ''}</td></tr>`;
  }).join('');
}
async function refresh() {
  try {
    state = await json('state');
    $('error-banner').hidden = true;
    $('connection').textContent = '本机服务已连接';
    $('runtime-hint').textContent = state.desktop ? '浏览器关闭后仍在运行，请从交邮窗口退出应用' : '关闭启动终端将停止本机服务';
    $('demo-banner').hidden = !state.demo;
    $('mode-pill').textContent = state.demo?'演示模式':'本机模式';
    $('sent-stat-label').textContent = state.demo?'模拟已接受':'服务器已接受';
    if (!initialized) {
      state.items.filter(i => i.eligible).forEach(i => selected.add(i.id));
      if (state.demo) { $('account').value = 'demo'; $('test-to').value = 'demo@example.com'; }
      updateSender(); initialized = true;
    }
    const ids = new Set(state.items.map(i=>i.id)); selected = new Set([...selected].filter(id=>ids.has(id)));
    $('nav-count').textContent = state.items.length;
    $('stat-total').textContent = state.items.length;
    $('stat-ready').textContent = state.items.filter(i=>i.eligible).length;
    $('stat-sent').textContent = state.items.filter(i=>i.status==='sent').length;
    $('stat-attention').textContent = state.items.filter(i=>['invalid','failed','partial','uncertain'].includes(i.status)).length;
    renderList(); renderJob(); if (activeView==='history') renderHistory();
  } catch (error) {
    $('connection').textContent = '本机连接已断开';
    $('error-banner').textContent = '无法连接本机服务：'+error.message+(state.desktop?'。请重新打开交邮应用。':'。请保持启动终端打开，或重新双击启动。');
    $('error-banner').hidden = false;
    for (const id of ['send-selected','test-send','choose-files']) $(id).disabled = true;
  }
}
async function importFiles(files) {
  if (busy() || uploading || !files.length) return;
  uploading = true; controls();
  let imported = 0, duplicates = 0, invalid = 0, failed = [];
  try {
    for (const [index,file] of [...files].entries()) {
      $('import-message').textContent = `正在导入 ${index+1} / ${files.length}：${file.name}`;
      if (!file.name.toLowerCase().endsWith('.eml')) { failed.push(file.name+'：不是 .eml'); continue; }
      if (file.size > 32*1024*1024) { failed.push(file.name+'：超过 32 MB'); continue; }
      try {
        const result = await (await api('import?name='+encodeURIComponent(file.name), file, true)).json();
        if (result.duplicate) duplicates++;
        else { imported++; if (result.problem) invalid++; else selected.add(result.id); }
      } catch (error) { failed.push(file.name+'：'+error.message); }
    }
  } finally {
    uploading = false; $('file-input').value = ''; await refresh();
    $('import-message').textContent = `已导入 ${imported} 封 · 跳过重复 ${duplicates} 封${invalid?' · 检查未通过 '+invalid+' 封':''}${failed.length?' · 导入失败 '+failed.length+' 个：'+failed.join('；'):''}`;
  }
}
function openPicker() { if (!busy() && !uploading) $('file-input').click(); }
$('choose-files').onclick = event => { event.stopPropagation(); openPicker(); };
$('dropzone').onclick = event => { if (event.target !== $('file-input')) openPicker(); };
$('dropzone').onkeydown = event => { if (event.target === $('dropzone') && ['Enter',' '].includes(event.key)) { event.preventDefault(); openPicker(); } };
$('file-input').onchange = event => importFiles(event.target.files);
for (const eventName of ['dragenter','dragover']) $('dropzone').addEventListener(eventName, event => { event.preventDefault(); $('dropzone').classList.add('dragging'); });
for (const eventName of ['dragleave','drop']) $('dropzone').addEventListener(eventName, event => { event.preventDefault(); $('dropzone').classList.remove('dragging'); });
$('dropzone').addEventListener('drop', event => importFiles(event.dataTransfer.files));
window.addEventListener('dragover', event => event.preventDefault());
window.addEventListener('drop', event => event.preventDefault());
$('search').oninput = () => renderList(); $('filter').onchange = () => renderList();
$('select-all').onchange = event => {
  filteredItems().forEach(item => event.target.checked ? selected.add(item.id) : selected.delete(item.id)); renderList();
};
$('mail-rows').onchange = event => { const id = event.target.dataset.select; if (id) { event.target.checked ? selected.add(id) : selected.delete(id); controls(); } };
$('mail-rows').onclick = event => { const button = event.target.closest('[data-detail]'); if (button) openDetail(button.dataset.detail); };
function switchView(view) {
  activeView = view; $('draft-view').hidden = view !== 'drafts'; $('history-view').hidden = view !== 'history';
  $('nav-drafts').classList.toggle('active',view==='drafts'); $('nav-history').classList.toggle('active',view==='history');
  $('page-title').textContent = view==='drafts'?'邮件草稿':'发送记录';
  $('breadcrumb').textContent = '工作区 / '+$('page-title').textContent;
  $('page-subtitle').textContent = view==='drafts'?'导入、核对、确认发送。在一个工作台里完成。':'每次提交都有记录。核对状态，再决定下一步。';
  if (view==='history') renderHistory();
}
$('nav-drafts').onclick=()=>switchView('drafts'); $('nav-history').onclick=()=>switchView('history');
$('export-top').onclick = () => download('export',state.demo?'演示发送结果.csv':'发送结果.csv');
function updateSender() { $('sender-preview').textContent = '发件地址：'+($('account').value.trim()||'用户名')+'@sjtu.edu.cn'; }
$('account').oninput = updateSender;
async function openDetail(id, scope='real') {
  try {
    const item = await json('detail?id='+encodeURIComponent(id)); detailId = id;
    $('detail-subject').textContent = item.subject; $('detail-filename').textContent = item.filename;
    $('detail-problem').hidden = !item.problem; $('detail-problem').textContent = item.problem;
    $('detail-recipients').innerHTML = ['To','Cc','Bcc'].map(key => `<div class="recipient-row"><b>${{To:'收件人',Cc:'抄送',Bcc:'密送'}[key]}</b><span>${item.groups[key].length ? item.groups[key].map(r=>esc((r.name?r.name+' · ':'')+r.address)).join('<br>') : '—'}</span></div>`).join('');
    $('attachment-count').textContent = `(${item.attachments.length})`;
    $('detail-attachments').innerHTML = item.attachments.length ? item.attachments.map(a=>`<button class="attachment" data-index="${a.index}" data-name="${esc(a.name)}">▧ ${esc(a.name)}<small>${sizeText(a.size)} · 点击保存</small></button>`).join('') : '<span class="muted">无附件</span>';
    $('plain-body').textContent = item.plain || '这封邮件没有纯文本正文，可切换到 HTML 预览。';
    $('html-body').srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>body{font:14px/1.8 -apple-system,BlinkMacSystemFont,sans-serif;color:#27344a;padding:16px;overflow-wrap:anywhere}img{max-width:100%}table{max-width:100%}</style></head><body>${item.html || '<p>这封邮件没有 HTML 正文。</p>'}</body></html>`;
    $('html-tab').disabled = !item.html; setBodyTab(item.plain || !item.html ? 'plain' : 'html');
    $('detail-deliveries').innerHTML = Object.keys(item.deliveries).length ? '<h3>正式发送 · 各地址状态</h3>'+Object.values(item.deliveries).map(d=>`<div class="delivery-row">${badge(d.status)}<span>${esc(d.recipient)}</span><small>${esc(d.detail)}</small></div>`).join('') : '';
    const uncertain = scope.startsWith('test:') || Object.values(item.deliveries).some(d=>['uncertain','attempting'].includes(d.status));
    $('unlock-scope').textContent = scope.startsWith('test:') ? '本次处理测试投递：'+scope.slice(5) : '本次处理正式投递中结果未知的地址。';
    $('unlock-box').hidden = !uncertain; $('unlock-confirm').value = ''; $('unlock-button').dataset.scope = scope;
    $('unlock-button').disabled = busy();
    if (!$('detail-dialog').open) $('detail-dialog').showModal();
  } catch (error) { toast(error.message); }
}
$('detail-attachments').onclick = event => { const button=event.target.closest('[data-index]'); if(button) download('attachment?id='+detailId+'&index='+button.dataset.index,button.dataset.name); };
function setBodyTab(tab) { $('plain-body').hidden=tab!=='plain'; $('html-body').hidden=tab!=='html'; $('plain-tab').classList.toggle('active',tab==='plain'); $('html-tab').classList.toggle('active',tab==='html'); }
$('plain-tab').onclick=()=>setBodyTab('plain'); $('html-tab').onclick=()=>setBodyTab('html');
$('unlock-button').onclick = async () => {
  try { await json('unlock',{id:detailId,scope:$('unlock-button').dataset.scope,confirmation:$('unlock-confirm').value}); toast('已记录人工核对，可以重新预览发送清单'); $('detail-dialog').close(); await refresh(); }
  catch(error) { toast(error.message); }
};
$('history-rows').onclick = event => { const button=event.target.closest('[data-retry]'); if(button) openDetail(button.dataset.retry,button.dataset.scope); };
async function previewSend(test) {
  try {
    const testTo = test ? $('test-to').value.trim() : null;
    plan = await json('plan',{ids:[...selected],account:$('account').value.trim(),test_to:testTo,delay:Number($('delay').value)});
    $('confirm-title').textContent = state.demo?'确认模拟发送':test?'确认测试发送':'确认批量发送';
    $('confirm-demo').hidden = !state.demo;
    $('confirm-summary').innerHTML = `<div><small>发件地址</small>${esc(plan.sender)}</div><div><small>本次实际投递</small>${plan.items.length} 封邮件 · ${plan.items.reduce((n,i)=>n+i.recipients.length,0)} 次地址投递</div>${test?`<div><small>仅投递到测试邮箱</small>${esc(testTo)}</div>`:''}`;
    $('confirm-items').innerHTML = plan.items.map(item=>`<div class="confirm-item"><b>${esc(item.subject)}</b><small>${esc(item.filename)}</small><small>实际投递：${item.recipients.map(esc).join('、')}</small></div>`).join('');
    $('confirm-skipped').hidden = !plan.skipped.length;
    $('confirm-skipped').textContent = '以下地址已接受或待核对，本次跳过：'+plan.skipped.map(i=>i.filename+'（'+i.recipients.join('、')+'）').join('；');
    $('password-field').hidden = state.demo; $('password').value = ''; $('password').required = !state.demo;
    $('confirm-label').textContent = `请输入“${plan.confirmation}”以继续`;
    $('confirmation').value = ''; $('confirm-error').hidden = true; $('confirm-submit').disabled = false;
    $('confirm-submit').textContent = state.demo?'确认并模拟发送':'确认并开始发送';
    $('confirm-dialog').showModal();
  } catch(error) { toast(error.message); }
}
$('test-send').onclick=()=>previewSend(true); $('send-selected').onclick=()=>previewSend(false);
$('confirm-form').onsubmit=async event=>{
  event.preventDefault(); if (!plan) return;
  $('confirm-submit').disabled = true; $('confirm-error').hidden = true;
  try {
    await json('start',{plan_id:plan.id,password:$('password').value,confirmation:$('confirmation').value});
    $('password').value = ''; $('confirm-dialog').close(); await refresh(); $('job-panel').scrollIntoView({behavior:'smooth',block:'nearest'});
  } catch(error) {
    $('password').value = '';
    $('confirm-error').textContent = error.message; $('confirm-error').hidden = false;
    await refresh(); $('confirm-submit').disabled = busy();
  }
};
$('confirm-dialog').addEventListener('close',()=>{ $('password').value=''; $('confirmation').value=''; plan=null; });
$('stop-job').onclick=async()=>{ try { await json('stop',{}); await refresh(); toast('已请求停止，当前提交可能仍会完成'); } catch(error) { toast(error.message); } };
$('remove-selected').onclick=()=>{ $('remove-description').textContent=`将从工作台移除 ${selected.size} 封草稿。`; $('remove-dialog').showModal(); };
$('remove-confirm').onclick=async()=>{ try { await json('remove',{ids:[...selected]}); selected.clear(); $('remove-dialog').close(); await refresh(); toast('所选草稿已移除，历史记录已保留'); } catch(error) { toast(error.message); } };
for (const button of document.querySelectorAll('.close-dialog')) button.onclick=()=>button.closest('dialog').close();
window.addEventListener('pagehide',()=>{ $('password').value=''; });
async function poll() { await refresh(); setTimeout(poll,1500); }
poll();
