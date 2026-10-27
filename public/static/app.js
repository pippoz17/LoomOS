(() => {
  const overlay = document.getElementById('commandOverlay');
  const input = document.getElementById('commandInput');
  const results = document.getElementById('commandResults');
  const open = document.getElementById('openCommand');
  const navItems = [...document.querySelectorAll('.nav-link')].map(a => ({label:a.innerText.trim(),href:a.getAttribute('href'),type:'Page',symbol:a.querySelector('.nav-icon')?.innerText||'↗'}));
  const quick = [
    {label:'Create customer',href:'/customers#new-customer',type:'Quick action',symbol:'+'},
    {label:'Create CRM lead',href:'/crm#new-lead',type:'Quick action',symbol:'+'},
    {label:'Create quotation',href:'/quotations#new-quotation',type:'Quick action',symbol:'+'},
    {label:'Create sales order',href:'/orders#new-order',type:'Quick action',symbol:'+'},
    {label:'Receive inventory stock',href:'/inventory#stock-movements',type:'Quick action',symbol:'+'},
    {label:'Add production batch',href:'/production#new-batch',type:'Quick action',symbol:'+'},
    {label:'View reports and exports',href:'/reports',type:'Quick action',symbol:'↗'}
  ];
  let selected = 0, current = navItems.concat(quick);
  function renderResults(query='') {
    if (!results) return;
    const q=query.toLowerCase(); current=navItems.concat(quick).filter(x => (x.label+' '+x.type).toLowerCase().includes(q)); selected=0;
    results.innerHTML = current.length ? current.map((x,i)=>`<a class="command-item ${i===0?'selected':''}" href="${x.href}"><span>${x.symbol}</span><span style="flex:1">${x.label}</span><small style="color:#89958c">${x.type}</small></a>`).join('') : '<div class="empty-state"><b>No matching destination</b><p>Try an order, customer, module or action.</p></div>';
  }
  function show(){if(!overlay)return;overlay.classList.add('open');overlay.setAttribute('aria-hidden','false');renderResults(input?.value||'');setTimeout(()=>input?.focus(),20)}
  function hide(){overlay?.classList.remove('open');overlay?.setAttribute('aria-hidden','true')}
  open?.addEventListener('click',show); input?.addEventListener('input',()=>renderResults(input.value));
  overlay?.addEventListener('click',e=>{if(e.target===overlay)hide()});
  document.addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();show()}if(e.key==='Escape')hide();if(overlay?.classList.contains('open')&&['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();const items=[...results.querySelectorAll('.command-item')];if(!items.length)return;items[selected]?.classList.remove('selected');selected=(selected+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;items[selected]?.classList.add('selected');items[selected]?.scrollIntoView({block:'nearest'})}if(overlay?.classList.contains('open')&&e.key==='Enter'){const items=[...results.querySelectorAll('.command-item')];if(items[selected])location.href=items[selected].getAttribute('href')}});
  document.getElementById('mobileMenu')?.addEventListener('click',()=>document.getElementById('sidebar')?.classList.toggle('open'));
  document.querySelectorAll('form[data-confirm]').forEach(form=>form.addEventListener('submit',e=>{if(!confirm(form.dataset.confirm))e.preventDefault()}));
  document.querySelectorAll('[data-toast]').forEach(btn=>btn.addEventListener('click',()=>{const region=document.getElementById('toastRegion');if(!region)return;const t=document.createElement('div');t.className='toast';t.textContent=btn.dataset.toast;region.appendChild(t);setTimeout(()=>t.remove(),3000)}));
  document.querySelectorAll('[data-copy]').forEach(btn=>btn.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(btn.dataset.copy);btn.textContent='Copied';}catch{const el=document.createElement('textarea');el.value=btn.dataset.copy;document.body.appendChild(el);el.select();document.execCommand('copy');el.remove();btn.textContent='Copied';}setTimeout(()=>btn.textContent=btn.dataset.label||'Copy link',1800)}));
  document.querySelectorAll('[data-range-target]').forEach(input=>input.addEventListener('input',()=>{const target=document.getElementById(input.dataset.rangeTarget);if(target)target.textContent=input.value+(input.dataset.suffix||'')}));
  // Client-only what-if estimates are labelled as projections, never actual financial results.
  const simInputs=[...document.querySelectorAll('[data-sim-input]')];
  function recalcSimulator(){const values={};simInputs.forEach(el=>values[el.dataset.simInput]=Number(el.value));const capacity=document.getElementById('sim-capacity');const completion=document.getElementById('sim-completion');const cost=document.getElementById('sim-cost');const waste=document.getElementById('sim-waste');const simRoot=document.querySelector('.simulator-grid');const currentOrders=Number(simRoot?.dataset.orders||10);const total=Number(simRoot?.dataset.orderValue||0);if(capacity)capacity.textContent=Math.round((1+(values.capacity||0)/100)*100)+'%';if(completion)completion.textContent=Math.max(0,Math.round((1+(values.capacity||0)/100-(values.delay||0)/30)*100))+'%';if(cost)cost.textContent='₹'+Math.round(total*(1+(values.material||0)/100)).toLocaleString('en-IN');if(waste)waste.textContent=Math.max(0,Number(document.body.dataset.rejection||5)-(values.rejection||0)).toFixed(1)+'%';}
  simInputs.forEach(el=>el.addEventListener('input',recalcSimulator));if(simInputs.length)recalcSimulator();
  // Copilot answers are returned by the local backend from current application records.
  const copilotForm=document.getElementById('copilotForm');
  copilotForm?.addEventListener('submit',async e=>{e.preventDefault();const field=copilotForm.querySelector('input[name="question"]');const btn=copilotForm.querySelector('button');const output=document.getElementById('copilotOutput');if(!field||!output)return;const old=btn.innerHTML;btn.disabled=true;btn.innerHTML='Thinking…';output.innerHTML='<div class="empty-state"><b>Checking current records</b><p>Evaluating business rules against saved workspace records.</p></div>';try{const res=await fetch('/actions/copilot',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]')?.content||''},body:new URLSearchParams({question:field.value})});if(!res.ok)throw new Error('Could not query current data.');const data=await res.json();output.innerHTML=`<div class="surface" style="padding:16px"><div class="eyebrow">RULE-BASED ANALYSIS · WORKSPACE RECORDS</div><p style="font-size:11px;line-height:1.65;margin:6px 0 12px">${escapeHTML(data.answer)}</p>${(data.results||[]).map(item=>`<div class="copilot-result"><div class="copilot-result-head"><b>${escapeHTML(item.title)}</b><strong>${escapeHTML(String(item.value))}</strong></div><p>${escapeHTML(item.detail||'')}</p><small class="muted">${escapeHTML(String(item.metric||''))}</small></div>`).join('')||'<p class="muted small">No matching records found.</p>'}</div>`;}catch(err){output.innerHTML=`<div class="flash flash-error">${escapeHTML(err.message||'Analysis unavailable')}</div>`;}finally{btn.disabled=false;btn.innerHTML=old;}});
  document.querySelectorAll('[data-prompt]').forEach(btn=>btn.addEventListener('click',()=>{const input=document.querySelector('#copilotForm input[name="question"]');if(input){input.value=btn.dataset.prompt;document.getElementById('copilotForm').requestSubmit()}}));
  function escapeHTML(value){return String(value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
})();
