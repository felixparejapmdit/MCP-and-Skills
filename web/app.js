(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const APP_NAMES = {claude:'Claude Code',codex:'Codex / Work',gemini:'Gemini CLI'};
  const isLivePage = location.protocol === 'http:' && ['127.0.0.1','localhost'].includes(location.hostname);
  const state = {inventory:null,updates:[],suggestions:[],activity:[],settings:null,job:null,connected:false,pending:false,saving:false,lastCheck:null,lastUpdate:null,checkedAt:null,suggestionsAt:null,lastJobKey:null,pollTimer:null,loading:false};
  const token = typeof window.INVENTORY_TOKEN === 'string' ? window.INVENTORY_TOKEN : '';
  const STATUS = {
    'up-to-date':['Up to date','current'], 'updated':['Updated','current'],
    'update-available':['Update available','available'], 'host-managed':['Provider-managed','managed'],
    'provider-managed':['Provider-managed','managed'], 'local-changes':['Local changes','attention'],
    'partial':['Review required','attention'],
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
    const mcp=allMcp(),groups=inventoryGroups();
    const shared=new Set(groups.filter(group=>group.kind!=='command' && !/disabled/i.test(group.state||'') && appsOf(group).includes('codex') && appsOf(group).includes('gemini')).flatMap(namesOf));
    const count=state.inventory?.stats?.sharedPortableSkillFolders||state.inventory?.stats?.sharedSkills||shared.size;
    const summary=$('#summary-line'); summary.replaceChildren();
    for (const [total,label,href] of [[mcp.length,'MCP servers','#mcp'],[count,'shared skills','#skills'],[mcp.filter(item=>item.status==='auth'||/sign.in|auth/i.test(item.status||'')).length,'need sign-in','#mcp']]) {
      summary.append(node('a',{href},[node('strong',{text:total}),' '+label]));
    }
    const available=state.updates.filter(updateAvailable).length;
    if (state.checkedAt) summary.append(node('a',{href:'#updates'},[node('strong',{text:available}),available===1?' update available':' updates available']));
    $('#inventory-time').textContent=(state.connected?'Live inventory':'Saved inventory')+' - '+dateLabel(state.inventory?.generatedAt);
    $('#sidebar-state').textContent=state.connected?'Local manager connected':'Saved inventory';
    $('#footer-state').textContent=state.connected?'Connected to your local manager.':'Saved inventory. Open the live dashboard for update controls.';
    const description=$('#skills-description');
    description.textContent=`Codex and Gemini share ${count} portable skills. Browse every source below; matching capabilities can appear in multiple source groups.`;
  }
  function renderMcp() {
    const tbody=$('#mcp-table tbody'); tbody.replaceChildren();
    for (const item of allMcp()) {
      const apps=appsOf(item),status=String(item.status||'').toLowerCase()==='connected'?'connected':/auth|sign.in/i.test(item.status||'')?'auth':'unknown';
      const row=node('tr'); row.dataset.apps=apps.join(' ');row.dataset.state=status;row.dataset.search=[item.name,item.id,item.description].join(' ').toLowerCase();
      row.append(node('td',{},[node('strong',{text:item.name}),node('small',{text:item.description||''})]),node('td',{},node('span',{class:'status '+status,text:status==='connected'?'Connected':status==='auth'?'Sign-in required':'Not checked'})));
      for (const app of ['claude','codex','gemini']) row.append(node('td',{text:apps.includes(app)?'Global':'Not configured'}));
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
    $('#results').textContent=`${mcpCount} MCP ${mcpCount===1?'server':'servers'}, ${skillCount} skill entries, and ${commandCount} commands match${app!=='all'?' '+APP_NAMES[app]:''}.`;
  }
  function render() {renderHeader();renderMcp();renderGroups();renderUpdates();renderSuggestions();renderActivity();renderControls();filterInventory();}
  function message(text,type='') { const element=$('#action-message');element.textContent=text;element.className='action-message '+type;element.hidden=!text; }
  async function api(path, options={}) {
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),8000);
    try{
      const response=await fetch(path,{...options,credentials:'same-origin',cache:'no-store',signal:controller.signal,headers:{...(options.method?{'Content-Type':'application/json','X-Inventory-Token':token}:{}),...(options.headers||{})}});
      const body=await response.json().catch(()=>null);
      if(!response.ok)throw new Error(body?.error||body?.message||`The manager returned HTTP ${response.status}. Please try again.`);
      return body||{};
    }catch(error){if(error.name==='AbortError')throw new Error('The local manager took too long to respond. Please try again.');throw error;}
    finally{clearTimeout(timeout);}
  }
  async function refreshResources() {
    if(state.loading)return;state.loading=true;
    const routes=['/api/inventory','/api/updates','/api/suggestions','/api/activity'];
    const results=await Promise.allSettled(routes.map(route=>api(route)));
    const [inventory,updates,suggestions,activity]=results;
    if(inventory.status==='fulfilled')state.inventory=normalizeInventory(inventory.value);
    if(updates.status==='fulfilled'){state.updates=updates.value.items||[];state.checkedAt=updates.value.checkedAt;$('#updates-error').hidden=true;}
    else{$('#updates-error').textContent='Publisher information could not be loaded. Your installed inventory is still available.';$('#updates-error').hidden=false;}
    if(suggestions.status==='fulfilled'){state.suggestions=suggestions.value.items||[];state.suggestionsAt=suggestions.value.checkedAt;$('#suggestions-error').hidden=true;}
    else{$('#suggestions-error').textContent='Suggestions could not be loaded. They will be retried with the next manager connection.';$('#suggestions-error').hidden=false;}
    if(activity.status==='fulfilled')state.activity=activity.value.items||[];
    state.loading=false;render();
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
      state.connected=true;state.settings=status.settings||state.settings;state.job=status.job||null;state.lastCheck=status.lastCheck;state.lastUpdate=status.lastUpdate;
      const key=state.job?`${state.job.id}:${state.job.status}`:'none';
      const completed=previousRunning&&state.job?.status!=='running';
      const changed=key!==state.lastJobKey;state.lastJobKey=key;
      renderControls();renderHeader();
      if(!wasConnected||completed||(changed&&state.job?.status!=='running'))await refreshResources();
      else if(state.job?.status==='running'){
        const activity=await api('/api/activity').catch(()=>null);if(activity){state.activity=activity.items||[];renderActivity();}
      }
    }catch{state.connected=false;renderControls();renderHeader();}
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
  function initialize() {
    let fallback={};try{fallback=JSON.parse($('#saved-inventory').textContent);}catch{}
    state.inventory=normalizeInventory(window.INVENTORY_SNAPSHOT||window.INVENTORY_FALLBACK||fallback);
    render();
    $('#search').addEventListener('input',filterInventory);$('#app-filter').addEventListener('change',filterInventory);$('#mcp-status').addEventListener('change',filterInventory);
    $('#reset-filters').addEventListener('click',()=>{$('#search').value='';$('#app-filter').value='all';$('#mcp-status').value='all';filterInventory();$('#search').focus();});
    $('#expand-all').addEventListener('click',()=>{$$('details.skill-group').filter(group=>!group.hidden).forEach(group=>group.open=true);});
    $('#collapse-all').addEventListener('click',()=>{$$('details.skill-group').forEach(group=>group.open=false);});
    $('#print-report').addEventListener('click',()=>{$$('details.skill-group').filter(group=>!group.hidden).forEach(group=>group.open=true);window.print();});
    $('#check-updates').addEventListener('click',()=>startJob('/api/check-updates',{}));
    $('#update-all').addEventListener('click',()=>startJob('/api/update',{}));
    document.addEventListener('click',event=>{const button=event.target.closest('[data-update-id]');if(button&&!button.disabled)startJob('/api/update',{ids:[button.dataset.updateId]});});
    $('#auto-settings').addEventListener('submit',event=>event.preventDefault());$('#auto-update').addEventListener('change',saveSettings);$('#update-interval').addEventListener('change',saveSettings);
    $('#retry-helper').addEventListener('click',()=>{message('Connecting to the local manager...');pollStatus().then(()=>{if(state.connected)message('');});});
    document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(state.pollTimer);else if(isLivePage)pollStatus();});
    window.addEventListener('beforeunload',()=>clearTimeout(state.pollTimer));
    for(const wrapper of $$('.table-wrap')){wrapper.tabIndex=0;wrapper.setAttribute('role','region');wrapper.setAttribute('aria-label',wrapper.querySelector('table')?.id==='mcp-table'?'MCP servers, scroll horizontally if needed':'Publisher versions, scroll horizontally if needed');}
    if(isLivePage)pollStatus();
  }
  initialize();
})();
