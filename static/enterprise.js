'use strict';
window.EnterpriseUI={
  recovery(){
    const completing=location.hash.startsWith('#reset=');
    document.querySelector('#recovery-email-label').classList.toggle('hidden',completing);
    document.querySelector('#recovery-complete').classList.toggle('hidden',!completing);
    document.querySelector('#recovery-title').textContent=completing?'Choose a new password':'Recover your account';
    document.querySelector('#recovery-message').textContent='';
    if(!document.querySelector('#recovery-dialog').open)document.querySelector('#recovery-dialog').showModal();
  },
  async upload(file,button,label){
    const created=await api('/api/uploads',{method:'POST',body:JSON.stringify({name:file.name})});
    let offset=0;
    while(offset<file.size){
      const bytes=new Uint8Array(await file.slice(offset,offset+created.chunk_bytes).arrayBuffer());
      let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
      const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),b=>b.toString(16).padStart(2,'0')).join('');
      try{const result=await api('/api/uploads/'+created.id,{method:'POST',body:JSON.stringify({offset,data:btoa(binary),sha256:digest})});offset=result.offset;}
      catch(error){const saved=await api('/api/uploads/'+created.id);if(saved.size!==offset+bytes.length)throw error;offset=saved.size;}
      button.textContent=`${label} · ${Math.round(offset/file.size*100)}% uploaded`;
    }
    await api('/api/uploads/'+created.id+'/complete',{method:'POST',body:'{}'});button.textContent='Start reconciliation →';return created.id;
  },
  async render(page){
    const root=$('#content');
    if(page==='operations'){
      const data=await api('/api/operations');
      root.innerHTML=`<div class="metrics"><article class="metric"><div class="metric-label">QUEUED</div><div class="metric-value">${fmt(data.runs.queued)}</div></article><article class="metric"><div class="metric-label">RUNNING</div><div class="metric-value">${fmt(data.runs.running)}</div></article><article class="metric"><div class="metric-label">FAILED RUNS</div><div class="metric-value">${fmt(data.runs.failed)}</div></article><article class="metric"><div class="metric-label">FREE STORAGE</div><div class="metric-value">${(data.free_bytes/1024**3).toFixed(1)} GB</div></article></div><section class="panel"><div class="panel-head"><div><h3>Operational alerts</h3><p>Storage and failure conditions measured by the service</p></div><button data-action="reload" class="button secondary small">Refresh</button></div><div class="dialog-body">${data.alerts.length?data.alerts.map(a=>`<div class="callout">${badge(a.severity)} ${esc(a.message)}</div>`).join(''):'<p class="success">No active conditions detected by these checks.</p>'}<p class="muted">Dataset permissions: ${data.acl_enforced?'Enforced':'Demo compatibility mode'}. Prometheus metrics require a configured bearer token.</p><h3>Coordinator heartbeats</h3>${data.workers.map(w=>`<p class="muted">${esc(w.id)} · ${Math.max(0,Math.round(Date.now()/1000-w.updated))} seconds ago · ${esc(w.state)}</p>`).join('')||'<p class="muted">No worker heartbeat has been recorded.</p>'}</div></section>`;
    }else if(page==='governance'){
      const data=await api('/api/retention');
      root.innerHTML=`<section class="panel"><div class="panel-head"><div><h3>Evidence inventory</h3><p>Legal holds prevent retention processing. No data is purged from this screen.</p></div></div><div class="table-wrap"><table><thead><tr><th>Dataset</th><th>Run</th><th>Created</th><th>State</th><th>Legal hold</th><th>Archive copy</th></tr></thead><tbody>${data.runs.map(r=>`<tr><td>${esc(r.dataset)}</td><td class="key-cell">${r.id.slice(0,12)}</td><td>${esc(date(r.created))}</td><td>${badge(r.state)}</td><td>${badge(r.legal_hold?'Held':'No hold',r.legal_hold?'review':'neutral')}</td><td>${r.archived?'Verified copy recorded':'No copy recorded'}</td></tr>`).join('')}</tbody></table></div><form id="hold-form" class="team-form"><label>Run<select name="id">${data.runs.map(r=>`<option value="${r.id}">${esc(r.dataset)} · ${r.id.slice(0,8)}</option>`).join('')}</select></label><label>Policy action<select name="hold"><option value="true">Place on legal hold</option><option value="false">Release legal hold</option></select></label><label>Rationale<input name="reason" required minlength="10" maxlength="2000" placeholder="Case or approval reference"></label><button type="submit" class="button primary" ${data.runs.length?'':'disabled'}>Record decision</button></form></section><div class="callout">Encrypted archival is an operator maintenance task. Originals can be purged only after a verified archive and, by default in production, a compliance-mode object-lock copy. Audit records follow a separate retention policy.</div>`;
    }else if(page==='access'){
      const [members,access]=await Promise.all([api('/api/users'),api('/api/access')]);
      root.innerHTML=`<section class="panel"><div class="panel-head"><div><h3>Dataset permissions</h3><p>Production denies dataset access unless explicitly granted. Administrators retain workspace access.</p></div></div><div class="table-wrap"><table><thead><tr><th>Dataset</th><th>Member</th><th>Permission</th></tr></thead><tbody>${access.grants.map(g=>`<tr><td>${esc(g.dataset)}</td><td>${esc(g.email)}</td><td>${badge(g.permission,'neutral')}</td></tr>`).join('')}</tbody></table></div><form id="access-form" class="team-form"><label>Dataset name<input name="dataset" required maxlength="120" placeholder="Exact dataset name"></label><label>Member<select name="user_id">${members.users.map(u=>`<option value="${u.id}">${esc(u.email)}</option>`).join('')}</select></label><label>Permission<select name="permission"><option value="read">Read only</option><option value="write">Read and reconcile</option><option value="revoke">Revoke access</option></select></label><button type="submit" class="button primary">Save permission</button></form></section><div class="callout">A write permission permits investigation and reconciliation within the member's workspace role. It does not grant administrator approval rights.</div>`;
    }else{
      const data=await api('/api/security');
      root.innerHTML=`<div class="info-grid"><section class="panel connection-card"><div class="connection-logo">◇</div>${badge(data.mfa_enabled?'Authenticator enabled':'Authenticator not enrolled',data.mfa_enabled?'completed':'review')}<h2>Multi-factor authentication</h2><p>Add a time-based authenticator. Codes can be used once; recovery codes provide a backup when your authenticator is unavailable.</p>${!data.mfa_enabled?'<form id="mfa-enroll-form"><label>Current password<input name="password" type="password" required autocomplete="current-password"></label><br><button class="button primary" type="submit">Set up authenticator</button></form><div id="mfa-setup"></div>':'<p class="success">Your account requires an authenticator or unused recovery code at native sign-in.</p><form id="mfa-replace-form"><label>Current password<input name="password" type="password" required autocomplete="current-password"></label><br><label>Authenticator or unused recovery code<input name="code" required autocomplete="one-time-code" maxlength="24"></label><br><button class="button secondary" type="submit">Remove old authenticator and sign out</button><p class="muted">Enroll your replacement on the next sign-in. All existing sessions will be revoked.</p></form>'}${state.config.demo?'<p class="muted">The synthetic administrator uses one-click sign-in. To test password-based MFA, create a test member and sign in with that member.</p>':''}<div id="recovery-codes"></div></section><section class="panel connection-card"><h2>Change password</h2><p>Changing your password revokes all active sessions. Your authenticator remains enabled.</p><form id="password-form"><label>Current password<input name="password" type="password" required autocomplete="current-password"></label><br><label>New password<input name="new_password" type="password" required minlength="12" maxlength="200" autocomplete="new-password"></label><br><label>Authenticator or recovery code, if enabled<input name="code" autocomplete="one-time-code" maxlength="24"></label><br><button class="button secondary" type="submit">Change password</button></form><div class="callout">Identity-provider password and MFA policies are managed by your organization when SSO is enabled.</div></section></div>`;
    }
  }
};
document.addEventListener('click',async event=>{
  const button=event.target.closest('button');if(!button)return;
  if(button.id==='forgot-password')EnterpriseUI.recovery();
  if(button.id==='sso-login')try{await enter(await api('/api/sso-login',{method:'POST',body:'{}'}));}catch(error){toast(error.message);}
});
document.addEventListener('submit',async event=>{
  const form=event.target;if(!['access-form','hold-form','mfa-enroll-form','mfa-confirm-form','mfa-replace-form','password-form','recovery-form'].includes(form.id))return;
  event.preventDefault();const button=form.querySelector('button[type="submit"]');button.disabled=true;
  try{
    const data=Object.fromEntries(new FormData(form));
    if(form.id==='hold-form'){await api('/api/runs/'+data.id+'/hold',{method:'POST',body:JSON.stringify({hold:data.hold==='true',reason:data.reason})});await EnterpriseUI.render('governance');toast('Legal-hold decision recorded.');}
    if(form.id==='access-form'){await api('/api/access',{method:'POST',body:JSON.stringify(data)});await EnterpriseUI.render('access');toast('Dataset permission saved.');}
    if(form.id==='mfa-enroll-form'){
      const setup=await api('/api/security/enroll',{method:'POST',body:JSON.stringify(data)});
      $('#mfa-setup').innerHTML=`<div class="callout">In your authenticator, add a time-based account named Reconcile with this secret. Keep it private.<pre class="rule-code">${esc(setup.secret)}</pre></div><form id="mfa-confirm-form"><label>Six-digit authenticator code<input name="code" required pattern="[0-9]{6}" inputmode="numeric" autocomplete="one-time-code"></label><br><button class="button primary" type="submit">Verify and enable</button></form>`;
    }
    if(form.id==='mfa-confirm-form'){
      const result=await api('/api/security/confirm',{method:'POST',body:JSON.stringify(data)});await EnterpriseUI.render('security');
      $('#recovery-codes').innerHTML=`<div class="callout"><strong>Save these recovery codes securely. Each can be used once. They are shown only now.</strong><pre class="rule-code">${result.recovery_codes.map(esc).join('\n')}</pre></div>`;toast('Authenticator enabled. Save your recovery codes.');
    }
    if(form.id==='password-form'){await api('/api/security/password',{method:'POST',body:JSON.stringify(data)});state.user=null;showLogin();toast('Password changed. Sign in again.');}
    if(form.id==='mfa-replace-form'){await api('/api/security/disable',{method:'POST',body:JSON.stringify(data)});state.user=null;showLogin();toast('Old authenticator removed. Sign in and enroll your replacement.');}
    if(form.id==='recovery-form'){
      if(location.hash.startsWith('#reset=')){data.token=decodeURIComponent(location.hash.slice(7));await api('/api/password-reset/complete',{method:'POST',body:JSON.stringify(data)});history.replaceState(null,'','#overview');$('#recovery-dialog').close();toast('Password reset. Sign in with your new password.');}
      else{const result=await api('/api/password-reset/request',{method:'POST',body:JSON.stringify(data)});$('#recovery-message').textContent=result.message;}
    }
  }catch(error){toast(error.message);}finally{button.disabled=false;}
});
