const assert=require('node:assert/strict');
const {createFirmwareController,firmwareStatus}=require('../static/manager/firmware-controller.js');
const ready=(status=null,stale=false)=>({preflight:{ready:true,build_confirmation:'ticket'},preparation:{jobs:[]},history:{builds:status?[{id:'build-one',status,stale}]:[],flashes:[],rollbacks:[]}});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
async function main(){
  let pending=[],post=0,next=ready(),failure=false;
  const c=createFirmwareController({schedule:()=>0,cancel:()=>{},api:async(path,options)=>{
    if(options?.method==='POST'){++post;return {task:{id:'build-one'}}}
    if(failure)throw Error('模拟断网');
    if(pending.length)return pending.shift().promise;
    return next;
  }});
  await c.refresh();assert.equal(c.snapshot().canBuild,true);
  const old=deferred(),newer=deferred();pending=[old,newer];
  const a=c.refresh(),b=c.refresh({invalidate:true});
  assert.equal(c.snapshot().data,null);assert.equal(c.snapshot().canBuild,false);
  newer.resolve(ready('running'));await b;old.resolve(ready());await a;
  assert.equal(c.snapshot().build.status,'running');assert.equal(c.snapshot().canBuild,false);
  next=ready('running');await c.refresh();assert.equal(c.snapshot().canBuild,false,'reopened page must keep active job locked');
  failure=true;await c.refresh();assert.equal(c.snapshot().data,null);assert.equal(c.snapshot().canBuild,false);
  failure=false;next=ready('cancelled');await c.refresh();assert.equal(c.snapshot().canBuild,true);
  next=ready('failed');await c.refresh();assert.equal(c.snapshot().canBuild,true);
  next=ready('completed',true);await c.refresh();assert.equal(!!c.snapshot().canFlash,false);
  next=ready('completed');await c.refresh();assert.equal(c.snapshot().canFlash,true);
  let confirmation=0;
  const started=c.start(()=>{++confirmation;next=ready('running');return true});
  await c.start(()=>{++confirmation;return true});await started;
  assert.equal(post,1);assert.equal(confirmation,1);assert.equal(c.snapshot().canBuild,false);
  c.invalidate();assert.equal(c.snapshot().data,null);assert.equal(c.snapshot().canBuild,false);
  for(const status of ['queued','pending','running','failed','cancelled','interrupted','completed'])assert.doesNotMatch(firmwareStatus(status),/[a-z]/i);
  console.log('PASS 10 firmware controller scenarios: stale responses, invalidation, active resume, offline lock, recovery, cancel retry, failed retry, stale artifact, duplicate click, Chinese states');
}
main().catch(error=>{console.error(error);process.exitCode=1});
