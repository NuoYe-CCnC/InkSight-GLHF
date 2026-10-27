// Offline state-machine checks. No browser, network or production DOM writes.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/manager/manager.js'),'utf8');
const controller=source.slice(source.indexOf('let previewDevices='),source.indexOf('async function loadFirmware'));
const elements=new Map();
function element(id){if(!elements.has(id))elements.set(id,{value:'',hidden:false,disabled:false,dataset:{},textContent:'',naturalWidth:800,naturalHeight:480,
  classList:{add(){},remove(){}},removeAttribute(name){delete this[name]},setAttribute(name,value){this[name]=value},decode:async()=>{}});return elements.get(id)}
let requests=[],urls=0;
const context=vm.createContext({$:element,URL:{createObjectURL:()=>`blob:offline-${++urls}`,revokeObjectURL(){}},URLSearchParams,AbortController,Date,Error,console,
  html:String,api:async()=>({devices:[]}),fetch:()=>new Promise(resolve=>requests.push(resolve))});
vm.runInContext(controller,context);
function response(stamp,status=200){return {ok:status===200,status,headers:{get:name=>({'Content-Type':'image/png','X-InkSight-Published-At':String(stamp)})[name]||null},blob:async()=>({}),json:async()=>({detail:'会话已过期'})}}
async function main(){
  element('previewDevice').value='screen-real';element('previewPage').value='ai';
  vm.runInContext("previewDevices=[{id:'screen-real',label:'面板',published_at:1792746802}]",context);
  const first=vm.runInContext('loadPreview()',context);
  element('previewPage').value='news_gold';
  const second=vm.runInContext('loadPreview()',context);
  requests[1](response(1792746900));await second;
  const latest=element('previewImage').dataset.url;
  requests[0](response(1792746802));await first;
  assert.equal(element('previewImage').dataset.url,latest,'late old response replaced new image');
  assert.match(element('exportPreview').download,/news_gold-1792746900/);
  const expired=vm.runInContext('loadPreview()',context);requests[2](response(0,401));await expired;
  assert.equal(element('previewImage').src,undefined);assert.equal(element('exportPreview').hidden,true);
  assert.equal(element('loginOverlay').hidden,false);assert.match(element('previewMessage').textContent,/会话已过期/);
  const size=vm.runInContext('loadPreview()',context);element('previewImage').naturalWidth=799;requests[3](response(1792746900));await size;
  assert.equal(element('previewImage').src,undefined);assert.match(element('previewMessage').textContent,/尺寸异常/);
  await vm.runInContext('loadPreviewDevices()',context);
  assert.equal(element('loadPreview').disabled,true);assert.match(element('previewMessage').textContent,/尚未绑定/);
  console.log('PASS 5 preview controller cases: out-of-order, export, expired session, bad dimensions, no devices');
}
main().catch(error=>{console.error(error);process.exitCode=1});
