// Offline UI logic checks. No browser, network, or SMTP is used.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname,'..');
const html = fs.readFileSync(path.join(root,'static/index.html'),'utf8');
const code = fs.readFileSync(path.join(root,'static/app.js'),'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);

class Element {
  constructor(id) {
    this.id=id; this.value=''; this.hidden=false; this.disabled=false; this.textContent='';
    this.innerHTML=''; this.checked=false; this.indeterminate=false; this.dataset={};
    this.open=false; this.required=false; this.listeners={}; this.children={};
    this.classList={toggle(){},add(){},remove(){}};
  }
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  querySelector(selector){return this.children[selector]??=(new Element(selector));}
  showModal(){this.open=true;}
  close(){this.open=false; for(const fn of this.listeners.close||[])fn();}
  scrollIntoView(){}
  click(){}
  append(){}
  remove(){}
}
function item(id,status='pending') {
 return {id,subject:'志愿者名单 '+id,filename:id+'.eml',recipients:[id+'@example.com'],attachments:[],
   groups:{To:[{name:'',address:id+'@example.com'}],Cc:[],Bcc:[]},status,eligible:['pending','failed','partial'].includes(status),test_count:0,problem:status==='invalid'?'没有有效收件人':''};
}
async function setup(overrides={}) {
 const elements=Object.fromEntries(ids.map(id=>[id,new Element(id)]));
 elements.filter.value='all';elements.delay.value='2';
 const initial={items:[item('one'),item('two','sent'),item('bad','invalid')],history:[],job:null,demo:true};
 const requests=[];
 const storage=new Map();
 const context=vm.createContext({URLSearchParams,URL,Set,Map,Date,JSON,Number,String,Promise,console,
   location:{hash:'#token=local-test-token'},history:{replaceState(){}},
   sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},
   document:{getElementById(id){assert.ok(elements[id],`Missing HTML element: ${id}`);return elements[id];},querySelectorAll(){return [];},createElement(){return new Element('created');},body:new Element('body')},
   window:{addEventListener(){}},setTimeout(){return 1;},clearTimeout(){},
   fetch:async(url,options)=>{
     requests.push({url,options});
     const route=url.slice(5).split('?')[0];
     const payload=options.body&&typeof options.body==='string'?JSON.parse(options.body):undefined;
     if(overrides[route])return overrides[route](payload,url,options);
     const responses={state:initial,plan:{id:'plan-one',sender:'demo@sjtu.edu.cn',confirmation:'确认发送 1',items:[{id:'one',subject:'<script>bad</script>',filename:'one.eml',recipients:['one@example.com']}],skipped:[]},start:{},stop:{},remove:{},unlock:{}};
     return {ok:true,json:async()=>responses[route]};
   }});
 vm.runInContext(code,context);
 await new Promise(setImmediate);
 return {elements,context,requests,initial,run:expression=>vm.runInContext(expression,context)};
}

test('Every referenced HTML id exists and initial selection excludes sent or invalid drafts',async()=>{
 assert.equal(new Set(ids).size,ids.length);
 const ui=await setup();
 assert.deepEqual(Array.from(ui.run('[...selected]')),['one']);
 assert.equal(ui.elements['stat-total'].textContent,3);
 assert.equal(ui.elements['send-selected'].disabled,false);
 assert.equal(ui.elements['demo-banner'].hidden,false);
 assert.equal(ui.elements.account.value,'demo');
});
test('Search and status filtering retain only matching drafts',async()=>{
 const ui=await setup();
 ui.elements.search.value='two@';
 assert.deepEqual(Array.from(ui.run('filteredItems().map(i=>i.id)')),['two']);
 ui.elements.search.value='';ui.elements.filter.value='attention';
 assert.deepEqual(Array.from(ui.run('filteredItems().map(i=>i.id)')),['bad']);
});
test('Test button requires exactly one valid draft',async()=>{
 const ui=await setup();
 assert.equal(ui.elements['test-send'].disabled,false);
 ui.run("selected.add('two'); controls()");
 assert.equal(ui.elements['test-send'].disabled,true);
 ui.run("selected=new Set(['bad']); controls()");
 assert.equal(ui.elements['test-send'].disabled,true);
});
test('Confirmation lists actual addresses and escapes email content',async()=>{
 const ui=await setup();
 await ui.run('previewSend(false)');
 assert.equal(ui.elements['confirm-dialog'].open,true);
 assert.ok(ui.elements['confirm-items'].innerHTML.includes('&lt;script&gt;'));
 assert.ok(!ui.elements['confirm-items'].innerHTML.includes('<script>'));
 assert.ok(ui.elements['confirm-items'].innerHTML.includes('one@example.com'));
 assert.equal(ui.elements['password-field'].hidden,true);
});
test('Closing confirmation clears password and confirmation state',async()=>{
 const ui=await setup();
 await ui.run('previewSend(false)');
 ui.elements.password.value='sensitive';ui.elements.confirmation.value='确认发送 1';
 ui.elements['confirm-dialog'].close();
 assert.equal(ui.elements.password.value,'');
 assert.equal(ui.run('plan'),null);
});
test('Successful submission clears password and prevents default form navigation',async()=>{
 const ui=await setup();
 await ui.run('previewSend(false)');
 ui.elements.password.value='sensitive';ui.elements.confirmation.value='确认发送 1';
 let prevented=false;
 await ui.elements['confirm-form'].onsubmit({preventDefault(){prevented=true;}});
 assert.equal(prevented,true);
 assert.equal(ui.elements.password.value,'');
 assert.equal(ui.elements['confirm-dialog'].open,false);
 const request=ui.requests.find(r=>r.url==='/api/start');
 assert.equal(JSON.parse(request.options.body).confirmation,'确认发送 1');
 assert.equal(request.options.headers['X-MailDesk-Token'],'local-test-token');
});
test('Submission error clears password and displays reason',async()=>{
 const ui=await setup({start:async()=>({ok:false,json:async()=>({error:'确认已过期'})})});
 await ui.run('previewSend(false)');
 ui.elements.password.value='sensitive';
 await ui.elements['confirm-form'].onsubmit({preventDefault(){}});
 assert.equal(ui.elements.password.value,'');
 assert.equal(ui.elements['confirm-error'].textContent,'确认已过期');
 assert.equal(ui.elements['confirm-error'].hidden,false);
});
test('Active job disables sending, upload and settings',async()=>{
 const ui=await setup();
 ui.run("state.job={status:'running'}; controls()");
 for(const id of ['send-selected','test-send','choose-files','account','test-to','delay'])assert.equal(ui.elements[id].disabled,true,id);
});
test('Connection error disables sending and explains restart',async()=>{
 const ui=await setup({state:async()=>{throw new Error('service closed');}});
 assert.equal(ui.elements['error-banner'].hidden,false);
 assert.equal(ui.elements['send-selected'].disabled,true);
 assert.ok(ui.elements['error-banner'].textContent.includes('重新双击启动'));
});

test('Invalid drafts can be selected for removal but block batch submission',async()=>{
 const ui=await setup();
 ui.run("selected=new Set(['bad']); renderList()");
 assert.equal(ui.elements['remove-selected'].disabled,false);
 assert.equal(ui.elements['send-selected'].disabled,true);
 assert.ok(ui.elements['send-selected'].textContent.includes('问题草稿'));
 assert.ok(!ui.elements['mail-rows'].innerHTML.includes('选择 志愿者名单 bad\" checked disabled'));
});
