'use strict';
const $ = (s, root=document) => root.querySelector(s);
const $$ = (s, root=document) => [...root.querySelectorAll(s)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = value => Number(value || 0).toLocaleString('en-IN');
const date = value => new Date(value).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
const labels = {missing_destination:'Missing in destination',unexpected_destination:'Unexpected records',duplicate_source:'Duplicate source keys',duplicate_destination:'Duplicate destination keys',value_mismatch:'Value mismatches',invalid_value:'Invalid values',invalid_key:'Invalid business keys',missing_column:'Missing columns',unexpected_column:'Unexpected columns'};
labels.changed_type='Changed declared types';
const pageInfo = {
  overview:['MIGRATION ASSURANCE','Reconciliation overview','A clear view of your data. Every difference accounted for.'],
  exceptions:['INVESTIGATION WORKSPACE','Exception explorer','Find the difference. Preserve the evidence. Record the decision.'],
  history:['EXECUTION & QUALITY','Run history','Track every comparison and measure the effect of corrections.'],
  rules:['COMPARISON POLICY','Comparison rules','Explicit rules. Versioned decisions. Reproducible results.'],
  connections:['DATA SOURCES','Data connections','Bring source and destination together through read-only snapshots.'],
  audit:['GOVERNANCE','Audit trail','A permanent record of the actions behind your data decisions.'],
  team:['WORKSPACE ADMINISTRATION','Team & access','Give each person the access they need to do their work.']
};
Object.assign(pageInfo,{operations:['SERVICE ASSURANCE','Operations center','Measured service health, worker activity, and storage alerts.'],access:['DATA GOVERNANCE','Dataset access','Control who can read and reconcile each dataset.'],security:['IDENTITY & ACCESS','Account security','Protect your account with an authenticator and recovery codes.'],governance:['EVIDENCE GOVERNANCE','Retention & legal holds','Preserve required evidence and apply controlled archival policies.']});
const state = {user:null,config:null,runs:[],selected:null,page:'overview',filters:{q:'',category:'',severity:'',status:''},issuePage:1,issues:[],total:0,historyDataset:'',historyDate:'',loading:false};
let toastTimer,searchTimer,issueRequest=0;
function toast(message){$('#toast').textContent=message;$('#toast').classList.remove('hidden');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.add('hidden'),5000);}
async function api(path,options={}){
  const headers={...(options.body?{'Content-Type':'application/json','X-CSRF-Token':state.user?.csrf||''}:{}),...options.headers};
  const response=await fetch(path,{...options,headers,credentials:'same-origin'});
  if(!response.ok){let body;try{body=await response.json();}catch{body={error:'Service unavailable. Please try again.'};}if(response.status===401 && state.user){state.user=null;showLogin();}throw Error(body.error || 'Request failed.');}
  return response.json();
}
function showLogin(){ $('#workspace').classList.add('hidden');$('#login').classList.remove('hidden');$('#demo-login').classList.toggle('hidden',!state.config?.demo);$('#sso-login').classList.toggle('hidden',!state.config?.sso_enabled);$('#login-form').classList.toggle('hidden',!!state.config?.sso_only);$('#forgot-password').classList.toggle('hidden',!!state.config?.sso_only);if(location.hash.startsWith('#reset='))EnterpriseUI.recovery(); }
function badge(text,kind){return `<span class="pill ${esc(kind||text)}">${esc(String(text).replaceAll('_',' '))}</span>`;}
function run(){return state.runs.find(r=>r.id===state.selected)||state.runs[0];}
function statusOf(r){return r.state!=='completed'?r.state:r.summary.exception_count?'Review needed':'Reconciled';}
function statusBadge(r){return badge(statusOf(r),r.state==='completed'?(r.summary.exception_count?'review':'completed'):r.state);}
function keyLabel(key){try{return JSON.parse(key).join(' / ');}catch{return key||'Dataset schema';}}
function valueLabel(v){if(v===null||v===undefined)return '<span class="empty-value">Not present</span>';return typeof v==='object'?`<span class="muted">${Array.isArray(v)?v.length:1} record${Array.isArray(v)&&v.length!==1?'s':''} · View evidence ↗</span>`:esc(v);}
function empty(title,description,action=''){return `<div class="panel empty-state"><span class="empty-icon">◈</span><h2>${esc(title)}</h2><p>${esc(description)}</p>${action}</div>`;}
async function enter(user){
  state.user=user;$('#login').classList.add('hidden');$('#workspace').classList.remove('hidden');
  $('#profile-name').textContent=user.email.split('@')[0];$('#profile-role').textContent=user.role;$('#avatar').textContent=user.email.slice(0,2).toUpperCase();
  $$('.admin-only').forEach(el=>el.classList.toggle('hidden',user.role!=='admin'));$$('.analyst-only').forEach(el=>el.classList.toggle('hidden',user.role==='viewer'));
  $('#demo-banner').classList.toggle('hidden',!state.config.demo);$('#mode-badge').textContent=state.config.demo?'SYNTHETIC DEMO':'SECURE WORKSPACE';$('#synthetic-option').classList.toggle('hidden',!state.config.demo);
  const security=await api('/api/security');
  if(state.config.require_mfa&&!security.mfa_enabled){navigate('security');return;}
  await loadRuns();
  await refreshHealth();
  if(!state.runs.length && state.config.demo){await startDemo(false);}
  navigate(location.hash.slice(1)||'overview');
}
async function refreshHealth(){
  let healthy=false;try{healthy=(await api('/healthz')).status==='ok';}catch{}
  $('#service-status').textContent=healthy?'All systems operational':'Service needs attention';
  $('.status-dot').classList.toggle('unavailable',!healthy);
}
setInterval(()=>{if(state.user)refreshHealth();},30000);
async function loadRuns(){
  const result=await api('/api/runs');state.runs=result.runs;
  if(!state.selected||!state.runs.some(r=>r.id===state.selected))state.selected=state.runs[0]?.id;
  const r=run();$('#nav-count').textContent=fmt(r?.summary?.exception_count||0);$('#refresh-status').textContent=`Updated ${new Date().toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit'})}`;
}
function navigate(page){
  if(!pageInfo[page])page='overview';if(['audit','team','operations','access','governance'].includes(page)&&state.user.role!=='admin')page='overview';
  state.page=page;history.replaceState(null,'','#'+page);$$('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.page===page));
  window.scrollTo(0,0);
  const info=pageInfo[page];$('#page-eyebrow').textContent=info[0];$('#page-title').textContent=info[1];$('#page-description').textContent=info[2];$('#breadcrumb-page').textContent=page==='history'?'Run history':pageInfo[page][1];
  render().catch(e=>{ $('#content').innerHTML=empty('Unable to load this view',e.message,'<button class="button secondary" data-action="reload">Try again</button>'); });
}
function selector(){return `<div class="run-selector"><span>SELECTED RUN</span><select id="run-select" aria-label="Selected reconciliation run">${state.runs.map(r=>`<option value="${r.id}" ${r.id===state.selected?'selected':''}>${esc(r.dataset)} · ${esc(date(r.created))} · ${r.id.slice(0,6)}</option>`).join('')}</select>${run()?.summary?`<span class="fingerprint">SHA-256 ${esc(run().summary.fingerprint.slice(0,18))}…</span>`:''}</div>`;}
function progress(r){return `<div class="run-progress"><span class="spinner"></span><div><strong>${r.state==='queued'?'Queued for reconciliation':'Reconciliation in progress'}</strong><p>${esc(r.phase)} · This page updates automatically.</p></div></div>`;}
function metrics(r){const s=r.summary;const clean=s.source_rows?((s.matching_records/s.source_rows)*100).toFixed(2):'0.00';return `<div class="metrics"><article class="metric"><div class="metric-label">SOURCE RECORDS<span class="metric-icon">▤</span></div><div class="metric-value">${fmt(s.source_rows)}</div><div class="metric-footer">${fmt(s.destination_rows)} in destination</div></article><article class="metric"><div class="metric-label">MATCHING RECORDS<span class="metric-icon">⊙</span></div><div class="metric-value">${fmt(s.matching_records)}</div><div class="metric-footer"><strong>✓ ${clean}%</strong> of source records</div></article><article class="metric metric-warning"><div class="metric-label">EXCEPTION EVENTS<span class="metric-icon">⚑</span></div><div class="metric-value">${fmt(s.exception_count)}</div><div class="metric-footer">${fmt(s.affected_keys)} affected valid business keys</div></article><article class="metric metric-status"><div class="metric-label">RECONCILIATION STATUS<span class="metric-icon">◈</span></div><div class="metric-value">${s.exception_count?'Review needed':'Reconciled'}</div><div class="metric-footer">${badge(s.exception_count?'Review needed':'All checks passed',s.exception_count?'review':'completed')}<span>${Number(r.elapsed||0).toFixed(2)}s runtime</span></div></article></div>`;}
function filters(){return `<div class="filters"><div class="search-box"><input id="issue-search" placeholder="Search keys, fields, or values…" aria-label="Search exceptions" value="${esc(state.filters.q)}"></div><select data-filter="category" aria-label="Exception category"><option value="">All discrepancy types</option>${Object.entries(labels).map(([key,value])=>`<option value="${key}" ${key===state.filters.category?'selected':''}>${esc(value)}</option>`).join('')}</select><select data-filter="severity" aria-label="Severity"><option value="">All severities</option>${['critical','high','medium','low'].map(v=>`<option ${v===state.filters.severity?'selected':''}>${v}</option>`).join('')}</select><select data-filter="status" aria-label="Review status"><option value="">All review statuses</option>${['open','investigating','accepted','resolved'].map(v=>`<option ${v===state.filters.status?'selected':''}>${v}</option>`).join('')}</select><button class="text-button" data-action="clear-filters">Reset</button></div>`;}
function issuePanel(full=false){return `<section class="panel"><div class="panel-head"><div><div class="title-line"><span class="table-title-icon">⊞</span><h3>${full?'Exception explorer':'Exceptions requiring attention'}</h3></div><p>Investigate source and destination evidence for the selected run.</p></div><div><button class="button secondary small" data-action="export">↓ Export CSV</button> <button class="button secondary small" data-action="export-xlsx">Excel</button></div></div>${filters()}<div id="issues-region"><div class="loading-state"><span class="spinner"></span></div></div></section>`;}
async function render(){
  const content=$('#content'),r=run();
  $('#nav-count').textContent=fmt(r?.summary?.exception_count||0);
  if(['operations','access','security','governance'].includes(state.page)){await EnterpriseUI.render(state.page);return;}
  if(['overview','exceptions'].includes(state.page)&&r?.state==='archived'){content.innerHTML=selector()+empty('Evidence moved to governed archive','The original summary and rules remain in run history. An authorized operator can recover archived evidence using its encryption key.');return;}
  if(['overview','exceptions','rules'].includes(state.page)&&!r){content.innerHTML=state.user.role==='viewer'?empty('No datasets have been shared with you','Ask an administrator for read access to a dataset.'):empty('Your first reconciliation starts here','Upload source and destination CSV files to create a measured comparison.','<button class="button primary analyst-only" data-action="new-run">＋ New reconciliation</button>');return;}
  if(state.page==='overview'){
    if(r.state!=='completed'){content.innerHTML=selector()+(r.state==='failed'?empty('This run needs attention',r.error,'<button class="button secondary" data-action="new-run">Create another run</button>'):progress(r));return;}
    const s=r.summary,max=Math.max(s.source_rows,s.destination_rows,1),difference=s.destination_rows-s.source_rows;
    const cats=Object.entries(s.categories).sort((a,b)=>b[1]-a[1]),catMax=Math.max(...cats.map(c=>c[1]),1);
    content.innerHTML=selector()+metrics(r)+`<div class="analytics-grid"><section class="panel"><div class="panel-head"><div><h3>Source vs. destination</h3><p>Row counts across your comparison snapshots</p></div><span class="pill neutral">RECORDS</span></div><div class="count-compare">${[['Source',s.source_rows,'#5375e8'],['Destination',s.destination_rows,'#a7baf5']].map(([label,count,color])=>`<div class="count-row"><span>${label}</span><div class="bar-track"><svg viewBox="0 0 300 12" preserveAspectRatio="none"><rect width="${count/max*300}" height="12" rx="3" fill="${color}"/></svg></div><strong>${fmt(count)}</strong></div>`).join('')}<div class="compare-note"><span>Net row difference <b>${difference>0?'+':''}${fmt(difference)}</b></span><span>Engine v${esc(s.engine)}</span></div></div><div class="panel-footer"><span>Counts alone do not establish data integrity.</span><span>↗ Snapshot comparison</span></div></section><section class="panel"><div class="panel-head"><div><h3>Exception distribution</h3><p>Findings by discrepancy type</p></div><span class="pill neutral">${fmt(s.exception_count)} TOTAL</span></div><div class="category-chart">${cats.length?cats.map(([key,count])=>`<div class="category-row"><span class="legend-dot"></span><span class="category-label">${esc(labels[key]||key)}</span><span class="mini-bar"><svg viewBox="0 0 75 5"><rect width="${count/catMax*75}" height="5" fill="#8ca3e7" rx="2"/></svg></span><strong>${fmt(count)}</strong></div>`).join(''):'<p class="muted">No exception events detected under the selected rules.</p>'}</div></section></div>`+issuePanel(false);
    await loadIssues();
  }else if(state.page==='exceptions'){
    content.innerHTML=selector()+(r.state==='completed'?issuePanel(true):r.state==='failed'?empty('No results for this run',r.error):progress(r));if(r.state==='completed')await loadIssues();
  }else if(state.page==='history'){
    const completed=state.runs.filter(r=>r.state==='completed').slice(0,8).reverse(),max=Math.max(...completed.map(r=>r.summary.exception_count),1);
    content.innerHTML=`<section class="panel"><div class="panel-head"><div><h3>Quality across recent runs</h3><p>Actual exception events per run. Compare runs of the same dataset and rules for a fair trend.</p></div><span class="pill neutral">LATEST ${completed.length} RUNS</span></div><div class="trend">${completed.map(r=>`<div class="trend-item"><strong>${fmt(r.summary.exception_count)} findings</strong><svg viewBox="0 0 100 90" preserveAspectRatio="none"><rect x="15" y="${90-Math.max(r.summary.exception_count/max*80,2)}" width="70" height="${Math.max(r.summary.exception_count/max*80,2)}" rx="4" fill="${r.summary.exception_count?'#a5b8f1':'#76bea9'}"/></svg><span>${esc(r.dataset.slice(0,18))}</span><span>${esc(date(r.created))}</span></div>`).join('')||'<p class="muted">Completed runs will appear here.</p>'}</div></section><br><section class="panel"><div class="panel-head"><div><h3>Reconciliation runs</h3><p>Snapshots and rule versions retained for each execution</p></div><button class="button secondary small" data-action="reload">↻ Refresh</button></div><div class="filters"><select id="history-dataset" aria-label="Filter history by dataset"><option value="">All datasets</option>${[...new Set(state.runs.map(r=>r.dataset))].map(n=>`<option value="${esc(n)}" ${n===state.historyDataset?'selected':''}>${esc(n)}</option>`).join('')}</select><input id="history-date" type="date" aria-label="Filter history by run date" value="${esc(state.historyDate)}"><button class="text-button" data-action="clear-history">Reset filters</button></div><div id="history-region"></div></section>`;
    renderHistory();
  }else if(state.page==='rules'){
    content.innerHTML=selector()+`<div class="rule-card"><section><div class="rule-item"><h3>Business key</h3><p>Keys match exactly. Row order has no effect. Repeated keys are isolated before value comparison.</p>${r.rules.keys.map(k=>`<span class="tag">${esc(k)}</span>`).join('')}</div>${Object.entries(r.rules.fields||{}).map(([name,rule])=>`<div class="rule-item"><h3>${esc(name)} ${badge(rule.type||'text','neutral')}</h3><p>Tolerance: ${esc(rule.tolerance||0)}${rule.type==='date'?' days':''} · Severity: ${esc(rule.severity||'medium')} · Nullable: ${rule.nullable===false?'No':'Yes'}</p><p>Trim whitespace: ${rule.trim?'Enabled':'Disabled'} · Case normalization: ${rule.casefold?'Enabled':'Disabled'}</p></div>`).join('')}</section><section class="panel"><div class="panel-head"><div><h3>Saved rule specification</h3><p>Version ${esc(r.rules.version||'unversioned')} · engine ${esc(r.summary?.engine||'2.0.0')}</p></div><button class="text-button" data-action="download-rules">↓ JSON</button></div><div class="dialog-body"><pre class="rule-code">${esc(JSON.stringify(r.rules,null,2))}</pre><div class="callout">A saved run always keeps its original rules. Change a policy by creating a new reconciliation with a new rule version.</div>${r.summary?`<p class="muted">Input + rule fingerprint</p><pre class="rule-code">${esc(r.summary.fingerprint)}</pre>`:''}<button class="button secondary analyst-only" data-action="clone-run">Use these rules in a new run →</button></div></section></div>`;
  }else if(state.page==='connections'){
    const data=await api('/api/connections');
    content.innerHTML=`<div class="info-grid"><section class="panel connection-card"><div class="connection-logo">▤</div>${badge('Available','completed')}<h2>CSV snapshots</h2><p>Compare exported source and destination datasets using immutable, UTF-8 CSV snapshots.</p><ul><li>Up to 12 MiB per snapshot</li><li>Composite business keys</li><li>Disk-backed comparison index</li><li>SHA-256 input fingerprints</li></ul><button class="button secondary analyst-only" data-action="new-run">Compare CSV files →</button></section><section class="panel connection-card"><div class="connection-logo">⇄</div>${badge(data.aliases.length?'Configured':'Administrator setup required',data.aliases.length?'completed':'neutral')}<h2>PostgreSQL</h2><p>Read two configured tables with server-managed credentials, repeatable-read transactions, and a query timeout.</p>${data.aliases.length?data.aliases.map(a=>`<p><span class="tag">${esc(a)}</span></p>`).join(''):'<div class="callout">Ask your administrator to configure a connection pair using RECON_CONNECTIONS and environment-based database credentials.</div>'}<p class="muted">For coordinated source and destination consistency, use a quiescent migration window or prepared snapshots.</p></section></div>`;
  }else if(state.page==='audit'){
    const data=await api('/api/audit');
    content.innerHTML=`<section class="panel"><div class="panel-head"><div><h3>Activity ledger</h3><p>Latest 200 events · Append-only storage with a SHA-256 hash chain</p></div>${badge('Evidence retained','completed')}</div><div class="table-wrap"><table><thead><tr><th>Timestamp</th><th>Action</th><th>Actor</th><th>Resource</th><th>Chain hash</th></tr></thead><tbody>${data.events.map(e=>`<tr><td>${esc(date(e.created))}</td><td><span class="audit-action">${esc(e.action)}</span></td><td>${esc(e.actor)}</td><td class="key-cell" title="${esc(e.resource)}">${esc(e.resource.slice(0,16))}</td><td class="audit-hash" title="${esc(e.hash)}">${esc(e.hash.slice(0,18))}…</td></tr>`).join('')}</tbody></table></div><div class="panel-footer"><span>All timestamps are stored in UTC and displayed in your local time.</span><span>Database administrators can alter the store; export backups to an independent system.</span></div></section>`;
  }else if(state.page==='team'){
    const data=await api('/api/users');
    content.innerHTML=`<section class="panel"><div class="panel-head"><div><h3>Workspace members</h3><p>Viewer: read · Analyst: run and investigate · Admin: accept exceptions and manage access</p></div><span class="pill neutral">${data.users.length} MEMBERS</span></div><div class="table-wrap"><table><thead><tr><th>Member</th><th>Role</th><th>Access</th><th>Action</th></tr></thead><tbody>${data.users.map(u=>`<tr><td>${esc(u.email)}</td><td>${badge(u.role,'neutral')}</td><td>${badge(u.active?'Active':'Disabled',u.active?'completed':'neutral')}</td><td>${u.id!==state.user.id?`<button class="text-button" data-user="${u.id}" data-active="${u.active?0:1}">${u.active?'Revoke access':'Restore access'}</button>`:'<span class="muted">Current user</span>'}</td></tr>`).join('')}</tbody></table></div><form id="team-form" class="team-form"><label>Work email<input name="email" type="email" required placeholder="analyst@company.com"></label><label>Initial password<input name="password" type="password" required minlength="12" maxlength="200" autocomplete="new-password" placeholder="At least 12 characters"></label><label>Role<select name="role"><option>viewer</option><option>analyst</option><option>admin</option></select></label><button class="button primary" type="submit">Add member</button></form></section><div class="callout">Revoking access invalidates every session for that member. Provision initial passwords through your approved secure channel.</div>`;
  }
  if(state.user.role==='viewer')$$('.analyst-only',content).forEach(el=>el.classList.add('hidden'));
}
function issueQuery(){const params=new URLSearchParams({...state.filters,page:state.issuePage});return params.toString();}
async function loadIssues(){
  const rid=run()?.id;if(!rid)return;const request=++issueRequest;
  try{
    const result=await api(`/api/runs/${rid}/issues?${issueQuery()}`);
    if(request!==issueRequest||!$('#issues-region'))return;
    state.issues=result.issues;state.total=result.total;
    $('#issues-region').innerHTML=result.issues.length?`<div class="table-wrap"><table><thead><tr><th>Business key</th><th>Discrepancy</th><th>Field</th><th>Source value</th><th>Destination value</th><th>Severity</th><th>Review</th><th></th></tr></thead><tbody>${result.issues.map(e=>`<tr><td class="key-cell">${esc(keyLabel(e.key))}</td><td>${esc(labels[e.category]||e.category)}</td><td>${esc(e.col)||'<span class="empty-value">Record</span>'}</td><td class="table-value">${valueLabel(e.source)}</td><td class="table-value"><span class="${e.category==='value_mismatch'?'value-different':''}">${valueLabel(e.destination)}</span></td><td>${badge(e.severity)}</td><td>${badge(e.status)}</td><td><button class="row-action" data-issue="${e.id}" aria-label="Investigate exception ${e.id}">↗</button></td></tr>`).join('')}</tbody></table></div><div class="pagination"><span>Showing ${fmt((result.page-1)*50+1)}–${fmt(Math.min(result.page*50,result.total))} of ${fmt(result.total)} exception events</span><div><button class="button secondary small" data-action="prev-page" ${result.page<=1?'disabled':''}>←</button><span>Page ${result.page}</span><button class="button secondary small" data-action="next-page" ${result.page*50>=result.total?'disabled':''}>→</button></div></div>`:empty('No exceptions in this view','Adjust the filters or choose another run. A clean comparison means all selected checks passed.');
  }catch(e){if($('#issues-region'))$('#issues-region').innerHTML=empty('Could not load exceptions',e.message);}
}
function renderHistory(){
  const rows=state.runs.filter(r=>(!state.historyDataset||r.dataset===state.historyDataset)&&(!state.historyDate||new Date(r.created).toLocaleDateString('en-CA')===state.historyDate));
  $('#history-region').innerHTML=rows.length?`<div class="table-wrap"><table><thead><tr><th>Dataset / Run</th><th>Run date</th><th>Source</th><th>Matches</th><th>Exceptions</th><th>Rule version</th><th>Status</th><th></th></tr></thead><tbody>${rows.map(r=>`<tr><td><strong>${esc(r.dataset)}</strong><div class="audit-time">${r.id.slice(0,12)}</div></td><td>${esc(date(r.created))}</td><td>${r.summary?fmt(r.summary.source_rows):'—'}</td><td>${r.summary?fmt(r.summary.matching_records):'—'}</td><td>${r.summary?fmt(r.summary.exception_count):'—'}</td><td>${esc(r.rules.version||'—')}</td><td>${statusBadge(r)}</td><td><button class="row-action" data-run="${r.id}" aria-label="Open run ${r.id.slice(0,8)}">↗</button></td></tr>`).join('')}</tbody></table></div><div class="panel-footer"><span>${rows.length} runs · Most recent 200 retained in this view</span><span>Full history remains in the audit database.</span></div>`:empty('No runs match these filters','Choose another dataset or run date.');
}
function openIssue(id){
  const e=state.issues.find(e=>e.id===Number(id));if(!e)return;
  $('#detail-content').innerHTML=`<div class="dialog-head"><div><p class="eyebrow">EXCEPTION #${e.id}</p><h2>${esc(labels[e.category]||e.category)}</h2></div><button class="icon-button close-dialog" aria-label="Close">×</button></div><div class="dialog-body">${badge(e.severity)} ${badge(e.status)}<div class="detail-key">${esc(keyLabel(e.key))}</div><p class="muted">${e.col?'Field: '+esc(e.col):'Record-level finding'} · Evidence captured at reconciliation time.</p><div class="evidence-grid"><section class="evidence"><h4>SOURCE SNAPSHOT</h4><pre>${esc(JSON.stringify(e.source,null,2)??'Not present')}</pre></section><section class="evidence destination"><h4>DESTINATION SNAPSHOT</h4><pre>${esc(JSON.stringify(e.destination,null,2)??'Not present')}</pre></section></div>${e.detail?`<div class="callout">${esc(e.detail)}</div>`:''}<h3>Decision history</h3>${e.reviews.length?e.reviews.map(n=>`<div class="review-note">${badge(n.decision)}<p>${esc(n.note)}</p><small>${esc(n.email)} · ${esc(date(n.created))}</small></div>`).join(''):'<p class="muted">No review decisions recorded yet.</p>'}${state.user.role!=='viewer'?`<form id="review-form" class="review-form" data-id="${e.id}"><h3>Record a review decision</h3><label>Decision<select name="decision"><option value="investigating">Investigating</option>${state.user.role==='admin'?'<option value="accepted">Accept with documented justification</option>':''}<option value="resolved">Mark correction reported</option><option value="open">Reopen</option></select></label><label>Evidence and rationale<textarea name="note" required minlength="10" maxlength="2000" rows="4" placeholder="Describe the investigation, approval, or correction reference…"></textarea></label><p class="muted">A review changes the investigation status. Verification requires a new reconciliation after the destination is corrected.</p><p class="error" id="review-error" role="alert"></p><button class="button primary" type="submit">Save decision</button></form>`:''}</div>`;
  if(!$('#detail-dialog').open)$('#detail-dialog').showModal();
}
async function openRun(clone=false){
  $('#run-error').textContent='';$('#run-form').reset();$('#source-name').textContent='Choose CSV file';$('#destination-name').textContent='Choose CSV file';
  const rules=clone&&run()?run().rules:state.config.default_rules;
  const {keys,...specification}=rules;
  $('#run-form').elements.keys.value=keys.join(', ');$('#rules-editor').value=JSON.stringify(specification,null,2);
  if(clone)$('#run-form').elements.dataset.value=run().dataset;
  const data=await api('/api/connections');$('#connection-alias').innerHTML=data.aliases.length?data.aliases.map(a=>`<option>${esc(a)}</option>`).join(''):'<option value="">No configured connections</option>';
  updateMode();$('#run-dialog').showModal();
}
function updateMode(){const mode=$('#run-form').elements.mode.value;$('#csv-inputs').classList.toggle('hidden',mode!=='csv');$('#database-inputs').classList.toggle('hidden',mode!=='database');$('#demo-inputs').classList.toggle('hidden',mode!=='demo');}
async function startDemo(corrected){
  const result=await api('/api/runs',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({dataset:'Customer migration',demo:true,corrected,rules:state.config.default_rules})});
  state.selected=result.id;await loadRuns();return result;
}
function download(data,name,type='application/json'){const url=URL.createObjectURL(new Blob([data],{type}));const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
async function exportIssues(format='csv'){
  const response=await fetch(`/api/runs/${run().id}/export?${issueQuery()}&format=${format}`,{credentials:'same-origin'});
  if(!response.ok){const data=await response.json();throw Error(data.error);}
  download(await response.blob(),`exceptions-${run().id.slice(0,8)}.${format}`,response.headers.get('Content-Type'));toast('Exception report downloaded.');
}
document.addEventListener('click',async event=>{
  const el=event.target.closest('button');if(!el)return;
  try{
    if(el.classList.contains('close-dialog')){el.closest('dialog').close();return;}
    if(el.dataset.page){navigate(el.dataset.page);return;}
    if(el.dataset.issue){openIssue(el.dataset.issue);return;}
    if(el.dataset.run){state.selected=el.dataset.run;state.issuePage=1;navigate('overview');return;}
    if(el.dataset.user){await api('/api/users/'+el.dataset.user,{method:'PATCH',body:JSON.stringify({active:Number(el.dataset.active)})});toast('Member access updated.');await render();return;}
    const action=el.dataset.action||el.id;
    if(['new-run','clone-run'].includes(action))await openRun(action==='clone-run');
    else if(action==='help')$('#help-dialog').showModal();
    else if(action==='demo-login'){el.disabled=true;try{await enter(await api('/api/demo-login',{method:'POST',body:'{}'}));}finally{el.disabled=false;}}
    else if(action==='logout'){await api('/api/logout',{method:'POST',body:'{}'});state.user=null;state.runs=[];state.selected=null;showLogin();}
    else if(action==='reload'){await loadRuns();await render();}
    else if(action==='corrected-demo'){el.disabled=true;try{await startDemo(true);navigate('overview');toast('Corrected synthetic comparison queued.');}finally{el.disabled=false;}}
    else if(action==='clear-filters'){state.filters={q:'',category:'',severity:'',status:''};state.issuePage=1;await render();}
    else if(action==='prev-page'){state.issuePage--;await loadIssues();}
    else if(action==='next-page'){state.issuePage++;await loadIssues();}
    else if(action==='clear-history'){state.historyDataset='';state.historyDate='';await render();}
    else if(action==='download-rules')download(JSON.stringify(run().rules,null,2),'comparison-rules.json');
    else if(action==='reset-rules')$('#rules-editor').value=JSON.stringify({version:state.config.default_rules.version,fields:state.config.default_rules.fields},null,2);
    else if(action==='export')await exportIssues();
    else if(action==='export-xlsx')await exportIssues('xlsx');
  }catch(e){toast(e.message);}
});
document.addEventListener('change',async event=>{
  const el=event.target;
  if(el.id==='run-select'){state.selected=el.value;state.issuePage=1;await render();}
  if(el.dataset.filter){state.filters[el.dataset.filter]=el.value;state.issuePage=1;await loadIssues();}
  if(el.id==='history-dataset'){state.historyDataset=el.value;renderHistory();}
  if(el.id==='history-date'){state.historyDate=el.value;renderHistory();}
  if(el.name==='mode')updateMode();
  if(el.id==='source-file')$('#source-name').textContent=el.files[0]?.name||'Choose CSV file';
  if(el.id==='destination-file')$('#destination-name').textContent=el.files[0]?.name||'Choose CSV file';
});
document.addEventListener('input',event=>{if(event.target.id==='issue-search'){state.filters.q=event.target.value;state.issuePage=1;clearTimeout(searchTimer);searchTimer=setTimeout(loadIssues,250);}});
document.addEventListener('submit',async event=>{
  const form=event.target;if(!['login-form','run-form','review-form','team-form'].includes(form.id))return;
  event.preventDefault();const button=$('button[type="submit"]',form);if(button)button.disabled=true;
  try{
    if(form.id==='login-form'){
      $('#login-error').textContent='';await enter(await api('/api/login',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(form)))}));
    }else if(form.id==='run-form'){
      $('#run-error').textContent='';const data=Object.fromEntries(new FormData(form));let rule;
      try{rule=JSON.parse($('#rules-editor').value);}catch{throw Error('Field rules must be valid JSON.');}
      const payload={dataset:data.dataset,rules:{...rule,keys:data.keys.split(',').map(k=>k.trim()).filter(Boolean)}};
      if(data.mode==='demo')payload.demo=true;
      else if(data.mode==='database'){payload.connection=$('#connection-alias').value;if(!payload.connection)throw Error('No database connection pair is configured.');}
      else{
        const a=$('#source-file').files[0],b=$('#destination-file').files[0];if(!a||!b)throw Error('Choose both source and destination CSV files.');
        if(Math.max(a.size,b.size)>(state.config.max_upload_bytes||1024**3))throw Error('A CSV exceeds the configured upload limit.');
        payload.source_upload=await EnterpriseUI.upload(a,button,'Source');payload.destination_upload=await EnterpriseUI.upload(b,button,'Destination');
      }
      const result=await api('/api/runs',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(payload)});
      state.selected=result.id;state.filters={q:'',category:'',severity:'',status:''};state.issuePage=1;$('#run-dialog').close();await loadRuns();navigate('overview');toast('Reconciliation queued. Your input snapshots are retained.');
    }else if(form.id==='review-form'){
      const id=form.dataset.id;await api(`/api/issues/${id}/review`,{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(form)))});await loadIssues();openIssue(id);toast('Decision saved to the audit trail.');
    }else if(form.id==='team-form'){
      await api('/api/users',{method:'POST',body:JSON.stringify(Object.fromEntries(new FormData(form)))});await render();toast('Member created.');
    }
  }catch(e){const target=form.id==='run-form'?$('#run-error'):form.id==='login-form'?$('#login-error'):form.id==='review-form'?$('#review-error'):null;if(target)target.textContent=e.message;else toast(e.message);}
  finally{if(button)button.disabled=false;}
});
window.addEventListener('hashchange',()=>{if(state.user)navigate(location.hash.slice(1));});
setInterval(async()=>{
  if(!state.user||!state.runs.some(r=>['queued','running'].includes(r.state)))return;
  try{const before=JSON.stringify(state.runs.map(r=>[r.id,r.state,r.phase]));await loadRuns();if(before!==JSON.stringify(state.runs.map(r=>[r.id,r.state,r.phase]))&&!$$('dialog[open]').length)await render();}catch(e){toast(e.message);}
},2000);
(async()=>{try{state.config=await api('/api/config');if(location.hash.startsWith('#reset=')){showLogin();return;}try{await enter(await api('/api/me'));}catch{showLogin();}}catch(e){$('#login').classList.remove('hidden');$('#login-error').textContent=e.message;}})();
