(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const APP_NAMES = {all:'All agents',claude:'Claude',codex:'Codex',gemini:'Gemini'};
  const isLivePage = location.protocol === 'http:' && ['127.0.0.1','localhost'].includes(location.hostname);
  const state = {inventory:null,updates:[],suggestions:[],activity:[],settings:null,job:null,connected:false,pending:false,saving:false,lastCheck:null,lastUpdate:null,checkedAt:null,suggestionsAt:null,lastJobKey:null,pollTimer:null,loading:false};
  let token = typeof window.INVENTORY_TOKEN === 'string' ? window.INVENTORY_TOKEN : '';
  Object.assign(state,{availability:null,availabilityByApp:{},availabilityState:'missing',availabilityLimit:60,resourceGeneration:0,lastResourcesAt:0,savedAt:null});
  Object.assign(state,{catalog:null,catalogByApp:{},catalogLimit:60,catalogSelection:new Set(),installRequest:null,installPlan:null,installJob:null,installHistory:[],installPending:false,installPolling:false,installGeneration:0,dockerGeneration:0,dialogTrigger:null,docker:null,dockerJob:null,dockerPending:false,dockerGenerated:false,runtime:null,startupSaving:false});
  const STATUS = {
    'up-to-date':['Up to date','current'], 'updated':['Updated','current'],
    'update-available':['Update available','available'], 'host-managed':['Provider-managed','managed'],
    'provider-managed':['Provider-managed','managed'], 'local-changes':['Local changes','attention'],
    'error':['Needs attention','error'], 'partial':['Partially updated','attention'],
    'checking':['Checking...','unchecked'], 'unchecked':['Not checked','unchecked']
  };
  function node(tag, options={}, children=[]) {
    const element = document.createElement(tag);
    for (const [key,value] of Object.entries(options)) {
      if (value === null || value === undefined) continue;
      if (key === 'text') element.textContent = String(value);
      else if (key === 'class') element.className = value;
      else if (key === 'hidden' || key === 'disabled' || key === 'open') element[key] = !!value;
      else element.setAttribute(key,String(value));
    }
    for (const child of (Array.isArray(children)?children:[children])) {
      if (child === null || child === undefined) continue;
      element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  }
  function normalizeName(value) { return String(value || '').toLowerCase().replace(/\b(official|mcp|server)\b/g,'').replace(/[^a-z0-9]/g,''); }
  function normalizedApp(value) { const s=String(value||'').toLowerCase(); return s.includes('claude')?'claude':s.includes('gemini')?'gemini':s.includes('codex')||s==='work'?'codex':s; }
  function appsOf(item) {
    const apps = Array.isArray(item.apps)?item.apps:Object.keys(item.apps||{}).filter(key=>item.apps[key]);
    return [...new Set(apps.map(normalizedApp))];
  }
  function dateLabel(value, compact=false) {
    if (!value) return 'Not recorded';
    const raw=typeof value==='object'?(value.at||value.timestamp||value.finishedAt):value;
    const date=new Date(typeof raw==='number' && raw<1e12 ? raw*1000 : raw);
    if (Number.isNaN(date.valueOf())) return 'Not recorded';
    if (typeof raw==='string' && /^\d{4}-\d{2}-\d{2}$/.test(raw)) return new Intl.DateTimeFormat('en-SG',{timeZone:'Asia/Singapore',day:'numeric',month:'short',year:'numeric'}).format(date);
    return new Intl.DateTimeFormat('en-SG',{timeZone:'Asia/Singapore',day:'numeric',month:'short',...(compact?{}:{year:'numeric'}),hour:'2-digit',minute:'2-digit',hour12:false}).format(date)+(compact?'':' SGT');
  }
  function safeUrl(value) { try { const url=new URL(value); return url.protocol==='https:' || (url.protocol==='http:' && ['127.0.0.1','localhost'].includes(url.hostname)) ? url.href : null; } catch { return null; } }
  function version(value) { return value===null||value===undefined||value===''?'Not recorded':String(value); }
  function inventoryGroups() { return Array.isArray(state.inventory?.skillGroups)?state.inventory.skillGroups:[]; }
  function namesOf(group) { return (group.names||[]).map(item=>typeof item==='string'?item:item.name).filter(Boolean); }
  function allMcp() { return Array.isArray(state.inventory?.mcp)?state.inventory.mcp:[]; }
  function busy() { return state.pending || state.saving || state.installPending || state.job?.status==='running' || state.installJob?.status==='running'; }
  function writable() { return isLivePage && state.connected && !!token; }
  function updateAvailable(item) { return item?.status==='update-available'&&item.actionable!==false; }
  function findUnit(item) {
    const id = item.sourceId || item.updateSource;
    const explicit=typeof id==='object'?(id.id||id.sourceId):id;
    if (explicit) { const direct=state.updates.find(unit=>unit.id===explicit); if (direct) return direct; }
      if (typeof explicit==='string' && explicit.startsWith('provider:')) return {
        id:explicit,name:item.title||item.name,status:'host-managed',
        installedVersion:item.version||'App managed',latestVersion:'Provider managed',
        detail:'This source is updated by its agent application.'
      };
    const candidates=[item.id,item.name,...(item.names||[])].filter(value=>typeof value==='string').map(normalizeName);
    const target=state.updates.find(unit=>(unit.targetNames||[]).some(name=>candidates.includes(normalizeName(name))));
    if (target) return target;
    const text=String(item.title||item.name||item.id||'').toLowerCase();
    if (text.includes('ecc') && !text.includes('everything-disabled')) return state.updates.find(unit=>/ecc/.test(String(unit.id)+' '+unit.name) && /skill|bundle|repo/.test(unit.kind||'') );
    if (text.includes('ponytail')) return state.updates.find(unit=>/ponytail/i.test(String(unit.id)+' '+unit.name));
    return state.updates.find(unit=>candidates.includes(normalizeName(unit.name)) || candidates.includes(normalizeName(unit.id)));
  }
  function badge(unit, fallback='unchecked') {
    const [label,style] = STATUS[unit?.status||fallback] || [String(unit?.status||'Not checked'),'unchecked'];
    return node('span',{class:'update-state '+style,text:label});
  }
  function updateButton(unit, label='Update') {
    const button=node('button',{class:'button small',type:'button',text:label,disabled:!writable()||busy(),'aria-label':`Update ${unit.name}`});
    button.dataset.updateId=unit.id;
    return button;
  }
  function sourceCell(item) {
    const unit=findUnit(item), cell=node('td',{class:'update-cell'});
    const isManaged=!unit && /host|provider|system|remote/.test(String(item.kind||''));
    cell.append(badge(unit,isManaged?'host-managed':'unchecked'));
    if (unit?.installedVersion) cell.append(node('small',{text:version(unit.installedVersion)}));
    if (updateAvailable(unit)) cell.append(updateButton(unit));
    return cell;
  }
  function normalizeInventory(input) {
    const previous=state.inventory||{};
    const data=input && typeof input==='object'?input:{};
    return {...previous,...data,mcp:Array.isArray(data.mcp)?data.mcp:(previous.mcp||[]),skillGroups:Array.isArray(data.skillGroups)?data.skillGroups:(Array.isArray(data.groups)?data.groups:previous.skillGroups||[])};
  }
  function renderHeader() {
    const app=selectedAgent(),mcp=allMcp().filter(item=>app==='all'||appsOf(item).includes(app));
    const names=new Set(inventoryGroups().filter(group=>group.kind!=='command'&&!/disabled/i.test(group.state||'')&&(app==='all'||appsOf(group).includes(app))).flatMap(namesOf));
    const shared=state.inventory?.stats?.sharedSkills||state.inventory?.stats?.sharedPortableSkillFolders||0;
    const availability=state.availability?.selectedApp===app?state.availability:null;
    const available=state.updates.filter(updateAvailable).length;
    const metrics=[[mcp.length,'MCP servers','#mcp',app==='all'?'Across your agents':APP_NAMES[app]+' configurations'],[app==='all'?shared:names.size,app==='all'?'Shared skills':'Agent skills','#skills',app==='all'?'Portable skill library':'Unique capabilities'],[availability?availability.summary?.missingCount??0:'--','Missing tools','#availability',app==='all'?'Gaps across agents':'Missing from '+APP_NAMES[app]],[state.checkedAt?available:'--','Source updates','#updates','Global publisher sources']];
    const summary=$('#summary-line');summary.replaceChildren();
    for(const [total,label,href,detail] of metrics)summary.append(node('a',{href,...(href==='#availability'?{'data-open-missing':'true'}:{})},[node('span',{class:'metric-label',text:label}),node('strong',{text:total}),node('span',{class:'metric-detail',text:detail})]));
    $('#inventory-time').textContent=(state.connected?'Live inventory':'Saved inventory')+' / '+dateLabel(state.connected?state.inventory?.generatedAt:state.savedAt||state.inventory?.generatedAt);
    $('#sidebar-state').textContent=state.connected?'Local manager connected':'Offline snapshot';
    $('#manager-dot').classList.toggle('connected',state.connected);
    $('#footer-state').textContent=state.connected?'Connected to your local manager.':'Offline snapshot. Reconnect to manage installations.';
    $('#skills-description').textContent=app==='all'?`${shared} shared portable skills, plus agent-specific sources. Matching capabilities can appear in more than one source.`:`Skill sources available to ${APP_NAMES[app]}. Shared publisher updates apply globally.`;
    $('#breadcrumb-target').textContent=APP_NAMES[app];
    $('#agent-context').textContent=app==='all'?'Your complete toolkit, with gaps across compatible agents.':`Showing ${APP_NAMES[app]} installations. Choose install targets separately.`;
    for(const button of $$('[data-agent]')){const active=button.dataset.agent===app;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}
    for(const agent of availability?.applications||[]){const el=$('#agent-'+agent.id+'-status');if(el)el.textContent=agent.cliInstalled?'CLI installed':agent.configured?'Configuration found':'Not detected';}
    const missing=availability?.summary?.missingCount||0;
    $('#missing-nav-count').textContent=missing;$('#missing-nav-count').hidden=!missing;
    $('#updates-nav-count').textContent=available;$('#updates-nav-count').hidden=!available;
  }
  function renderMcp() {
    const tbody=$('#mcp-table tbody'); tbody.replaceChildren();
    for (const item of allMcp()) {
      const apps=appsOf(item),status=String(item.status||'').toLowerCase()==='connected'?'connected':/auth|sign.in/i.test(item.status||'')?'auth':'unknown';
      const row=node('tr'); row.dataset.apps=apps.join(' ');row.dataset.state=status;row.dataset.search=[item.name,item.id,item.description].join(' ').toLowerCase();
      row.append(node('td',{},[node('strong',{text:item.name}),node('small',{text:item.description||''})]),node('td',{},node('span',{class:'status '+status,text:status==='connected'?'Connected':status==='auth'?'Sign-in required':'Not checked'})));
      for (const app of ['claude','codex','gemini']) {const cell=node('td',{text:apps.includes(app)?'Installed':'Not configured'});cell.dataset.agentColumn=app;row.append(cell);}
      row.append(sourceCell(item));tbody.append(row);
    }
  }
  function renderGroups() {
    const openIds=new Set($$('details.skill-group[open]').map(group=>group.dataset.groupId));
    const skillRoot=$('#skill-groups'),commandRoot=$('#command-groups');skillRoot.replaceChildren();commandRoot.replaceChildren();
    for (const group of inventoryGroups()) {
      const names=namesOf(group),apps=appsOf(group),kind=group.kind==='command'?'command':'skill',unit=findUnit(group);
      const detail=node('details',{class:'skill-group',open:openIds.has(group.id)||(openIds.size===0 && group.id==='shared-global')});
      detail.dataset.groupId=group.id;detail.dataset.kind=kind;detail.dataset.apps=apps.join(' ');detail.dataset.groupSearch=[group.title,group.id].join(' ').toLowerCase();
      const title=node('span',{class:'group-title',text:group.title||group.name||'Skills'});
      if (group.version) title.append(node('span',{class:'version',text:' / '+group.version}));
      const summary=node('summary',{},[title,node('span',{class:'group-count',text:`${names.length} ${kind==='command'?'commands':'skills'}`}),node('span',{class:'state-label'+(/disabled/i.test(group.state||'')?' disabled':''),text:String(group.state||'Installed').replace(/^./,c=>c.toUpperCase())})]);
      if (unit) summary.append(badge(unit));
      detail.append(summary);
      const body=node('div',{class:'group-body'});body.append(node('p',{class:'group-apps',text:apps.map(app=>APP_NAMES[app]||app).join(', ')}));
      if (group.detail || group.notes?.length) body.append(node('p',{class:'group-note',text:group.detail || (Array.isArray(group.notes)?group.notes.join(' '):String(group.notes||''))}));
      if (unit) {
        const line=node('div',{class:'group-update'},[badge(unit),node('p',{text:unit.detail||`Publisher: ${unit.name}.`})]);
        if (updateAvailable(unit)) line.append(updateButton(unit,'Update this source'));
        body.append(line);
      }
      const list=node('ul',{class:'skill-list'});
      for (const name of names) { const entry=node('li',{class:'skill-entry'},node('code',{text:name}));entry.dataset.search=[name,group.title].join(' ').toLowerCase();list.append(entry); }
      body.append(list);detail.append(body);(kind==='command'?commandRoot:skillRoot).append(detail);
    }
  }
  function renderUpdates() {
    const tbody=$('#updates-table tbody');tbody.replaceChildren();
    const items=[...state.updates].sort((a,b)=>Number(updateAvailable(b))-Number(updateAvailable(a)));
    for (const item of items) {
      const row=node('tr');row.dataset.search=[item.name,item.detail,...(item.targetNames||[])].join(' ').toLowerCase();
      const cell=node('td'),url=safeUrl(item.sourceUrl);
      cell.append(node(url?'a':'span',{class:'source-title',...(url?{href:url,target:'_blank',rel:'noopener noreferrer'}:{}),text:item.name||item.id}));
      const detail=[item.itemCount?`${item.itemCount} ${/mcp/i.test(item.kind||'')?'server configurations':'items'}`:'',item.detail||''].filter(Boolean).join(' - ');
      if (detail) cell.append(node('span',{class:'source-detail',text:detail}));
      row.append(cell,node('td',{},node('span',{class:'version-value',text:version(item.installedVersion)})),node('td',{},node('span',{class:'version-value',text:version(item.latestVersion)})),node('td',{},badge(item)));
      const action=node('td');
      if (updateAvailable(item)) action.append(updateButton(item));
      else action.append(node('span',{class:'muted',text:item.status==='local-changes'?'Review local changes':item.status==='host-managed'?'Updated by provider':'-'}));
      row.append(action);tbody.append(row);
    }
    $('#source-details').hidden=items.length===0;
    $('#source-count').textContent=items.length?`(${items.length} sources)`:'';
    $('#updates-empty').hidden=items.length>0;
    const available=items.filter(updateAvailable).length,attention=items.filter(item=>['error','local-changes','partial'].includes(item.status)).length;
    $('#update-summary').textContent=items.length?[`${available} ${available===1?'source has':'sources have'} updates available`,attention?`${attention} ${attention===1?'source needs':'sources need'} attention`:null,`${items.filter(item=>item.status==='host-managed').length} managed by their providers`].filter(Boolean).join(' - '):'Publisher versions have not been checked in this view.';
    $('#last-checked').textContent=state.checkedAt||state.lastCheck?'Last checked '+dateLabel(state.checkedAt||state.lastCheck):'No publisher check recorded';
    $('#update-all').textContent=available?`Update ${available} available ${available===1?'source':'sources'}`:'Update available sources';
  }
  function installedNames() {
    return new Set([...allMcp().flatMap(item=>[item.id,item.name]),...inventoryGroups().flatMap(namesOf)].map(normalizeName));
  }
  function renderSuggestions() {
    const installed=installedNames(),list=$('#suggestion-list');list.replaceChildren();
    const items=state.suggestions.filter(item=>!installed.has(normalizeName(item.id))&&!installed.has(normalizeName(item.name)));
    for (const item of items) {
      const row=node('li',{class:'suggestion-row'});row.dataset.search=[item.name,item.description,item.reason,item.kind].join(' ').toLowerCase();
      const text=node('div');text.append(node('h3',{},[item.name,node('span',{text:/skill/i.test(item.kind||'')?'Skill':'MCP server'})]),node('p',{text:item.description||''}));
      if (item.reason) text.append(node('p',{class:'reason',text:item.reason}));
      text.append(node('p',{class:'account-note',text:item.requiresAccount?'An account or authorization is required.':'No separate account is required.'}));
      row.append(text);
      const url=safeUrl(item.url);if(url)row.append(node('a',{href:url,target:'_blank',rel:'noopener noreferrer',text:'View publisher','aria-label':`View ${item.name} publisher`}));
      list.append(row);
    }
    $('#suggestions-time').textContent=state.suggestionsAt?'Reviewed '+dateLabel(state.suggestionsAt,true):'';
    $('#suggestions-empty').textContent=state.connected?'No additional tools are suggested for your current inventory.':'Open the live dashboard to see suggestions based on your installed tools.';
    $('#suggestions-empty').hidden=items.length>0;
  }
  function renderActivity() {
    const list=$('#activity-list');list.replaceChildren();
    for(const item of state.activity.slice(0,15)) {
      const level=['error','warning'].includes(item.level)?item.level:'';
      const row=node('li',{class:'activity-entry '+level},[node('time',{text:dateLabel(item.at||item.timestamp,true)}),node('p',{text:item.message||item.detail||''})]);list.append(row);
    }
    $('#activity-empty').hidden=state.activity.length>0;
  }
  function renderControls() {
    const running=state.job?.status==='running',available=state.updates.some(updateAvailable);
    $('#check-updates').disabled=!writable()||busy();$('#update-all').disabled=!writable()||busy()||!available;
    $('#check-updates').textContent=state.pending?'Starting...':running&&state.job.type?.includes('check')?'Checking...':'Check for updates';
    $('#auto-update').disabled=!writable()||state.saving;$('#update-interval').disabled=!writable()||state.saving;
    if(state.settings&&!state.saving){$('#auto-update').checked=!!state.settings.autoUpdate;$('#update-interval').value=String(state.settings.intervalHours||24);}
    $('#schedule-note').textContent=state.saving?'Saving your update schedule...':state.connected&&state.settings?(state.settings.autoUpdate?`Managed installations are checked every ${state.settings.intervalHours} hours. Updates retain their registered agent targets.`:'Automatic updates are paused. You can still check and update sources here.'):'Open the live dashboard to manage the update schedule.';
    for(const button of $$('[data-update-id]'))button.disabled=!writable()||busy();
    const panel=$('#job-panel'),job=state.job;panel.hidden=!job;
    if(job){
      panel.className='job-panel '+(job.status==='done'?'done':job.status==='error'?'error':'');
      $('#job-title').textContent=job.status==='error'?'Update task needs attention':job.status==='done'?'Task completed':/check/i.test(job.type||'')?'Checking publishers':'Updating installed sources';
      const progress=Number(job.progress),hasProgress=job.progress!==null&&job.progress!==undefined&&Number.isFinite(progress);
      if(hasProgress)$('#job-progress').value=Math.max(0,Math.min(100,progress));else $('#job-progress').removeAttribute('value');
      $('#job-progress').hidden=!running;$('#job-percent').textContent=running&&hasProgress?Math.round(progress)+'%':'';
      $('#job-message').textContent=job.error||job.message||(running?'The local manager is working.':'Finished.');
    }
    $('#refresh-inventory').disabled=state.loading||!state.connected;
    const notice=$('#helper-notice');notice.hidden=state.connected&&!!token;
    if(!notice.hidden){
      notice.classList.toggle('warning',isLivePage);
      $('#helper-notice-title').textContent=!isLivePage?'Saved inventory':state.connected?'Open a secure manager session':'The local manager is unavailable';
      $('#helper-notice-text').textContent=!isLivePage?'This saved copy remains searchable. Open the live dashboard to check publishers and manage updates.':state.connected?'Reopen the local dashboard to enable update controls.':'Your last inventory is still available. Start the Desktop manager, then retry the connection.';
      $('#open-dashboard').hidden=isLivePage&&!state.connected;$('#retry-helper').hidden=!isLivePage||state.connected;
    }
    $('#updates').setAttribute('aria-busy',String(running));
    renderManagementControls();
  }
  function filterInventory() {
    const query=$('#search').value.trim().toLowerCase(),app=$('#app-filter').value,status=$('#mcp-status').value;
    const matchesApp=element=>app==='all'||(element.dataset.apps||'').split(' ').includes(app);
    let mcpCount=0,skillCount=0,commandCount=0;
    for(const row of $$('#mcp-table tbody tr')){row.hidden=!matchesApp(row)||(status!=='all'&&row.dataset.state!==status)||(query&&!row.dataset.search.includes(query));if(!row.hidden)mcpCount++;}
    for(const group of $$('details.skill-group')){
      const entries=[...group.querySelectorAll('.skill-entry')];let shown=0;
      for(const entry of entries){entry.hidden=!!query&&!entry.dataset.search.includes(query);if(!entry.hidden)shown++;}
      group.hidden=!matchesApp(group)||shown===0;
      if(!group.hidden){if(group.dataset.kind==='command')commandCount+=shown;else skillCount+=shown;if(query)group.open=true;}
      group.querySelector('.group-count').textContent=`${query?`${shown} of `:''}${entries.length} ${group.dataset.kind==='command'?'commands':'skills'}`;
    }
    let suggestions=0;
    for(const row of $$('#suggestion-list .suggestion-row')){row.hidden=!!query&&!row.dataset.search.includes(query);if(!row.hidden)suggestions++;}
    for(const row of $$('#updates-table tbody tr'))row.hidden=!!query&&!row.dataset.search.includes(query);
    $('#mcp-count').textContent=`${mcpCount} of ${allMcp().length} servers`;
    $('#mcp-empty').hidden=mcpCount>0;$('#skills-empty').hidden=skillCount>0;$('#commands-empty').hidden=commandCount>0;
    if(state.suggestions.length){$('#suggestions-empty').hidden=suggestions>0;$('#suggestions-empty').textContent=query?'No suggestions match your search.':'No additional tools are suggested for your current inventory.';}
    for(const cell of $$('[data-agent-column]'))cell.hidden=app!=='all'&&cell.dataset.agentColumn!==app;
    renderAvailability();
    renderCatalog();
    $('#results').textContent=`${mcpCount} MCP ${mcpCount===1?'server':'servers'}, ${skillCount} skill entries, and ${commandCount} commands match${app!=='all'?' '+APP_NAMES[app]:''}.`;
  }
  function render() {renderHeader();renderMcp();renderGroups();renderUpdates();renderSuggestions();renderActivity();renderControls();decorateTables();filterInventory();renderInstallHistory();renderRuntime();renderDocker();}
  function message(text,type='') { const element=$('#action-message');element.textContent=text;element.className='action-message '+type;element.hidden=!text; }
  async function api(path, options={},allowRetry=true) {
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),8000);
    try{
      const response=await fetch(path,{...options,credentials:'same-origin',cache:'no-store',signal:controller.signal,headers:{...(options.method?{'Content-Type':'application/json','X-Inventory-Token':token}:{}),...(options.headers||{})}});
      const body=await response.json().catch(()=>null);
      if(!response.ok){
        if(response.status===403&&options.method){
          token='';
          if(allowRetry&&isLivePage){clearTimeout(timeout);await refreshSession();return api(path,options,false);}
        }
        throw new Error(body?.error||body?.message||`The manager returned HTTP ${response.status}. Please try again.`);
      }
      return body||{};
    }catch(error){if(error.name==='AbortError')throw new Error('The local manager took too long to respond. Please try again.');throw error;}
    finally{clearTimeout(timeout);}
  }
  async function refreshResources(force=false) {
    if(state.loading&&!force)return;state.loading=true;
    const generation=++state.resourceGeneration,app=selectedAgent();renderControls();
    const routes=['/api/inventory','/api/updates','/api/suggestions','/api/activity','/api/availability?app='+app,'/api/catalog?app='+app,'/api/install/history'];
    const results=await Promise.allSettled(routes.map(route=>api(route)));
    if(generation!==state.resourceGeneration)return;
    const [inventory,updates,suggestions,activity,availability,catalog,history]=results;
    if(inventory.status==='fulfilled')state.inventory=normalizeInventory(inventory.value);
    if(updates.status==='fulfilled'){state.updates=updates.value.items||[];state.checkedAt=updates.value.checkedAt;$('#updates-error').hidden=true;}
    else{$('#updates-error').textContent='Publisher information could not be loaded. Your installed inventory is still available.';$('#updates-error').hidden=false;}
    if(suggestions.status==='fulfilled'){state.suggestions=suggestions.value.items||[];state.suggestionsAt=suggestions.value.checkedAt;$('#suggestions-error').hidden=true;}
    else{$('#suggestions-error').textContent='Suggestions could not be loaded. They will be retried with the next manager connection.';$('#suggestions-error').hidden=false;}
    if(activity.status==='fulfilled')state.activity=activity.value.items||[];
    if(availability.status==='fulfilled'&&availability.value.selectedApp===app){state.availability=availability.value;state.availabilityByApp[app]=availability.value;$('#availability-error').hidden=true;}
    else{$('#availability-error').textContent='Installation status could not be refreshed. Check the helper and try Refresh inventory.';$('#availability-error').hidden=false;}
    if(catalog.status==='fulfilled'&&Array.isArray(catalog.value.items)){state.catalog=catalog.value;state.catalogByApp[app]=catalog.value;$('#catalog-error').hidden=true;}
    else{$('#catalog-error').textContent=state.catalog?'The catalog could not be refreshed. Saved data is still available.':'The default catalog could not be loaded. Check the helper and refresh inventory.';$('#catalog-error').hidden=false;}
    if(history.status==='fulfilled')state.installHistory=Array.isArray(history.value.items)?history.value.items:[];
    state.loading=false;state.lastResourcesAt=Date.now();saveOffline();render();
    return inventory.status==='fulfilled'&&availability.status==='fulfilled';
  }
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if(isLivePage&&!document.hidden)state.pollTimer=setTimeout(pollStatus,state.job?.status==='running'||state.installJob?.status==='running'||state.dockerJob?.status==='running'?1200:state.connected?15000:8000);
  }
  async function pollStatus() {
    if(!isLivePage)return;
    try{
      const status=await api('/api/status'),wasConnected=state.connected;
      const previousRunning=state.job?.status==='running';
      if(!wasConnected||!token)await refreshSession();
      state.connected=true;state.settings=status.settings||state.settings;state.job=status.job||null;state.lastCheck=status.lastCheck;state.lastUpdate=status.lastUpdate;
      if(!wasConnected)refreshRuntime();
      await pollManagementJobs();
      const key=state.job?`${state.job.id}:${state.job.status}`:'none';
      const completed=previousRunning&&state.job?.status!=='running';
      const changed=key!==state.lastJobKey;state.lastJobKey=key;
      renderControls();renderHeader();
      if(!wasConnected||completed||(changed&&state.job?.status!=='running')||(state.job?.status!=='running'&&Date.now()-state.lastResourcesAt>30000))await refreshResources();
      else if(state.job?.status==='running'){
        const activity=await api('/api/activity').catch(()=>null);if(activity){state.activity=activity.items||[];renderActivity();}
      }
    }catch{state.connected=false;token='';renderControls();renderHeader();renderAvailability();}
    finally{schedulePoll();}
  }
  async function startJob(path,body) {
    if(!writable()||busy())return;
    state.pending=true;message('');renderControls();
    try{
      const result=await api(path,{method:'POST',body:JSON.stringify(body)});
      state.job=result.job||{id:'starting-'+Date.now(),status:'running',type:path.includes('check')?'check':'update',progress:null,message:'Your request was accepted. Waiting for the local manager...'};
      state.pending=false;$('#source-details').open=true;renderControls();await pollStatus();
    }catch(error){state.pending=false;message(error.message,'error');renderControls();await pollStatus();}
  }
  async function saveSettings() {
    if(!writable()||state.saving)return;
    const interval=Number($('#update-interval').value);if(![6,12,24,48,168].includes(interval))return;
    const desired={autoUpdate:$('#auto-update').checked,intervalHours:interval};state.saving=true;renderControls();
    try{const result=await api('/api/settings',{method:'POST',body:JSON.stringify(desired)});state.settings=result.settings||{...state.settings,...desired};message('Update schedule saved.','success');}
    catch(error){message(error.message,'error');}
    finally{state.saving=false;renderControls();await pollStatus();}
  }
  function selectedAgent() { return $('#app-filter').value || 'all'; }
  function icon(name) {
    const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('class','icon');svg.setAttribute('aria-hidden','true');
    const use=document.createElementNS('http://www.w3.org/2000/svg','use');use.setAttribute('href','#i-'+name);svg.append(use);return svg;
  }
  function readOffline() { try { const value=JSON.parse(localStorage.getItem('mcp-skills:offline:v1')||'null'); return value&&typeof value==='object'?value:{}; } catch { return {}; } }
  function saveOffline() {
    if(!state.connected)return;
    const inv=state.inventory||{};
    const inventory={mcp:inv.mcp||[],skillGroups:inv.skillGroups||[],stats:inv.stats||{},applications:inv.applications||[],generatedAt:inv.generatedAt,plugins:inv.plugins||[]};
    state.savedAt=new Date().toISOString();
    try { localStorage.setItem('mcp-skills:offline:v1',JSON.stringify({inventory,availabilityByApp:state.availabilityByApp,catalogByApp:safeCatalogCache(),suggestions:state.suggestions,savedAt:state.savedAt})); } catch {}
  }
  function decorateTables() {
    for(const table of $$('table')){
      const heads=[...table.querySelectorAll('thead th')];
      for(const row of table.querySelectorAll('tbody tr'))[...row.children].forEach((cell,i)=>cell.dataset.label=heads[i]?.textContent||'');
    }
  }
  function renderAvailability() {
    const app=selectedAgent(),data=state.availability?.selectedApp===app?state.availability:null,list=$('#availability-list');list.replaceChildren();
    const summary=data?.summary||{};
    for(const [id,field] of [['missing-tab-count','missingCount'],['not-installed-tab-count','notInstalledCount'],['installed-tab-count','installedCount']])$('#'+id).textContent=data?summary[field]??0:'--';
    for(const button of $$('[data-availability-state]')){const active=button.dataset.availabilityState===state.availabilityState;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}
    const query=$('#search').value.trim().toLowerCase(),kind=$('#availability-kind').value;
    const items=(data?.items||[]).filter(item=>(state.availabilityState==='all'||item.state===state.availabilityState)&&(kind==='all'||item.kind===kind)&&(!query||[item.name,item.reason,item.kind,...(item.missingApps||[])].join(' ').toLowerCase().includes(query)));
    for(const item of items.slice(0,state.availabilityLimit)){
      const type=item.kind==='mcp'?'MCP server':'Agent skill',name=node('div',{class:'availability-name'},[node('strong',{text:item.name}),node('span',{class:'kind-label',text:type})]);
      const info=node('div',{class:'availability-info'},[name,node('p',{text:item.reason||'Recorded from your global installations.'})]);
      const label=item.removed?'Removed':item.runtimeMissing?'Runtime missing':item.state==='missing'?'Missing':item.state==='not-installed'?'Not installed':'Installed';
      const apps=item.state==='missing'?item.missingApps:item.installedApps;
      const status=node('div',{class:'availability-status'},[node('span',{class:'install-state '+item.state,text:label}),node('small',{text:(apps||[]).map(a=>APP_NAMES[a]||a).join(', ')||'Available to add'})]);
      const row=node('div',{class:'availability-row'},[node('span',{class:'tool-kind-icon'},icon(item.kind==='mcp'?'server':'skill')),info,status]);
      const url=safeUrl(item.sourceUrl||item.url||findUnit(item)?.sourceUrl);if(url)row.append(node('a',{href:url,target:'_blank',rel:'noopener noreferrer','aria-label':'Review '+item.name+' publisher'},['Review source',icon('arrow')]));
      list.append(row);
    }
    const empty=$('#availability-empty');empty.hidden=items.length>0;
    empty.textContent=!data?(state.connected?'Scanning installation status...':`Reconnect to scan ${APP_NAMES[app]}. Your cached inventory remains available.`):query?'No tools match this search. Try a shorter name.':state.availabilityState==='missing'?(app==='all'?'No missing installations detected across compatible agents. Explore Not installed for new additions.':`No missing tools detected for ${APP_NAMES[app]}. Explore Not installed for new additions.`):'No tools match this installation state and type.';
    $('#availability-time').textContent=data?(state.connected?'Scanned ':'Saved ')+dateLabel(data.generatedAt,true):'';
    $('#availability-description').textContent=app==='all'?'Gaps between compatible agents, removed installations, and tools you have not added.':`What is installed in ${APP_NAMES[app]}, what is missing, and what you can add next.`;
    $('#availability-more').hidden=items.length<=state.availabilityLimit;
    $('#availability-more').textContent=`Show more tools (${items.length-state.availabilityLimit} remaining)`;
  }
  async function refreshSession() { const session=await api('/api/session');token=typeof session.token==='string'?session.token:''; }
  function selectAgent(app) {
    if(!['all','claude','codex','gemini'].includes(app))app='all';
    $('#app-filter').value=app;
    state.catalog=state.catalogByApp[app]||null;state.catalogLimit=60;
    try{localStorage.setItem('mcp-skills:agent',app);}catch{}
    state.availability=state.availabilityByApp[app]||null;state.availabilityLimit=60;renderHeader();filterInventory();
    if(state.connected)refreshResources(true);
  }
  function setNavigation(open) {
    $('#sidebar').classList.toggle('open',open);$('#sidebar-scrim').hidden=!open;
    $('#menu-toggle').setAttribute('aria-expanded',String(open));
    if(open)$('#sidebar nav a').focus();else $('#menu-toggle').focus({preventScroll:true});
  }
  function catalogItems() { return Array.isArray(state.catalog?.items)?state.catalog.items:[]; }
  function agentList(value) { return Array.isArray(value)?value.map(normalizedApp).filter(app=>['claude','codex','gemini'].includes(app)):[]; }
  function catalogAgentState(item,app) {
    const raw=item.states?.[app],value=typeof raw==='string'?{state:raw}:raw||{};
    const known=raw!==undefined||Array.isArray(item.installedAgents)||Array.isArray(item.managedAgents)||Array.isArray(item.sharedAgents);
    const managed=value.managed===true||agentList(item.managedAgents).includes(app);
    const shared=agentList(item.sharedAgents).includes(app);
    const installed=managed||shared||agentList(item.installedAgents).includes(app)||['installed','existing','managed','shared','configured','needs-auth','needs-authentication'].includes(value.state);
    return {...value,known,managed,shared,installed,state:managed?'managed':shared?'shared':installed?'existing':value.state||(known?'not-installed':'unknown')};
  }
  function catalogScope(item) { const supported=agentList(item.supportedAgents);return selectedAgent()==='all'?(supported.length?supported:['claude','codex','gemini']):[selectedAgent()]; }
  function catalogManaged(item) { return ['claude','codex','gemini'].filter(app=>catalogAgentState(item,app).managed); }
  function filteredCatalog() {
    const query=$('#search').value.trim().toLowerCase(),kind=$('#catalog-kind').value,filter=$('#catalog-filter').value;
    return catalogItems().filter(item=>{
      if(kind!=='all'&&item.kind!==kind)return false;
      const supported=agentList(item.supportedAgents);
      if(selectedAgent()!=='all'&&supported.length&&!supported.includes(selectedAgent()))return false;
      const scope=catalogScope(item),states=scope.map(app=>catalogAgentState(item,app));
      if(filter==='missing'&&!states.some(x=>x.known&&!x.installed))return false;
      if(filter==='installed'&&!states.some(x=>x.installed))return false;
      if(filter==='managed'&&!states.some(x=>x.managed))return false;
      if(filter==='installable'&&item.installable===false)return false;
      return !query||[item.name,item.id,item.description,item.kind,item.reason,item.sourceUrl].join(' ').toLowerCase().includes(query);
    });
  }
  function catalogAction(item,action,label,style='') {
    const button=node('button',{type:'button',class:'button small '+style,text:label,disabled:!writable()||busy()});
    button.dataset.catalogAction=action;button.dataset.itemId=item.id;return button;
  }
  function renderCatalog() {
    const list=$('#catalog-list');if(!list)return;list.replaceChildren();
    const items=filteredCatalog(),shown=items.slice(0,state.catalogLimit),all=catalogItems();
    for(const item of shown){
      const row=node('div',{class:'catalog-row'});
      const check=node('input',{type:'checkbox','aria-label':'Select '+item.name,disabled:item.installable===false||!writable()||busy()});
      check.checked=state.catalogSelection.has(item.id);check.dataset.catalogSelect=item.id;
      const content=node('div'),name=node('div',{class:'catalog-name'},[node('strong',{text:item.name||item.id}),node('span',{class:'kind-label',text:item.kind==='mcp'?'MCP server':'Agent skill'})]);
      if(item.version)name.append(node('span',{class:'version-value',text:item.version}));
      content.append(name,node('p',{class:'catalog-description',text:item.description||''}));
      const presence=node('div',{class:'catalog-presence'});
      for(const app of catalogScope(item)){
        const s=catalogAgentState(item,app),label=s.managed?'Managed':s.shared?'Shared':s.installed?'Existing':/unsupported|unavailable/.test(s.state)?'Unsupported':!s.known?'Not scanned':'Not installed';
        presence.append(node('span',{class:'agent-state '+(s.managed?'managed':s.shared?'shared':''),text:APP_NAMES[app]+': '+label}));
      }
      content.append(presence);
      if(item.reason||item.installable===false)content.append(node('p',{class:'catalog-reason',text:item.reason||'This item is managed by its publisher or requires a manual source.'}));
      const actions=node('div',{class:'catalog-actions'});
      if(item.installable!==false)actions.append(catalogAction(item,'install','Choose agents'));
      if(catalogManaged(item).length){actions.append(catalogAction(item,'update','Update'));actions.append(catalogAction(item,'uninstall','Uninstall','danger-action'));}
      if(state.catalog?.capabilities?.sharedMigration&&agentList(item.sharedAgents).length)actions.append(catalogAction(item,'migrate','Review migration'));
      const url=safeUrl(item.sourceUrl||item.url);if(url)actions.append(node('a',{class:'quiet-button',href:url,target:'_blank',rel:'noopener noreferrer','aria-label':'View '+item.name+' publisher'},['Publisher',icon('arrow')]));
      row.append(check,content,actions);list.append(row);
    }
    const counts={mcp:all.filter(x=>x.kind==='mcp').length,skill:all.filter(x=>x.kind==='skill').length};
    $('#catalog-count').textContent=all.length?counts.mcp+' MCPs / '+counts.skill+' skills':'Catalog unavailable';
    $('#catalog-empty').hidden=items.length>0;
    $('#catalog-empty').textContent=!all.length?(state.connected?'The default catalog is not available yet. Refresh after the manager is ready.':'Reconnect to load the default catalog. Saved installations remain available.'):'No catalog items match these filters.';
    $('#catalog-more').hidden=items.length<=state.catalogLimit;
    $('#catalog-more').textContent='Show more tools ('+Math.max(0,items.length-state.catalogLimit)+' remaining)';
    $('#catalog-selection-count').textContent=state.catalogSelection.size?state.catalogSelection.size+' tools selected':'No tools selected';
    $('#catalog-install-selected').disabled=!writable()||busy()||!state.catalogSelection.size;
    $('#catalog-select-visible').disabled=!writable()||busy()||!shown.some(x=>x.installable!==false);
    $('#catalog-clear').disabled=!state.catalogSelection.size;
  }
  function safeCatalogCache() {
    const output={};
    for(const [app,data] of Object.entries(state.catalogByApp)){
      if(!['all','claude','codex','gemini'].includes(app)||!Array.isArray(data?.items))continue;
      output[app]={version:data.version,generatedAt:data.generatedAt,items:data.items.map(item=>{
        const clean={};
        for(const key of ['id','name','kind','description','sourceUrl','version','installable','reason','supportedAgents','supportedPlatforms','requiresAuth','installedAgents','managedAgents','sharedAgents'])if(item[key]!==undefined)clean[key]=item[key];
        clean.states={};for(const target of ['claude','codex','gemini']){const raw=item.states?.[target];if(typeof raw==='string')clean.states[target]=raw;else if(raw&&typeof raw==='object')clean.states[target]={state:raw.state,managed:raw.managed,reason:raw.reason};}
        return clean;
      })};
    }
    return output;
  }
  function installItems() { return (state.installRequest?.ids||[]).map(id=>catalogItems().find(item=>item.id===id)).filter(Boolean); }
  function installTargets() { return $$('#install-targets input:checked').filter(el=>!el.disabled).map(el=>el.value); }
  function invalidateInstallPlan() {
    state.installPlan=null;$('#install-review').hidden=true;$('#install-execute').disabled=true;
    $('#install-error').hidden=true;$('#install-dialog-state').textContent='Nothing changes until you review and apply the plan.';
  }
  function closeInstallDialog() {
    if(state.installPending)return;
    const dialog=$('#install-dialog');dialog.close();
    for(const field of $$('#install-options input,#install-options textarea'))field.value='';
    state.installRequest=null;state.installPlan=null;$('#install-options').replaceChildren();
    state.dialogTrigger?.focus?.({preventScroll:true});
  }
  function openInstallDialog(ids,operation='install') {
    if(!writable()||busy())return;
    const items=ids.map(id=>catalogItems().find(item=>item.id===id)).filter(Boolean);
    if(!items.length)return;
    state.dialogTrigger=document.activeElement;state.installRequest={ids:items.map(x=>x.id),operation};state.installPlan=null;
    const verb={install:'Install',update:'Update',uninstall:'Uninstall',migrate:'Migrate'}[operation]||'Install';
    $('#install-dialog-title').textContent=verb+' '+(items.length===1?items[0].name:items.length+' tools');
    $('#install-dialog-description').textContent=operation==='uninstall'?'Remove only registrations and files owned by this manager for the agents you choose.':'Choose the agents that should receive these tools globally for your user account.';
    $('#install-execute').textContent=operation==='uninstall'?'Uninstall selected':operation==='migrate'?'Apply migration':verb+' selected';
    $('#install-selected-tools').replaceChildren(...items.slice(0,20).map(item=>node('span',{class:'selected-tool',text:item.name})));
    if(items.length>20)$('#install-selected-tools').append(node('span',{class:'selected-tool',text:'+'+(items.length-20)+' more'}));
    let saved=[];try{const value=JSON.parse(localStorage.getItem('mcp-skills:install-targets')||'[]');saved=agentList(value);}catch{}
    const defaults=selectedAgent()==='all'?saved:[selectedAgent()];
    for(const input of $$('#install-targets input')){
      const supported=items.some(item=>{const list=agentList(item.supportedAgents);return !list.length||list.includes(input.value);});
      const owned=items.some(item=>catalogAgentState(item,input.value).managed);
      input.disabled=!supported||(['update','uninstall'].includes(operation)&&!owned);
      input.checked=!input.disabled&&defaults.includes(input.value);
    }
    $('#install-targets legend').textContent=operation==='uninstall'?'Remove from these agents':'Apply to these agents';
    const shared=[...new Set(items.flatMap(item=>agentList(item.sharedAgents)))];
    $('#shared-install-notice').hidden=!shared.length;
    $('#shared-install-description').textContent=operation==='migrate'?'The plan must account for every agent affected by the existing shared folder. Review all changes before continuing.':'These tools are already shared with '+shared.map(x=>APP_NAMES[x]).join(', ')+'. Existing shared files are preserved. A private installation does not remove their current shared visibility.';
    const options=$('#install-options');options.replaceChildren();
    if(operation!=='uninstall'&&operation!=='migrate'){
      if(items.some(x=>x.id==='mcp:filesystem'))options.append(node('label',{class:'install-option'},[node('span',{text:'Filesystem access folders'}),node('textarea',{id:'install-filesystem-roots',placeholder:'One absolute folder path per line',spellcheck:'false'}),node('small',{text:'Choose the folders the Filesystem MCP may access. Leave blank during an update to preserve existing roots.'})]));
      if(items.some(x=>x.id==='mcp:postgres'))options.append(node('label',{class:'install-option'},[node('span',{text:'PostgreSQL connection string'}),node('input',{id:'install-postgres-connection',type:'password',autocomplete:'new-password',placeholder:'postgresql://user:password@host:5432/database',spellcheck:'false'}),node('small',{text:'Used only for the selected agent configurations. It is not stored in browser history or offline snapshots. Leave blank during an update to preserve the existing value.'})]));
    }
    invalidateInstallPlan();$('#install-dialog').showModal();renderManagementControls();
    setTimeout(()=>$('#install-targets input:not(:disabled)')?.focus(),0);
  }
  function collectInstallOptions() {
    const options={},roots=$('#install-filesystem-roots'),pg=$('#install-postgres-connection');
    if(roots?.value.trim())options['mcp:filesystem']={roots:roots.value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean)};
    if(pg?.value.trim())options['mcp:postgres']={connectionString:pg.value.trim()};
    return options;
  }
  function privateSafeText(value) {
    let text=String(value||'');
    const secret=$('#install-postgres-connection')?.value;
    if(secret&&secret.length>=4)text=text.split(secret).join('[private connection string]');
    return text.replace(/([a-z]+:\/\/)[^\s/@]+:[^\s/@]+@/gi,'$1[private credentials]@');
  }
  function planEntry(value) {
    if(typeof value==='string')return privateSafeText(value);
    if(!value||typeof value!=='object')return String(value||'');
    return privateSafeText([value.action||value.operation,value.target||value.agent,value.name||value.itemName||value.itemId||value.id,value.path||value.location,value.description||value.message||value.reason].filter(Boolean).join(' / '));
  }
  function listValues(value) { return Array.isArray(value)?value:value&&typeof value==='object'?Object.entries(value).map(([key,item])=>typeof item==='string'?key+': '+item:{name:key,...item}):value?[value]:[]; }
  function renderPlanGroup(root,title,values,classes='') {
    root.replaceChildren();const items=listValues(values);if(!items.length)return;
    const section=node('div',{class:'plan-group '+classes},[node('h4',{text:title}),node('ul',{},items.map(value=>node('li',{text:planEntry(value)})))]);
    root.append(section);
  }
  async function requestInstallPlan() {
    if(!state.installRequest||state.installPending||!writable())return;
    const targets=installTargets();if(!targets.length){$('#install-error').textContent='Select at least one agent.';$('#install-error').hidden=false;return;}
    invalidateInstallPlan();state.installPending=true;renderManagementControls();
    try{
      const payload={...state.installRequest,targets,options:collectInstallOptions()};
      const result=await api('/api/install/plan',{method:'POST',body:JSON.stringify(payload)});
      const plan=result.plan||result;state.installPlan=plan;
      $('#install-review').hidden=false;
      $('#install-review-summary').textContent=state.installRequest.ids.length+' selected tools / '+targets.map(x=>APP_NAMES[x]).join(', ')+' / global user scope';
      renderPlanGroup($('#install-plan-changes'),'Changes',plan.changes);
      if(plan.migrations?.length){
        const details=plan.migrations.map(item=>'Shared folder removed after backup: '+item.id+'. Affected agents: '+agentList(item.affectedAgents).map(x=>APP_NAMES[x]).join(', ')+'. Removed from: '+(agentList(item.removedFromAgents).map(x=>APP_NAMES[x]).join(', ')||'none')+'.');
        $('#install-plan-changes').append(node('div',{class:'plan-group conflicts'},[node('h4',{text:'Shared folder migration'}),node('ul',{},details.map(text=>node('li',{text})))]));
      }
      renderPlanGroup($('#install-plan-dependencies'),'Dependencies',plan.dependencies);
      renderPlanGroup($('#install-plan-conflicts'),'Items to review',plan.conflicts,'conflicts');
      const required=listValues(plan.requiresInput);$('#install-input-required').hidden=!required.length;
      $('#install-input-required').textContent=required.length?'Additional input is required: '+required.map(planEntry).join('; ')+'. Complete the fields above and review the plan again.':'';
      $('#install-dialog-state').textContent=plan.canExecute===true?'Plan ready. Apply it to change only the reviewed targets.':'This plan needs attention before it can be applied.';
      try{localStorage.setItem('mcp-skills:install-targets',JSON.stringify(targets));}catch{}
    }catch(error){$('#install-error').textContent=privateSafeText(error.message);$('#install-error').hidden=false;}
    finally{state.installPending=false;renderManagementControls();}
  }
  async function executeInstallPlan() {
    const plan=state.installPlan;if(!plan?.id||plan.canExecute!==true||state.installPending||!writable())return;
    state.installPending=true;renderManagementControls();
    try{
      const result=await api('/api/install/execute',{method:'POST',body:JSON.stringify({planId:plan.id})});
      state.installGeneration++;state.installJob=result.job||{id:'starting',status:'running',progress:0,message:'Your installation request was accepted.'};
      state.installPlan=null;state.installPending=false;closeInstallDialog();state.catalogSelection.clear();
      message('Installation task started. Follow its progress in the catalog.','success');renderManagementControls();renderCatalog();
      await pollManagementJobs();schedulePoll();
      if(state.installJob?.status!=='running')await refreshResources(true);
    }catch(error){state.installPending=false;$('#install-error').textContent=privateSafeText(error.message);$('#install-error').hidden=false;if(/expired|changed|review/i.test(error.message))state.installPlan=null;renderManagementControls();}
  }
  function renderManagementControls() {
    if(!$('#catalog-install-selected'))return;
    $('#catalog-install-selected').disabled=!writable()||busy()||!state.catalogSelection.size;
    for(const button of $$('[data-catalog-action]'))button.disabled=!writable()||busy();
    const dialogOpen=$('#install-dialog').open;
    if(dialogOpen){
      $('#install-targets').disabled=state.installPending;
      for(const field of $$('#install-options input,#install-options textarea'))field.disabled=state.installPending;
      $('#install-plan').disabled=!writable()||state.installPending;
      $('#install-plan').textContent=state.installPending?'Preparing...':'Review plan';
      $('#install-execute').disabled=!writable()||state.installPending||state.installPlan?.canExecute!==true;
      $('#install-dialog-close').disabled=state.installPending;$('#install-cancel').disabled=state.installPending;
    }
    const job=state.installJob;$('#install-job-panel').hidden=!job;
    if(job){
      $('#install-job-title').textContent=job.status==='error'?'Installation needs attention':job.status==='done'?'Installation task completed':'Applying selected changes';
      $('#install-job-message').textContent=job.error||job.message||'The local manager is working.';
      const progress=Number(job.progress);$('#install-job-progress').value=Number.isFinite(progress)?Math.min(100,Math.max(0,progress)):0;
      $('#install-job-progress').hidden=job.status!=='running';$('#install-job-percent').textContent=job.status==='running'&&Number.isFinite(progress)?Math.round(progress)+'%':'';
      $('#install-job-panel').className='job-panel '+(job.status==='error'?'error':job.status==='done'?'done':'');
    }
  }
  function renderInstallHistory() {
    const list=$('#install-history-list');if(!list)return;list.replaceChildren();
    for(const item of state.installHistory.slice(0,30)){
      const text=item.message||[item.operation,item.status||item.state].filter(Boolean).join(' / ');
      const detail=[...listValues(item.items).map(x=>typeof x==='string'?x:x.name||x.id),...agentList(item.targets).map(x=>APP_NAMES[x])].filter(Boolean).join(' / ');
      list.append(node('li',{class:'activity-entry '+(item.status==='error'||String(item.state).startsWith('rollback')?'error':'')},[node('time',{text:dateLabel(item.at||item.completedAt||item.finishedAt||item.startedAt,true)}),node('div',{},[node('p',{text}),node('small',{class:'history-agents',text:detail})])]));
    }
    $('#install-history-empty').hidden=state.installHistory.length>0;
  }
  async function pollManagementJobs() {
    if(state.installPolling||!state.connected)return;state.installPolling=true;
    const installGeneration=state.installGeneration,dockerGeneration=state.dockerGeneration;
    try{
      const results=await Promise.allSettled([api('/api/install/status'),api('/api/docker/status')]);
      if(results[0].status==='fulfilled'&&installGeneration===state.installGeneration){
        const previous=state.installJob,next=results[0].value.job||null;
        if(Object.prototype.hasOwnProperty.call(results[0].value,'job'))state.installJob=next;
        if(previous?.status==='running'&&next?.status!=='running')refreshResources(true);
      }
      if(results[1].status==='fulfilled'&&dockerGeneration===state.dockerGeneration){
        const previous=state.dockerJob,next=results[1].value.job||null;
        if(Object.prototype.hasOwnProperty.call(results[1].value,'job'))state.dockerJob=next;
        if(previous?.status==='running'&&next?.status!=='running')checkDocker(false);
      }
    }finally{state.installPolling=false;renderManagementControls();renderDocker();}
  }
  function renderRuntime() {
    const data=state.runtime,facts=$('#runtime-facts');if(!facts)return;facts.replaceChildren();
    if(data){
      const platform=typeof data.platform==='string'?data.platform:data.platform?.name||'Native';
      for(const [label,value] of [['Platform',platform+(data.architecture?' / '+data.architecture:'')],['Environment',data.environment||'native'],['Node',data.node?.available?data.node.version||'Available':'Not detected'],['Python',data.python?.available?data.python.version||'Available':'Not detected']])facts.append(node('span',{class:'runtime-fact'},[node('strong',{text:label+': '}),value]));
      $('#runtime-summary').textContent=data.stateDir?'Settings, backups, and managed runtimes: '+data.stateDir:'Your manager stores writable data separately from installed application files.';
      const startup=data.startup||{},enabled=startup.enabled;
      $('#startup-enabled').checked=enabled===true;
      $('#startup-enabled').disabled=!writable()||state.startupSaving||startup.supported===false||data.environment==='container';
      $('#startup-description').textContent=data.environment==='container'?'Container startup is controlled through Docker.':startup.supported===false?'Automatic startup is not supported in this environment.':'Start the local manager in the background when you sign in. You can open the dashboard from its installed shortcut.';
    }else{$('#runtime-summary').textContent='Connect to inspect the platform, runtimes, and startup integration.';$('#startup-enabled').disabled=true;}
    const theme=window.AgentTheme?.getPreference?.()||'system';$('#appearance-theme').value=theme;
  }
  async function refreshRuntime() {
    if(!state.connected)return;
    try{state.runtime=await api('/api/runtime');}catch{}
    renderRuntime();await checkDocker(false);
  }
  async function saveStartup() {
    if(!writable()||state.startupSaving)return;const enabled=$('#startup-enabled').checked;state.startupSaving=true;renderRuntime();
    try{
      const result=await api('/api/runtime/startup',{method:'POST',body:JSON.stringify({enabled})});
      if(result.runtime)state.runtime=result.runtime;else if(state.runtime)state.runtime.startup=result.startup||result;
      message('Startup preference saved.','success');await refreshRuntime();
    }catch(error){message(error.message,'error');}
    finally{state.startupSaving=false;renderRuntime();}
  }
  function renderDocker() {
    const data=state.docker,job=state.dockerJob,runningJob=job?.status==='running',ready=writable()&&!state.dockerPending&&!runningJob;
    const available=data?.available===true,compose=data?.composeAvailable===true,running=data?.running===true,engineReady=available&&data?.engineRunning!==false;
    const configured=running||state.dockerGenerated||data?.configured===true||data?.configExists===true||data?.generated===true;
    const badge=$('#docker-badge');if(!badge)return;
    badge.textContent=!data?'Not checked':running?'Sandbox running':available&&compose?'Docker ready':available?'Compose unavailable':'Docker not installed';
    badge.className='install-state '+(running?'installed':data&&!available?'missing':'');
    $('#docker-description').textContent=!data?'Check Docker to inspect the local engine and Compose.':!available?'Install Docker first, then choose Check Docker. The native manager works without it.':!engineReady?'Docker is installed, but its engine is not running. Start Docker Desktop or Docker Engine, then check again.':!compose?'Docker is available, but Compose is missing or unavailable. Install or enable Compose, then check again.':running?'The separate container workspace is running. Host agent configurations are not used by this sandbox.':'Generate the sandbox configuration, then start it when you need a container development environment.';
    $('#docker-check').disabled=!writable()||state.dockerPending||runningJob;
    $('#docker-check').textContent=state.dockerPending?'Working...':'Check Docker';
    $('#docker-generate').disabled=!ready;
    $('#docker-start').disabled=!ready||!engineReady||!compose||running;
    $('#docker-stop').disabled=!ready||!available||!compose||!running;
    $('#docker-logs-button').disabled=!ready||!engineReady||!compose||!configured;
    const installUrl=safeUrl(data?.installUrl||'https://docs.docker.com/get-started/get-docker/');
    $('#docker-install-link').hidden=!data||available;$('#docker-install-link').href=installUrl||'https://docs.docker.com/get-started/get-docker/';
    const url=safeUrl(data?.url);$('#docker-open').hidden=!running||!url;if(url)$('#docker-open').href=url;
    $('#docker-job-panel').hidden=!job;
    if(job){$('#docker-job-title').textContent=job.status==='error'?'Sandbox task needs attention':job.status==='done'?'Sandbox task completed':'Preparing the Docker sandbox';$('#docker-job-message').textContent=job.error||job.message||'The local manager is working.';$('#docker-job-panel').className='job-panel '+(job.status==='error'?'error':job.status==='done'?'done':'');}
  }
  async function checkDocker(showMessage=true) {
    if(!writable()||state.dockerPending)return;
    state.dockerPending=true;renderDocker();
    try{state.docker=await api('/api/docker');$('#docker-error').hidden=true;if(showMessage)message(state.docker.available&&state.docker.composeAvailable?'Docker and Compose are available.':'Docker check completed. Follow the setup details in Runtimes & Docker.','success');}
    catch(error){$('#docker-error').textContent=error.message;$('#docker-error').hidden=true;if(showMessage){$('#docker-error').hidden=false;}}
    finally{state.dockerPending=false;renderDocker();}
  }
  async function dockerOperation(operation) {
    if(!writable()||state.dockerPending||state.dockerJob?.status==='running')return;
    state.dockerPending=true;$('#docker-error').hidden=true;renderDocker();
    try{
      const result=await api('/api/docker/'+operation,{method:'POST',body:JSON.stringify(operation==='generate'?{mode:'sandbox'}:{})});
      if(operation==='generate'){
        state.dockerGenerated=true;
        $('#docker-output').hidden=false;$('#docker-output').open=true;$('#docker-output-title').textContent='Generated sandbox configuration';
        $('#docker-output-text').textContent=(result.path?'Saved to '+result.path+'\n\n':'')+(result.content||'Configuration generated. Start sandbox when Docker and Compose are ready.');
        message('Sandbox configuration generated.','success');
      }else{
        state.dockerGeneration++;state.dockerJob=result.job||{status:'running',message:'The sandbox request was accepted.'};
        message('Sandbox '+operation+' requested.','success');schedulePoll();
        if(state.dockerJob.status!=='running'){state.dockerPending=false;await checkDocker(false);}
      }
    }catch(error){$('#docker-error').textContent=error.message;$('#docker-error').hidden=false;}
    finally{state.dockerPending=false;renderDocker();}
  }
  async function showDockerLogs() {
    if(!writable()||state.dockerPending)return;state.dockerPending=true;renderDocker();
    try{const result=await api('/api/docker/logs');$('#docker-output').hidden=false;$('#docker-output').open=true;$('#docker-output-title').textContent='Sandbox logs';$('#docker-output-text').textContent=String(result.logs||result.text||'No logs are available yet.').slice(-60000);$('#docker-error').hidden=true;}
    catch(error){$('#docker-error').textContent=error.message;$('#docker-error').hidden=false;}
    finally{state.dockerPending=false;renderDocker();}
  }
  function initialize() {
    let fallback={};try{fallback=JSON.parse($('#saved-inventory').textContent);}catch{}
    const saved=readOffline();state.savedAt=saved.savedAt||null;state.suggestions=Array.isArray(saved.suggestions)?saved.suggestions:[];
    state.availabilityByApp=saved.availabilityByApp&&typeof saved.availabilityByApp==='object'?saved.availabilityByApp:{};
    state.catalogByApp=saved.catalogByApp&&typeof saved.catalogByApp==='object'?saved.catalogByApp:{};
    if(Array.isArray(window.INVENTORY_CATALOG?.items))state.catalogByApp.all=window.INVENTORY_CATALOG;
    else if(Array.isArray(window.DEFAULT_CATALOG?.items))state.catalogByApp.all=window.DEFAULT_CATALOG;
    if(window.INVENTORY_AVAILABILITY?.selectedApp)state.availabilityByApp[window.INVENTORY_AVAILABILITY.selectedApp]=window.INVENTORY_AVAILABILITY;
    state.inventory=normalizeInventory(window.INVENTORY_SNAPSHOT||window.INVENTORY_FALLBACK||saved.inventory||fallback);
    let agent='all';try{const pref=localStorage.getItem('mcp-skills:agent');if(['all','claude','codex','gemini'].includes(pref))agent=pref;}catch{}
    $('#app-filter').value=agent;state.availability=state.availabilityByApp[agent]||null;state.catalog=state.catalogByApp[agent]||state.catalogByApp.all||null;
    render();
    $('#search').addEventListener('input',()=>{state.availabilityLimit=60;filterInventory();});
    $('#app-filter').addEventListener('change',()=>selectAgent($('#app-filter').value));
    for(const button of $$('[data-agent]'))button.addEventListener('click',()=>selectAgent(button.dataset.agent));
    $('#mcp-status').addEventListener('change',filterInventory);
    $('#reset-filters').addEventListener('click',()=>{$('#search').value='';$('#mcp-status').value='all';$('#availability-kind').value='all';selectAgent('all');$('#search').focus();});
    $('#availability-kind').addEventListener('change',()=>{state.availabilityLimit=60;renderAvailability();});
    for(const button of $$('[data-availability-state]'))button.addEventListener('click',()=>{state.availabilityState=button.dataset.availabilityState;state.availabilityLimit=60;renderAvailability();});
    $('#availability-more').addEventListener('click',()=>{state.availabilityLimit+=60;renderAvailability();});
    for(const id of ['catalog-kind','catalog-filter'])$('#'+id).addEventListener('change',()=>{state.catalogLimit=60;renderCatalog();});
    $('#catalog-more').addEventListener('click',()=>{state.catalogLimit+=60;renderCatalog();});
    $('#catalog-clear').addEventListener('click',()=>{state.catalogSelection.clear();renderCatalog();});
    $('#catalog-select-visible').addEventListener('click',()=>{for(const item of filteredCatalog().slice(0,state.catalogLimit))if(item.installable!==false)state.catalogSelection.add(item.id);renderCatalog();});
    $('#catalog-install-selected').addEventListener('click',()=>openInstallDialog([...state.catalogSelection],'install'));
    document.addEventListener('change',event=>{const field=event.target.closest('[data-catalog-select]');if(field){if(field.checked)state.catalogSelection.add(field.dataset.catalogSelect);else state.catalogSelection.delete(field.dataset.catalogSelect);renderCatalog();}});
    document.addEventListener('click',event=>{const button=event.target.closest('[data-catalog-action]');if(button&&!button.disabled)openInstallDialog([button.dataset.itemId],button.dataset.catalogAction);});
    $('#install-targets').addEventListener('change',invalidateInstallPlan);
    $('#install-options').addEventListener('input',invalidateInstallPlan);
    $('#install-plan').addEventListener('click',requestInstallPlan);
    $('#install-execute').addEventListener('click',executeInstallPlan);
    $('#install-dialog-close').addEventListener('click',closeInstallDialog);
    $('#install-cancel').addEventListener('click',closeInstallDialog);
    $('#install-dialog').addEventListener('cancel',event=>{event.preventDefault();closeInstallDialog();});
    $('#install-dialog').addEventListener('close',()=>{for(const field of $$('#install-options input,#install-options textarea'))field.value='';state.installPlan=null;});
    for(const link of $$('[data-catalog-nav]'))link.addEventListener('click',()=>{$('#catalog-kind').value=link.dataset.catalogNav;state.catalogLimit=60;renderCatalog();});
    $('#appearance-theme').addEventListener('change',event=>{window.AgentTheme?.setPreference?.(event.target.value);renderRuntime();});
    $('#theme-select').addEventListener('change',()=>setTimeout(renderRuntime,0));
    $('#startup-enabled').addEventListener('change',saveStartup);
    $('#docker-check').addEventListener('click',()=>checkDocker());
    for(const operation of ['generate','start','stop'])$('#docker-'+operation).addEventListener('click',()=>dockerOperation(operation));
    $('#docker-logs-button').addEventListener('click',showDockerLogs);
    document.addEventListener('click',event=>{if(event.target.closest('[data-open-missing]')){state.availabilityState='missing';state.availabilityLimit=60;renderAvailability();}});
    $('#refresh-inventory').addEventListener('click',async()=>{const refreshed=await refreshResources(true);message(refreshed?'Inventory refreshed. Global installations and missing items were rescanned.':'Some installation checks could not be refreshed. Check the local manager and try again.',refreshed?'success':'error');});
    $('#expand-all').addEventListener('click',()=>{$$('details.skill-group').filter(group=>!group.hidden).forEach(group=>group.open=true);});
    $('#collapse-all').addEventListener('click',()=>{$$('details.skill-group').forEach(group=>group.open=false);});
    $('#print-report').addEventListener('click',()=>{$$('details.skill-group').filter(group=>!group.hidden).forEach(group=>group.open=true);window.print();});
    $('#check-updates').addEventListener('click',()=>startJob('/api/check-updates',{}));
    $('#update-all').addEventListener('click',()=>startJob('/api/update',{}));
    document.addEventListener('click',event=>{const button=event.target.closest('[data-update-id]');if(button&&!button.disabled)startJob('/api/update',{ids:[button.dataset.updateId]});});
    $('#auto-settings').addEventListener('submit',event=>event.preventDefault());$('#auto-update').addEventListener('change',saveSettings);$('#update-interval').addEventListener('change',saveSettings);
    $('#retry-helper').addEventListener('click',()=>{message('Connecting to the local manager...');pollStatus().then(()=>{if(state.connected)message('');});});
    $('#menu-toggle').addEventListener('click',()=>setNavigation(!$('#sidebar').classList.contains('open')));
    $('#sidebar-close').addEventListener('click',()=>setNavigation(false));
    $('#sidebar-scrim').addEventListener('click',()=>setNavigation(false));
    for(const link of $$('#sidebar a'))link.addEventListener('click',()=>{if($('#sidebar').classList.contains('open'))setNavigation(false);});
    document.addEventListener('keydown',event=>{
      if($('#install-dialog').open)return;
      if(event.key==='Escape'&&$('#sidebar').classList.contains('open'))setNavigation(false);
      if(event.key==='Tab'&&$('#sidebar').classList.contains('open')){
        const focusable=[...$('#sidebar').querySelectorAll('a[href],button:not(:disabled)')].filter(el=>!el.hidden&&getComputedStyle(el).visibility!=='hidden');
        const first=focusable[0],last=focusable[focusable.length-1];
        if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
        else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
      }
      if(event.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){event.preventDefault();$('#search').focus();}
    });
    if('IntersectionObserver'in window){const observer=new IntersectionObserver(entries=>{for(const entry of entries)if(entry.isIntersecting){for(const link of $$('.sidebar nav a'))link.classList.toggle('active',link.hash==='#'+entry.target.id);}},{rootMargin:'-15% 0px -65% 0px'});for(const section of $$('main>section'))observer.observe(section);}
    document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(state.pollTimer);else if(isLivePage)pollStatus();});
    window.addEventListener('online',()=>{if(isLivePage)pollStatus();});
    window.addEventListener('offline',()=>{state.connected=false;token='';renderControls();renderHeader();renderAvailability();});
    window.addEventListener('beforeunload',()=>clearTimeout(state.pollTimer));
    for(const wrapper of $$('.table-wrap')){wrapper.tabIndex=0;wrapper.setAttribute('role','region');wrapper.setAttribute('aria-label',wrapper.querySelector('table')?.id==='mcp-table'?'MCP servers, scroll horizontally if needed':'Publisher versions, scroll horizontally if needed');}
    if(isLivePage)pollStatus();
  }
  initialize();
})();
