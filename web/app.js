(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const APP_NAMES = {all:'All agents',claude:'Claude',codex:'Codex',gemini:'Gemini'};
  const isLivePage = location.protocol === 'http:' && ['127.0.0.1','localhost'].includes(location.hostname);
  const state = {inventory:null,updates:[],suggestions:[],activity:[],settings:null,job:null,connected:false,pending:false,saving:false,lastCheck:null,lastUpdate:null,checkedAt:null,suggestionsAt:null,lastJobKey:null,pollTimer:null,loading:false};
  let token = typeof window.INVENTORY_TOKEN === 'string' ? window.INVENTORY_TOKEN : '';
  Object.assign(state,{availability:null,availabilityByApp:{},availabilityState:'missing',availabilityLimit:60,resourceGeneration:0,lastResourcesAt:0,savedAt:null});
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
  function busy() { return state.pending || state.saving || state.job?.status==='running'; }
  function writable() { return isLivePage && state.connected && !!token; }
  function updateAvailable(item) { return item?.status==='update-available'; }
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
    $('#agent-context').textContent=app==='all'?'Your complete toolkit, with gaps across compatible agents.':`Showing ${APP_NAMES[app]} installations. Publisher updates remain global.`;
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
    $('#schedule-note').textContent=state.saving?'Saving your update schedule...':state.connected&&state.settings?(state.settings.autoUpdate?`Automatic checks and supported updates run every ${state.settings.intervalHours} hours. Provider-managed services stay with their publishers.`:'Automatic updates are paused. You can still check and update sources here.'):'Open the live dashboard to manage the update schedule.';
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
    $('#results').textContent=`${mcpCount} MCP ${mcpCount===1?'server':'servers'}, ${skillCount} skill entries, and ${commandCount} commands match${app!=='all'?' '+APP_NAMES[app]:''}.`;
  }
  function render() {renderHeader();renderMcp();renderGroups();renderUpdates();renderSuggestions();renderActivity();renderControls();decorateTables();filterInventory();}
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
    const routes=['/api/inventory','/api/updates','/api/suggestions','/api/activity','/api/availability?app='+app];
    const results=await Promise.allSettled(routes.map(route=>api(route)));
    if(generation!==state.resourceGeneration)return;
    const [inventory,updates,suggestions,activity,availability]=results;
    if(inventory.status==='fulfilled')state.inventory=normalizeInventory(inventory.value);
    if(updates.status==='fulfilled'){state.updates=updates.value.items||[];state.checkedAt=updates.value.checkedAt;$('#updates-error').hidden=true;}
    else{$('#updates-error').textContent='Publisher information could not be loaded. Your installed inventory is still available.';$('#updates-error').hidden=false;}
    if(suggestions.status==='fulfilled'){state.suggestions=suggestions.value.items||[];state.suggestionsAt=suggestions.value.checkedAt;$('#suggestions-error').hidden=true;}
    else{$('#suggestions-error').textContent='Suggestions could not be loaded. They will be retried with the next manager connection.';$('#suggestions-error').hidden=false;}
    if(activity.status==='fulfilled')state.activity=activity.value.items||[];
    if(availability.status==='fulfilled'&&availability.value.selectedApp===app){state.availability=availability.value;state.availabilityByApp[app]=availability.value;$('#availability-error').hidden=true;}
    else{$('#availability-error').textContent='Installation status could not be refreshed. Check the helper and try Refresh inventory.';$('#availability-error').hidden=false;}
    state.loading=false;state.lastResourcesAt=Date.now();saveOffline();render();
    return inventory.status==='fulfilled'&&availability.status==='fulfilled';
  }
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if(isLivePage&&!document.hidden)state.pollTimer=setTimeout(pollStatus,state.job?.status==='running'?1200:state.connected?15000:8000);
  }
  async function pollStatus() {
    if(!isLivePage)return;
    try{
      const status=await api('/api/status'),wasConnected=state.connected;
      const previousRunning=state.job?.status==='running';
      if(!wasConnected||!token)await refreshSession();
      state.connected=true;state.settings=status.settings||state.settings;state.job=status.job||null;state.lastCheck=status.lastCheck;state.lastUpdate=status.lastUpdate;
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
    try { localStorage.setItem('mcp-skills:offline:v1',JSON.stringify({inventory,availabilityByApp:state.availabilityByApp,suggestions:state.suggestions,savedAt:state.savedAt})); } catch {}
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
    try{localStorage.setItem('mcp-skills:agent',app);}catch{}
    state.availability=state.availabilityByApp[app]||null;state.availabilityLimit=60;renderHeader();filterInventory();
    if(state.connected)refreshResources(true);
  }
  function setNavigation(open) {
    $('#sidebar').classList.toggle('open',open);$('#sidebar-scrim').hidden=!open;
    $('#menu-toggle').setAttribute('aria-expanded',String(open));
    if(open)$('#sidebar nav a').focus();else $('#menu-toggle').focus({preventScroll:true});
  }
  function initialize() {
    let fallback={};try{fallback=JSON.parse($('#saved-inventory').textContent);}catch{}
    const saved=readOffline();state.savedAt=saved.savedAt||null;state.suggestions=Array.isArray(saved.suggestions)?saved.suggestions:[];
    state.availabilityByApp=saved.availabilityByApp&&typeof saved.availabilityByApp==='object'?saved.availabilityByApp:{};
    if(window.INVENTORY_AVAILABILITY?.selectedApp)state.availabilityByApp[window.INVENTORY_AVAILABILITY.selectedApp]=window.INVENTORY_AVAILABILITY;
    state.inventory=normalizeInventory(window.INVENTORY_SNAPSHOT||window.INVENTORY_FALLBACK||saved.inventory||fallback);
    let agent='all';try{const pref=localStorage.getItem('mcp-skills:agent');if(['all','claude','codex','gemini'].includes(pref))agent=pref;}catch{}
    $('#app-filter').value=agent;state.availability=state.availabilityByApp[agent]||null;
    render();
    $('#search').addEventListener('input',()=>{state.availabilityLimit=60;filterInventory();});
    $('#app-filter').addEventListener('change',()=>selectAgent($('#app-filter').value));
    for(const button of $$('[data-agent]'))button.addEventListener('click',()=>selectAgent(button.dataset.agent));
    $('#mcp-status').addEventListener('change',filterInventory);
    $('#reset-filters').addEventListener('click',()=>{$('#search').value='';$('#mcp-status').value='all';$('#availability-kind').value='all';selectAgent('all');$('#search').focus();});
    $('#availability-kind').addEventListener('change',()=>{state.availabilityLimit=60;renderAvailability();});
    for(const button of $$('[data-availability-state]'))button.addEventListener('click',()=>{state.availabilityState=button.dataset.availabilityState;state.availabilityLimit=60;renderAvailability();});
    $('#availability-more').addEventListener('click',()=>{state.availabilityLimit+=60;renderAvailability();});
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
