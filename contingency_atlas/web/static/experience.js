/* Enterprise workbench. Local assets only; source text is escaped at rendering. */
(() => {
  'use strict';
  const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)];
  const h = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const label = s => String(s || '').replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());
  const pretty = value => {
    if(typeof value==='string'&&value.trim().startsWith('[')){try{const parsed=JSON.parse(value);if(Array.isArray(parsed))value=parsed;}catch{}}
    return value == null || value === '' ? 'Not recorded' : Array.isArray(value) ? value.map(x=>typeof x==='object'?JSON.stringify(x):String(x)).join(' · ') || 'Not recorded' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  };
  const memory = {context:null, tab:'overview', format:'pdf', scenario:null,principal:null};
  const canEdit = () => !!memory.principal && (memory.principal.mode==='local' || ['analyst','admin'].includes(memory.principal.role));
  const canExport = () => !!memory.principal && (memory.principal.mode==='local' || memory.principal.role==='admin' || memory.principal.capabilities.includes('export'));
  const canAdmin = () => !!memory.principal && (memory.principal.mode==='local' || memory.principal.role==='admin');
  function applyPermissions(){
    qa('#ex-export,#ex-filter-export,#ex-scenario-export,#ex-handoff,#ex-compare-export,#ex-changes-export').forEach(b=>{b.disabled=!canExport();b.title=b.disabled?'Your role does not include export access.':'';});
    qa('[data-add-kind],#ex-add-record,#ex-import,#ex-baseline,#ex-watchlist,#ex-scenario-exposure,#ex-scenario-action,#ex-scenario-exercise,#ex-archive').forEach(b=>{b.disabled=!canEdit();b.title=b.disabled?'An analyst or administrator can change organization records.':'';});
    qa('#ex-enable-context').forEach(b=>b.disabled=!canAdmin());
  }
  let dialog, returnFocus;
  const readPref = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
  const writePref = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} };
  let actorName = readPref('atlas-actor','');
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
    return data;
  }
  function open(title, content, wide=false) {
    close(); returnFocus = document.activeElement;
    dialog = document.createElement('dialog'); dialog.className = 'ex-dialog'+(wide?' ex-wide':'');
    dialog.setAttribute('aria-labelledby','ex-dialog-title');
    dialog.innerHTML = `<div class="ex-dialog-head"><h2 id="ex-dialog-title">${h(title)}</h2><button class="ex-btn" data-close aria-label="Close dialog">✕</button></div><div class="ex-dialog-body">${content}</div>`;
    document.body.append(dialog); dialog.querySelector('[data-close]').onclick=close;
    dialog.addEventListener('cancel',e=>{e.preventDefault();close();});
    dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)close();}});
    dialog.showModal();applyPermissions();
    return dialog;
  }
  function close() { if(dialog){dialog.close();dialog.remove();dialog=null;returnFocus?.focus?.();} }
  function error(message, target='#ex-error') { const el=q(target); if(el){el.textContent=message;el.setAttribute('role','alert');} }
  function actorField(){return `<div class="ex-field"><label for="ex-actor">Your name · recorded with this decision</label><input id="ex-actor" maxlength="160" autocomplete="name" value="${h(memory.principal?.mode==='proxy'?memory.principal.subject:actorName)}" ${memory.principal?.mode==='proxy'?'readonly':''} required></div>`;}
  function getActor(){const a=memory.principal?.mode==='proxy'?memory.principal.subject:q('#ex-actor')?.value.trim()||actorName;if(!a)throw new Error('Enter your name to record this change.');actorName=a;writePref('atlas-actor',a);return a;}
  function pills(status){return `<span class="ex-pill ${h(status)}">${h(label(status))}</span>`;}

  function preferences(){
    open('Make yourself comfortable',`<p>Choose how Atlas feels while you work. These display preferences are saved in this browser.</p><div class="ex-field"><label><input id="ex-motion" type="checkbox" ${atlasMotion?'checked':''}>Ambient motion and automatic chart rotation</label></div><div class="ex-field"><label><input id="ex-reading" type="checkbox" ${document.documentElement.classList.contains('reading-mode')?'checked':''}>Reading mode · larger text and a quieter background</label></div><p>Every portfolio can also be explored as a table. Charts remain interactive with automatic motion switched off.</p>`);
    q('#ex-motion').onchange=e=>{atlasMotion=e.target.checked;try{localStorage.setItem('atlas-motion',atlasMotion?'on':'off');}catch{}document.documentElement.classList.toggle('reduce-motion',!atlasMotion);};
    q('#ex-reading').onchange=e=>{document.documentElement.classList.toggle('reading-mode',e.target.checked);writePref('atlas-reading',e.target.checked);};
  }
  document.documentElement.classList.toggle('reading-mode',readPref('atlas-reading',false));
  q('#ex-preferences').onclick=preferences;
  q('#ex-research').onclick=()=>openRunModal('full');
  q('#ex-export').onclick=()=>exportDialog();

  function exportDialog(ids=null,baselineId=null){
    let selection = baselineId?{baseline_id:baselineId,include_context:true}:ids ? {scenario_ids:ids} : {};
    const filtered = !baselineId && !ids && currentView==='scenarios' ? scnFiltered().map(x=>x.id) : null;
    open('Share a traceable snapshot',`<p>${baselineId?'This report contains changes across the private organization workspace and the selected baseline’s research scope. Preparing it explicitly includes private before-and-after records.':'Take the current decision, its evidence and its review history into your next meeting or another platform.'}</p>
      <div class="ex-field"><label for="ex-scope">Include</label><select id="ex-scope">${baselineId?'<option value="baseline">Selected baseline comparison · private organization review</option>':ids?`<option value="selected">${ids.length} selected scenario${ids.length===1?'':'s'} and linked records</option>`:`${filtered?`<option value="filtered">Current portfolio view · ${filtered.length} scenarios</option>`:''}<option value="all">Entire public research catalog</option>`}</select></div>
      <div class="ex-format-grid">${[['pdf','Meeting-ready brief'],['xlsx','Excel workbook'],['csv','Tables for other tools'],['json','Complete structured record'],['docx','Editable Word brief'],['pptx','Editable slide deck']].map(([f,d])=>`<button class="ex-format" data-format="${f}" aria-pressed="${f===memory.format}"><b>${f==='xlsx'?'Excel':f.toUpperCase()}</b><span>${d}</span></button>`).join('')}</div>
      <div class="ex-field"><label><input id="ex-private" type="checkbox" ${baselineId?'checked disabled':!ids&&!filtered?'disabled':''}>Include ${baselineId?'private baseline changes':'linked organization context'}</label><small>${baselineId?'Private context is required for this committee review pack.':'Off by default. Organization context may contain confidential material. Select scenarios to include their linked private records.'}</small></div>
      <div id="ex-preview"></div><div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn" id="ex-preview-btn">Preview contents</button><button class="ex-btn ex-primary" id="ex-download" disabled>Download</button></div>`);
    const exportView=dialog;let previewToken=null,selectionRevision=0;
    const invalidate=()=>{selectionRevision++;previewToken=null;q('#ex-download').disabled=true;q('#ex-preview').innerHTML='';};
    q('#ex-scope').onchange=()=>{invalidate();q('#ex-private').disabled=q('#ex-scope').value==='all';if(q('#ex-private').disabled)q('#ex-private').checked=false;};
    q('#ex-private').onchange=invalidate;
    qa('[data-format]').forEach(b=>b.onclick=()=>{memory.format=b.dataset.format;qa('[data-format]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));});
    q('#ex-preview-btn').onclick=async()=>{
      const button=q('#ex-preview-btn'),requestedRevision=selectionRevision;button.disabled=true;button.textContent='Preparing snapshot…';error('');
      try{
        selection=baselineId?{baseline_id:baselineId,include_context:true}:ids?{scenario_ids:ids}:q('#ex-scope').value==='filtered'?{scenario_ids:filtered}:{};
        if(q('#ex-private').checked)selection.include_context=true;
        const result=await api('/api/exports/preview',{selection});if(dialog!==exportView||requestedRevision!==selectionRevision)return;
        previewToken=result.preview_token;const m=result.manifest;
        q('#ex-preview').innerHTML=`<div class="ex-preview"><h3>Ready to share</h3><div class="ex-counts">${Object.entries(m.record_counts||{}).filter(([,n])=>n).map(([k,n])=>`<span>${h(n)} ${h(label(k))}</span>`).join('')||'<span>No records</span>'}</div><p class="ex-status">Snapshot ${h(m.snapshot_id)} · frozen for 15 minutes</p>${(m.warnings||[]).length?`<ul>${m.warnings.map(x=>`<li>${h(x)}</li>`).join('')}</ul>`:''}</div>`;
        q('#ex-download').disabled=false;
      }catch(e){error(e.message);}finally{button.disabled=false;button.textContent='Preview contents';}
    };
    q('#ex-download').onclick=async()=>{
      const button=q('#ex-download'),downloadRevision=selectionRevision;button.disabled=true;button.textContent='Preparing download…';error('');
      try{
        const response=await fetch('/api/exports/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preview_token:previewToken,format:memory.format})});
        if(!response.ok){const d=await response.json();throw new Error(d.error||'Download failed.');}
        const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');
        a.href=url;a.download=(response.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||`atlas.${memory.format}`;
        document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);button.textContent='Downloaded · download again';
      }catch(e){if(dialog===exportView&&downloadRevision===selectionRevision)error(e.message);button.textContent='Download';}finally{button.disabled=!previewToken;if(dialog===exportView&&downloadRevision!==selectionRevision)button.textContent='Download';}
    };
  }

  function compareDialog(){
    const list=scnFiltered();const selected=new Set();
    open('Compare scenarios',`<p>Choose two or three hypotheses to examine the differences that would change your decision.</p><div class="ex-field"><label for="ex-compare-search">Find a scenario</label><input id="ex-compare-search" type="search" placeholder="Search titles"></div><div id="ex-compare-list">${list.map(s=>`<label class="ex-check-row" data-title="${h(s.title?.toLowerCase())}"><input type="checkbox" data-compare-id="${h(s.id)}"><span>${h(s.title)}<small>${h(s.category)} · Urgency ${h(s.urgency_score??'unscored')}</small></span></label>`).join('')||'<p>No scenarios match the current filters.</p>'}</div><div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn ex-primary" id="ex-compare-go" disabled>Compare selected</button></div>`);
    q('#ex-compare-search').oninput=e=>qa('[data-title]').forEach(row=>row.hidden=!row.dataset.title.includes(e.target.value.toLowerCase()));
    qa('[data-compare-id]').forEach(b=>b.onchange=()=>{if(b.checked&&selected.size>=3){b.checked=false;error('Choose up to three scenarios.');return;}b.checked?selected.add(b.dataset.compareId):selected.delete(b.dataset.compareId);q('#ex-compare-go').disabled=selected.size<2;error('');});
    q('#ex-compare-go').onclick=()=>{
      const scenarios=[...selected].map(id=>DATA.scenarios.find(s=>s.id===id));
      open('What changes the decision?',`<p>Urgency is a prioritization score. These hypotheses have not been assigned calibrated probabilities of occurring.</p><div class="ex-compare-grid">${scenarios.map(s=>`<article class="ex-compare-card"><span class="ex-pill">${h(s.category)}</span><h3>${h(s.title)}</h3>${[['Urgency',s.urgency_score],['What happens',s.description],['Transmission mechanism',s.transmission_mechanism],['Controls to examine',s.controls_to_examine||s.affected_capabilities],['Assumptions',s.challenged_assumptions||s.element_of_surprise],['What would weaken it',s.weakening_signposts||s.disconfirming_evidence],['Evidence',asList(s.evidence_refs)]].map(([k,v])=>`<h4>${h(k)}</h4><p>${h(pretty(v))}</p>`).join('')}</article>`).join('')}</div><div class="ex-dialog-foot"><button class="ex-btn ex-primary" id="ex-compare-export">Export these scenarios</button></div>`,true);
      q('#ex-compare-export').onclick=()=>exportDialog([...selected]);
    };
  }
  function savedViewDialog(){
    const saved=readPref('atlas-saved-views',[]);
    open('Your portfolio views',`<p>Keep useful filters for your next visit. Saved in this browser.</p><div class="ex-field"><label for="ex-view-name">Name this view</label><input id="ex-view-name" maxlength="80" placeholder="For example, dependencies to challenge"></div><button class="ex-btn ex-primary" id="ex-view-save">Save current filters</button><div class="ex-toolbar">${saved.map((v,i)=>`<button class="ex-btn" data-saved-view="${i}">${h(v.name)}</button>`).join('')}</div><div id="ex-error" class="ex-error"></div>`);
    q('#ex-view-save').onclick=()=>{const name=q('#ex-view-name').value.trim();if(!name){error('Give this view a name.');return;}writePref('atlas-saved-views',[...saved.filter(v=>v.name!==name),{name,state:{...scnState}}].slice(-20));close();};
    qa('[data-saved-view]').forEach(b=>b.onclick=()=>{Object.assign(scnState,saved[Number(b.dataset.savedView)].state);q('#scn-search').value=scnState.q;q('#scn-sort').value=scnState.sort;q('#scn-status').value=scnState.status;q('#scn-origin').value=scnState.origin;renderScenarioChips();renderScenarios();close();});
  }
  function portfolioTools(){
    const el=q('#ex-portfolio-tools');if(el&&!el.children.length){el.innerHTML='<button class="ex-btn" id="ex-compare">Compare scenarios</button><button class="ex-btn" id="ex-save-view">Saved views</button><button class="ex-btn" id="ex-watchlist">Track this view</button><button class="ex-btn" id="ex-handoff">Enterprise handoff</button><span class="ex-spacer"></span><button class="ex-btn" id="ex-filter-export">Export this view</button>';q('#ex-compare').onclick=compareDialog;q('#ex-save-view').onclick=savedViewDialog;q('#ex-watchlist').onclick=()=>baselineDialog(scnFiltered().map(s=>s.id));q('#ex-handoff').onclick=()=>integrationDialog(scnFiltered().map(s=>s.id));q('#ex-filter-export').onclick=()=>exportDialog();}
    if(DATA&&q('#ex-portfolio-table')){
      const rows=[...DATA.scenarios].sort((a,b)=>(b.urgency_score??-1)-(a.urgency_score??-1));
      q('#ex-portfolio-table').innerHTML=`<div class="ex-table-wrap"><table class="ex-table"><caption class="ex-status">All ${rows.length} scenarios. Unscored entries remain explicit.</caption><thead><tr><th>Scenario</th><th>Urgency</th><th>Impact</th><th>Control</th></tr></thead><tbody>${rows.map(s=>`<tr><td><button class="ex-link" data-open-scenario="${h(s.id)}">${h(s.title)}</button></td><td>${h(s.urgency_score??'Unscored')}</td><td>${h(s.impact_rank||'Unrated')}</td><td>${h(s.controllability||'Unrated')}</td></tr>`).join('')}</tbody></table></div>`;
      qa('[data-open-scenario]').forEach(b=>b.onclick=()=>openScenario(b.dataset.openScenario));
    }
  }

  const GROUPS={overview:'Exposure overview',services:'Services',dependencies:'Dependencies',controls:'Controls',assumptions:'Assumptions',exposures:'Exposures',actions:'Actions',exercises:'Exercises',evidence_assessments:'Evidence assessments',impact_assessments:'Business impact',assessments:'Coverage',observations:'Observations',mechanisms:'Failure mechanisms',org_scenarios:'Scenario register',internal_loss_events:'Internal losses',taxonomy_crosswalk:'Taxonomy crosswalk'};
  const descriptions={services:'Start with the services you promise to deliver. Name an owner and the disruption each service can tolerate.',dependencies:'Connect services and suppliers to reveal shared points of failure.',controls:'Record how a control was tested and what evidence supports it.',assumptions:'Make assumptions visible, assign an owner and decide how to test them.',exposures:'Link a scenario to a business service with a reviewed rationale.',actions:'Turn the next decision into an accountable action. Closing an action requires evidence.',evidence_assessments:'Challenge a source claim explicitly: record directness, corroboration or contradiction and declared source families. Verification is an attributed reviewer decision, not an inferred confidence score.',impact_assessments:'Record an impact timeline against a service tolerance. Keep assumed values separate from measured exercise results.',exercises:'Plan a tabletop or exercise, record injects and capture what actually happened.',assessments:'Assessment coverage uses your declared services and failure mechanisms. Untested does not mean safe.',observations:'Record new observations with evidence and the services they concern.',mechanisms:'Declare the failure mechanisms your assessment covers. This defines the coverage denominator.',org_scenarios:'Your own scenario register: reference, owner, frequency and severity estimates. Crosswalked to Atlas scenarios, never merged into them.',internal_loss_events:'Your own loss and near-miss records. Near misses carry a potential impact, never a booked loss.',taxonomy_crosswalk:'Map your risk taxonomy onto Atlas categories. A mapping stays proposed until a named reviewer signs it off.'};
  const names=r=>r.name||r.title||r.statement||r.scenario_id||r.id;
  async function loadContext(){
    try{memory.context=await api('/api/context');renderOrg();renderBriefingContext();}
    catch(e){memory.context=null;const brief=q('#ex-briefing-context');if(brief)brief.innerHTML='<div class="ex-notice">Organization data could not be refreshed. Refresh the workspace before making changes.</div>';const root=q('#organization-root');if(root)root.innerHTML=`<div class="ex-notice" role="alert">Organization workspace unavailable. ${h(e.message)} <button class="ex-btn" id="ex-org-retry">Retry</button></div>`;q('#ex-org-retry')?.addEventListener('click',loadContext);}
  }
  function records(kind){return (memory.context?.records?.[kind]||[]).filter(r=>!r.archived);}
  function displayRef(id){for(const list of Object.values(memory.context?.records||{})){const r=list.find(x=>x.id===id);if(r)return names(r);}return DATA?.scenarios.find(s=>s.id===id)?.title||id;}
  function renderBriefingContext(){
    const el=q('#ex-briefing-context');if(!el)return;
    if(!memory.context?.enabled){el.innerHTML='<div class="ex-strip"><div><strong>Bring the picture closer to your business</strong><p>Map your critical services and dependencies to investigate which scenarios could affect you.</p></div><button class="ex-btn" data-go="organization">Set up organization</button></div>';return;}
    const overdue=memory.context.overdue;const n=Array.isArray(overdue)?overdue.length:Object.values(overdue||{}).reduce((a,v)=>a+(Array.isArray(v)?v.length:0),0);
    el.innerHTML=`<div class="ex-strip"><div><strong>${records('services').length} business services in your assessment</strong><p>${n?n+' item(s) need attention.':'Review the coverage map to find your next investigation.'} Organization context stays separate from public research.</p></div><button class="ex-btn" data-go="organization">Explore exposures</button></div>`;
  }
  const NAV_GROUPS={overview:{name:'Exposure overview',kinds:['overview']},services:{name:'Service map',kinds:['services','dependencies']},exposures:{name:'Challenge & controls',kinds:['exposures','controls','assumptions','evidence_assessments']},actions:{name:'Decisions & exercises',kinds:['actions','exercises','impact_assessments','observations']},assessments:{name:'Assessment coverage',kinds:['assessments','mechanisms']},losses:{name:'Losses & scenario register',kinds:['org_scenarios','internal_loss_events','taxonomy_crosswalk']}};
  function orgTabs(){
    const active=Object.keys(NAV_GROUPS).find(k=>NAV_GROUPS[k].kinds.includes(memory.tab))||'overview';
    return `<div class="ex-tabs" role="tablist" aria-label="Organization workspace">${Object.entries(NAV_GROUPS).map(([k,g])=>`<button role="tab" data-org-tab="${k}" aria-selected="${active===k}">${h(g.name)}</button>`).join('')}</div>${NAV_GROUPS[active].kinds.length>1?`<div class="ex-toolbar" aria-label="Workspace sections">${NAV_GROUPS[active].kinds.map(k=>`<button class="ex-btn ${memory.tab===k?'ex-primary':''}" data-org-tab="${k}">${h(GROUPS[k])}</button>`).join('')}</div>`:''}`;
  }
  function renderOrg(){
    const root=q('#organization-root'),c=memory.context;if(!root||!c)return;
    if(!c.enabled){root.innerHTML=`<div class="view-head"><div class="ex-eyebrow">YOUR ORGANIZATION</div><h1>Make the unknown <em>visible.</em></h1><p>Connect research hypotheses to the services, dependencies and controls your organization relies on.</p></div><div class="ex-setup"><div class="ex-eyebrow">A SEPARATE, PRIVATE WORKSPACE</div><h2>Start with what matters.</h2><p>Your service map belongs to your organization. It is stored separately and excluded from public research and standard exports.</p><ol><li>Define a critical service and its owner.</li><li>Map dependencies and the mechanisms that could disrupt it.</li><li>Challenge controls, assign actions and record what you test.</li></ol><div class="ex-toolbar"><button class="ex-btn ex-primary" id="ex-enable-context">Activate organization workspace</button></div><p class="ex-muted">Organization data is not submitted to the research model. Activation changes this local workspace configuration.</p></div>`;q('#ex-enable-context').onclick=()=>{open('Activate your organization workspace',`<p>Keep organization records in a separate local store. Public research stays separate, and private context is included in an export only when you explicitly select it.</p>${actorField()}<div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn ex-primary" id="ex-confirm-enable">Activate workspace</button></div>`);q('#ex-confirm-enable').onclick=async()=>{try{await api('/api/context/enable',{actor:getActor()});close();await loadContext();}catch(e){error(e.message);}};};applyPermissions();return;}
    root.innerHTML=`<div class="view-head"><div class="ex-eyebrow">YOUR ORGANIZATION · PRIVATE CONTEXT</div><h1>Find your <em>blind spots.</em></h1><p>A living map of your services, the hypotheses that could affect them and the evidence that changes your decisions.</p></div>${orgTabs()}<div id="ex-org-body"></div>`;
    qa('[data-org-tab]').forEach(b=>b.onclick=()=>{memory.tab=b.dataset.orgTab;renderOrg();});
    memory.tab==='overview'?renderOrgOverview():renderOrgRecords(memory.tab);applyPermissions();
  }
  function network(){
    const services=records('services'),deps=records('dependencies');if(!services.length)return '<div class="ex-empty"><h3>Every map starts with a service</h3><p>Add the first service to reveal its dependencies.</p></div>';
    const nodes=services.slice(0,14),pos=new Map(nodes.map((r,i)=>[r.id,{x:300+200*Math.cos(i/nodes.length*Math.PI*2-Math.PI/2),y:170+115*Math.sin(i/nodes.length*Math.PI*2-Math.PI/2)}]));
    return `<svg class="ex-network" viewBox="0 0 600 350" role="img" aria-label="Business service dependencies. All connections are also listed in the Dependencies tab."><defs><radialGradient id="ex-halo"><stop stop-color="#65d8ef" stop-opacity=".12"/><stop offset="1" stop-color="#65d8ef" stop-opacity="0"/></radialGradient></defs><circle cx="300" cy="170" r="170" fill="url(#ex-halo)"/>${deps.map(d=>{const a=pos.get(d.from_service_id),b=pos.get(d.to_service_id);return a&&b?`<path d="M${a.x} ${a.y} Q300 170 ${b.x} ${b.y}" stroke="${d.common_cause?'#f5c777':'#466b86'}" fill="none" stroke-width="${d.common_cause?2:1}"/>`:'';}).join('')}${nodes.map(r=>{const p=pos.get(r.id);return `<g class="ex-node" tabindex="0" role="button" aria-label="Open service ${h(r.name)}" data-service-id="${h(r.id)}"><circle cx="${p.x}" cy="${p.y}" r="${r.criticality==='critical'?12:8}" fill="${r.criticality==='critical'?'#f5c777':'#65d8ef'}" stroke="#153149" stroke-width="5"/><text x="${p.x}" y="${p.y+30}" text-anchor="middle">${h(r.name?.length>25?r.name.slice(0,23)+'…':r.name)}</text></g>`;}).join('')}</svg><p class="ex-status">${services.length>14?'Showing the first 14 services. ':''}Amber nodes mark critical services. Amber connections identify declared common causes. External suppliers appear in Dependencies.</p>`;
  }
  function renderOrgOverview(){
    const services=records('services'),assessments=records('assessments'),mechanisms=records('mechanisms');
    const denominator=memory.context.coverage.denominator;
    const tested=memory.context.coverage.counts.tested;
    const controls=records('controls'),unvalidated=controls.filter(x=>(x.effective_validation_status||x.validation_status)!=='validated').length;
    const actions=records('actions').filter(x=>!['closed','cancelled'].includes(x.status));
    q('#ex-org-body').innerHTML=`<div class="ex-grid"><div class="ex-metric"><div class="ex-eyebrow">DECLARED SCOPE</div><b>${services.length}</b><span>Business services · ${mechanisms.length} failure mechanisms</span></div><div class="ex-metric"><div class="ex-eyebrow">TESTED COVERAGE</div><b>${denominator?Math.round(tested/denominator*100)+'%':'—'}</b><span>${tested} of ${denominator} declared service–mechanism combinations · ${denominator?'Unassessed combinations remain unknown.':'Define your scope to establish a denominator.'}</span></div><div class="ex-metric"><div class="ex-eyebrow">FOLLOW THROUGH</div><b>${actions.length}</b><span>Open actions · ${unvalidated} controls awaiting successful validation</span></div></div>
      <div class="ex-toolbar"><button class="ex-btn ex-primary" data-add-kind="services">+ Add a service</button><button class="ex-btn" data-add-kind="dependencies">Map a dependency</button><button class="ex-btn" data-add-kind="mechanisms">Define a failure mechanism</button><span class="ex-spacer"></span><button class="ex-btn" id="ex-import">Import records</button><button class="ex-btn" id="ex-baseline">Save baseline</button><button class="ex-btn" id="ex-changes">Changes since baseline</button></div>
      <div class="ex-org-columns"><div class="panel"><div class="panel-title">Your dependency landscape</div>${network()}</div><div class="panel"><div class="panel-title">Where to look next</div>${!services.length?'<p class="ex-muted">Define a service to begin a focused assessment.</p>':`<div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Service</th><th>Accountable owner</th><th>Exposures</th></tr></thead><tbody>${services.map(s=>`<tr><td><button class="ex-link" data-service-id="${h(s.id)}">${h(s.name)}</button></td><td>${h(s.owner)}</td><td>${records('exposures').filter(x=>x.service_id===s.id&&x.status==='confirmed').length} confirmed</td></tr>`).join('')}</tbody></table></div>`}<p class="ex-status">No confirmed exposure means the mapping may still be incomplete. It is not a risk clearance.</p><button class="ex-btn" id="ex-open-coverage">Examine coverage</button></div></div>`;
    qa('[data-add-kind]').forEach(b=>b.onclick=()=>recordForm(b.dataset.addKind));
    qa('[data-service-id]').forEach(b=>{const action=()=>recordForm('services',services.find(s=>s.id===b.dataset.serviceId));b.onclick=action;b.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();action();}};});
    q('#ex-open-coverage').onclick=()=>{memory.tab='assessments';renderOrg();};
    q('#ex-baseline').onclick=()=>baselineDialog();q('#ex-changes').onclick=changesDialog;q('#ex-import').onclick=importDialog;
  }
  function renderOrgRecords(kind){
    const list=records(kind),columns={services:['name','owner','criticality','impact_tolerance'],dependencies:['name','from_service_id','to_service_id','external_provider','common_cause'],controls:['name','owner','validation_status'],assumptions:['statement','owner','expires_on','status'],exposures:['scenario_id','service_id','status'],actions:['title','owner','due_date','status'],exercises:['title','status','completed_on'],evidence_assessments:['claim','owner','relation','directness','effective_verification_status'],impact_assessments:['title','service_id','basis','observed_on'],assessments:['service_id','mechanism_id','status','assessed_on'],observations:['title','observed_on'],mechanisms:['name','description'],org_scenarios:['org_ref','title','owner','frequency_text','status'],internal_loss_events:['org_ref','title','occurrence_date','net_loss','currency','status'],taxonomy_crosswalk:['org_code','org_label','atlas_category','mapping_type','status','reviewer']}[kind]||['name'];
    const coverage=kind==='assessments'?`<div class="ex-table-wrap"><table class="ex-table"><caption class="ex-status">${h(memory.context.coverage.interpretation)}</caption><thead><tr><th>Service / failure mechanism</th>${records('mechanisms').map(m=>`<th>${h(m.name)}</th>`).join('')}</tr></thead><tbody>${records('services').map(s=>`<tr><th>${h(s.name)}</th>${records('mechanisms').map(m=>{const cell=memory.context.coverage.rows.find(r=>r.service_id===s.id&&r.mechanism_id===m.id);return `<td class="ex-cell-${h(cell?.status||'unassessed')}"><button class="ex-link" data-coverage-service="${h(s.id)}" data-coverage-mechanism="${h(m.id)}">${h(label(cell?.status||'unassessed'))}${cell?.needs_reassessment?' · reassess':''}</button></td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`:'';
    q('#ex-org-body').innerHTML=`<p class="ex-muted">${h(descriptions[kind])}</p><div class="ex-toolbar"><button class="ex-btn ex-primary" id="ex-add-record">+ Add ${h(kind==='assessments'?'assessment':label(kind).replace(/ies$/,'y').replace(/s$/,''))}</button><button class="ex-btn" id="ex-import">Import records</button><span class="ex-spacer"></span><input class="ex-filter" style="max-width:280px" id="ex-org-search" type="search" placeholder="Search ${h(GROUPS[kind].toLowerCase())}" aria-label="Search records"></div>${coverage}<div class="ex-table-wrap"><table class="ex-table"><thead><tr>${columns.map(k=>`<th>${h(fieldLabels[k]||label(k).replace(/ ids?$/,''))}</th>`).join('')}<th>Record</th></tr></thead><tbody>${list.map(r=>`<tr data-org-search="${h(JSON.stringify(r).toLowerCase())}">${columns.map(k=>`<td>${['status','validation_status','effective_verification_status','relation','directness','basis'].includes(k)?pills(k==='validation_status'?(r.effective_validation_status||r[k]):r[k]):h(k.endsWith('_id')?displayRef(r[k]):pretty(r[k]))}</td>`).join('')}<td><button class="ex-btn" data-edit-record="${h(r.id)}">Open</button></td></tr>`).join('')}</tbody></table></div>${!list.length?`<div class="ex-empty"><h3>No ${h(GROUPS[kind].toLowerCase())} recorded yet</h3><p>${h(descriptions[kind])}</p></div>`:''}`;
    q('#ex-add-record').onclick=()=>recordForm(kind);q('#ex-import').onclick=()=>importDialog(kind);
    q('#ex-org-search').oninput=e=>qa('[data-org-search]').forEach(r=>r.hidden=!r.dataset.orgSearch.includes(e.target.value.toLowerCase()));
    qa('[data-edit-record]').forEach(b=>b.onclick=()=>recordForm(kind,list.find(r=>r.id===b.dataset.editRecord)));
    qa('[data-coverage-service]').forEach(b=>{b.onclick=()=>recordForm('assessments',list.find(r=>r.service_id===b.dataset.coverageService&&r.mechanism_id===b.dataset.coverageMechanism)||null,{service_id:b.dataset.coverageService,mechanism_id:b.dataset.coverageMechanism,assessed_on:new Date().toISOString().slice(0,10)});});
  }
  const refKinds={evidence_id:'evidence',service_id:'services',service_ids:'services',from_service_id:'services',to_service_id:'services',mechanism_id:'mechanisms',mechanism_ids:'mechanisms',exposure_ids:'exposures',exercise_ids:'exercises',control_ids:'controls',assumption_ids:'assumptions',scenario_id:'scenarios',scenario_ids:'scenarios',atlas_scenario_ids:'scenarios'};
  const fieldLabels={effective_verification_status:'Verification',relation:'Relationship to the claim',directness:'Type of evidence',time_since_onset_minutes:'Minutes since disruption began',downtime_minutes:'Service downtime (minutes)',backlog:'Backlog (items)',customers:'Customers affected (count)',capacity_loss:'Capacity lost (%)',tolerance_reference:'Service tolerance being assessed',basis:'Assumed or measured impact',impact_tolerance:'Maximum tolerable disruption',from_service_id:'Dependent service',to_service_id:'Supporting service',external_provider:'External supplier (instead of a service)',common_cause:'Shared cause of failure',service_ids:'Affected services',mechanism_ids:'Failure mechanisms',source:'Source or basis for this declaration',source_refs:'Source references',evidence_refs:'Evidence references',closure_evidence:'Evidence of completion',rationale:'Rationale for this decision',test:'How will you test this assumption?',injects:'Exercise injects',outcomes:'Observed outcomes'};
  function schemaShape(property){return property.anyOf?.find(s=>s.type!=='null')||property;}
  function formField(key,property,value,required){
    const shape=schemaShape(property),title=fieldLabels[key]||label(key).replace(/ ids?$/,''),id='ctx-'+key;
    let input;
    if(refKinds[key]){
      const choices=refKinds[key]==='scenarios'?[...(DATA?.scenarios||[]),...(key==='atlas_scenario_ids'?[]:records('org_scenarios'))]:refKinds[key]==='evidence'?(DATA?.evidence_events||[]).map(r=>({...r,id:r.event_id||r.ref||r.id,title:r.headline||r.title})):records(refKinds[key]);const multiple=shape.type==='array';
      const selected=multiple?(value||[]):value?[value]:[];input=`<select id="${id}" data-field="${h(key)}" ${multiple?'multiple':''} ${required?'required':''}>${!multiple?'<option value="">Choose…</option>':''}${choices.map(r=>`<option value="${h(r.id)}" ${selected.includes(r.id)?'selected':''}>${h(names(r))}</option>`).join('')}</select>${multiple?'<small>Select all that apply (⌘/Ctrl-click for several).</small>':''}`;
    }else if(shape.enum){input=`<select id="${id}" data-field="${h(key)}" ${required?'required':''}>${required&&value==null&&property.default==null?'<option value="">Choose…</option>':''}${shape.enum.map(v=>`<option value="${h(v)}" ${v===(value??property.default)?'selected':''}>${h(label(v))}</option>`).join('')}</select>`;}
    else if(shape.type==='array'){input=`<textarea id="${id}" data-field="${h(key)}" ${required?'required':''}>${h((value||[]).join('\n'))}</textarea><small>One item per line. Use references your team can retrieve.</small>`;}
    else if(shape.type==='object'){input=`<textarea id="${id}" data-field="${h(key)}">${h(JSON.stringify(value||{}))}</textarea>`;}
    else if(shape.type==='boolean'){input=`<input id="${id}" data-field="${h(key)}" type="checkbox" ${value??property.default?'checked':''}>`;}
    else if(['number','integer'].includes(shape.type)){input=`<input id="${id}" data-field="${h(key)}" type="number" step="${shape.type==='integer'?'1':'any'}" min="${shape.minimum??0}" ${shape.maximum!=null?'max="'+shape.maximum+'"':''} value="${h(value??'')}" ${required?'required':''}>`;}
    else if(shape.format==='date'){input=`<input id="${id}" data-field="${h(key)}" type="date" value="${h(value||'')}" ${required?'required':''}>`;}
    else if(['notes','description','impact_tolerance','statement','claim','rationale','test'].includes(key)){input=`<textarea id="${id}" data-field="${h(key)}" ${required?'required':''}>${h(value||'')}</textarea>`;}
    else input=`<input id="${id}" data-field="${h(key)}" value="${h(value??property.default??'')}" ${required?'required':''} maxlength="${shape.maxLength||4000}">`;
    return `<div class="ex-field"><label for="${id}">${h(title)}${required?' *':''}</label>${input}</div>`;
  }
  function recordForm(kind,record=null,prefill={}){
    const schema=memory.context?.schemas?.[kind];if(!schema){go('organization');loadContext();return;}
    const values={...prefill,...(record||{})},properties=schema.properties,common=['source','source_refs','classification','notes'];
    const keys=[...Object.keys(properties).filter(k=>!common.includes(k)&&!(kind==='impact_assessments'&&['units','service_revision'].includes(k))&&!(kind==='evidence_assessments'&&k==='evidence_revision')),...common.filter(k=>k in properties)];
    const primaryKeys=keys.filter(k=>schema.required?.includes(k)||['status','validation_status','criticality','decision_type','basis','rationale','acceptance_expires_on','downtime_minutes','backlog','customers','capacity_loss','relation','directness','independence_status'].includes(k));
    const sourceReview=kind==='evidence_assessments'&&record?.effective_evidence_state==='changed'?`<div class="ex-notice" style="grid-column:1/-1"><strong>The source changed after this assessment.</strong><p>Review its current claim and supporting references before reaffirming this declaration.</p><label><input type="checkbox" id="ex-rereview-source">I reviewed the changed source and want to record a new assessment revision.</label><small>Source revision: ${h(record.current_evidence_revision)}</small></div>`:'';
    const fields=sourceReview+primaryKeys.map(k=>formField(k,properties[k],values[k],schema.required?.includes(k))).join('')+`<details class="ex-form-details"><summary>Supporting evidence & optional details</summary>${keys.filter(k=>!primaryKeys.includes(k)).map(k=>formField(k,properties[k],values[k],false)).join('')}</details>`;
    open(record?'Review '+label(kind).replace(/ies$/,'y').replace(/s$/,''):'Add '+label(kind).replace(/ies$/,'y').replace(/s$/,''),`<p>${h(descriptions[kind]||'Record the context for this decision.')}</p><form id="ex-record-form">${fields}${actorField()}${record?`<p class="ex-status">Revision ${h(record.revision)} · last changed ${h(record.updated_at)}. Changes preserve the previous revision.</p>`:''}<div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot">${record?'<button class="ex-btn ex-danger" type="button" id="ex-archive">Archive record</button>':''}<button class="ex-btn ex-primary" type="submit" ${canEdit()?'':'disabled'}>Save record</button></div></form>`);
    const recordView=dialog;
    q('#ex-record-form').onsubmit=async e=>{
      e.preventDefault();const btn=e.submitter;btn.disabled=true;
      try{
        if(!canEdit())throw new Error('Your role can view this record but cannot change it.');
        const data={};if(record)data.id=record.id;
        if(kind==='evidence_assessments'&&record?.evidence_revision)data.evidence_revision=q('#ex-rereview-source')?.checked?record.current_evidence_revision:record.evidence_revision;
        if(kind==='impact_assessments'){data.units=record?.units||{time_since_onset_minutes:'minutes',downtime_minutes:'minutes',backlog:'items',customers:'count',capacity_loss:'percent'};}
        for(const k of keys){const el=q('#ctx-'+k),shape=schemaShape(properties[k]);
          data[k]=shape.type==='boolean'?el.checked:el.multiple?[...el.selectedOptions].map(o=>o.value):shape.type==='array'?el.value.split('\n').map(x=>x.trim()).filter(Boolean):shape.type==='object'?JSON.parse(el.value||'{}'):el.value.trim();
          if(['number','integer'].includes(shape.type)&&data[k]!=='')data[k]=Number(data[k]);
          if(data[k]===''&&properties[k].anyOf?.some(s=>s.type==='null'))data[k]=null;
        }
        await api('/api/context/save',{kind,record:data,actor:getActor(),expected_revision:record?.revision??null});if(dialog===recordView)close();await loadContext();
      }catch(err){if(dialog===recordView){error(err.message);dialog.querySelectorAll('.ex-form-details').forEach(d=>d.open=true);}btn.disabled=false;}
    };
    q('#ex-archive')?.addEventListener('click',async()=>{const actor=getActor();open('Archive this record?',`<p>Archive “${h(names(record))}” from the active assessment. Its history remains available. Linked records may require reassessment.</p><div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn ex-danger" id="ex-confirm-archive">Archive record</button></div>`);q('#ex-confirm-archive').onclick=async()=>{try{await api('/api/context/archive',{kind,record_id:record.id,actor,expected_revision:record.revision});close();loadContext();}catch(e){error(e.message);}};});
  }
  function baselineDialog(ids=null){
    if(!memory.context?.enabled){open('Start a private assessment', '<p>Activate the organization workspace to retain named baselines and watchlists. Your research catalog remains separate.</p><button class="ex-btn ex-primary" id="ex-baseline-setup">Open organization workspace</button>');q('#ex-baseline-setup').onclick=()=>{close();go('organization');};return;}
    open(ids?'Track these scenarios':'Keep a baseline',`<p>Save your organization assessment and ${ids?`${ids.length} selected scenarios with their linked evidence. This watchlist keeps the same scenario IDs at future reviews.`:'the current research catalog with its linked evidence. Future comparisons show new, changed and retired scenarios.'}</p><p class="ex-status">Snapshots record the read time of each store. A baseline is a comparison point, not an approval.</p><div class="ex-field"><label for="ex-baseline-name">Baseline name</label><input id="ex-baseline-name" maxlength="120" placeholder="Quarterly resilience review"></div>${actorField()}<div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn ex-primary" id="ex-baseline-save" ${canEdit()?'':'disabled'}>Save baseline</button></div>`);
    const baselineView=dialog;
    q('#ex-baseline-save').onclick=async()=>{const btn=q('#ex-baseline-save');btn.disabled=true;try{await api('/api/context/baseline',{name:q('#ex-baseline-name').value,actor:getActor(),expected_revision:memory.context.store_revision,...(ids?{scenario_ids:ids}:{})});if(dialog===baselineView){close();await loadContext();changesDialog();}}catch(e){if(dialog===baselineView)error(e.message);btn.disabled=false;}};
  }
  function researchChanges(research){
    if(!research||research.status==='not_captured')return '<p class="ex-status">This older baseline did not capture research. Save a new baseline to track evidence and scenario changes.</p>';
    return `<h3>Research & evidence</h3><p class="ex-status">${research.scope?.mode==='selected'?'Fixed scenario watchlist':'Research catalog'} · ${research.status==='partial'?'Some data could not be verified':'Comparison complete'}. Review the changes and decide what needs reassessment.</p>${['scenarios','evidence'].map(kind=>{const part=research[kind];if(!part||part.status!=='compared')return `<p class="ex-notice">${h(label(kind))}: ${h(label(part?.status||'unavailable'))}. Changes cannot be verified.</p>`;const rows=[...part.added.map(r=>({r,type:'Added'})),...part.changed.map(r=>({r:r.after,type:'Changed',fields:r.fields})),...part.removed.map(r=>({r,type:'Removed from captured scope'}))];return `<details open><summary>${h(label(kind))} · ${Object.entries(part.counts||{}).map(([k,n])=>h(n)+' '+h(k)).join(' · ')}</summary>${rows.length?`<div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Record</th><th>Change</th><th>Before → after</th></tr></thead><tbody>${rows.map(({r,type,fields})=>`<tr><td>${h(r.title||r.event_title||r.scenario_id||r.event_id||r.id)}</td><td>${h(type)}</td><td>${fields?fields.map(f=>`${h(label(f.field))}: ${h(pretty(f.before))} → ${h(pretty(f.after))}`).join('<br>'):h(r.status||r.scope_state||'')}</td></tr>`).join('')}</tbody></table></div>`:'<p class="ex-status">No verified changes in this scope.</p>'}</details>`;}).join('')}<details><summary>How to interpret these changes</summary><p class="ex-status">Added evidence may be older material newly linked to these scenarios. Removed evidence may have been unlinked. A change requires review before it becomes a conclusion.</p><p class="ex-status">Baseline: ${h(research.baseline_as_of)}<br>Current comparison: ${h(research.current_as_of)}</p></details>`;
  }
  function changesDialog(){
    const baselines=memory.context?.baselines||[];
    open('What changed?',`${baselines.length?`<div class="ex-field"><label for="ex-baseline-select">Compare the current assessment with</label><select id="ex-baseline-select">${baselines.map(b=>`<option value="${h(b.id)}">${h(b.name)} · ${h(b.created_at.slice(0,10))}</option>`).join('')}</select></div><div id="ex-change-result"></div>`:'<p>Save a baseline first to compare future changes.</p>'}<details><summary>Recent organization decisions</summary><div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>When</th><th>Who</th><th>Change</th></tr></thead><tbody>${(memory.context.changes||[]).map(c=>`<tr><td>${h(c.recorded_at)}</td><td>${h(c.actor)}</td><td>${h(label(c.operation))} · ${h(label(c.kind))} · ${h(displayRef(c.record_id))}</td></tr>`).join('')}</tbody></table></div></details><div id="ex-error" class="ex-error"></div>${baselines.length?'<div class="ex-dialog-foot"><button class="ex-btn ex-primary" id="ex-changes-export">Export this baseline review</button></div>':''}`,true);
    if(baselines.length)q('#ex-changes-export').onclick=()=>exportDialog(null,q('#ex-baseline-select').value);
    const changeView=dialog;let requestRevision=0;
    async function compare(){const revision=++requestRevision;try{const d=await api('/api/context/compare?baseline_id='+encodeURIComponent(q('#ex-baseline-select').value));if(dialog!==changeView||revision!==requestRevision)return;q('#ex-change-result').innerHTML=`<h3>Organization context</h3><div class="ex-counts">${Object.entries(d.counts).map(([k,n])=>`<span>${h(n)} ${h(k)}</span>`).join('')}</div><div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Record</th><th>Change</th><th>Details</th></tr></thead><tbody>${[...d.added.map(r=>({r,type:'Added'})),...d.changed.map(r=>({r:r.after,before:r.before,type:r.after.archived?'Archived':'Changed'})),...d.removed.map(r=>({r,type:'Removed'}))].map(({r,before,type})=>`<tr><td>${h(names(r))}</td><td>${h(type)}</td><td>${before?Object.keys(r).filter(k=>!['revision','updated_at'].includes(k)&&JSON.stringify(before[k])!==JSON.stringify(r[k])).map(k=>`${h(label(k))}: ${h(pretty(before[k]))} → ${h(pretty(r[k]))}`).join('<br>'):h(label(r.kind))}</td></tr>`).join('')}</tbody></table></div>${researchChanges(d.research_changes)}`;}catch(e){if(dialog===changeView&&revision===requestRevision)error(e.message);}}
    if(baselines.length){q('#ex-baseline-select').onchange=compare;compare();}
  }
  function importDialog(initialKind='services'){
    open('Bring your organization records',`<p>Import a CSV or JSON file into the private workspace. Map its columns, review each candidate and choose what to save. No research record is approved by importing it.</p><div class="ex-field"><label for="ex-import-kind">Record type</label><select id="ex-import-kind">${Object.entries(GROUPS).filter(([k])=>k!=='overview').map(([k,v])=>`<option value="${k}" ${k===initialKind?'selected':''}>${h(v)}</option>`).join('')}</select></div><div class="ex-field"><label for="ex-import-file">CSV or JSON file (up to 2 MB)</label><input id="ex-import-file" type="file" accept=".csv,.json,text/csv,application/json"></div><div class="ex-field"><label for="ex-import-source">Source or basis for these records</label><input id="ex-import-source" placeholder="For example, service catalog approved at the September review"></div><div id="ex-import-mapping"></div>${actorField()}<div id="ex-import-results"></div><div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn" id="ex-import-preview">Preview candidates</button><button class="ex-btn ex-primary" id="ex-import-submit" disabled>Import selected</button></div>`);
    const importView=dialog;let preview=null,content='',format='csv',mapping={},importRevision=0,fileRevision=0;
    const invalidate=()=>{importRevision++;preview=null;q('#ex-import-submit').disabled=true;q('#ex-import-results').innerHTML='';};
    q('#ex-import-kind').onchange=()=>{invalidate();q('#ex-import-mapping').innerHTML='';mapping={};};
    q('#ex-import-file').onchange=async e=>{invalidate();content='';const readRevision=++fileRevision;const file=e.target.files[0];if(!file)return;if(file.size>2000000){error('Choose a file smaller than 2 MB.');return;}const text=await file.text();if(dialog!==importView||readRevision!==fileRevision)return;content=text;format=file.name.toLowerCase().endsWith('.json')?'json':'csv';mapping={};q('#ex-import-mapping').innerHTML='';};
    q('#ex-import-source').oninput=invalidate;
    q('#ex-import-preview').onclick=async()=>{
      try{
        if(!content)throw new Error('Choose a file first.');
        const requestedRevision=importRevision;
        const result=await api('/api/imports/preview',{content,format,kind:q('#ex-import-kind').value,field_mapping:Object.keys(mapping).length?mapping:null,source:q('#ex-import-source').value,actor:getActor()});
        if(dialog!==importView||requestedRevision!==importRevision)return;preview=result;
        const props={id:{},expected_revision:{},...memory.context.schemas[q('#ex-import-kind').value].properties};
        if(preview.columns?.length&&!q('#ex-import-mapping').children.length){q('#ex-import-mapping').innerHTML=`<details open><summary>Match columns to Atlas fields</summary>${preview.columns.map(col=>`<div class="ex-field"><label>${h(col)}</label><select data-map-column="${h(col)}"><option value="">Ignore this column</option>${Object.keys(props).map(k=>`<option value="${h(k)}" ${k===col?'selected':''}>${h(fieldLabels[k]||label(k))}</option>`).join('')}</select></div>`).join('')}</details>`;qa('[data-map-column]').forEach(el=>el.onchange=()=>{mapping=Object.fromEntries(qa('[data-map-column]').filter(x=>x.value).map(x=>[x.dataset.mapColumn,x.value]));invalidate();});}
        q('#ex-import-results').innerHTML=`<div class="ex-preview"><h3>Review candidates before saving</h3>${(preview.candidates||[]).map(c=>`<label class="ex-check-row"><input type="checkbox" data-import-candidate="${h(c.candidate_id)}" ${(c.errors||[]).length?'disabled':'checked'}><span>${h(c.label||names(c.record?.data||{})||'Row '+c.row_index)}<small>${h((c.errors||[]).join(' · ')||(c.warnings||[]).join(' · ')||'Ready to import')}</small><details><summary>Inspect normalized fields</summary><dl>${Object.entries(c.record?.data||{}).map(([k,v])=>`<dt>${h(fieldLabels[k]||label(k))}</dt><dd>${h(pretty(v))}</dd>`).join('')}</dl></details></span></label>`).join('')}</div>`;
        q('#ex-import-submit').disabled=!(preview.candidates||[]).some(c=>!(c.errors||[]).length);error('');
      }catch(e){error(e.message);}
    };
    q('#ex-import-submit').onclick=async()=>{try{const selected_ids=qa('[data-import-candidate]:checked').map(x=>x.dataset.importCandidate);if(!selected_ids.length)throw new Error('Select at least one valid candidate.');await api('/api/imports/submit',{preview_id:preview.preview_id,selected_ids,actor:getActor()});close();await loadContext();}catch(e){error(e.message);}};
  }
  async function integrationDialog(ids){
    open('Prepare an enterprise handoff',`<p>Stage ${ids.length} selected scenario(s) and their linked records for an enterprise system. Preparing a package saves a frozen copy in the private local handoff history, including any organization context you select. Importing it into the destination remains a separate, reviewable step.</p><div class="ex-field"><label for="ex-adapter">Destination format</label><select id="ex-adapter"><option value="servicenow">ServiceNow import set</option><option value="generic_rest">Generic REST record package</option></select></div><div class="ex-field"><label><input type="checkbox" id="ex-handoff-private">Include linked private organization context</label></div><details><summary>Destination mapping</summary><p class="ex-status">The receiving team must create the staging table and transform map. The package preserves source IDs, revisions and full source records.</p><div class="ex-field"><label for="ex-staging-table">Destination staging table</label><input id="ex-staging-table" value="u_contingency_atlas_import"></div><div class="ex-field"><label for="ex-source-system">Stable source-system identifier</label><input id="ex-source-system" value="contingency-atlas"></div></details>${actorField()}<div id="ex-handoff-result"></div><div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn" id="ex-outbox">Previous handoffs</button><button class="ex-btn ex-primary" id="ex-stage-handoff">Prepare package</button></div>`);
    const handoffView=dialog,controls=[...handoffView.querySelectorAll('input,select,button:not([data-close])')];let preparing=false;
    q('#ex-adapter').onchange=e=>{q('#ex-staging-table').value=e.target.value==='servicenow'?'u_contingency_atlas_import':'atlas_records';};
    q('#ex-outbox').onclick=outboxDialog;
    q('#ex-stage-handoff').onclick=async()=>{
      if(preparing||dialog!==handoffView)return;
      const btn=q('#ex-stage-handoff');
      try{
        const actor=getActor(),body={actor,adapter:q('#ex-adapter').value,selection:{scenario_ids:[...ids],include_context:q('#ex-handoff-private').checked},mapping:{source_system:q('#ex-source-system').value,staging_table:q('#ex-staging-table').value}};
        preparing=true;controls.forEach(el=>el.disabled=true);btn.textContent='Saving frozen package…';error('');
        const d=await api('/api/integrations/preview',body);
        if(dialog===handoffView)showIntegrationPackage(d,actor);
      }catch(e){if(dialog===handoffView)error(e.message);}
      finally{preparing=false;if(dialog===handoffView){controls.forEach(el=>el.disabled=false);btn.textContent='Prepare package';}}
    };
  }
  function integrationManifest(manifest={}){
    const privateContext=manifest.classification==='includes_organization_context',mapping=manifest.mapping||{},selection=manifest.selection||{};
    return `<div class="ex-notice"><strong>${privateContext?'Includes private organization context':'Public research only'}</strong><p>${privateContext?'This frozen package contains the linked private records listed below. Review their contents before sharing.':'Organization context is excluded from this frozen package.'}</p></div><p class="ex-status">${h(manifest.record_count??0)} staged records · source snapshot ${h(manifest.snapshot_id)} · captured ${h(manifest.source_generated_at)}</p><details><summary>Frozen selection and destination mapping</summary><dl><dt>Selected scenarios</dt><dd>${h(pretty(selection.scenario_ids))}</dd><dt>Source-system identifier</dt><dd>${h(mapping.source_system)}</dd><dt>Destination staging table</dt><dd>${h(mapping.staging_table)}</dd></dl><p class="ex-status">The receiving team must configure the destination transform. A retry packet contains only the outstanding rows listed in the package, while its source selection describes the original snapshot.</p><div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Atlas field</th><th>Destination column</th></tr></thead><tbody>${Object.entries(mapping.fields||{}).map(([source,target])=>`<tr><td>${h(label(source))}</td><td>${h(target)}</td></tr>`).join('')}</tbody></table></div></details>`;
  }
  function integrationRecordDetails(record){
    try{return `<dl>${Object.entries(JSON.parse(record.record_json)).map(([key,value])=>`<dt>${h(label(key))}</dt><dd>${h(pretty(value))}</dd>`).join('')}</dl>`;}
    catch{return `<p>${h(record.record_json)}</p>`;}
  }
  function showIntegrationPackage(d,actor){
    open('Review the handoff package',`<p>${h(d.notice)}</p>${integrationManifest(d.manifest)}<div class="ex-counts">${Object.entries(d.counts).map(([k,n])=>`<span>${h(n)} ${h(k)}</span>`).join('')}</div><p class="ex-status">${h(d.package_id)} · ${h(d.adapter)} · prepared by ${h(d.actor)}</p><div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Record and contents</th><th>Type</th><th>Source status</th></tr></thead><tbody>${(d.records||[]).map(r=>`<tr><td><strong>${h(r.display_name||r.external_id)}</strong><small class="ex-muted">${h(r.external_id)}</small><details><summary>Inspect frozen record</summary><p class="ex-status">Classification: ${h(r.classification)}<br>Revision: ${h(r.revision_hash)}</p>${integrationRecordDetails(r)}</details></td><td>${h(label(r.record_type))}</td><td>${h(r.source_status)}</td></tr>`).join('')}</tbody></table></div><div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn" id="ex-view-outbox">Handoff history</button><button class="ex-btn ex-primary" id="ex-download-handoff">Download package</button></div>`);
    const packageView=dialog;
    q('#ex-view-outbox').onclick=outboxDialog;
    q('#ex-download-handoff').onclick=async()=>{const btn=q('#ex-download-handoff');if(btn.disabled)return;btn.disabled=true;try{await downloadPackage(d.package_id,actor);if(dialog===packageView)btn.textContent='Downloaded';}catch(e){if(dialog===packageView)error(e.message);}finally{if(dialog===packageView)btn.disabled=false;}};
  }
  async function downloadPackage(package_id,actor){
    const response=await fetch('/api/integrations/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({package_id,actor})});
    if(!response.ok){const d=await response.json();throw new Error(d.error||'Package download failed.');}
    const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');a.href=url;a.download=package_id+'.zip';document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);
  }
  async function outboxDialog(){
    open('Enterprise handoff history',`<p>Track staged packages and operator-reported destination outcomes. A downloaded package is not a successful integration.</p>${actorField()}<div id="ex-outbox-list"></div><div id="ex-error" class="ex-error"></div>` ,true);
    const outboxView=dialog;
    try{const d=await api('/api/integrations');if(dialog!==outboxView)return;q('#ex-outbox-list').innerHTML=d.packages.length?`<div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Prepared package</th><th>Owner / destination</th><th>Reconciliation</th><th>Next action</th></tr></thead><tbody>${d.packages.map((p,i)=>`<tr><td>${h(p.created_at)}<br><small>${h(p.package_id)}</small><details><summary>Inspect package scope</summary>${integrationManifest(p.manifest)}</details></td><td>${h(p.actor)} · ${h(p.adapter)}</td><td>${h(p.counts.accepted)} accepted · ${h(p.counts.rejected)} rejected · ${h(p.counts.pending)} unconfirmed</td><td><button class="ex-btn" data-reconcile="${i}">View / reconcile receipts</button><button class="ex-btn" data-download-package="${i}">Download again</button></td></tr>`).join('')}</tbody></table></div>`:'<div class="ex-empty"><h3>No handoffs yet</h3><p>Choose scenarios in the portfolio, then prepare your first enterprise handoff.</p></div>';
      qa('[data-reconcile]').forEach(b=>b.onclick=()=>{try{reconcileDialog(d.packages[Number(b.dataset.reconcile)],getActor());}catch(e){error(e.message);}});
      qa('[data-download-package]').forEach(b=>b.onclick=async()=>{if(b.disabled||dialog!==outboxView)return;b.disabled=true;try{await downloadPackage(d.packages[Number(b.dataset.downloadPackage)].package_id,getActor());if(dialog===outboxView)b.textContent='Downloaded';}catch(e){if(dialog===outboxView)error(e.message);}finally{if(dialog===outboxView)b.disabled=false;}});
    }catch(e){if(dialog===outboxView)error(e.message);}
  }
  function reconcileDialog(pack,actor){
    const pending=pack.rows.filter(r=>r.status==='pending'),recorded=pack.rows.filter(r=>r.status!=='pending'),outstanding=pack.rows.filter(r=>r.status!=='accepted');
    open('Record the destination outcome',`<p>Use the actual destination result for each record. An accepted result requires its destination ID; a rejection requires a reason. Atlas source approvals are unaffected.</p><p class="ex-status">${h(pack.package_id)} · operator ${h(actor)}</p>${recorded.length?`<h3>Recorded outcomes</h3><div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Source record</th><th>Outcome</th><th>Destination ID / reason</th><th>Recorded</th></tr></thead><tbody>${recorded.map(r=>`<tr><td>${h(r.external_id)}</td><td>${h(label(r.status))}</td><td>${h(r.target_id||r.message)}</td><td>${h(r.acknowledged_at)}</td></tr>`).join('')}</tbody></table></div><p class="ex-status">Recorded outcomes remain preserved. Use a separate retry packet to report another attempt.</p>`:''}${pending.length?`<h3>Unconfirmed outcomes</h3><div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Source record</th><th>Outcome</th><th>Destination ID or rejection reason</th></tr></thead><tbody>${pending.map((r,i)=>`<tr><td>${h(r.external_id)}</td><td><select data-receipt-status="${i}" aria-label="Outcome for ${h(r.external_id)}"><option value="">Not confirmed</option><option value="accepted">Accepted</option><option value="rejected">Rejected</option></select></td><td><input data-receipt-detail="${i}" maxlength="1000" aria-label="Destination ID or rejection reason for ${h(r.external_id)}"></td></tr>`).join('')}</tbody></table></div>`:'<p>All outcomes in this packet have been recorded.</p>'}<p id="ex-retry-hint" class="ex-status"></p><div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn" id="ex-retry-package">Prepare retry of ${outstanding.length} outstanding records</button><button class="ex-btn ex-primary" id="ex-record-receipt">Record confirmed outcomes</button></div>`,true);
    const receiptView=dialog,inputs=[...receiptView.querySelectorAll('input,select')],statuses=[...receiptView.querySelectorAll('[data-receipt-status]')],saveButton=q('#ex-record-receipt'),retryButton=q('#ex-retry-package');let busy=false;
    const hasEdits=()=>inputs.some(el=>el.value.trim());
    const sync=()=>{inputs.forEach(el=>el.disabled=busy);saveButton.disabled=busy||!statuses.some(el=>el.value);retryButton.disabled=busy||hasEdits()||!outstanding.length;q('#ex-retry-hint').textContent=hasEdits()?'Save or clear receipt edits before preparing a retry.':'A retry preserves the frozen source revisions. Investigate uncertain destination outcomes before importing again.';};
    inputs.forEach(el=>{el.oninput=sync;el.onchange=sync;});sync();
    saveButton.onclick=async()=>{
      if(busy||dialog!==receiptView||saveButton.disabled)return;
      const acknowledgments=statuses.filter(el=>el.value).map(el=>{const i=Number(el.dataset.receiptStatus),detail=receiptView.querySelector('[data-receipt-detail="'+i+'"]').value;return {external_id:pending[i].external_id,revision_hash:pending[i].revision_hash,status:el.value,target_id:el.value==='accepted'?detail:'',message:el.value==='rejected'?detail:''};});
      busy=true;sync();error('');
      try{await api('/api/integrations/ack',{package_id:pack.package_id,actor,acknowledgments});if(dialog===receiptView)await outboxDialog();}
      catch(e){if(dialog===receiptView)error(e.message);}
      finally{busy=false;if(dialog===receiptView)sync();}
    };
    retryButton.onclick=async()=>{
      if(busy||dialog!==receiptView||retryButton.disabled||hasEdits())return;
      busy=true;sync();error('');
      try{const d=await api('/api/integrations/retry',{package_id:pack.package_id,actor,selected_ids:outstanding.map(r=>r.external_id)});if(dialog===receiptView)showIntegrationPackage(d,actor);}
      catch(e){if(dialog===receiptView)error(e.message);}
      finally{busy=false;if(dialog===receiptView)sync();}
    };
  }
  async function loadOperations(){
    let el=q('#ex-operations');if(!el){el=document.createElement('details');el.id='ex-operations';el.className='panel';el.style.padding='22px';el.style.marginBottom='22px';q('#settings-root').before(el);}
    try{const [readiness,jobs]=await Promise.all([api('/api/readiness').then(value=>({value})).catch(error=>({error:error.message})),api('/api/jobs')]);const status=readiness.value;
      el.innerHTML=`<summary>Workspace health & run recovery</summary><p class="ex-status">Interrupted runs require a deliberate retry. A queued run can be cancelled before it starts.</p><div class="ex-counts">${status?status.checks.map(c=>`<span>${h(label(c.name))}: ${h(label(c.status))}</span>`).join(''):'<span>Detailed deployment checks are available to administrators.</span>'}</div><div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Run</th><th>State</th><th>When</th><th>Recovery</th></tr></thead><tbody>${jobs.jobs.map(j=>`<tr><td>${h(PHASE_RUN[j.phase]?.name||j.phase)}</td><td>${pills(j.state)}</td><td>${h(j.created_at||j.started_at)}</td><td>${canEdit()&&j.state==='queued'?`<button class="ex-btn" data-job-action="cancel" data-job-id="${h(j.id)}">Cancel queued run</button>`:canEdit()&&['failed','interrupted','cancelled'].includes(j.state)?`<button class="ex-btn" data-job-action="retry" data-job-id="${h(j.id)}">Review & retry</button>`:h(j.error||'')}</td></tr>`).join('')}</tbody></table></div><p class="ex-status">Backup and restore are operator procedures. Recovery verifies checksums and restores into a new directory before any live store is switched.</p>`;
      qa('[data-job-action]').forEach(b=>b.onclick=()=>{const action=b.dataset.jobAction;open(action==='retry'?'Retry this run?':'Cancel queued run?',`<p>${action==='retry'?'Start a new run with the retained inputs. Completed writes from the previous attempt remain; inspect its result before retrying.':'Remove this run from the queue. It has not started executing.'}</p>${actorField()}<div id="ex-error" class="ex-error"></div><div class="ex-dialog-foot"><button class="ex-btn ex-primary" id="ex-job-confirm">${action==='retry'?'Start a new attempt':'Cancel this run'}</button></div>`);q('#ex-job-confirm').onclick=async()=>{try{await api('/api/jobs/'+action,{job_id:b.dataset.jobId,actor:getActor()});close();loadOperations();}catch(e){error(e.message);}};});
    }catch(e){el.innerHTML=`<summary>Workspace health</summary><p class="ex-error">${h(e.message)}</p>`;}
  }
  async function loadDiagnostics(){
    let el=q('#ex-diagnostics');if(!el){el=document.createElement('details');el.id='ex-diagnostics';el.className='panel';el.style.padding='22px';el.style.marginBottom='22px';q('#view-evidence .view-head').after(el);}
    el.innerHTML='<summary>Evidence quality & source gaps</summary><p class="ex-status">Loading evidence diagnostics…</p>';
    try{const d=await api('/api/diagnostics');
      const sources=d.independence?.publishers?.groups||[],stale=d.stale_approvals?.records||[],broken=d.broken_refs?.records||[];
      const table=(heads,rows)=>`<div class="ex-table-wrap"><table class="ex-table"><thead><tr>${heads.map(k=>`<th>${h(k)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(v=>`<td>${h(pretty(v))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      el.innerHTML=`<summary>Evidence quality & source gaps</summary><p class="ex-status">Citation breadth counts references. It does not establish independent corroboration or calibrated confidence.</p>${d.status!=='available'?'<div class="ex-notice">Some stores could not be read. These diagnostics are incomplete.</div>':''}<div class="ex-grid">${[['Evidence records',d.summary.evidence_records],['Records with missing fields',d.summary.records_with_missing_fields],['Approvals due for review',d.summary.stale_or_undated_approvals]].map(([k,v])=>`<div class="ex-metric"><b>${h(v)}</b><span>${h(k)}</span></div>`).join('')}</div><div class="ex-org-columns"><div><h3>Where your evidence comes from</h3><p class="ex-status">${h(d.independence.interpretation)}</p>${table(['Publisher','Records','Share of named sources'],sources.map(r=>[r.label,r.count,Math.round(r.share_of_known*100)+'%']))}</div><div><h3>What deserves another look</h3><p class="ex-status">${h(d.stale_approvals.interpretation)}</p>${table(['Record','Review reason','Age in days'],stale.slice(0,30).map(r=>[r.record_id,label(r.reason),r.age_days]))}</div></div><details><summary>Repeated content & source links</summary><p class="ex-status">${h(d.duplicates.interpretation)}</p>${table(['Matching content','Record IDs'],d.duplicates.fingerprint_groups.map(r=>[r.count+' records',r.event_ids]))}${table(['Matching source URL','Record IDs'],d.duplicates.canonical_url_groups.map(r=>[r.label,r.event_ids]))}</details><details><summary>Freshness, missing fields & regional gaps</summary><p class="ex-status">${h(d.recency.interpretation)}</p>${table(['Date type','Within review interval','Older','Missing'],['publication','retrieval','event'].map(k=>[label(k),d.recency[k].counts.within_threshold,d.recency[k].counts.older_than_threshold,d.recency[k].counts.missing]))}${table(['Missing field','Records'],Object.entries(d.missing_data.field_counts).map(([k,n])=>[label(k),n]))}<p class="ex-status">${h(d.region_coverage.interpretation)}</p>${table(['Region','Records'],d.region_coverage.groups.map(r=>[r.label,r.count]))}</details><details><summary>Broken evidence references (${broken.length})</summary>${table(['Record','Finding'],broken.map(r=>[r.record_id||r.scenario_id||r.risk_id,Object.entries(r).filter(([k])=>!['record_id','scenario_id','risk_id'].includes(k)).map(([k,v])=>label(k)+': '+pretty(v)).join(' · ')]))}</details><div class="ex-toolbar"><button class="ex-btn" id="ex-evidence-history">Decision history</button></div>`;
      q('#ex-evidence-history').onclick=historyDialog;
    }catch(e){el.innerHTML=`<summary>Evidence quality & source gaps</summary><p class="ex-error">${h(e.message)}</p>`;}
  }
  async function historyDialog(){
    open('Evidence decision history','<p id="ex-history-status">Loading decisions…</p><div id="ex-history-table"></div>');
    try{const d=await api('/api/review/history?kind=event');if(!q('#ex-history-table'))return;const rows=d.decisions||d.history||[];q('#ex-history-status').textContent=rows.length+' recorded decisions. Previous revisions remain preserved.';q('#ex-history-table').innerHTML=`<div class="ex-table-wrap"><table class="ex-table"><thead><tr><th>Record</th><th>Decision</th><th>Reviewer</th><th>Reason</th><th>When</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${h(r.record_id)}</td><td>${h(r.action)}</td><td>${h(r.actor)}</td><td>${h(r.reason)}</td><td>${h(r.decided_at)}</td></tr>`).join('')}</tbody></table></div>`;}catch(e){if(q('#ex-history-status'))q('#ex-history-status').textContent=e.message;}
  }
  async function loadAccess(){
    try{memory.principal=await api('/api/access');const p=memory.principal;window.atlasPrincipal=p;applyPermissions();if(typeof updateReviewBatchControls==='function')updateReviewBatchControls();
      if(p.mode==='proxy'){
        actorName=p.subject;
        window.atlasMayReview=['reviewer','admin'].includes(p.role);
        window.atlasMayPrepare=['analyst','admin'].includes(p.role);
        q('#ex-research').disabled=!window.atlasMayPrepare;
        q('#ex-export').disabled=p.role!=='admin'&&!p.capabilities.includes('export');
        let identity=q('#ex-identity');if(!identity){identity=document.createElement('span');identity.id='ex-identity';identity.className='ex-pill';q('#ex-preferences').after(identity);}identity.textContent=p.subject+' · '+label(p.role);
      }
    }catch(e){q('#ex-research').disabled=true;q('#ex-export').disabled=true;}
  }
  document.addEventListener('atlas:data',()=>{portfolioTools();loadAccess();loadContext();});
  document.addEventListener('atlas:organization',loadContext);
  document.addEventListener('atlas:view',e=>{if(e.detail==='scenarios'){portfolioTools();applyPermissions();}if(e.detail==='settings')loadOperations();if(e.detail==='evidence')loadDiagnostics();});
  document.addEventListener('atlas:scenario',e=>{
    const s=e.detail;memory.scenario=s;
    const el=document.createElement('div');el.className='ex-scenario-actions';el.innerHTML='<button class="ex-btn ex-primary" id="ex-scenario-export">Export brief</button><button class="ex-btn" id="ex-scenario-exposure">Map to a service</button><button class="ex-btn" id="ex-scenario-action">Assign an action</button><button class="ex-btn" id="ex-scenario-exercise">Plan an exercise</button>';
    q('#scenario-modal-title').after(el);
    const contextCard=document.createElement('section');contextCard.className='brief-card';
    const exposureRows=records('exposures').filter(r=>r.scenario_id===s.id&&r.status!=='dismissed');
    const actionRows=records('actions').filter(r=>(r.scenario_ids||[]).includes(s.id));
    contextCard.innerHTML=`<h4>Relevance to your organization</h4>${!memory.context?'<p>Organization context is unavailable. Refresh the workspace before mapping this hypothesis.</p>':!memory.context.enabled?'<p>Activate the organization workspace to connect this hypothesis to your services and owners.</p>':exposureRows.length?exposureRows.map(r=>`<p><strong>${h(displayRef(r.service_id))}</strong> · ${h(label(r.status))}<br>${h(r.rationale)}</p>`).join(''):'<p>Applicability has not been assessed for your services. Use “Map to a service” to record your rationale.</p>'}${actionRows.length?`<h4>Owned decisions & next steps</h4><ul>${actionRows.map(r=>`<li>${h(r.title)} · ${h(r.owner)} · due ${h(r.due_date)} · ${h(label(r.effective_acceptance_status||r.status))}</li>`).join('')}</ul>`:''}`;
    el.after(contextCard);
    q('#ex-scenario-export').onclick=()=>exportDialog([s.id]);
    for(const [id,kind,prefill] of [['ex-scenario-exposure','exposures',{scenario_id:s.id}],['ex-scenario-action','actions',{scenario_ids:[s.id],title:'Investigate '+s.title}],['ex-scenario-exercise','exercises',{scenario_ids:[s.id],title:'Exercise: '+s.title}]])q('#'+id).onclick=()=>{closeModal();if(!memory.context?.enabled){go('organization');return;}recordForm(kind,null,prefill);};
    applyPermissions();
  });
  if(DATA){portfolioTools();loadAccess();loadContext();}
})();
